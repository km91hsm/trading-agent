"""계좌별 실행 (스펙 §6.2, R16): 결정 1개 → 계좌마다 리스크 판정 → 모드별 주문·승인·기록.

주문은 이 모듈 안에서만 나간다. 실전 계좌의 `auto` 는 control 테이블의 확인 기록
(`live_confirmed:<id>`, `control.set_account_mode(confirm="LIVE")` 가 남김) 없이는
`off` 로 취급한다.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from trader import names
from trader.agents.schemas import Decision
from trader.broker.errors import UNKNOWN_RESPONSE_CODE, KisApiError
from trader.broker.models import OrderRequest, OrderResult
from trader.config import KST, AccountConfig, MemorySettings, Mode, Settings
from trader.control import control
from trader.ledger import repo
from trader.notify import messages
from trader.notify.telegram import Notifier, NullNotifier
from trader.orchestrator.run_store import checkpoint_payload as _wrap
from trader.orchestrator.run_store import run_dir, write_checkpoint
from trader.platform import paths
from trader.risk import regime as regime_mod
from trader.risk import rules
from trader.risk import stance as stance_mod

logger = logging.getLogger("trader.executor")

APPROVAL_TTL = timedelta(minutes=30)
QUOTE_PRICE_KEY = "quote_price"  # approvals.order_json 에 덧붙이는 요청 시 현재가
STAGE_KEY = "order_stage"  # approvals.order_json 에 덧붙이는 주문 체크포인트 이름(50_ 외일 때)
NAME_KEY = "name"  # approvals.order_json 에 덧붙이는 종목명 (표시용, OrderRequest 에는 없다)


@dataclass
class ExecContext:
    settings: Settings
    accounts: list[AccountConfig]
    conn: sqlite3.Connection
    brokers: dict[str, Any] = field(default_factory=dict)  # account_id → KisBroker | FakeBroker
    notifier: Notifier = field(default_factory=NullNotifier)
    state: Path | None = None
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    holiday_fn: Callable[[date], bool] | None = None  # 보유 거래일 계산용 (KIS 휴장일, 캐시)
    rl_sizer: Any | None = None  # ml.rl.shadow.Sizer (Phase 9, 모의 계좌·BUY/ADD 에만)
    sectors: Any | None = None  # config.SectorMap (섹터 상한). None 이면 첫 사용 때 load_sectors()


def _sector_of(ctx: ExecContext, market: str) -> Callable[[str], str | None] | None:
    """섹터 상한용 종목→섹터. sectors.yaml 을 못 읽으면 None(규칙 생략, 경고 1회)."""
    if ctx.sectors is None:
        from trader.config import load_sectors

        try:
            ctx.sectors = load_sectors()
        except Exception as e:  # noqa: BLE001 - 섹터 맵이 없으면 상한 규칙만 빠진다
            logger.warning("sectors.yaml 로드 실패, 섹터 상한 생략: %s", e)
            ctx.sectors = False
    if not ctx.sectors:
        return None
    sectors = ctx.sectors
    return lambda t: sectors.sector_of(t, market)  # type: ignore[arg-type]


def _agent_tilt(ctx: ExecContext, market: str, today: date) -> int:
    """Phase 22: 오늘 시장 세션 제안의 기울기(+1 RISK_ON / −1 RISK_OFF). `hedge.agent_gate` 가 꺼져
    있거나 확신이 `agent_min_confidence` 미만이면 0(기존 국면 한도 그대로)."""
    cfg = ctx.settings.hedge
    if not cfg.enabled or not getattr(cfg, "agent_gate", False):
        return 0
    from trader.orchestrator.tools import market_view_today

    view = market_view_today(ctx.conn, market, today)
    return stance_mod.tilt_of(view, min_confidence=cfg.agent_min_confidence)


def regime_state(ctx: ExecContext, market: str) -> str:
    """국면(bull|neutral|bear). 헤지 기능이 꺼져 있거나 판정 기록이 없으면 bull."""
    if not ctx.settings.hedge.enabled:
        return "bull"
    r = regime_mod.load(ctx.conn, market) or {}
    return str(r.get("state") or "bull")


def _holidays_between(
    fn: Callable[[date], bool] | None, start: date | None, end: date
) -> set[date]:
    if fn is None or start is None or start >= end:
        return set()
    out: set[date] = set()
    d = start
    while d < end:
        d += timedelta(days=1)
        if d.weekday() < 5:
            try:
                if fn(d):
                    out.add(d)
            except Exception:  # noqa: BLE001 - 휴장일 조회 실패는 개장일로 본다
                pass
    return out


def effective_mode(conn: sqlite3.Connection, account: AccountConfig) -> tuple[Mode, str | None]:
    """control 우선 모드. 실전 auto 는 확인 기록이 없으면 off 로 강등하고 사유를 돌려준다."""
    mode = control.get_account_mode(conn, account)
    if account.env == "real" and mode == "auto":
        if not control.get_flag(conn, f"live_confirmed:{account.id}"):
            return "off", "실전 auto 는 확인 기록(live_confirmed) 없이는 off 로 취급"
    return mode, None


def _update_breaker(
    conn,
    account: AccountConfig,
    market: str,
    today: date,
    nav: Decimal,
    settings: MemorySettings | None = None,
) -> None:
    """모의 계좌 NAV(최근 30일 + 오늘)·최근 VIX 변화로 그 시장의 메모리 서킷브레이커를 갱신한다.
    모의 계좌가 둘 이상이면(Phase 21) 날짜별 NAV 를 합친 한 열로 판정한다 — 한 계좌의 드로다운이
    다른 계좌의 판정으로 바로 풀리지(또는 켜지지) 않도록 OR 가 아니라 합산."""
    from trader.memory import store

    try:
        series = _paper_nav_series(conn, account, market, today, nav)
        store.circuit_breaker(
            conn,
            nav_series=series,
            vix_change=store.latest_vix_change(conn),
            settings=settings or MemorySettings(),
            market=market,
        )
    except Exception as e:  # noqa: BLE001 - 브레이커 갱신 실패는 판정을 막지 않는다
        logger.warning("서킷브레이커 갱신 실패 %s: %s", account.id, e)


def _paper_nav_series(
    conn, account: AccountConfig, market: str, today: date, nav: Decimal
) -> list[tuple[str, Decimal]]:
    """이 계좌의 30일 NAV(오늘은 인자 값) + 다른 모의 계좌들의 같은 시장 NAV 를 날짜별로
    합친다. 다른 계좌는 그 날짜 이하의 마지막 스냅샷(forward-fill)이고 스냅샷이 전혀 없으면
    더하지 않는다."""
    mine = repo.nav_history(conn, account.id, market, days=30)
    mine = [s for s in mine if s[0] != today.isoformat()] + [(today.isoformat(), nav)]
    others = [
        str(r["id"])
        for r in conn.execute(
            "SELECT id FROM accounts WHERE env='paper' AND id != ? ORDER BY id", (account.id,)
        ).fetchall()
    ]
    if not others:
        return mine
    out: list[tuple[str, Decimal]] = []
    for d, v in mine:
        total = Decimal(str(v))
        for o in others:
            hist = repo.nav_history(conn, o, market, days=60, today=date.fromisoformat(d))
            if hist:
                total += Decimal(str(hist[-1][1]))
        out.append((d, total))
    return out


def _cache_dir(ctx: ExecContext) -> Path:
    return (ctx.state or paths.state_dir()) / "cache"


def _label(ctx: ExecContext, market: str, ticker: str) -> str:
    """알림용 '이름 코드' 표기 (원장·캐시만, 네트워크 없음)."""
    return names.display(ctx.conn, _cache_dir(ctx), market, ticker)


def build_portfolio(
    conn: sqlite3.Connection,
    broker: Any,
    account: AccountConfig,
    market: str,
    today: date,
    memory_settings: MemorySettings | None = None,
    sector_of: Callable[[str], str | None] | None = None,
    hedge_tickers: frozenset[str] = frozenset(),
) -> rules.Portfolio:
    """브로커 잔고·현금 + 원장 집계로 Portfolio 를 만들고 positions_snapshot 을 남긴다."""
    get_balance = getattr(broker, "get_balance", None)
    if get_balance is not None:  # KR 은 잔고 API 1회로 보유·예수금을 함께 (모의 2/s)
        pos_list, cash = get_balance(market)
    else:
        pos_list, cash = broker.get_positions(market), broker.get_cash(market)
    positions = {p.ticker: p for p in pos_list}
    for p in positions.values():  # 잔고 응답의 종목명은 추가 호출 없이 얻는 이름이다
        repo.upsert_ticker_name(conn, p.market, p.ticker, getattr(p, "name", None), "balance")
    # NAV: 잔고 응답의 총평가(tot_evlu_amt)가 있으면 그것. 예수금(dnca_tot_amt) + 평가 합은 체결 뒤
    # 2영업일(결제 전) 동안 매수분을 이중 계산해 일일손실 −5% 오탐·비중 과대를 만들었다
    eval_sum = sum((p.eval_amount for p in positions.values()), Decimal(0))
    nav = cash.nav if getattr(cash, "nav", None) else cash.total + eval_sum
    prev = repo.latest_nav_before(conn, account.id, today, market=market)
    today_pnl = float(nav / prev - 1) if prev else 0.0
    if account.env == "paper":
        _update_breaker(conn, account, market, today, nav, memory_settings)
    repo.snapshot_positions(
        conn,
        account_id=account.id,
        d=today,
        market=market,
        nav=nav,
        cash=cash.total,
        positions=[
            {
                "ticker": p.ticker,
                "market": p.market,
                "qty": p.qty,
                "avg_price": p.avg_price,
                "current_price": p.current_price,
                "eval_amount": p.eval_amount,
            }
            for p in positions.values()
        ],
    )
    return rules.Portfolio(
        account_id=account.id,
        market=market,
        nav=nav,
        cash_available=cash.available,
        positions=positions,
        today_pnl_pct=today_pnl,
        today_buy_tickers=repo.today_buy_tickers(conn, account.id, today, market=market),
        # 가능금액(prvs_rcdl_excc_amt)은 체결된 매수를 이미 반영하므로 미체결 live 주문만 더 뺀다
        today_order_amount=repo.today_order_amount(
            conn, account.id, today, market=market, exclude_filled=True
        ),
        held_since=repo.held_since(conn, account.id, tickers=list(positions)),
        trades_this_week=repo.trades_this_week(conn, account.id, today, market=market),
        sector_of=sector_of,
        hedge_tickers=hedge_tickers,
    )


@dataclass
class _RlProposal:
    obs: list[float]
    proposed: float
    apply: bool
    note: str | None


def _rl_size(
    ctx: ExecContext,
    account: AccountConfig,
    decision: Decision,
    market: str,
    portfolio: rules.Portfolio,
    base: list[float] | None,
) -> tuple[Decision, _RlProposal | None, list[float] | None]:
    """RL 사이저 관측·제안 (모의 계좌·BUY/ADD 전용, 호출자가 조건을 건다).

    반환: (판정에 넘길 결정, 제안, 재사용할 base 관측). 기록은 게이트 판정 뒤 `_rl_record` 가 한다.
    실패는 경고 후 RL 없이 진행한다.
    """
    from trader.ml.rl import shadow

    sizer = ctx.rl_sizer
    try:
        if base is None:
            base = shadow.base_observation(
                ctx.state, ticker=decision.ticker, market=market, decision=decision
            )
        obs = shadow.observe(
            ctx.state,
            ticker=decision.ticker,
            market=market,
            decision=decision,
            portfolio=portfolio,
            nav_hist=repo.nav_history(ctx.conn, account.id, market, days=30),
            base=base,
        )
        proposed = shadow.propose(sizer, obs)
    except Exception as e:  # noqa: BLE001 - 실험 기능이 주문 판정을 막지 않는다
        logger.warning("RL 사이저 건너뜀 %s %s: %s", account.id, decision.ticker, e)
        return decision, None, base
    apply = sizer.cfg.mode == "apply"
    if not apply:
        return decision, _RlProposal(obs, proposed, False, None), base
    note = f"RL 사이저 적용: {decision.target_weight:.2f} → {proposed:.2f} (v{sizer.version})"
    sized = decision.model_copy(update={"target_weight": proposed})
    return sized, _RlProposal(obs, proposed, True, note), base


def _rl_record(
    ctx: ExecContext,
    account: AccountConfig,
    decision: Decision,
    decision_id: int,
    prop: _RlProposal,
    verdict: rules.RiskVerdict,
) -> rules.RiskVerdict:
    """게이트 판정 뒤 rl_shadow 기록. applied 는 apply 모드이면서 게이트가 승인했을 때만."""
    from trader.ml.rl import shadow

    applied = prop.apply and verdict.approved
    try:
        shadow.record(
            ctx.conn,
            decision_id=decision_id,
            account_id=account.id,
            model_version=ctx.rl_sizer.version,
            obs=prop.obs,
            llm_weight=decision.target_weight,
            proposed_weight=prop.proposed,
            applied=applied,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("RL 섀도 기록 실패 %s %s: %s", account.id, decision.ticker, e)
    if prop.note:
        return verdict.model_copy(update={"reasons": [prop.note, *verdict.reasons]})
    return verdict


def execute(
    ctx: ExecContext,
    decision: Decision,
    *,
    date: date,
    market: str,
    price: Decimal,
    exchange: str | None = None,
    dry_run: bool = True,
    is_stop_loss: bool = False,
    decision_id: int | None = None,
    resume_round: int | None = None,
    is_hedge: bool = False,
    skip_accounts: frozenset[str] = frozenset(),
    extra_note: str | None = None,
    scalp_leg: str | None = None,
    is_reprice: bool = False,
) -> list[dict]:
    """계좌마다 판정·기록·(모드별) 주문. 계좌 하나의 실패가 다른 계좌를 막지 않는다.
    scalp_leg(Phase 23 S, "entry"|"exit")는 분 단위 단타: 결정 행 없이(decision_id None 유지 —
    오늘 LLM 결정과 섞지 않는다) 체크포인트 47_/57_ 에 남기고 게이트는 단타 면제 규칙을 쓴다.
    extra_note 는 판정 note 에 덧붙이는 호출자 문구(재시세 등, Phase 21).

    skip_accounts 는 건너뛸 계좌(이미 판정이 있는 계좌의 실행 재개, 헤지의 계좌별 보유 판단).
    브로커·규칙 밖의 예외(원장 잠금·OSError·OrderBookkeepingError)도 그 계좌의 `error` 항목으로
    남기고 다음 계좌로 간다 — 한 계좌의 실패가 다른 계좌의 주문을 막지 않는다.

    is_stop_loss 면 보유기간·주 2회 규칙을 면제하고 approve 계좌도 승인 없이 즉시 낸다(사후 통보).
    resume_round 는 재평가(31_decision_r<n>) 라운드로, 판정·주문 체크포인트 이름에 들어간다.
    is_hedge 는 국면 헤지(인버스 ETF, Phase 17 H): 체크포인트 46_/56_, 게이트 면제, RL 비대상.
    """
    d = run_dir(date, market, decision.ticker, ctx.state)
    is_scalp = scalp_leg is not None
    if decision_id is None and not is_scalp:
        decision_id = repo.decision_id(ctx.conn, date, market, decision.ticker)
    # 손절·재평가는 오늘 분석의 40_/50_ 체크포인트를 덮어쓰지 않도록 별도 이름을 쓴다
    if is_stop_loss:
        risk_stage, order_stage = "45_stop_loss", "55_order_sl"
    elif is_scalp:
        risk_stage, order_stage = f"47_scalp_{scalp_leg}", f"57_order_scalp_{scalp_leg}"
    elif is_reprice:  # 아침 미체결 재호가(Phase 23 B): 오늘 분석의 40_/50_ 을 덮지 않는다
        risk_stage, order_stage = "42_reprice", "52_order_reprice"
    elif is_hedge:
        risk_stage, order_stage = "46_hedge", "56_order_hedge"
    elif resume_round is not None:
        risk_stage, order_stage = f"41_risk_check_r{resume_round}", f"51_order_r{resume_round}"
    else:
        risk_stage, order_stage = "40_risk_check", "50_order"
    paused = control.is_paused(ctx.conn)
    results: list[dict] = []
    bookkeeping_error: OrderBookkeepingError | None = None
    rl_base = _RlBase()
    rl_eligible = (
        ctx.rl_sizer is not None
        and decision_id is not None
        and not is_stop_loss
        and not is_hedge
        and not is_scalp
        and not dry_run  # dry-run·FAKE 세션의 제안은 실험 집계를 오염시킨다
        and decision.action in ("BUY", "ADD")
    )
    regime = regime_state(ctx, market)
    hedge_cfg = ctx.settings.hedge
    agent_tilt = _agent_tilt(ctx, market, date)
    hedge_tickers = frozenset(hedge_cfg.tickers(market)) if hedge_cfg.enabled else frozenset()
    sector_of = _sector_of(ctx, market)
    for account in ctx.accounts:
        if market not in account.markets or account.id in skip_accounts:
            continue
        try:
            results.append(
                _execute_account(
                    ctx,
                    account,
                    decision,
                    d=d,
                    date=date,
                    market=market,
                    price=price,
                    exchange=exchange,
                    dry_run=dry_run,
                    is_stop_loss=is_stop_loss,
                    is_hedge=is_hedge,
                    decision_id=decision_id,
                    risk_stage=risk_stage,
                    order_stage=order_stage,
                    paused=paused,
                    regime=regime,
                    hedge_cfg=hedge_cfg,
                    hedge_tickers=hedge_tickers,
                    sector_of=sector_of,
                    rl_eligible=rl_eligible,
                    rl_base=rl_base,
                    extra_note=extra_note,
                    agent_tilt=agent_tilt,
                    is_scalp=is_scalp,
                )
            )
        except OrderBookkeepingError as e:  # 접수 뒤 기록 실패: 다른 계좌를 마친 뒤 올린다
            results.append({"account_id": account.id, "error": str(e), "order_no": e.order_no})
            bookkeeping_error = bookkeeping_error or e
        except Exception as e:  # noqa: BLE001 - 원장 잠금·OSError 등: 다음 계좌는 계속 간다
            logger.exception("계좌 %s 실행 실패 %s %s", account.id, market, decision.ticker)
            _notify_quietly(
                ctx,
                messages.execution_failure(
                    account=account.id,
                    market=market,
                    label=_label(ctx, market, decision.ticker),
                    err=e,
                ),
            )
            results.append({"account_id": account.id, "error": f"{type(e).__name__}: {e}"})
    if bookkeeping_error is not None:
        # 호출자(monitor 의 손절 플래그·submit-decision 의 CRITICAL)가 접수번호로 수동 대조한다
        raise bookkeeping_error
    return results


class _RlBase:
    """계좌 간에 공유하는 RL 기준 관측(첫 계좌가 만든다)."""

    value: list[float] | None = None


def _execute_account(
    ctx: ExecContext,
    account: AccountConfig,
    decision: Decision,
    *,
    d: Path,
    date: date,
    market: str,
    price: Decimal,
    exchange: str | None,
    dry_run: bool,
    is_stop_loss: bool,
    is_hedge: bool,
    decision_id: int | None,
    risk_stage: str,
    order_stage: str,
    paused: bool,
    regime: str,
    hedge_cfg: Any,
    hedge_tickers: frozenset[str],
    sector_of: Callable[[str], str | None] | None,
    rl_eligible: bool,
    rl_base: _RlBase,
    extra_note: str | None = None,
    agent_tilt: int = 0,
    is_scalp: bool = False,
) -> dict:
    """계좌 하나의 판정·기록·(모드별) 주문. 반환은 results 항목 하나."""
    if True:  # 들여쓰기 유지(execute 루프 본문을 그대로 옮김)
        rl_prop: _RlProposal | None = None
        mode, note = effective_mode(ctx.conn, account)
        broker = ctx.brokers.get(account.id)
        st = stance_mod.decide(repo.realized_pnl_5d(ctx.conn, account.id, date, market=market))
        try:
            if broker is None:
                raise KisApiError("NO_BROKER", f"계좌 {account.id} 브로커 없음(앱키 미설정)")
            portfolio = build_portfolio(
                ctx.conn,
                broker,
                account,
                market,
                date,
                memory_settings=ctx.settings.memory,
                sector_of=sector_of,
                hedge_tickers=hedge_tickers,
            )
            sized = decision
            if rl_eligible and account.env == "paper":
                sized, rl_prop, rl_base.value = _rl_size(
                    ctx, account, decision, market, portfolio, rl_base.value
                )
            verdict = rules.evaluate(
                sized,
                portfolio,
                limits=ctx.settings.risk,
                stance=st,
                market=market,
                price=price,
                today=date,
                is_stop_loss=is_stop_loss,
                holidays=_holidays_between(
                    ctx.holiday_fn, portfolio.held_since.get(decision.ticker), date
                ),
                regime=regime,
                is_hedge=is_hedge,
                hedge_max_weight=hedge_cfg.max_weight_pct if is_hedge else None,
                agent_tilt=agent_tilt,
                is_scalp=is_scalp,
            )
        except (KisApiError, ValueError) as e:
            verdict = rules.RiskVerdict(
                account_id=account.id,
                approved=False,
                adjusted_qty=0,
                reasons=[f"잔고 조회 실패로 판정 불가: {e}"],
            )
        # 멱등: 같은 결정·계좌에 살아 있는 주문·승인 요청이 있으면 다시 내지 않는다 (재실행·재시도).
        # 손절은 이 결정 단위 조건을 보지 않는다: 결정은 며칠 전 것일 수 있고 미체결로 끝난
        # 손절 SELL(accepted 그대로)이 다음 손절을 영구히 막기 때문이다. 손절의 중복 방지는
        # monitor._sell_now 의 "같은 현지 날짜의 살아 있는 SELL" 조건이 맡는다.
        if (
            verdict.approved
            and decision_id is not None
            and not is_stop_loss
            and repo.has_open_order_or_approval(ctx.conn, decision_id, account.id, verdict.side)
        ):
            verdict = verdict.model_copy(
                update={
                    "approved": False,
                    "adjusted_qty": 0,
                    "reasons": verdict.reasons + ["이미 이 결정에 대한 주문/승인 요청이 존재"],
                }
            )
        if rl_prop is not None and decision_id is not None:
            verdict = _rl_record(ctx, account, decision, decision_id, rl_prop, verdict)
        if paused and verdict.approved:
            note = (note + "; " if note else "") + "일시정지 중: 판정만 기록, 주문·승인 요청 없음"
        if regime != "bull" and not is_hedge:
            note = (note + "; " if note else "") + f"국면 {regime}"
        if is_scalp:
            note = (note + "; " if note else "") + "분 단위 단타(모의 전용)"
        if extra_note:
            note = (note + "; " if note else "") + extra_note
        verdict = verdict.model_copy(
            update={"mode": mode, "note": note, "stance": st, "dry_run": dry_run}
        )
        entry: dict[str, Any] = {
            "account_id": account.id,
            "mode": mode,
            "note": note,
            "stance": st,
            "approved": verdict.approved,
            "qty": verdict.adjusted_qty,
            "reasons": verdict.reasons,
            "dry_run": dry_run,
            "order": None,
            "approval_id": None,
        }
        write_checkpoint(d, f"{risk_stage}_{account.id}", _wrap(verdict.model_dump(mode="json")))
        if decision_id is not None:
            repo.insert_risk_check(
                ctx.conn,
                decision_id=decision_id,
                account_id=account.id,
                approved=verdict.approved,
                adjusted_qty=verdict.adjusted_qty,
                reasons=verdict.reasons,
            )
        if not verdict.approved or mode == "off" or dry_run or paused:
            return entry
        # 알림의 '최종 의견' (손절·헤지·단타는 파이썬 결정이라 붙이지 않는다)
        opinion = (
            None if is_stop_loss or is_hedge or is_scalp else messages.DecisionBrief.of(decision)
        )
        request = OrderRequest(
            ticker=decision.ticker,
            market=market,  # type: ignore[arg-type]
            side=verdict.side,  # type: ignore[arg-type]
            qty=verdict.adjusted_qty,
            order_type=verdict.order_type,  # type: ignore[arg-type]
            price=verdict.price,
            exchange=exchange,
        )
        if mode == "approve" and not is_stop_loss:
            entry["approval_id"] = _request_approval(
                ctx,
                account,
                decision_id,
                request,
                verdict,
                quote_price=price,
                order_stage=order_stage,
                opinion=opinion,
            )
        else:
            entry["order"] = _place(
                ctx,
                d,
                account,
                broker,
                decision_id,
                request,
                mode,
                price,
                order_stage=order_stage,
                opinion=opinion,
            )
        return entry


def _request_approval(
    ctx: ExecContext,
    account: AccountConfig,
    decision_id: int | None,
    request: OrderRequest,
    verdict: rules.RiskVerdict,
    *,
    quote_price: Decimal | None = None,
    order_stage: str = "50_order",
    opinion: messages.DecisionBrief | None = None,
) -> int:
    """승인 요청 저장. 시장가는 금액을 모르므로 요청 시 현재가(quote_price)를 함께 남겨
    승인 뒤 주문의 amount(가능금액 차감)로 쓴다. 주문 체크포인트 이름(order_stage: 재평가 51_·
    헤지 56_)도 남겨 승인 실행이 오늘 분석의 50_order 를 덮지 않게 한다. OrderRequest 복원
    때는 이 키들을 뺀다."""
    now = ctx.now()
    order = request.model_dump(mode="json")
    if quote_price is not None:
        order[QUOTE_PRICE_KEY] = str(quote_price)
    if order_stage != "50_order":
        order[STAGE_KEY] = order_stage
    shown = _label(ctx, request.market, request.ticker)
    name = names.resolve(ctx.conn, _cache_dir(ctx), request.market, request.ticker)
    if name and name != request.ticker:
        order[NAME_KEY] = name
    aid = repo.create_approval(
        ctx.conn,
        decision_id=decision_id or 0,
        account_id=account.id,
        order=order,
        requested_at=now.isoformat(),
        expires_at=(now + APPROVAL_TTL).isoformat(),
    )
    ctx.notifier.send(
        messages.approval_request(
            approval_id=aid,
            account=account.id,
            market=request.market,
            label=shown,
            side=request.side,
            qty=request.qty,
            price=request.price,
            reasons=verdict.reasons,
            opinion=opinion,
            kind=_STAGE_KIND.get(order_stage.split("_r")[0], ""),
            env=account.env,
            quote_price=quote_price,
            expires_at=(now + APPROVAL_TTL).isoformat(),
        )
    )
    return aid


def _place(
    ctx: ExecContext,
    d: Path,
    account: AccountConfig,
    broker: Any,
    decision_id: int | None,
    request: OrderRequest,
    mode: Mode,
    quote_price: Decimal | None = None,
    order_stage: str = "50_order",
    opinion: messages.DecisionBrief | None = None,
) -> dict:
    shown = _label(ctx, request.market, request.ticker)
    try:
        result = broker.place_order(request)
        status = "accepted" if result.accepted else "rejected"
    except KisApiError as e:
        result = OrderResult(order_no=None, accepted=False, message=f"{e.msg_cd}: {e.msg1}")
        if e.msg_cd == UNKNOWN_RESPONSE_CODE:
            # 요청은 나갔는데 응답이 없다: KIS 에 접수됐을 수 있어 failed 로 적으면 중복 방지
            # 조건(failed 제외)이 놓쳐 같은 주문을 다시 낸다. unknown 으로 살려 두고 체결
            # 동기화가 같은 날·종목·방향 체결로 확정한다.
            status = "unknown"
            logger.error("주문 응답 없음 %s %s: %s", account.id, request.ticker, e)
            _notify_quietly(
                ctx,
                messages.order_unknown(
                    account=account.id,
                    market=request.market,
                    label=shown,
                    side=request.side,
                    qty=request.qty,
                    err=e.msg1,
                ),
            )
        else:
            status = "failed"
            logger.warning("주문 실패 %s %s: %s", account.id, request.ticker, e)
    except ValueError as e:  # 거래소 코드 누락·계좌 시장 불일치 등 요청 자체의 오류
        result = OrderResult(order_no=None, accepted=False, message=f"INVALID: {e}")
        status = "failed"
        logger.warning("주문 요청 오류 %s %s: %s", account.id, request.ticker, e)
    sizing_price = request.price if request.price is not None else quote_price
    # orders 행은 멱등의 근거(has_open_order_or_approval)라 접수 뒤 반드시 남아야 한다.
    # 기록이 실패하면 접수번호를 알림·CRITICAL 로그에 남기고 올린다
    # (조용히 묻히면 재실행 때 같은 주문을 다시 낸다).
    try:
        repo.insert_order(
            ctx.conn,
            decision_id=decision_id,
            account_id=account.id,
            ticker=request.ticker,
            market=request.market,
            side=request.side,
            qty=request.qty,
            order_type=request.order_type,
            price=request.price,
            status=status,
            order_no=result.order_no,
            message=result.message,
            mode=mode,
            created_at=ctx.now().isoformat(),
            amount=Decimal(request.qty) * sizing_price if sizing_price is not None else None,
            org_no=result.org_no,
        )
    except Exception as e:
        if status not in ("accepted", "unknown"):
            # 브로커가 거부·실패한 주문은 KIS 에 없으므로 행이 빠져도 추적할 것이 없다
            logger.error(
                "실패 주문의 기록 실패 %s %s (%s): %s", account.id, request.ticker, status, e
            )
            return result.model_dump(mode="json") | {"status": status}
        logger.critical(
            "주문 기록 실패 %s %s %s %d주 접수번호 %s: %s",
            account.id,
            request.side,
            request.ticker,
            request.qty,
            result.order_no,
            e,
        )
        _notify_quietly(
            ctx,
            messages.bookkeeping_failure(
                account=account.id,
                label=shown,
                side=request.side,
                qty=request.qty,
                order_no=result.order_no,
                status=status,
                err=e,
            ),
        )
        raise OrderBookkeepingError(result.order_no, e) from e
    payload = {
        "request": request.model_dump(mode="json"),
        "result": result.model_dump(mode="json"),
        "mode": mode,
        "status": status,
    }
    # 체크포인트·알림 실패는 원장에 이미 기록됐으므로 경고만 남긴다
    try:
        write_checkpoint(d, f"{order_stage}_{account.id}", _wrap(payload))
    except OSError as e:
        logger.warning("주문 체크포인트 기록 실패 %s %s: %s", account.id, request.ticker, e)
    if status != "unknown":  # unknown 은 위에서 이미 알렸다 (실패로 덧알리면 운영자가 재주문함)
        _notify_quietly(
            ctx,
            messages.order(
                account=account.id,
                market=request.market,
                label=shown,
                side=request.side,
                qty=request.qty,
                price=request.price,
                mode=mode,
                accepted=result.accepted,
                order_no=result.order_no,
                message=result.message,
                opinion=opinion,
                kind=_STAGE_KIND.get(order_stage.split("_r")[0], ""),
            ),
        )
    return payload["result"] | {"status": status}


class OrderBookkeepingError(RuntimeError):
    """브로커가 접수한 뒤 원장 기록이 실패했다. order_no 로 수동 대조한다."""

    def __init__(self, order_no: str | None, cause: Exception) -> None:
        super().__init__(f"주문 기록 실패 (접수번호 {order_no or '-'}): {cause}")
        self.order_no = order_no


# 주문 체크포인트 접두 → 알림 제목 꼬리 ('[주문 · 재평가]')
_STAGE_KIND = {
    "51_order": "재평가",
    "52_order_reprice": "재호가",
    "55_order_sl": "손절",
    "56_order_hedge": "헤지",
    "57_order_scalp": "단타",
}


def _notify_quietly(ctx: ExecContext, text: str) -> None:
    """알림 실패가 주문 흐름을 끊지 않도록 예외를 경고로 바꾼다."""
    try:
        ctx.notifier.send(text)
    except Exception as e:  # noqa: BLE001 - 알림 채널 장애는 주문 결과와 무관
        logger.warning("알림 전송 실패: %s", type(e).__name__)


# --- 승인 처리 (approve 모드) ---------------------------------------------------


def expire_approvals(ctx: ExecContext) -> list[int]:
    """만료 시각이 지난 pending 승인을 expired 로 바꾸고 id 목록을 돌려준다."""
    now = ctx.now()
    expired: list[int] = []
    for a in repo.pending_approvals(ctx.conn):
        if datetime.fromisoformat(a["expires_at"]) <= now:
            repo.decide_approval(ctx.conn, a["id"], "expired", decided_at=now.isoformat())
            expired.append(a["id"])
    return expired


def _take_pending(ctx: ExecContext, approval_id: int) -> tuple[dict | None, str | None]:
    a = repo.get_approval(ctx.conn, approval_id)
    if a is None:
        return None, f"승인 요청 #{approval_id} 없음"
    if a["status"] != "pending":
        return None, f"승인 요청 #{approval_id} 은 이미 처리됨: {a['status']}"
    if datetime.fromisoformat(a["expires_at"]) <= ctx.now():
        repo.decide_approval(ctx.conn, approval_id, "expired", decided_at=ctx.now().isoformat())
        return None, f"승인 요청 #{approval_id} 은 만료됨 (30분 초과)"
    return a, None


def approve(ctx: ExecContext, approval_id: int) -> dict:
    """pending·미만료 승인 요청을 주문으로 실행한다. 두 번째 호출은 거부된다."""
    a, err = _take_pending(ctx, approval_id)
    if a is None:
        return {"ok": False, "error": err}
    if control.is_paused(ctx.conn):
        return {"ok": False, "error": "일시정지 중이라 승인을 실행하지 않음 (요청은 pending 유지)"}
    account = next((x for x in ctx.accounts if x.id == a["account_id"]), None)
    if account is None:
        return {"ok": False, "error": f"계좌 {a['account_id']} 가 설정에 없음"}
    mode, _ = effective_mode(ctx.conn, account)
    if mode != "approve":  # 요청 뒤 off 로 내렸으면 승인해도 주문이 나가면 안 된다
        return {
            "ok": False,
            "error": f"계좌 모드가 approve 가 아님({mode}): 승인 미실행 (요청은 pending 유지)",
        }
    order = dict(a["order"])
    order.pop(NAME_KEY, None)  # 표시용 — OrderRequest(extra=forbid) 에는 없다
    raw_quote = order.pop(QUOTE_PRICE_KEY, None)
    quote_price = Decimal(str(raw_quote)) if raw_quote is not None else None
    order_stage = str(order.pop(STAGE_KEY, None) or "50_order")
    request = OrderRequest.model_validate(order)
    dec = repo.get_decision(ctx.conn, a["decision_id"], hydrate=True) if a["decision_id"] else None
    day = date.fromisoformat(dec["date"]) if dec else ctx.now().astimezone(KST).date()
    d = run_dir(day, request.market, request.ticker, ctx.state)
    broker = ctx.brokers.get(account.id)
    if broker is None:
        return {
            "ok": False,
            "error": f"계좌 {account.id} 브로커 없음(앱키 미설정): 요청은 pending 유지",
        }
    # 조건부 UPDATE: 다른 프로세스(telegram poll ↔ approvals approve)가 먼저 처리했으면 주문 없음
    if not repo.decide_approval(
        ctx.conn, approval_id, "approved", decided_at=ctx.now().isoformat()
    ):
        return {"ok": False, "error": f"승인 요청 #{approval_id} 은 이미 다른 프로세스가 처리함"}
    try:
        result = _place(
            ctx,
            d,
            account,
            broker,
            a["decision_id"] or None,
            request,
            "approve",
            quote_price,
            order_stage=order_stage,
            opinion=messages.DecisionBrief.of((dec or {}).get("data")),
        )
    except OrderBookkeepingError as e:  # 접수 뒤 기록 실패: approved 로 굳지 않게 failed 로 남긴다
        repo.decide_approval(ctx.conn, approval_id, "failed", expect="approved")
        return {
            "ok": False,
            "approval_id": approval_id,
            "error": f"주문 기록 실패(접수번호 {e.order_no or '-'}): {e.__cause__}",
        }
    if not result["accepted"] and result.get("status") != "unknown":
        # unknown(응답 없음)은 KIS 에 접수됐을 수 있어 failed 로 적지 않는다 — 운영자가 재주문함
        repo.decide_approval(ctx.conn, approval_id, "failed", expect="approved")
    return {"ok": bool(result["accepted"]), "approval_id": approval_id, "order": result}


def reject(ctx: ExecContext, approval_id: int) -> dict:
    a, err = _take_pending(ctx, approval_id)
    if a is None:
        return {"ok": False, "error": err}
    if not repo.decide_approval(
        ctx.conn, approval_id, "rejected", decided_at=ctx.now().isoformat()
    ):
        return {"ok": False, "error": f"승인 요청 #{approval_id} 은 이미 다른 프로세스가 처리함"}
    o = a["order"]
    ctx.notifier.send(
        messages.approval_rejected(
            approval_id=approval_id,
            account=a["account_id"],
            label=names.label(str(o.get("ticker")), o.get(NAME_KEY)),
        )
    )
    return {"ok": True, "approval_id": approval_id}
