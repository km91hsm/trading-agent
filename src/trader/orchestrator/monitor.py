"""장중 모니터 (스펙 §5.2): 손절은 파이썬이 즉시 매도, 그 밖의 신호는 triggers 기록 → 재평가 세션.

- 손절선: 결정의 stop_loss, 없으면 평단 × (1 + settings.risk.stop_loss_pct).
- 트리거: take_profit 도달, ±7% 급등락(평단 대비), invalidation_conditions 의 가격 조건.
- 같은 종목 재평가는 30분에 1회. paused·세션 상한이면 기록만.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from trader import names
from trader.agents.schemas import Decision
from trader.broker.errors import KisApiError
from trader.broker.models import Position
from trader.config import KST, MARKET_TZ, AccountConfig, HorizonProfile, Settings
from trader.control import control
from trader.ledger import repo
from trader.notify import messages
from trader.notify.telegram import Notifier, NullNotifier
from trader.orchestrator import executor, resume, tools
from trader.platform import paths
from trader.risk import horizon

logger = logging.getLogger("trader.monitor")

MOVE_PCT = Decimal("0.07")  # 프로필이 없을 때(mid) 기본 — 정본은 risk.horizon_profiles
REEVAL_INTERVAL = timedelta(minutes=30)
_NUM_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)")


@dataclass
class Signal:
    kind: str  # stop_loss | take_profit | move_7pct | invalidation
    price: Decimal
    detail: dict = field(default_factory=dict)


_NON_PRICE_WORDS = ("거래량", "시총", "시가총액", "억", "만주", "%", "RSI", "배수", "PER", "PBR")


def _label(ctx: MonitorContext, market: str, ticker: str) -> str:
    return names.display(ctx.conn, (ctx.state or paths.state_dir()) / "cache", market, ticker)


def parse_price_conditions(conditions: list[str]) -> list[tuple[str, Decimal]]:
    """'종가 < 65000', '70,000원 이상' 같은 문장에서 (연산자, 가격) 만 뽑는다. 지표 조건은 무시.

    거래량·시총·비율 문장은 숫자가 커도 가격이 아니므로 건너뛰고, 한 문장에 숫자가 여럿이면
    ('3일 연속 종가 65000 이하') 가장 큰 수를 가격으로 본다."""
    out: list[tuple[str, Decimal]] = []
    for c in conditions or []:
        text = str(c)
        if any(w in text for w in _NON_PRICE_WORDS):
            continue
        nums = [Decimal(x.replace(",", "")) for x in _NUM_RE.findall(text)]
        if not nums:
            continue
        num = max(nums)
        if num < 100:  # RSI·비율 같은 작은 수는 가격이 아니다
            continue
        if "<" in text or "이하" in text or "미만" in text or "하회" in text:
            out.append(("<", num))
        elif ">" in text or "이상" in text or "초과" in text or "돌파" in text or "상회" in text:
            out.append((">=", num))
    return out


def _q2(v: Decimal) -> str:
    return str(v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def check_position(
    pos: Position,
    decision: dict | None,
    quote: Decimal,
    *,
    stop_loss_pct: float,
    now: datetime,
    move_pct: Decimal | None = None,
) -> list[Signal]:
    """순수 함수. 손절이면 손절 신호만, 아니면 take_profit → move_7pct → invalidation 순.
    `move_pct` 는 보유기간 프로필의 급등락 임계(Phase 23 T, 없으면 7%)."""
    dec = decision or {}
    threshold = move_pct if move_pct is not None else MOVE_PCT
    stop = dec.get("stop_loss")
    level = Decimal(str(stop)) if stop else pos.avg_price * (1 + Decimal(str(stop_loss_pct)))
    if quote <= level:
        return [Signal("stop_loss", quote, {"level": _q2(level), "avg_price": str(pos.avg_price)})]
    out: list[Signal] = []
    tp = dec.get("take_profit")
    if tp and quote >= Decimal(str(tp)):
        out.append(Signal("take_profit", quote, {"level": str(tp)}))
    if pos.avg_price > 0:
        move = quote / pos.avg_price - 1
        if abs(move) >= threshold:
            out.append(
                Signal(
                    "move_7pct",
                    quote,
                    {"move": _q2(move * 100) + "%", "threshold": _q2(threshold * 100) + "%"},
                )
            )
    for op, px in parse_price_conditions(dec.get("invalidation_conditions") or []):
        if (op == "<" and quote < px) or (op == ">=" and quote >= px):
            out.append(Signal("invalidation", quote, {"condition": f"{op} {px}"}))
            break
    return out


@dataclass
class MonitorContext:
    settings: Settings
    accounts: list[AccountConfig]
    conn: sqlite3.Connection
    brokers: dict[str, Any]
    market_client: Any
    notifier: Notifier = field(default_factory=NullNotifier)
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    session_runner: Callable[..., Any] | None = None
    dry_run: bool = False
    state: Path | None = None
    exchanges: dict[str, str] = field(default_factory=dict)  # US ticker → NAS|NYS|AMS
    # 데몬: 재평가 세션(≤ llm.timeout_s)을 틱 안에서 돌리지 않고 작업 대기열로 넘긴다 — 세션이
    # 도는 동안 손절 점검(60초 틱)이 멈추면 안 된다. None 이면 CLI 처럼 인라인 실행
    enqueue_reevaluate: Callable[[dict], int | None] | None = None


def _exchange(ctx: MonitorContext, pos: Position) -> str | None:
    """US 거래소: 브로커 잔고가 준 값 우선, 없으면 universe.us. 둘 다 없으면 None (NAS 추정 금지:
    NYSE 종목을 NAS 로 조회하면 시세 0 → 손절 불능)."""
    return pos.exchange or ctx.exchanges.get(pos.ticker)


def _quote(ctx: MonitorContext, market: str, pos: Position) -> Decimal:
    if market == "KR":
        return ctx.market_client.get_price_kr(pos.ticker).price
    exchange = _exchange(ctx, pos)
    if exchange is None:
        raise ValueError(f"US 거래소 코드 없음 (잔고·universe.us 모두): {pos.ticker}")
    return ctx.market_client.get_price_us(pos.ticker, exchange).price


STOP_LOSS_LIMIT_DISCOUNT = Decimal("0.03")  # US 손절은 현재가 3% 아래 지정가(체결 가능 지정가)


def _stop_loss_decision(ticker: str, market: str, quote: Decimal, sig: Signal) -> Decision:
    limit = (quote * (1 - STOP_LOSS_LIMIT_DISCOUNT)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return Decision(
        ticker=ticker,
        action="SELL",
        target_weight=0.0,
        order_type="MARKET" if market == "KR" else "LIMIT",
        limit_price=None if market == "KR" else float(limit),
        stop_loss=None,
        take_profit=None,
        horizon_days=1,
        confidence=1.0,
        rationale=(
            f"손절선 도달: 현재가 {quote} ≤ 손절선 {sig.detail.get('level')} (파이썬 즉시 매도)"
        ),
        invalidation_conditions=[],
    )


def periodic_signal(
    quote: Decimal,
    ref_price: Decimal,
    *,
    profile: HorizonProfile,
    last_periodic_at: datetime | None,
    count_today: int,
    now: datetime,
) -> Signal | None:
    """단기 보유의 시간 단위 정기 재평가(Phase 23 T, 순수 함수). `periodic_check_min` 이 0 이면
    없음. 직전 정기 점검(없으면 결정 시각 기준 — 호출자가 None 을 넘기면 바로 가능)에서 간격이
    지났고, 기준가 대비 변동이 `move_pct/2` 이상이며, 운영일 상한 안일 때만."""
    if profile.periodic_check_min <= 0 or count_today >= horizon.PERIODIC_DAILY_CAP:
        return None
    if last_periodic_at is not None and now - last_periodic_at < timedelta(
        minutes=profile.periodic_check_min
    ):
        return None
    if ref_price <= 0:
        return None
    move = quote / ref_price - 1
    if abs(move) < Decimal(str(profile.move_pct)) / 2:
        return None
    return Signal(
        "periodic",
        quote,
        {"move": _q2(move * 100) + "%", "ref_price": str(ref_price), "profile": "short"},
    )


def _can_reevaluate(
    ctx: MonitorContext,
    market: str,
    ticker: str,
    today: date,
    interval: timedelta = REEVAL_INTERVAL,
) -> str | None:
    if ctx.session_runner is None:
        return "session_runner 없음"
    if control.is_paused(ctx.conn):
        return "일시정지"
    if repo.decision_id(ctx.conn, today, market, ticker) is None:
        # submit-decision --stage 31_* 은 오늘 30_decision 을 요구한다. analyze 가 아직(또는 실패로)
        # 안 끝난 보유분은 세션을 띄워도 제출이 거부되므로 트리거만 남겨 다음 틱에 다시 본다
        return "오늘 결정 없음 (analyze 미완료)"
    if tools.sessions_today(ctx.conn, ctx.now()) >= ctx.settings.llm.daily_session_cap:
        return "세션 상한"
    last = repo.last_trigger_handled_at(ctx.conn, market, ticker)
    if last is not None and ctx.now() - last < interval:
        return f"{int(interval.total_seconds() // 60)}분 내 재평가 있음"
    return None


def monitor_once(ctx: MonitorContext, *, market: str, today: date) -> dict:
    """계좌별 보유종목 1회 점검. 1차: 손절(전 계좌), 2차: 트리거 기록·재평가.

    재평가 세션 실패는 트리거에 기록하고 다음 종목으로 넘어간다 (손절 점검을 막지 않는다).
    """
    result = {"checked": 0, "stop_loss_orders": 0, "triggers": 0, "reevaluated": 0}
    quotes: dict[str, Decimal | None] = {}
    pending: list[tuple[AccountConfig, Position, list[Signal], dict | None]] = []
    for account in ctx.accounts:
        broker = ctx.brokers.get(account.id)
        if market not in account.markets or broker is None:
            continue
        try:
            positions = broker.get_positions(market)
        except (KisApiError, ValueError) as e:
            logger.warning("보유 조회 실패 %s: %s", account.id, e)
            continue
        hedge_tickers = ctx.settings.hedge.tickers(market) if ctx.settings.hedge.enabled else set()
        for pos in positions:
            if pos.ticker in hedge_tickers:
                continue  # 헤지 ETF 는 국면 해제로만 판다 (손절·트리거 대상 아님, Phase 17 H)
            quote = _cached_quote(ctx, market, pos, quotes)
            if quote is None:
                continue
            result["checked"] += 1
            latest = repo.latest_decision(ctx.conn, market, pos.ticker)
            dec_data = _monotone_stop(ctx, account, market, pos, (latest or {}).get("data"))
            pname, profile = horizon.of_decision(ctx.settings.risk, dec_data)
            signals = check_position(
                pos,
                dec_data,
                quote,
                stop_loss_pct=ctx.settings.risk.stop_loss_pct,
                now=ctx.now(),
                move_pct=Decimal(str(profile.move_pct)),
            )
            if not signals or signals[0].kind != "stop_loss":
                per = _periodic(ctx, market, pos, quote, profile, today)
                if per is not None:
                    signals.append(per)
            if not signals:
                continue
            if signals[0].kind == "stop_loss":
                result["stop_loss_orders"] += _sell_now(
                    ctx, account, market, pos, quote, signals[0], today, latest
                )
            else:
                pending.append((account, pos, signals, latest))
    handled: set[str] = set()
    day_lo, day_hi = repo.day_bounds(today, market)
    for account, pos, signals, latest in pending:
        tids: list[int] = []  # 이 종목의 트리거 전부 — 재평가 세션 1회가 모두 처리한다
        for sig in signals:
            # 같은 운영일에 같은 종류로 이미 재평가했으면 조건이 지속돼도 다시 쌓지 않는다
            # (익절선 위에 머무는 종목이 30분마다 유료 세션을 띄워 일일 상한을 소진하지 않도록)
            if repo.reevaluated_between(ctx.conn, market, pos.ticker, sig.kind, day_lo, day_hi):
                continue
            existing = repo.unhandled_trigger_id(ctx.conn, market, pos.ticker, sig.kind, today)
            if existing is not None:  # 미처리 트리거가 있으면 다시 쌓지 않고 그 행을 쓴다
                tids.append(existing)
                continue
            tid = repo.insert_trigger(
                ctx.conn,
                market=market,
                ticker=pos.ticker,
                kind=sig.kind,
                detail=sig.detail,
                price=sig.price,
                decision_id=(latest or {}).get("id"),
                account_id=account.id,
                created_at=ctx.now().isoformat(),
            )
            tids.append(tid)
            result["triggers"] += 1
        if not tids or pos.ticker in handled:
            continue
        handled.add(pos.ticker)
        first_id = tids[0]
        _, profile = horizon.of_decision(ctx.settings.risk, (latest or {}).get("data"))
        why = _can_reevaluate(
            ctx, market, pos.ticker, today, timedelta(minutes=profile.reeval_interval_min)
        )
        if why:
            logger.info("재평가 생략 %s %s: %s", market, pos.ticker, why)
            continue
        s0 = signals[0]
        if ctx.enqueue_reevaluate is not None:
            params = {
                "market": market,
                "ticker": pos.ticker,
                "exchange": _exchange(ctx, pos),
                "trigger_id": first_id,
                "trigger_ids": tids[1:],
            }
            try:
                rid = ctx.enqueue_reevaluate(params)
            except Exception as e:  # noqa: BLE001
                logger.warning("재평가 큐 등록 실패 %s %s: %s", market, pos.ticker, e)
                continue
            if rid is not None:
                logger.info("재평가 큐 등록 #%s %s %s", rid, market, pos.ticker)
                result["queued"] = result.get("queued", 0) + 1
            continue
        try:
            res = resume.reevaluate(
                ctx.settings,
                ctx.conn,
                ctx.accounts,
                market=market,
                date=today,
                ticker=pos.ticker,
                exchange=_exchange(ctx, pos),
                trigger_id=first_id,
                trigger={"kind": s0.kind, "price": str(s0.price), "detail": s0.detail},
                brokers=ctx.brokers,
                now=ctx.now,
                dry_run=ctx.dry_run,
                state=ctx.state,
                session_runner=ctx.session_runner,
                notifier=ctx.notifier,
            )
        except Exception as e:  # noqa: BLE001 - 세션 기동 실패가 다른 종목 점검을 막지 않는다
            logger.exception("재평가 세션 기동 실패 %s %s", market, pos.ticker)
            for tid in tids:
                repo.mark_trigger_handled(
                    ctx.conn,
                    tid,
                    f"실패: {type(e).__name__}: {e}"[:200],
                    handled_at=ctx.now().isoformat(),
                )
            ctx.notifier.send(
                messages.reevaluation_failed(
                    market=market, label=_label(ctx, market, pos.ticker), err=e
                )
            )
            continue
        for tid in tids[1:]:  # 같은 세션이 나머지 트리거도 처리한 것으로 표시 (실패도 같은 표시)
            repo.mark_trigger_handled(
                ctx.conn, tid, resume.handled_marker(res), handled_at=ctx.now().isoformat()
            )
        if res.ok:
            result["reevaluated"] += 1
    return result


def _periodic(
    ctx: MonitorContext,
    market: str,
    pos: Position,
    quote: Decimal,
    profile: HorizonProfile,
    today: date,
) -> Signal | None:
    """정기 재평가 신호: 기준가는 오늘 마지막 트리거 가격(없으면 평단). 운영일 횟수·직전 시각은
    triggers 표의 kind='periodic' 행에서 센다."""
    if profile.periodic_check_min <= 0:
        return None
    lo, hi = repo.day_bounds(today, market)
    row = ctx.conn.execute(
        "SELECT COUNT(*) AS n, MAX(created_at) AS t FROM triggers WHERE market=? AND ticker=? "
        "AND kind='periodic' AND created_at >= ? AND created_at < ?",
        (market, pos.ticker, lo, hi),
    ).fetchone()
    last = datetime.fromisoformat(row["t"]) if row and row["t"] else None
    ref = repo.latest_trigger_price(ctx.conn, market, pos.ticker, today) or pos.avg_price
    return periodic_signal(
        quote,
        ref,
        profile=profile,
        last_periodic_at=last,
        count_today=int(row["n"] if row else 0),
        now=ctx.now(),
    )


def _cached_quote(
    ctx: MonitorContext, market: str, pos: Position, quotes: dict[str, Decimal | None]
) -> Decimal | None:
    ticker = pos.ticker
    if ticker not in quotes:
        try:
            q = _quote(ctx, market, pos)
        except (KisApiError, ValueError) as e:
            logger.warning("시세 실패 %s %s: %s", market, ticker, e)
            q = None
        if q is not None and q <= 0:
            logger.warning("시세 0 (거래소 코드·종목 확인): %s %s", market, ticker)
            q = None
        quotes[ticker] = q
    return quotes[ticker]


def _blocked_by_open_sell(
    ctx: MonitorContext, account: AccountConfig, market: str, ticker: str, local_day: date
) -> bool:
    """같은 현지 날짜의 살아 있는 SELL 이 있으면 True. 원장 status 는 fills sync(마감 뒤)가 바꾸므로
    장중엔 체결된 TRIM 도 accepted 로 남아 손절을 막는다 → 막힐 때만 그 계좌의 체결을 한 번 동기화해
    (KIS 1회) filled 로 바뀌면 막지 않는다."""
    kw = {"market": market, "exclude_filled": True}
    if not repo.has_open_order_today(ctx.conn, account.id, ticker, "SELL", local_day, **kw):
        return False
    broker = ctx.brokers.get(account.id)
    if broker is not None:
        try:
            from trader.orchestrator.fills import sync_fills

            sync_fills(ctx.conn, broker, account, local_day, now=ctx.now)
        except Exception as e:  # noqa: BLE001 - 체결 조회 실패면 보수적으로 막힌 채로 둔다
            logger.warning("손절 전 체결 동기화 실패 %s %s: %s", account.id, ticker, e)
    return repo.has_open_order_today(ctx.conn, account.id, ticker, "SELL", local_day, **kw)


def _cancellable_resting_sell(row, quote: Decimal) -> bool:
    """현재가보다 위에 걸린 지정가 SELL(아침 TRIM 등)만 손절 전 취소 대상이다. 시장가·현재가
    이하 지정가는 곧 체결될(또는 이미 낸 손절) 주문이라 취소하지 않고 그대로 둔다."""
    if row["order_type"] != "LIMIT" or row["price"] is None:
        return False
    return Decimal(str(row["price"])) > quote


def _cancel_open_sells(
    ctx: MonitorContext,
    account: AccountConfig,
    market: str,
    pos: Position,
    local_day: date,
    quote: Decimal,
) -> bool:
    """손절 전 같은 종목의 당일 미체결 SELL 중 현재가 위 지정가를 전부 취소한다. 살아 있는
    SELL 이 하나도 남지 않으면 True(손절 진행), 하나라도 남으면 False(막힌 채 둔다)."""
    broker = ctx.brokers.get(account.id)
    rows = repo.open_orders_today(
        ctx.conn, account.id, pos.ticker, "SELL", local_day, market=market
    )
    if broker is None or ctx.dry_run:
        return False
    if not all(_cancellable_resting_sell(r, quote) for r in rows):
        return False  # 시장가·현재가 이하 지정가(이미 낸 손절 포함)가 살아 있다 → 중복 방지
    ok = True
    for r in rows:
        if not r["order_no"]:  # 응답 없는(unknown) 주문은 번호가 없어 취소할 수 없다
            ok = False
            continue
        try:
            res = broker.cancel_order(
                str(r["order_no"]),
                market=market,
                org_no=r["org_no"],
                ticker=pos.ticker,
                exchange=_exchange(ctx, pos),
                qty=int(r["qty"]),
            )
        except (KisApiError, ValueError) as e:
            logger.warning(
                "손절 전 미체결 SELL 취소 실패 %s %s #%s: %s",
                account.id,
                pos.ticker,
                r["order_no"],
                e,
            )
            ok = False
            continue
        if res.accepted:
            repo.set_order_status(ctx.conn, int(r["id"]), "cancelled")
            logger.info("손절 전 미체결 SELL 취소 %s %s #%s", account.id, pos.ticker, r["order_no"])
        else:
            ok = False
    if not ok:
        ctx.notifier.send(
            messages.stop_deferred(
                account=account.id, market=market, label=_label(ctx, market, pos.ticker)
            )
        )
    return ok


def _cancel_open_buys(
    ctx: MonitorContext, account: AccountConfig, market: str, pos: Position, local_day: date
) -> int:
    """손절할 종목의 당일 미체결 BUY(아침 ADD 지정가 등)를 취소한다 — 손절 뒤 체결돼 다시 보유하게
    되는 것을 막는다(전체 검토 O13). 실패는 경고만(손절은 그대로 진행). 취소한 건수."""
    broker = ctx.brokers.get(account.id)
    if broker is None:
        return 0
    n = 0
    for r in repo.open_orders_today(
        ctx.conn, account.id, pos.ticker, "BUY", local_day, market=market
    ):
        if not r["order_no"]:
            continue  # 응답 없는 주문은 번호가 없어 취소할 수 없다
        try:
            res = broker.cancel_order(
                str(r["order_no"]),
                market=market,
                org_no=r["org_no"],
                ticker=pos.ticker,
                exchange=_exchange(ctx, pos),
                qty=int(r["qty"]),
            )
        except (KisApiError, ValueError) as e:
            logger.warning("손절 전 미체결 BUY 취소 실패 %s %s: %s", account.id, pos.ticker, e)
            continue
        if res.accepted:
            repo.set_order_status(ctx.conn, int(r["id"]), "cancelled")
            n += 1
    return n


def _monotone_stop(
    ctx: MonitorContext, account: AccountConfig, market: str, pos: Position, data: dict | None
) -> dict | None:
    """손절선을 보유 기간 중 결정들의 최고값으로 단조 유지한다(전체 검토 L5). 최신 결정이 손절가를
    비웠거나(보유 재분석 HOLD) 더 낮게 냈어도 이전 손절선이 살아 있다 — 기본 −15% 로 풀리지 않게."""
    since = repo.held_since(ctx.conn, account.id, [pos.ticker]).get(pos.ticker)
    best = repo.max_stop_since(ctx.conn, market, pos.ticker, since)
    if best is None:
        return data
    cur = (data or {}).get("stop_loss")
    if cur is not None and Decimal(str(cur)) >= best:
        return data
    return {**(data or {}), "stop_loss": float(best)}


def _sell_now(
    ctx: MonitorContext,
    account: AccountConfig,
    market: str,
    pos: Position,
    quote: Decimal,
    sig: Signal,
    today: date,
    latest: dict | None,
) -> int:
    """손절 즉시 매도. 같은 날 같은 종목의 살아 있는 SELL 이 있으면 다시 내지 않는다.

    "같은 날" 은 시장 현지 날짜다 (US 는 KST 자정을 넘겨 장이 열려 있어 KST 날짜로 보면
    23:30 KST 에 낸 손절 매도를 00:30 KST 점검이 놓치고 다시 낸다)."""
    local_day = ctx.now().astimezone(MARKET_TZ.get(market, KST)).date()
    mode, _ = executor.effective_mode(ctx.conn, account)
    # 실제로 손절 주문을 낼 수 있는 상태인가 — 아니면 KIS 쓰기(미체결 SELL 취소)를 하지 않는다.
    # 일시정지·off·dry-run 에서 취소만 하고 손절은 안 내면 보호 매도만 사라진다(전체 검토 O5)
    live = not ctx.dry_run and mode != "off" and not control.is_paused(ctx.conn)
    if live and _blocked_by_open_sell(ctx, account, market, pos.ticker, local_day):
        # 아침 TRIM 지정가 같은 미체결 SELL 이 하루 종일 손절을 막으면 안 된다 → 취소하고 진행.
        # 취소가 안 되면(응답 실패·이미 체결 중) 보수적으로 막힌 채 둔다
        if not _cancel_open_sells(ctx, account, market, pos, local_day, quote):
            return 0
    if live:
        _cancel_open_buys(ctx, account, market, pos, local_day)
    unrecorded = f"sl_unrecorded:{account.id}:{market}:{pos.ticker}:{local_day.isoformat()}"
    if control.get_flag(ctx.conn, unrecorded):
        return 0  # 접수됐지만 원장에 못 적은 손절이 있다 (아래) — 오늘은 다시 내지 않는다
    # 기록만 하는 손절(dry-run·off)의 하루 1회 억제는 **그 상태에서만** — 같은 날 auto 로 바꾸면
    # 실제 손절이 나가야 한다(전체 검토 O15). 키에 실행 상태를 넣는다
    state = f"{'dry' if ctx.dry_run else 'live'}-{mode}"
    recorded = f"sl_recorded:{account.id}:{market}:{pos.ticker}:{local_day.isoformat()}:{state}"
    if control.get_flag(ctx.conn, recorded):
        return 0  # dry-run·off 계좌의 손절은 기록만 하므로 하루 1회면 충분하다(매 분 반복 금지)
    if control.is_paused(ctx.conn):
        key = f"sl_paused_notified:{market}:{pos.ticker}:{today.isoformat()}"
        if not control.get_flag(ctx.conn, key):
            control.set_flag(ctx.conn, key, True)
            ctx.notifier.send(
                messages.stop_paused(
                    market=market,
                    label=_label(ctx, market, pos.ticker),
                    quote=quote,
                    level=sig.detail.get("level"),
                )
            )
        return 0
    dec = _stop_loss_decision(pos.ticker, market, quote, sig)
    ectx = executor.ExecContext(
        settings=ctx.settings,
        accounts=[account],
        conn=ctx.conn,
        brokers=ctx.brokers,
        notifier=ctx.notifier,
        state=ctx.state,
        now=ctx.now,
    )
    try:
        results = executor.execute(
            ectx,
            dec,
            date=today,
            market=market,
            price=quote,
            exchange=_exchange(ctx, pos),
            dry_run=ctx.dry_run,
            is_stop_loss=True,
            decision_id=(latest or {}).get("id"),
        )
    except executor.OrderBookkeepingError as e:
        # 접수는 됐는데 orders 행이 없다: 다음 틱이 "살아 있는 SELL 없음" 으로 보고 같은 매도를
        # 다시 내지 않도록 플래그로 막고(알림은 _place 가 보냈다) 다른 종목 점검은 계속한다
        logger.error("손절 주문 기록 실패 %s %s %s: %s", account.id, market, pos.ticker, e)
        control.set_flag(ctx.conn, unrecorded, e.order_no or "-")
        return 0
    r = results[0] if results else {}
    if r and not r.get("error") and (ctx.dry_run or r.get("mode") == "off"):
        control.set_flag(ctx.conn, recorded, True)
    order = r.get("order")
    if order and order.get("accepted"):
        ctx.notifier.send(
            messages.stop_loss(
                account=account.id,
                market=market,
                label=_label(ctx, market, pos.ticker),
                qty=r.get("qty"),
                quote=quote,
                level=sig.detail.get("level"),
                order_no=order.get("order_no"),
            )
        )
        return 1
    return 0
