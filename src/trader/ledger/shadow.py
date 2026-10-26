"""섀도 포트폴리오(전체 검토 L1): LLM 이 고른 종목이 같은 날 같은 후보를 1/N·정량 상위로 산 것보다
나은지를 운영일마다 쌍으로 잰다.

전략 3개(운영일·시장마다 1행씩, 주문 없음):
- `candidates_1n`: 그날 scan 정량 후보(점수 있는 것) 균등 비중 — LLM 이 이겨야 할 기준선
- `quant_topn`: 정량 점수 상위 `top_n` 균등 비중 — LLM 없이 정량만 쓴 경우
- `llm`: 그날 LLM 결정 중 BUY/ADD 의 `target_weight` 를 합 1 로 정규화(헤지 제외, 매수 없으면 현금)

만기는 결정일 이후 첫 거래일 종가 → `horizon` 거래일 뒤 종가(메모리 `outcomes.window_return` 과
같은 창)이고 만기 봉이 today 이하일 때만 채운다(백필 데이터로 미래를 보지 않음). readiness 는
`paired_stats` 의 llm − candidates_1n 부트스트랩 신뢰구간 하한이 0 보다 큰지를 본다.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np

logger = logging.getLogger("trader.ledger")

STRATEGIES = ("candidates_1n", "quant_topn", "llm")
DEFAULT_TOP_N = 3
HORIZON = 5
LEAD_DAYS = 10  # 시작일 직전 거래일 탐색 여유
BUY_ACTIONS = ("BUY", "ADD")


def _equal(tickers: list[str]) -> dict[str, float]:
    return {t: 1 / len(tickers) for t in tickers} if tickers else {}


def build(conn: sqlite3.Connection, *, market: str, day: date, scan: dict, top_n: int) -> dict:
    """전략별 비중 {strategy: {ticker: weight}}."""
    scored = [c for c in scan.get("candidates") or [] if c.get("score") is not None]
    scored.sort(key=lambda c: float(c["score"]), reverse=True)
    tickers = [str(c["ticker"]) for c in scored]
    rows = conn.execute(
        "SELECT ticker, target_weight FROM decisions WHERE date=? AND market=? "
        f"AND source != 'hedge' AND action IN ({','.join('?' * len(BUY_ACTIONS))})",
        (day.isoformat(), market, *BUY_ACTIONS),
    ).fetchall()
    llm = {r["ticker"]: float(r["target_weight"] or 0) for r in rows}
    total = sum(w for w in llm.values() if w > 0)
    return {
        "candidates_1n": _equal(tickers),
        "quant_topn": _equal(tickers[:top_n]),
        "llm": {t: w / total for t, w in llm.items() if w > 0} if total > 0 else {},
    }


def record(
    conn: sqlite3.Connection,
    *,
    market: str,
    day: date,
    scan: dict,
    top_n: int = DEFAULT_TOP_N,
    now: datetime | None = None,
) -> int:
    """운영일 기록. 만기 전 행은 같은 날 재실행(실패 뒤 재시도)이 덮어쓰고, 만기된 행은 그대로
    둔다(PR #33 리뷰). 쓰거나 덮어쓴 행 수."""
    created = (now or datetime.now(UTC)).isoformat()
    n = 0
    for strategy, weights in build(conn, market=market, day=day, scan=scan, top_n=top_n).items():
        cur = conn.execute(
            "INSERT INTO shadow_portfolios(market, date, strategy, weights_json, created_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(market, date, strategy) DO UPDATE SET "
            "weights_json=excluded.weights_json, created_at=excluded.created_at "
            "WHERE shadow_portfolios.fwd_ret IS NULL",
            (market, day.isoformat(), strategy, json.dumps(weights, sort_keys=True), created),
        )
        n += cur.rowcount
    conn.commit()
    return n


def rows(conn: sqlite3.Connection, *, market: str | None = None) -> list[dict]:
    sql = "SELECT * FROM shadow_portfolios"
    args: tuple = ()
    if market:
        sql += " WHERE market=?"
        args = (market,)
    return [
        {
            "market": r["market"],
            "date": r["date"],
            "strategy": r["strategy"],
            "weights": json.loads(r["weights_json"]),
            "fwd_ret": r["fwd_ret"],
            "matured_at": r["matured_at"],
        }
        for r in conn.execute(sql + " ORDER BY date, strategy", args).fetchall()
    ]


def _window_closed(dates: list[date], start: date, horizon: int, today: date) -> bool:
    starts = [i for i, d in enumerate(dates) if d >= start]
    if not starts:
        return False
    i1 = starts[0] + horizon
    return i1 < len(dates) and dates[i1] <= today


def mature(
    conn: sqlite3.Connection, *, state: Path | None, today: date, horizon: int = HORIZON
) -> int:
    """만기가 지난 미결 행의 `fwd_ret` 를 채운다(멱등). 채운 행 수.
    종가가 없는 종목은 빼고 남은 비중으로 다시 나눈다(전부 없으면 다음에 다시 본다)."""
    from trader.memory import outcomes

    pending = conn.execute(
        "SELECT id, market, date, weights_json FROM shadow_portfolios WHERE fwd_ret IS NULL "
        "ORDER BY date"
    ).fetchall()
    if not pending:
        return 0
    tables: dict[str, object] = {}
    filled = 0
    stamp = datetime.now(UTC).isoformat()
    for r in pending:
        market, start = r["market"], date.fromisoformat(r["date"])
        if market not in tables:
            since = min(
                date.fromisoformat(p["date"]) for p in pending if p["market"] == market
            ) - timedelta(days=LEAD_DAYS)
            tables[market] = outcomes._close_table(state, market, since=since, until=today)
        table = tables[market]
        if table.empty or not _window_closed(list(table.index), start, horizon, today):
            continue
        weights: dict[str, float] = json.loads(r["weights_json"])
        got: list[tuple[float, float]] = []
        for t, w in weights.items():
            res = outcomes.window_return(table, t, start, horizon, end_before=today)
            if res is not None:
                got.append((w, res[0]))
        if weights and not got:
            logger.info("섀도 만기 보류 %s %s: 종가 없음", market, r["date"])
            continue
        wsum = sum(w for w, _ in got)
        ret = sum(w * x for w, x in got) / wsum if wsum > 0 else 0.0
        conn.execute(
            "UPDATE shadow_portfolios SET fwd_ret=?, matured_at=? WHERE id=?",
            (ret, stamp, r["id"]),
        )
        filled += 1
    conn.commit()
    return filled


def _block_bootstrap_means(diffs: np.ndarray, *, block: int, n_boot: int, seed: int) -> np.ndarray:
    """이동 블록 부트스트랩: 길이 `block` 의 연속 구간을 복원 추출해 이어 붙이고 n 개로 자른 표본의
    평균 n_boot 개."""
    n = diffs.size
    b = max(1, min(block, n))
    k = -(-n // b)  # ceil
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, n - b + 1, size=(n_boot, k))
    idx = (starts[:, :, None] + np.arange(b)).reshape(n_boot, k * b)[:, :n]
    return diffs[idx].mean(axis=1)


def paired_stats(
    conn: sqlite3.Connection,
    *,
    a: str = "llm",
    b: str = "candidates_1n",
    market: str | None = None,
    n_boot: int = 2000,
    seed: int = 0,
) -> dict:
    """같은 (시장, 운영일) 에 둘 다 만기된 쌍의 a − b 평균과 95% 신뢰구간.
    연속 운영일의 5거래일 수익은 4일씩 겹쳐 독립이 아니다 → 날짜 순서를 지키는 이동 블록
    부트스트랩(블록 길이 = `HORIZON`)으로 구간을 넓게 잡는다(PR #33 리뷰)."""
    sql = (
        "SELECT x.fwd_ret - y.fwd_ret AS d FROM shadow_portfolios x JOIN shadow_portfolios y "
        "ON x.market = y.market AND x.date = y.date "
        "WHERE x.strategy=? AND y.strategy=? AND x.fwd_ret IS NOT NULL AND y.fwd_ret IS NOT NULL"
    )
    order = " ORDER BY x.market, x.date"
    args: list = [a, b]
    if market:
        sql += " AND x.market=?"
        args.append(market)
    diffs = np.array([float(r["d"]) for r in conn.execute(sql + order, args).fetchall()])
    if diffs.size == 0:
        return {"n": 0, "mean_diff": None, "ci_low": None, "ci_high": None}
    means = _block_bootstrap_means(diffs, block=HORIZON, n_boot=n_boot, seed=seed)
    return {
        "n": int(diffs.size),
        "mean_diff": round(float(diffs.mean()), 6),
        "ci_low": round(float(np.percentile(means, 2.5)), 6),
        "ci_high": round(float(np.percentile(means, 97.5)), 6),
    }
