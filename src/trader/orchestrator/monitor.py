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
from zoneinfo import ZoneInfo

from trader.agents.schemas import Decision
from trader.broker.errors import KisApiError
from trader.broker.models import Position
from trader.config import MARKET_TZ, AccountConfig, Settings
from trader.control import control
from trader.ledger import repo
from trader.notify.telegram import Notifier, NullNotifier
from trader.orchestrator import executor, resume, tools

logger = logging.getLogger("trader.monitor")
_KST = ZoneInfo("Asia/Seoul")

MOVE_PCT = Decimal("0.07")
REEVAL_INTERVAL = timedelta(minutes=30)
_NUM_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)")


@dataclass
class Signal:
    kind: str  # stop_loss | take_profit | move_7pct | invalidation
    price: Decimal
    detail: dict = field(default_factory=dict)


def parse_price_conditions(conditions: list[str]) -> list[tuple[str, Decimal]]:
    """'종가 < 65000', '70,000원 이상' 같은 문장에서 (연산자, 가격) 만 뽑는다. 지표 조건은 무시."""
    out: list[tuple[str, Decimal]] = []
    for c in conditions or []:
        text = str(c)
        m = _NUM_RE.search(text)
        if not m:
            continue
        num = Decimal(m.group(1).replace(",", ""))
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
) -> list[Signal]:
    """순수 함수. 손절이면 손절 신호만, 아니면 take_profit → move_7pct → invalidation 순."""
    dec = decision or {}
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
        if abs(move) >= MOVE_PCT:
            out.append(Signal("move_7pct", quote, {"move": _q2(move * 100) + "%"}))
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


def _quote(ctx: MonitorContext, market: str, ticker: str) -> Decimal:
    if market == "KR":
        return ctx.market_client.get_price_kr(ticker).price
    return ctx.market_client.get_price_us(ticker, ctx.exchanges.get(ticker, "NAS")).price


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


def _can_reevaluate(ctx: MonitorContext, market: str, ticker: str) -> str | None:
    if ctx.session_runner is None:
        return "session_runner 없음"
    if control.is_paused(ctx.conn):
        return "일시정지"
    if tools.sessions_today(ctx.conn, ctx.now()) >= ctx.settings.llm.daily_session_cap:
        return "세션 상한"
    last = repo.last_trigger_handled_at(ctx.conn, market, ticker)
    if last is not None and ctx.now() - last < REEVAL_INTERVAL:
        return "30분 내 재평가 있음"
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
        for pos in positions:
            quote = _cached_quote(ctx, market, pos.ticker, quotes)
            if quote is None:
                continue
            result["checked"] += 1
            latest = repo.latest_decision(ctx.conn, market, pos.ticker)
            dec_data = (latest or {}).get("data")
            signals = check_position(
                pos, dec_data, quote, stop_loss_pct=ctx.settings.risk.stop_loss_pct, now=ctx.now()
            )
            if not signals:
                continue
            if signals[0].kind == "stop_loss":
                result["stop_loss_orders"] += _sell_now(
                    ctx, account, market, pos, quote, signals[0], today, latest
                )
            else:
                pending.append((account, pos, signals, latest))
    handled: set[str] = set()
    for account, pos, signals, latest in pending:
        first_id: int | None = None
        for sig in signals:
            existing = repo.unhandled_trigger_id(ctx.conn, market, pos.ticker, sig.kind)
            if existing is not None:  # 미처리 트리거가 있으면 다시 쌓지 않고 그 행을 쓴다
                first_id = first_id or existing
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
            )
            first_id = first_id or tid
            result["triggers"] += 1
        if pos.ticker in handled:
            continue
        handled.add(pos.ticker)
        why = _can_reevaluate(ctx, market, pos.ticker)
        if why:
            logger.info("재평가 생략 %s %s: %s", market, pos.ticker, why)
            continue
        s0 = signals[0]
        try:
            res = resume.reevaluate(
                ctx.settings,
                ctx.conn,
                ctx.accounts,
                market=market,
                date=today,
                ticker=pos.ticker,
                exchange=ctx.exchanges.get(pos.ticker),
                trigger_id=first_id or 0,
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
            if first_id:
                repo.mark_trigger_handled(
                    ctx.conn,
                    first_id,
                    f"실패: {type(e).__name__}: {e}"[:200],
                    handled_at=ctx.now().isoformat(),
                )
            ctx.notifier.send(f"[재평가 실패] {market} {pos.ticker}: {type(e).__name__}: {e}")
            continue
        if res.ok:
            result["reevaluated"] += 1
    return result


def _cached_quote(
    ctx: MonitorContext, market: str, ticker: str, quotes: dict[str, Decimal | None]
) -> Decimal | None:
    if ticker not in quotes:
        try:
            q = _quote(ctx, market, ticker)
        except (KisApiError, ValueError) as e:
            logger.warning("시세 실패 %s %s: %s", market, ticker, e)
            q = None
        if q is not None and q <= 0:
            logger.warning("시세 0 (거래소 코드·종목 확인): %s %s", market, ticker)
            q = None
        quotes[ticker] = q
    return quotes[ticker]


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
    local_day = ctx.now().astimezone(MARKET_TZ.get(market, _KST)).date()
    if repo.has_open_order_today(
        ctx.conn, account.id, pos.ticker, "SELL", local_day, market=market
    ):
        return 0
    if control.is_paused(ctx.conn):
        key = f"sl_paused_notified:{market}:{pos.ticker}:{today.isoformat()}"
        if not control.get_flag(ctx.conn, key):
            control.set_flag(ctx.conn, key, True)
            ctx.notifier.send(
                f"[손절 보류: 일시정지] {market} {pos.ticker} 현재가 {quote} ≤ 손절선 "
                f"{sig.detail.get('level')} — 재개하면 다음 점검에서 매도"
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
    results = executor.execute(
        ectx,
        dec,
        date=today,
        market=market,
        price=quote,
        exchange=ctx.exchanges.get(pos.ticker),
        dry_run=ctx.dry_run,
        is_stop_loss=True,
        decision_id=(latest or {}).get("id"),
    )
    r = results[0] if results else {}
    order = r.get("order")
    if order and order.get("accepted"):
        ctx.notifier.send(
            f"[손절] {account.id} {market} {pos.ticker} {r.get('qty')}주 즉시 매도 "
            f"(현재가 {quote}, 손절선 {sig.detail.get('level')}) 접수번호 {order.get('order_no')}"
        )
        return 1
    return 0
