"""섀도 포트폴리오(전체 검토 L1): LLM 이 고른 종목이 같은 날 같은 후보를 1/N·정량 상위로 산 것보다
나은지를 운영일마다 쌍으로 잰다.

전략(운영일·시장마다 1행씩, 주문 없음):
- `candidates_1n`: 그날 scan 정량 후보(점수 있는 것) 균등 비중 — LLM 이 이겨야 할 기준선
- `quant_topn`: 정량 점수 상위 `top_n` 균등 비중 — LLM 없이 정량만 쓴 경우
- `llm`: 그날 LLM 결정 중 BUY/ADD 의 `target_weight` 를 합 1 로 정규화(헤지 제외, 매수 없으면 현금)
- `index_hold`·`index_trend`(Stock D-037~039 교훈 — 규칙 조합이 3년간 지수 보유를 못 이겼다):
  지수 proxy(`hedge.index_proxy`) 보유, 그리고 proxy 가 `TREND_MA` 일선 위면 보유·아래면 현금.
  proxy 는 피처 스토어에 없어 기록·만기 때 KIS 일봉 종가표(`index_table`)를 넘겨 받는다
  (없으면 생략).

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
import pandas as pd

logger = logging.getLogger("trader.ledger")

STRATEGIES = ("candidates_1n", "quant_topn", "llm", "index_hold", "index_trend")
INDEX_STRATEGIES = ("index_hold", "index_trend")
TREND_MA = 100
DEFAULT_TOP_N = 3
HORIZON = 5
LEAD_DAYS = 10  # 시작일 직전 거래일 탐색 여유
# 비중 있는 종목의 종가가 없을 때 만기 봉 뒤 이만큼(달력일)은 보류 — 그 사이 일일 스냅샷(열린 창
# 종목, `ledger/open_window`)이 봉을 채운다. 지나면 `partial` 로 채우고 paired_stats 에서 뺀다(2-L1)
GRACE_DAYS = 14
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


def index_weights(index: pd.DataFrame, *, day: date) -> dict[str, dict[str, float]]:
    """지수 기준선 비중. `index` 는 날짜 색인·proxy 열 하나의 종가표(day 까지의 봉만 본다)."""
    if index is None or index.empty or len(index.columns) != 1:
        return {}
    sym = str(index.columns[0])
    closes = index[sym].dropna()
    closes = closes[[d <= day for d in closes.index]]
    if closes.empty:
        return {}
    trend = len(closes) >= TREND_MA and float(closes.iloc[-1]) > float(
        closes.iloc[-TREND_MA:].mean()
    )
    return {"index_hold": {sym: 1.0}, "index_trend": {sym: 1.0} if trend else {}}


def index_table(market_client, hedge_cfg, market: str, *, days: int = 200) -> pd.DataFrame:
    """지수 proxy 일봉 종가표(KIS). 실패·설정 없음은 빈 표 — 지수 기준선만 빠진다."""
    inst = (hedge_cfg.index_proxy or {}).get(market) if hedge_cfg is not None else None
    if market_client is None or inst is None:
        return pd.DataFrame()
    try:
        candles = (
            market_client.get_daily_kr(inst.symbol, days=days)
            if market == "KR"
            else market_client.get_daily_us(inst.symbol, inst.exchange or "AMS", days=days)
        )
    except Exception as e:  # noqa: BLE001 - 측정 보조: 실패하면 지수 기준선만 생략
        logger.warning("지수 proxy 일봉 실패 %s %s: %s", market, inst.symbol, e)
        return pd.DataFrame()
    if not candles:
        return pd.DataFrame()
    return pd.DataFrame(
        {inst.symbol: [float(c.close) for c in candles]},
        index=pd.Index([c.date for c in candles], name="date"),
    )


def record(
    conn: sqlite3.Connection,
    *,
    market: str,
    day: date,
    scan: dict,
    top_n: int = DEFAULT_TOP_N,
    now: datetime | None = None,
    index: pd.DataFrame | None = None,
) -> int:
    """운영일 기록. 만기 전 행은 같은 날 재실행(실패 뒤 재시도)이 덮어쓰고, 만기된 행은 그대로
    둔다(PR #33 리뷰). `index` 가 있으면 지수 기준선도. 쓰거나 덮어쓴 행 수."""
    created = (now or datetime.now(UTC)).isoformat()
    n = 0
    plans = build(conn, market=market, day=day, scan=scan, top_n=top_n)
    if index is not None:
        plans.update(index_weights(index, day=day))
    for strategy, weights in plans.items():
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
            "partial": bool(r["partial"]),
        }
        for r in conn.execute(sql + " ORDER BY date, strategy", args).fetchall()
    ]


def pending_tickers(conn: sqlite3.Connection, market: str) -> set[str]:
    """만기 전 행의 비중 종목(지수 기준선 제외) — 열린 창 종목(2-L1) 의 섀도 부분."""
    out: set[str] = set()
    for r in conn.execute(
        "SELECT weights_json FROM shadow_portfolios WHERE market=? AND fwd_ret IS NULL "
        f"AND strategy NOT IN ({','.join('?' * len(INDEX_STRATEGIES))})",
        (market, *INDEX_STRATEGIES),
    ):
        out |= {str(t) for t in (json.loads(r["weights_json"]) or {})}
    return out


def _maturity_date(dates: list[date], start: date, horizon: int) -> date | None:
    """시작일 이후 첫 거래일에서 horizon 거래일 뒤 봉의 날짜(표에 아직 없으면 None)."""
    starts = [i for i, d in enumerate(dates) if d >= start]
    if not starts:
        return None
    i1 = starts[0] + horizon
    return dates[i1] if i1 < len(dates) else None


def _window_closed(dates: list[date], start: date, horizon: int, today: date) -> bool:
    due = _maturity_date(dates, start, horizon)
    return due is not None and due <= today


def mature(
    conn: sqlite3.Connection,
    *,
    state: Path | None,
    today: date,
    horizon: int = HORIZON,
    index_tables: dict[str, pd.DataFrame] | None = None,
) -> int:
    """만기가 지난 미결 행의 `fwd_ret` 를 채운다(멱등). 채운 행 수.
    비중 있는 종목이 하나라도 종가가 없으면 만기 봉 뒤 `GRACE_DAYS` 까지 보류한다(2-L1 — 유니버스
    밖 종목의 봉은 일일 스냅샷이 열린 창 종목으로 채운다). 유예가 지나도 없으면 남은 비중으로 다시
    나눠 채우되 `partial` 로 표시해 `paired_stats` 에서 뺀다(전부 없으면 0).
    `index_tables` 는 시장별 지수 proxy 종가표(피처 스토어에 없는 proxy 를 채운다)."""
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
            table = outcomes._close_table(state, market, since=since, until=today)
            idx = (index_tables or {}).get(market)
            if idx is not None and not idx.empty:
                idx = idx[[since <= d <= today for d in idx.index]]
                extra = idx[[c for c in idx.columns if c not in table.columns]]
                table = extra if table.empty else table.join(extra, how="outer").sort_index()
            tables[market] = table
        table = tables[market]
        if table.empty or not _window_closed(list(table.index), start, horizon, today):
            continue
        weights: dict[str, float] = json.loads(r["weights_json"])
        got: list[tuple[float, float]] = []
        missing: list[str] = []
        for t, w in weights.items():
            res = outcomes.window_return(table, t, start, horizon, end_before=today)
            if res is not None:
                got.append((w, res[0]))
            else:
                missing.append(t)
        if missing:
            due = _maturity_date(list(table.index), start, horizon)
            if due is None or today <= due + timedelta(days=GRACE_DAYS):
                logger.info(
                    "섀도 만기 보류 %s %s: 종가 없음 %s", market, r["date"], ", ".join(missing)
                )
                continue
            logger.warning(
                "섀도 부분 만기 %s %s: 유예 뒤에도 종가 없음 %s — paired_stats 제외",
                market,
                r["date"],
                ", ".join(missing),
            )
        wsum = sum(w for w, _ in got)
        ret = sum(w * x for w, x in got) / wsum if wsum > 0 else 0.0
        conn.execute(
            "UPDATE shadow_portfolios SET fwd_ret=?, matured_at=?, partial=? WHERE id=?",
            (ret, stamp, 1 if missing else 0, r["id"]),
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
    """같은 (시장, 운영일) 에 둘 다 만기된 쌍의 a − b 평균과 95% 신뢰구간(`partial` 행 제외).
    연속 운영일의 5거래일 수익은 4일씩 겹쳐 독립이 아니다 → 날짜 순서를 지키는 이동 블록
    부트스트랩(블록 길이 = `HORIZON`)으로 구간을 넓게 잡는다(PR #33 리뷰)."""
    sql = (
        "SELECT x.fwd_ret - y.fwd_ret AS d FROM shadow_portfolios x JOIN shadow_portfolios y "
        "ON x.market = y.market AND x.date = y.date "
        "WHERE x.strategy=? AND y.strategy=? AND x.fwd_ret IS NOT NULL AND y.fwd_ret IS NOT NULL "
        "AND x.partial = 0 AND y.partial = 0"
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
