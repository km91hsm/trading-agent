"""거시 지표 수집기: FRED, ECOS(한국은행), KIS 지수. 항목별 Section 으로 돌려준다.

키는 FRED 는 params, ECOS 는 URL 경로(API 설계상 불가피)로 전달하되 로그·예외에는 넣지 않는다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from trader.config import MacroCfg, Secrets
from trader.logging_setup import quiet_httpx
from trader.sources.base import Section, owned_client, run_section, skipped_section

logger = logging.getLogger("trader.sources")

FRED_URL = "https://api.stlouisfed.org/fred/series/observations"
ECOS_URL = "https://ecos.bok.or.kr/api/StatisticSearch"

# 항목 → (출처, 식별자). FRED 시리즈는 공식 ID, ECOS 는 (통계표, 항목) 코드.
FRED_SERIES: dict[str, str] = {
    "sp500": "SP500",
    "usdkrw": "DEXKOUS",
    "us10y": "DGS10",
    "vix": "VIXCLS",
    "base_rate_us": "DFF",
}
ECOS_SERIES: dict[str, tuple[str, str]] = {
    "base_rate_kr": ("722Y001", "0101000"),  # 한국은행 기준금리 (일별)
}


def _utc_now() -> datetime:
    return datetime.now(UTC)


class Fred:
    def __init__(self, api_key: str, http: httpx.Client) -> None:
        quiet_httpx()
        self._key = api_key
        self._http = http

    def latest(self, series_id: str) -> dict[str, Any]:
        """최신 유효 관측값 {value: float, date: YYYY-MM-DD}. 결측(".") 은 건너뛴다."""
        resp = self._http.get(
            FRED_URL,
            params={
                "series_id": series_id,
                "api_key": self._key,
                "file_type": "json",
                "sort_order": "desc",
                "limit": "10",
            },
            timeout=10,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"FRED HTTP {resp.status_code} ({series_id})")
        for obs in resp.json().get("observations") or []:
            raw = str(obs.get("value", ".")).strip()
            if raw and raw != ".":
                return {"value": float(raw), "date": str(obs.get("date"))}
        raise RuntimeError(f"FRED 관측값 없음 ({series_id})")


class Ecos:
    def __init__(
        self, api_key: str, http: httpx.Client, *, now: Callable[[], datetime] = _utc_now
    ) -> None:
        quiet_httpx()
        self._key = api_key
        self._http = http
        self._now = now

    def latest(self, stat_code: str, item_code: str, *, cycle: str = "D") -> dict[str, Any]:
        end = self._now().astimezone(UTC).date()
        start = end - timedelta(days=45)
        url = (
            f"{ECOS_URL}/{self._key}/json/kr/1/100/{stat_code}/{cycle}/"
            f"{start.strftime('%Y%m%d')}/{end.strftime('%Y%m%d')}/{item_code}"
        )
        resp = self._http.get(url, timeout=10)
        if resp.status_code != 200:
            raise RuntimeError(f"ECOS HTTP {resp.status_code} ({stat_code})")
        data = resp.json()
        if "RESULT" in data:
            r = data["RESULT"]
            raise RuntimeError(f"ECOS {r.get('CODE')}: {r.get('MESSAGE')} ({stat_code})")
        rows = (data.get("StatisticSearch") or {}).get("row") or []
        rows = [r for r in rows if str(r.get("DATA_VALUE", "")).strip()]
        if not rows:
            raise RuntimeError(f"ECOS 데이터 없음 ({stat_code})")
        last = max(rows, key=lambda r: str(r.get("TIME", "")))
        t = str(last["TIME"])
        date_s = f"{t[:4]}-{t[4:6]}-{t[6:8]}" if len(t) == 8 else t
        return {"value": float(last["DATA_VALUE"]), "date": date_s}


def collect_macro(
    cfg: MacroCfg,
    secrets: Secrets,
    *,
    market_client: Any | None,
    asof: datetime,
    http: httpx.Client | None = None,
    now: Callable[[], datetime] = _utc_now,
) -> dict[str, Section]:
    """항목별 Section. 키 없는 소스는 error "no api key", 꺼진 소스는 "disabled"."""
    with owned_client(http, timeout=10) as http:
        out: dict[str, Section] = {}

        if market_client is None:
            out["kospi"] = skipped_section("no market client", now=now)
        else:
            out["kospi"] = run_section(lambda: _index_payload(market_client), now=now)

        fred = Fred(secrets.FRED_API_KEY, http) if secrets.FRED_API_KEY else None
        for key, series in FRED_SERIES.items():
            if not cfg.fred:
                out[key] = skipped_section("disabled", now=now)
            elif fred is None:
                out[key] = skipped_section("no api key", now=now)
            else:
                out[key] = run_section(lambda s=series: fred.latest(s), now=now)

        ecos = Ecos(secrets.ECOS_API_KEY, http, now=now) if secrets.ECOS_API_KEY else None
        for key, (stat, item) in ECOS_SERIES.items():
            if not cfg.ecos:
                out[key] = skipped_section("disabled", now=now)
            elif ecos is None:
                out[key] = skipped_section("no api key", now=now)
            else:
                out[key] = run_section(lambda s=stat, i=item: ecos.latest(s, i), now=now)
        return out


def _index_payload(market_client: Any) -> dict[str, Any]:
    q = market_client.get_index_kr("0001")
    return {"value": float(q.price), "change_pct": q.change_pct, "asof": q.asof.isoformat()}
