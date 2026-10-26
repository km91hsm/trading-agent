"""테마 열기 감지 (Phase 25 단계 4). 오늘 '불이 붙은' 테마를 점수로 고른다 — 매매는 하지 않는다.

신호(전부 파이썬, LLM 없음):
- news: 오늘 새 내용 뉴스(`news_items.novel=1`) 건수 − 전일 건수 (테마 종목 태그 기준)
- rank: KIS 거래량·등락률 순위(실전 전용, KR)에 든 테마 종목 (순위가 높을수록 가중)
- flow: KIS 외인·기관 순매수 합계 순위에 든 테마 종목 수 (KR)
- saveticker: 세이브티커 일별 화면(community·market jsonl)에서 전일보다 순위가 오르거나 새로 든 종목
- vol: 피처 스토어 `vol_z_20` 의 양수 평균

테마 = config/sectors.yaml 대분류(`섹터:<이름>`) + 오늘 새 뉴스 제목 키워드 공출현 군집
(`kw:<키워드>`).
성분마다 테마 간 최댓값으로 0~1 정규화 뒤 `theme.weights` 가중합. `catalyst_ok` 는 테마 종목에
태그된 새 뉴스 중 커뮤니티·세이브티커가 아닌 촉매가 1건 이상일 때만 참 — scout 가 테마 종목을
후보로 올릴 수 있는 조건이고, 실제 매매는 기존 분석 세션 → 게이트 경로로만 간다.

순위 API 응답 필드명은 미확인이라 빈 목록이 와도 나머지 신호로 점수를 낸다.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from trader.ledger import repo

logger = logging.getLogger("trader.scan.theme")

FLAG = "theme_heat"  # control 키 접두: theme_heat:<market>
TOP_FLAG = "theme_top"  # 알림용 직전 상위 테마 집합: theme_top:<market>
COMPONENTS = ("news", "rank", "flow", "saveticker", "vol")
# 촉매로 치지 않는 출처(커뮤니티·세이브티커 화면). news_items.source 접두로 판정
NON_CATALYST = (
    "saveticker",
    "community",
    "reddit",
    "clien",
    "dc",
    "fmkorea",
    "youtube",
    "telegram",
    "blog",
)
_TOKEN = re.compile(r"[가-힣A-Za-z][가-힣A-Za-z0-9]{1,}")
_STOP = {
    "속보",
    "단독",
    "종합",
    "오늘",
    "주가",
    "증시",
    "코스피",
    "코스닥",
    "시장",
    "투자",
    "종목",
    "관련",
    "the",
    "and",
    "for",
    "with",
    "stock",
    "stocks",
    "shares",
    "market",
    "says",
    "from",
}


@dataclass
class ThemeInputs:
    market: str
    news_today: list[dict]  # {id, title, tickers, source}
    news_prev: list[dict]
    ranks: dict[str, list[Any]] = field(default_factory=dict)  # volume|fluct|flow → [RankRow]
    st_today: dict[str, int] = field(default_factory=dict)  # 종목 → 세이브티커 최고 순위
    st_prev: dict[str, int] = field(default_factory=dict)
    vol_z: dict[str, float] = field(default_factory=dict)
    sector_of: Callable[[str], str | None] | None = None
    names: dict[str, str] = field(default_factory=dict)


def is_catalyst(source: str | None) -> bool:
    s = (source or "").lower()
    return bool(s) and not s.startswith(NON_CATALYST)


def _tokens(title: str) -> set[str]:
    return {t.lower() for t in _TOKEN.findall(title or "") if t.lower() not in _STOP}


def cluster_themes(inp: ThemeInputs, *, min_titles: int = 3) -> dict[str, set[str]]:
    """테마 → 종목 집합. 섹터 테마는 신호에 한 번이라도 나온 종목을 sectors.yaml 대분류로 묶고,
    키워드 테마는 오늘 새 뉴스 제목 min_titles 건 이상에 함께 나온 키워드의 태그 종목."""
    seen: set[str] = set()
    for n in inp.news_today:
        seen.update(n.get("tickers") or [])
    for rows in inp.ranks.values():
        seen.update(r.ticker for r in rows)
    seen.update(inp.st_today)
    out: dict[str, set[str]] = {}
    if inp.sector_of is not None:
        for t in seen:
            sec = inp.sector_of(t)
            if sec and sec != "기타":
                out.setdefault(f"섹터:{sec}", set()).add(t)
    count: dict[str, int] = {}
    tickers_of: dict[str, set[str]] = {}
    for n in inp.news_today:
        for tok in _tokens(n.get("title") or ""):
            count[tok] = count.get(tok, 0) + 1
            tickers_of.setdefault(tok, set()).update(n.get("tickers") or [])
    for tok, c in count.items():
        if c >= min_titles and tickers_of.get(tok):
            out[f"kw:{tok}"] = set(tickers_of[tok])
    return out


def _norm(vals: dict[str, float]) -> dict[str, float]:
    top = max(vals.values(), default=0.0)
    return {k: (v / top if top > 0 else 0.0) for k, v in vals.items()}


def score_themes(
    inp: ThemeInputs, clusters: dict[str, set[str]], weights: dict[str, float]
) -> list[dict]:
    """테마별 성분(0~1)·점수·순위·대표 종목·촉매. 점수 0 인 테마는 뺀다. 점수 내림차순."""
    raw: dict[str, dict[str, float]] = {c: {} for c in COMPONENTS}
    why: dict[str, dict[str, list[str]]] = {}
    rank_w: dict[str, float] = {}
    for key in ("volume", "fluct"):
        rows = inp.ranks.get(key) or []
        n = len(rows)
        for r in rows:
            rank_w[r.ticker] = rank_w.get(r.ticker, 0.0) + (1 - (r.rank - 1) / max(n, 1))
    flow_pos = {r.ticker for r in inp.ranks.get("flow") or [] if (r.net_buy or 0) > 0}
    for theme, tickers in clusters.items():
        w = why.setdefault(theme, {t: [] for t in tickers})
        today = [n for n in inp.news_today if set(n.get("tickers") or []) & tickers]
        prev = [n for n in inp.news_prev if set(n.get("tickers") or []) & tickers]
        raw["news"][theme] = max(0, len(today) - len(prev))
        for n in today:
            for t in set(n.get("tickers") or []) & tickers:
                w[t].append("새 뉴스")
        raw["rank"][theme] = sum(rank_w.get(t, 0.0) for t in tickers)
        raw["flow"][theme] = float(len(tickers & flow_pos))
        up = 0
        for t in tickers:
            if t in rank_w:
                w[t].append("거래량·등락 순위")
            if t in flow_pos:
                w[t].append("외인·기관 순매수")
            r_now, r_prev = inp.st_today.get(t), inp.st_prev.get(t)
            if r_now is not None and (r_prev is None or r_now < r_prev):
                up += 1
                w[t].append("세이브티커 순위 상승")
        raw["saveticker"][theme] = float(up)
        zs = [max(0.0, inp.vol_z[t]) for t in tickers if t in inp.vol_z]
        raw["vol"][theme] = sum(zs) / len(zs) if zs else 0.0
        for t in tickers:
            if inp.vol_z.get(t, 0) >= 2:
                w[t].append(f"거래량 z {inp.vol_z[t]:.1f}")
    normed = {c: _norm(raw[c]) for c in COMPONENTS}
    out = []
    for theme, tickers in clusters.items():
        comps = {c: round(normed[c].get(theme, 0.0), 4) for c in COMPONENTS}
        score = sum(weights.get(c, 0.0) * comps[c] for c in COMPONENTS)
        if score <= 0:
            continue
        cat = [
            n
            for n in inp.news_today
            if set(n.get("tickers") or []) & tickers and is_catalyst(n.get("source"))
        ]
        ranked = sorted(tickers, key=lambda t: (-len(why[theme][t]), t))
        out.append(
            {
                "theme": theme,
                "score": round(score, 4),
                "components": comps,
                "tickers": [
                    {
                        "ticker": t,
                        "name": inp.names.get(t),
                        "why": sorted(set(why[theme][t])),
                    }
                    for t in ranked[:5]
                ],
                "members": sorted(tickers),
                "catalyst_ok": bool(cat),
                "catalyst_ids": [int(n["id"]) for n in cat[:5] if n.get("id") is not None],
            }
        )
    out.sort(key=lambda x: (-x["score"], x["theme"]))
    for i, x in enumerate(out, start=1):
        x["rank"] = i
    return out


# --- 수집 (원장·KIS·세이브티커·피처) ------------------------------------------------------


def _novel_news(conn: sqlite3.Connection, market: str, d: date) -> list[dict]:
    lo, hi = repo.day_bounds(d, market)
    rows = conn.execute(
        "SELECT id, title, tickers_json, source FROM news_items WHERE market=? AND novel=1 "
        "AND COALESCE(published_at, received_at) >= ? AND COALESCE(published_at, received_at) < ?",
        (market, lo, hi),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "title": r["title"],
            "tickers": json.loads(r["tickers_json"] or "[]"),
            "source": r["source"],
        }
        for r in rows
    ]


def saveticker_ranks(root: Path | None, d: date) -> dict[str, int]:
    """세이브티커 일별 화면(`data/<d>/community.jsonl`·`market.jsonl`)의 종목별 그날 최고 순위.
    파일이 없으면 빈 dict. 종목 키는 item.tickers(없으면 건너뜀)."""
    out: dict[str, int] = {}
    if root is None:
        return out
    for name in ("community.jsonl", "market.jsonl"):
        p = root / "data" / d.isoformat() / name
        if not p.is_file():
            continue
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except OSError as e:
            logger.warning("세이브티커 일별 파일 읽기 실패 %s: %s", p, e)
            continue
        for ln in lines:
            try:
                item = (json.loads(ln) or {}).get("item") or {}
                rank = int(item.get("rank"))
            except (ValueError, TypeError, AttributeError):
                continue
            for t in item.get("tickers") or []:
                t = str(t).strip().upper()
                if t and (t not in out or rank < out[t]):
                    out[t] = rank
    return out


def _ranks(market_client: Any, market: str) -> dict[str, list]:
    out: dict[str, list] = {}
    if market != "KR" or market_client is None:
        return out
    for key, fn in (
        ("volume", "volume_rank_kr"),
        ("fluct", "fluctuation_rank_kr"),
        ("flow", "foreign_institution_kr"),
    ):
        try:
            out[key] = list(getattr(market_client, fn)(limit=30))
        except Exception as e:  # noqa: BLE001 - 순위는 보조 신호: 실패해도 나머지로 점수
            logger.warning("순위 조회 실패 %s: %s", fn, e)
            out[key] = []
    return out


def compute(
    conn: sqlite3.Connection,
    settings: Any,
    *,
    market: str,
    today: date,
    now: datetime,
    market_client: Any = None,
    saveticker_root: Path | None = None,
    state: Path | None = None,
) -> dict:
    """오늘 테마 열기 결과 `{market, date, observed_at, themes:[…]}`."""
    from trader.config import load_sectors

    cfg = settings.theme
    try:
        smap = load_sectors()
        sector_of = lambda t: smap.sector_of(t, market)  # noqa: E731
    except Exception:  # noqa: BLE001 - 섹터 맵 없으면 키워드 테마만
        sector_of = None
    news_today = _novel_news(conn, market, today)
    news_prev = _novel_news(conn, market, today - timedelta(days=1))
    ranks = _ranks(market_client, market)
    st_today = saveticker_ranks(saveticker_root, today)
    st_prev = saveticker_ranks(saveticker_root, today - timedelta(days=1))
    names = {t: n for (m, t), n in repo.ticker_names(conn, market=market).items()}
    for rows in ranks.values():
        for r in rows:
            if r.name and r.ticker not in names:
                names[r.ticker] = r.name
    cand = set()
    for n in news_today:
        cand.update(n.get("tickers") or [])
    for rows in ranks.values():
        cand.update(r.ticker for r in rows)
    vol_z: dict[str, float] = {}
    if cand:
        try:
            from trader.ml import features

            df = features.latest_rows(state, market, tickers=sorted(cand), asof=today)
            if "vol_z_20" in getattr(df, "columns", []):
                for t, v in zip(df["ticker"], df["vol_z_20"], strict=False):
                    try:
                        if v == v and v is not None:  # NaN 제외
                            vol_z[str(t)] = float(v)
                    except (TypeError, ValueError):
                        continue
        except Exception as e:  # noqa: BLE001 - 피처가 없으면 vol 성분 0
            logger.debug("vol_z 읽기 실패: %s", e)
    inp = ThemeInputs(
        market=market,
        news_today=news_today,
        news_prev=news_prev,
        ranks=ranks,
        st_today=st_today,
        st_prev=st_prev,
        vol_z=vol_z,
        sector_of=sector_of,
        names=names,
    )
    themes = score_themes(
        inp, cluster_themes(inp, min_titles=cfg.min_cluster_titles), cfg.weights.model_dump()
    )
    return {
        "market": market,
        "date": today.isoformat(),
        "observed_at": now.isoformat(),
        "themes": themes[: max(cfg.top_n * 3, 10)],
        "signals": {
            "news_today": len(news_today),
            "news_prev": len(news_prev),
            "ranks": {k: len(v) for k, v in ranks.items()},
            "saveticker": len(st_today),
            "vol_z": len(vol_z),
        },
    }


def top(heat: dict | None, n: int) -> list[dict]:
    return list((heat or {}).get("themes") or [])[:n]


def theme_of_map(conn: sqlite3.Connection, market: str, d: date, top_n: int) -> dict[str, str]:
    """오늘 상위 테마의 종목 → 테마 (게이트의 테마 상한·테마 종목 조건 용).
    오늘 결과가 없으면 {}."""
    from trader.control import control

    heat = control.get_flag(conn, f"{FLAG}:{market}")
    if not heat or heat.get("date") != d.isoformat():
        return {}
    out: dict[str, str] = {}
    for th in top(heat, top_n):
        for t in th.get("members") or [x["ticker"] for x in th.get("tickers") or []]:
            out.setdefault(t, th["theme"])
    return out
