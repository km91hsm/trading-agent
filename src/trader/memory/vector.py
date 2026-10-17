"""상황 벡터 12차원 (스펙 §6.5). 피처 스토어 행의 수치만 쓰며 NaN 은 0, 각 차원은 대략 [-1, 1]."""

from __future__ import annotations

import math

DIMS = (
    "roc_10",  # 10일 수익률(%) / 10 (캡 ±1)
    "rsi_14",  # (RSI − 50) / 50
    "vol_z_20",  # 거래량 z / 3 (캡 ±1)
    "high_52w_pos",  # 0~1
    "atr_pct",  # ATR / close
    "macd_hist_rel",  # MACD 히스토그램 / close
    "sma_5_rel",  # close / sma_5 − 1
    "sma_20_rel",  # close / sma_20 − 1
    "flow_foreign_sign",  # 외국인 20일 순매수 부호 (−1/0/1)
    "news_log",  # log1p(뉴스 수) / 5 (캡 1)
    "community_log",  # log1p(커뮤니티 수) / 5 (캡 1)
    "is_us",  # 시장 (0 KR / 1 US)
)


def _f(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def situation_vector(row: dict, market: str) -> list[float]:
    r = row or {}
    close = _f(r.get("close"))
    close = close if close and close > 0 else None

    def rel(key: str, scale: float = 1.0) -> float:
        v = _f(r.get(key))
        if v is None:
            return 0.0
        return _clip(v / scale)

    def ratio(key: str) -> float:
        v = _f(r.get(key))
        if v is None or close is None:
            return 0.0
        return _clip(v / close)

    def rel_to(key: str) -> float:
        """close / 이평 − 1 (지표 열 `sma_*_rel` 과 같은 부호: 이평 위면 양수)."""
        v = _f(r.get(key))
        if v is None or close is None or v <= 0:
            return 0.0
        return _clip(close / v - 1)

    rsi = _f(r.get("rsi_14"))
    flow = _f(r.get("flow_foreign_20d"))
    news = _f(r.get("news_count"))
    comm = _f(r.get("community_count"))
    return [
        rel("roc_10", 10.0),
        _clip((rsi - 50) / 50) if rsi is not None else 0.0,
        rel("vol_z_20", 3.0),
        _clip(_f(r.get("high_52w_pos")) or 0.0, 0.0, 1.0),
        _clip(ratio("atr_14"), 0.0, 1.0),
        ratio("macd_hist"),
        rel_to("sma_5"),
        rel_to("sma_20"),
        0.0 if not flow else (1.0 if flow > 0 else -1.0),
        _clip(math.log1p(max(news, 0.0)) / 5, 0.0, 1.0) if news is not None else 0.0,
        _clip(math.log1p(max(comm, 0.0)) / 5, 0.0, 1.0) if comm is not None else 0.0,
        1.0 if market == "US" else 0.0,
    ]


def cosine(a: list[float], b: list[float]) -> float:
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return sum(x * y for x, y in zip(a, b, strict=True)) / (na * nb)


def summary(row: dict, market: str) -> str:
    """한 줄 상황 요약 (사람·LLM 용)."""
    r = row or {}
    parts = [f"{market}"]
    rsi = _f(r.get("rsi_14"))
    if rsi is not None:
        parts.append(f"RSI {rsi:.0f}")
    roc = _f(r.get("roc_10"))
    if roc is not None:
        parts.append(f"10일 {roc:+.1f}%")
    pos = _f(r.get("high_52w_pos"))
    if pos is not None:
        parts.append(f"52주 위치 {pos:.2f}")
    flow = _f(r.get("flow_foreign_20d"))
    if flow is not None:
        parts.append("외인 순매수" if flow > 0 else "외인 순매도" if flow < 0 else "외인 중립")
    news = _f(r.get("news_count"))
    if news is not None:
        parts.append(f"뉴스 {int(news)}건")
    return " · ".join(parts)
