"""간이 백테스트 (스펙 §13): 컷오프 이후 결정만 집계한다 (룩어헤드 방지).

메모리 결과(realized_return·alpha)가 있으면 그대로, 없으면 피처 스토어 종가로 결정일 이후
horizon 거래일 수익을 outcomes 와 같은 규칙(BUY/ADD +, SELL/TRIM −, HOLD 0)으로 계산한다.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from trader.memory import outcomes

_KST = ZoneInfo("Asia/Seoul")


def _decided_date(iso: str) -> date | None:
    try:
        return datetime.fromisoformat(iso).astimezone(_KST).date()
    except ValueError:
        return None


HOLD_BAND = 0.02  # HOLD 적중 = 가격이 밴드 안에 머묾(라벨 up/down 임계와 같은 값)


def _hit(action: str, realized: float, price_return: float | None) -> bool:
    if action == "HOLD":
        return price_return is not None and abs(float(price_return)) <= HOLD_BAND
    return realized > 0


def simple_backtest(
    conn: sqlite3.Connection,
    state: Path | None,
    *,
    since: date,
    market: str | None = None,
    horizon_default: int = 5,
) -> dict:
    """{n, hit_rate, avg_return, avg_alpha, by_action, cutoff, decisions, skipped}."""
    sql = "SELECT * FROM memory_entries"
    args: list = []
    if market:
        sql += " WHERE market=?"
        args.append(market)
    rows = conn.execute(sql + " ORDER BY decided_at, id", args).fetchall()
    tables: dict = {}
    decisions: list[dict] = []
    skipped = 0
    for r in rows:
        d = _decided_date(r["decided_at"])
        if d is None or d < since:
            continue
        dec = json.loads(r["decision"] or "{}") or {}
        action = str(dec.get("action") or "HOLD").upper()
        if r["realized_return"] is not None:
            realized, alpha, source = float(r["realized_return"]), r["alpha_vs_benchmark"], "memory"
            price_return = r["price_return"]
        else:
            m = r["market"]
            if m not in tables:
                tables[m] = outcomes._close_table(state, m)
            res = outcomes.window_return(
                tables[m], r["ticker"], d, int(r["horizon_days"] or horizon_default)
            )
            if res is None:
                skipped += 1
                continue
            ret, bench = res
            realized = ret * outcomes.SIGN.get(action, 0.0)
            alpha, source = realized - bench, "features"
            price_return = ret
        decisions.append(
            {
                "entry_id": int(r["id"]),
                "ticker": r["ticker"],
                "market": r["market"],
                "decided": d.isoformat(),
                "action": action,
                "realized_return": round(realized, 6),
                "alpha": None if alpha is None else round(float(alpha), 6),
                "price_return": None if price_return is None else round(float(price_return), 6),
                "hit": _hit(action, realized, price_return),
                "source": source,
            }
        )

    def agg(items: list[dict]) -> dict:
        n = len(items)
        if n == 0:
            return {"n": 0, "hit_rate": None, "avg_return": None, "avg_alpha": None}
        alphas = [x["alpha"] for x in items if x["alpha"] is not None]
        return {
            "n": n,
            "hit_rate": round(sum(1 for x in items if x["hit"]) / n, 6),
            "avg_return": round(sum(x["realized_return"] for x in items) / n, 6),
            "avg_alpha": round(sum(alphas) / len(alphas), 6) if alphas else None,
        }

    by_action = {
        a: agg([x for x in decisions if x["action"] == a])
        for a in sorted({x["action"] for x in decisions})
    }
    return {
        **agg(decisions),
        "by_action": by_action,
        "cutoff": since.isoformat(),
        "market": market,
        "skipped": skipped,
        "decisions": decisions,
    }
