"""만기 결정의 결과 채우기 (스펙 §6.5): 피처 스토어 종가로 결정의 판단 품질을 채점한다.

결정일 D 의 종가 → D 에서 horizon 거래일 뒤 종가의 원시 수익 ret(price_return).

- direction_score (전체 검토 L3, 판단 품질의 기준): BUY/ADD = +ret, SELL/TRIM = −ret,
  HOLD 는 결정 시점에 보유 중이었으면 +ret(들고 있기로 한 판단), 아니면 0(관망은 손익 없음).
- avoided_loss: SELL/TRIM 과 미보유 HOLD 의 max(0, −ret) — 판 뒤·안 산 뒤 떨어진 만큼. 그 외 NULL.
- post_fill_return: 그 라운드의 실제 체결 평균가 → 만기 종가 수익에 같은 방향 부호(BUY +, SELL −).
  매수는 보유 성과, 매도는 판 뒤 회피 성과다. 결정일 종가 대신 체결가라 슬리피지가 들어간다.
- realized_pnl_pct: **SELL/TRIM 의 실제 실현손익** — 그 라운드 매도 체결을 계좌 전체 체결의 FIFO
  원가에 맞춰 (매도 대금 − 원가)/원가 − 왕복비용(2-L8: 옛 값은 매도 뒤 전방 수익의 부호 반전이라
  매입원가와 무관했다). 원가를 모르는 수량(원장 밖 매수)만 있으면 NULL, 매수·HOLD 는 NULL.
- realized_return·alpha 는 하위 호환(옛 채점: HOLD 0 · 알파 = −벤치마크, SELL 은 가상 공매도).

보유 여부는 엔트리의 held_at_decision(기록 때 잔고 스냅샷), 없으면 결정일 positions_snapshot.
종가가 없거나 만기 전이면 건너뛴다.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd

from trader.config import KST
from trader.memory import store
from trader.ml import dataset
from trader.risk import costs

logger = logging.getLogger("trader.memory")
WINDOW_LEAD_DAYS = 10  # 결정일 직전 거래일 탐색 여유
SIGN = {"BUY": 1.0, "ADD": 1.0, "SELL": -1.0, "TRIM": -1.0, "HOLD": 0.0}
ORDER_SIDE = {"BUY": "BUY", "ADD": "BUY", "SELL": "SELL", "TRIM": "SELL"}


def _close_table(
    state: Path | None, market: str, *, since: date | None = None, until: date | None = None
) -> pd.DataFrame:
    # 만기 계산은 미결 결정일 이후 봉만 필요하다 — 스토어 전체(1년에 250파일씩 증가)를 읽지 않는다
    return dataset.close_table(state, market, since=since, until=until)


def window_return(
    table: pd.DataFrame, ticker: str, start: date, horizon: int, *, end_before: date | None = None
) -> tuple[float, float] | None:
    """(종목 수익, 1/N 벤치마크 수익): 시작일 이후 첫 거래일 → horizon 거래일 뒤. 없으면 None.
    end_before 가 있으면 만기 봉 날짜가 그 날 이후일 때 None(백필 데이터로 미래를 보지 않음)."""
    w = _window(table, ticker, start, horizon, end_before=end_before)
    return None if w is None else (w[0], w[1])


def _window(
    table: pd.DataFrame, ticker: str, start: date, horizon: int, *, end_before: date | None = None
) -> tuple[float, float, float] | None:
    """(종목 수익, 1/N 벤치마크 수익, 만기 종가)."""
    if table.empty or ticker not in table.columns:
        return None
    dates = list(table.index)
    starts = [i for i, d in enumerate(dates) if d >= start]
    if not starts:
        return None
    i0 = starts[0]
    i1 = i0 + horizon
    if i1 >= len(dates) or (end_before is not None and dates[i1] > end_before):
        return None
    c0, c1 = table.iloc[i0][ticker], table.iloc[i1][ticker]
    if pd.isna(c0) or pd.isna(c1) or float(c0) <= 0:
        return None
    r = float(c1) / float(c0) - 1
    row0, row1 = table.iloc[i0], table.iloc[i1]
    both = (~row0.isna()) & (~row1.isna()) & (row0 > 0)
    bench = float((row1[both] / row0[both] - 1).mean()) if both.any() else 0.0
    return r, bench, float(c1)


def direction_scores(action: str, ret: float, held: bool | None) -> tuple[float, float | None]:
    """(direction_score, avoided_loss). 보유 모름(None)은 미보유로 본다."""
    if action in ("BUY", "ADD"):
        return ret, None
    if action in ("SELL", "TRIM"):
        return -ret, max(0.0, -ret)
    if held:
        return ret, None
    return 0.0, max(0.0, -ret)


def _realized_sell_pct(conn, r, fill_ids: set[int], cost_bp: float) -> float | None:
    """그 라운드 매도 체결의 실제 실현손익률(FIFO 원가, 왕복비용 차감). 원가를 모르면 None."""
    from decimal import Decimal

    from trader.ledger import lots

    if not fill_ids:
        return None
    rows = conn.execute(
        "SELECT f.id, o.side, f.qty, f.price, f.filled_at FROM fills f JOIN orders o "
        "ON o.id = f.order_id WHERE f.account_id=? AND o.ticker=? AND o.market=? "
        "ORDER BY f.filled_at, f.id",
        (r["account_id"], r["ticker"], r["market"]),
    ).fetchall()
    fills = []
    for x in rows:
        try:
            fills.append(
                lots.FillRow(
                    ticker=r["ticker"],
                    side=str(x["side"]),
                    qty=int(x["qty"]),
                    price=Decimal(str(x["price"])),
                    filled_at=datetime.fromisoformat(x["filled_at"]),
                    fill_id=int(x["id"]),
                )
            )
        except (TypeError, ValueError):
            continue
    try:
        realized = lots.fifo(fills, market=r["market"])["realized"]
    except TypeError:  # 체결 시각 형식이 섞여(시간대 유무) 정렬할 수 없으면 원가를 모른다고 본다
        return None
    got = [z for z in realized if z.sell_fill in fill_ids]
    cost = sum((z.cost for z in got), Decimal(0))
    if cost <= 0:
        return None
    pnl = sum((z.pnl for z in got), Decimal(0))
    return float(pnl / cost) - cost_bp / 10_000


def _round_fills(conn, r, action: str, next_decided_at: str | None) -> list[tuple[int, int, float]]:
    """그 라운드의 체결 [(fills.id, 수량, 가격)]: 같은 결정·계좌·방향 주문 중 엔트리 decided_at
    이후, 다음 라운드 엔트리 decided_at 이전에 낸 것(decided_at 은 실행 직전 시각이라 라운드 주문이
    그 뒤에 생긴다)."""
    side = ORDER_SIDE.get(action)
    if side is None or r["decision_id"] is None:
        return []
    start = datetime.fromisoformat(r["decided_at"])
    end = datetime.fromisoformat(next_decided_at) if next_decided_at else None
    if end is not None and end <= start:
        end = None
    approvals = [
        (datetime.fromisoformat(a["decided_at"]), datetime.fromisoformat(a["requested_at"]))
        for a in conn.execute(
            "SELECT requested_at, decided_at FROM approvals WHERE decision_id=? AND account_id=? "
            "AND decided_at IS NOT NULL",
            (r["decision_id"], r["account_id"]),
        ).fetchall()
    ]
    out: list[tuple[int, int, float]] = []
    for o in conn.execute(
        "SELECT o.created_at, o.mode, f.id AS fill_id, f.qty, f.price FROM orders o "
        "JOIN fills f ON f.order_id = o.id WHERE o.decision_id=? AND o.account_id=? AND o.side=?",
        (r["decision_id"], r["account_id"], side),
    ).fetchall():
        try:
            at = datetime.fromisoformat(o["created_at"])
        except ValueError:
            continue
        if o["mode"] == "approve":
            # 승인 주문은 승인 시각에 생긴다 — 요청을 낸 라운드로 돌린다(승인 처리 시각 ≤ 주문)
            took = [(dec, req) for dec, req in approvals if dec <= at]
            if took:
                at = max(took)[1]  # 이 주문을 낸 승인 = 주문 직전에 처리된 것
        if at < start or (end is not None and at >= end):
            continue
        out.append((int(o["fill_id"]), int(o["qty"]), float(o["price"])))
    return out


def _decided_date(r) -> date | None:
    """가격 창의 시작일 = 결정 행의 운영일(US 는 뉴욕 날짜), 없으면 decided_at 의 KST 날짜."""
    if r["run_date"]:
        try:
            return date.fromisoformat(r["run_date"])
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(r["decided_at"]).astimezone(KST).date()
    except ValueError:
        return None


def _held(conn, r) -> bool | None:
    if r["held_at_decision"] is not None:
        return bool(r["held_at_decision"])
    d = conn.execute("SELECT date FROM decisions WHERE id=?", (r["decision_id"],)).fetchone()
    if d is None or not r["account_id"]:
        return None
    return store.held_at(
        conn, account_id=r["account_id"], market=r["market"], ticker=r["ticker"], d=d["date"]
    )


def mature(conn, *, state: Path | None, today: date, horizon_default: int = 5) -> list[dict]:
    """outcome 이 없는 엔트리 중 만기가 지난 것의 결과를 채우고 요약을 돌려준다 (멱등)."""
    rows = conn.execute(
        "SELECT m.id, m.ticker, m.market, m.account_id, m.decision_id, m.round, m.decided_at, "
        "m.decision, m.horizon_days, m.held_at_decision, d.date AS run_date "
        "FROM memory_entries m LEFT JOIN decisions d ON d.id = m.decision_id "
        "WHERE m.outcome_at IS NULL ORDER BY m.id"
    ).fetchall()
    # 라운드 r 의 체결 창 상한 = 같은 결정의 다음 라운드 엔트리 decided_at
    next_at: dict[tuple[int, int], str] = {}
    prev: tuple[int, int] | None = None
    for x in conn.execute(
        "SELECT decision_id, round, decided_at FROM memory_entries "
        "WHERE decision_id IS NOT NULL ORDER BY decision_id, round"
    ).fetchall():
        key = (int(x["decision_id"]), int(x["round"]))
        if prev is not None and prev[0] == key[0]:
            next_at[prev] = x["decided_at"]
        prev = key
    earliest: dict[str, date] = {}
    for r in rows:
        d0 = _decided_date(r)
        if d0 is None:
            continue
        m = r["market"]
        earliest[m] = min(earliest.get(m, d0), d0)
    tables: dict[str, pd.DataFrame] = {}
    out: list[dict] = []
    for r in rows:
        market = r["market"]
        if market not in tables:
            since = (
                earliest[market] - timedelta(days=WINDOW_LEAD_DAYS) if market in earliest else None
            )
            tables[market] = _close_table(state, market, since=since, until=today)
        decided = _decided_date(r)
        if decided is None:
            continue
        horizon = int(r["horizon_days"] or horizon_default)
        res = _window(tables[market], r["ticker"], decided, horizon, end_before=today)
        if res is None:
            continue
        ret, bench, close1 = res
        action = str((json.loads(r["decision"] or "{}") or {}).get("action") or "HOLD").upper()
        sign = SIGN.get(action, 0.0)
        realized = ret * sign
        alpha = realized - bench
        direction, avoided = direction_scores(action, ret, _held(conn, r))
        key = (int(r["decision_id"]), int(r["round"])) if r["decision_id"] is not None else None
        round_fills = _round_fills(conn, r, action, next_at.get(key) if key else None)
        fqty = sum(q for _, q, _ in round_fills)
        fill = sum(q * p for _, q, p in round_fills) / fqty if fqty > 0 else None
        post = sign * (close1 / fill - 1) if fill else None
        realized_pnl = (
            _realized_sell_pct(
                conn, r, {i for i, _, _ in round_fills}, costs.round_trip_bp(r["market"])
            )
            if action in ("SELL", "TRIM")
            else None
        )
        outcome_at = datetime.now(UTC).isoformat()
        store.fill_outcome(
            conn,
            int(r["id"]),
            realized_return=realized,
            alpha=alpha,
            outcome_at=outcome_at,
            price_return=ret,
            direction_score=direction,
            avoided_loss=avoided,
            realized_pnl_pct=realized_pnl,
            post_fill_return=post,
        )
        out.append(
            {
                "entry_id": int(r["id"]),
                "ticker": r["ticker"],
                "market": market,
                "realized_return": round(realized, 6),
                "alpha": round(alpha, 6),
                "direction_score": round(direction, 6),
            }
        )
    return out
