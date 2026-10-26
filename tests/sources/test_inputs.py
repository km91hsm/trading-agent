import json
from datetime import timedelta

import httpx
import pytest
from helpers_sources import T0, FakeDart, FakeMarket

from trader.config import ConfigError, Secrets, SourcesConfig
from trader.sources import inputs
from trader.sources.inputs import InputDeps, build_inputs, collect_and_save


def cfg(**community) -> SourcesConfig:
    return SourcesConfig.model_validate(
        {
            "edgar": {"user_agent": "trader test@example.com"},
            "news": {"enabled": False},
            "macro": {"enabled": False},
            "community": {"enabled": False, **community},
        }
    )


def deps(**kw) -> InputDeps:
    base = {
        "market": FakeMarket(),
        "dart": FakeDart(),
        "edgar": None,
        "http": httpx.Client(),
        "now": lambda: T0,
    }
    base.update(kw)
    return InputDeps(**base)


def test_build_inputs_sections(project):
    d = build_inputs(
        ticker="005930",
        market="KR",
        exchange=None,
        asof=T0,
        cfg=cfg(),
        secrets=Secrets(),
        deps=deps(),
    )
    assert set(d) == {"meta", "technical", "fundamental", "news_macro", "sentiment", "errors"}
    assert d["meta"]["ticker"] == "005930" and d["meta"]["name"] == "삼성전자"
    assert d["meta"]["schema_version"] == inputs.SCHEMA_VERSION
    assert set(d["technical"]) == {"quote", "daily", "minute", "orderbook", "indicators"}
    assert d["technical"]["quote"]["data"]["price"] == "71500"  # Decimal → str
    assert len(d["technical"]["daily"]["data"]) == cfg().kis.daily_days == 260
    assert isinstance(d["technical"]["indicators"]["data"]["sma_5"], float)
    assert d["fundamental"]["ratios"]["data"]["ROE"] == pytest.approx(0.1)
    assert d["fundamental"]["disclosures"]["data"][0]["title"] == "공시"
    assert d["sentiment"]["investor_flow"]["data"][-1]["foreign_net"] == 100
    assert d["sentiment"]["short_interest"]["error"] == "phase6"
    assert d["news_macro"]["news"]["error"] == "disabled"
    assert d["errors"] == []


def test_inputs_fundamental_history_section(project):
    """스펙 §3.1: DART 3개년 + 최근 분기 재무가 fundamental.history 에 들어간다 (v2)."""
    d = build_inputs(
        ticker="005930",
        market="KR",
        exchange=None,
        asof=T0,
        cfg=cfg(),
        secrets=Secrets(),
        deps=deps(),
    )
    assert inputs.SCHEMA_VERSION == 3 and d["meta"]["schema_version"] == 3
    h = d["fundamental"]["history"]["data"]
    assert set(h["years"]) == {"2023", "2024", "2025"}  # JSON 키는 문자열
    assert h["years"]["2025"]["매출액"] == "330"  # Decimal → str
    assert h["latest_quarter"]["reprt_code"] == "11012"
    us = build_inputs(
        ticker="AAPL",
        market="US",
        exchange="NAS",
        asof=T0,
        cfg=cfg(),
        secrets=Secrets(),
        deps=deps(),
    )
    assert us["fundamental"]["history"]["error"] == "no edgar client"


def test_failed_section_recorded_and_file_written(project):
    p = collect_and_save(
        ticker="005930",
        market="KR",
        exchange=None,
        asof=T0,
        cfg=cfg(),
        secrets=Secrets(),
        deps=deps(market=FakeMarket(fail={"get_minute_kr"})),
    )
    d = json.loads(p.read_text(encoding="utf-8"))
    assert d["technical"]["minute"]["data"] is None
    assert d["errors"] == ["technical.minute: KisApiError: FAKE: 가짜 실패: get_minute_kr"]
    assert d["technical"]["daily"]["data"] is not None
    assert p == project / "state" / "runs" / "2026-10-09" / "KR" / "005930" / "00_inputs.json"


def test_daily_failure_skips_indicators(project):
    d = build_inputs(
        ticker="005930",
        market="KR",
        exchange=None,
        asof=T0,
        cfg=cfg(),
        secrets=Secrets(),
        deps=deps(market=FakeMarket(fail={"get_daily_kr"})),
    )
    assert d["technical"]["indicators"]["error"] == "no daily candles"
    assert "technical.daily" in d["errors"][0]


def test_zero_price_is_error_section(project):
    m = FakeMarket()
    m._quote = lambda t, mk: m.__class__._quote(m, t, mk).model_copy(update={"price": 0})
    d = build_inputs(
        ticker="005930",
        market="KR",
        exchange=None,
        asof=T0,
        cfg=cfg(),
        secrets=Secrets(),
        deps=deps(market=m),
    )
    assert d["technical"]["quote"]["data"] is None and "가격" in d["technical"]["quote"]["error"]


def test_collect_skips_existing(project):
    m = FakeMarket()
    for _ in range(2):
        collect_and_save(
            ticker="005930",
            market="KR",
            exchange=None,
            asof=T0,
            cfg=cfg(),
            secrets=Secrets(),
            deps=deps(market=m),
        )
    assert m.calls.count("get_daily_kr") == 1


def test_force_recollects(project):
    m = FakeMarket()
    for force in (False, True):
        collect_and_save(
            ticker="005930",
            market="KR",
            exchange=None,
            asof=T0,
            cfg=cfg(),
            secrets=Secrets(),
            deps=deps(market=m),
            force=force,
        )
    assert m.calls.count("get_daily_kr") == 2


def test_past_asof_rejected(project):
    with pytest.raises(ConfigError, match="과거"):
        collect_and_save(
            ticker="005930",
            market="KR",
            exchange=None,
            asof=T0 - timedelta(hours=25),
            cfg=cfg(),
            secrets=Secrets(),
            deps=deps(),
        )


def test_us_requires_exchange_and_skips_kr_only_sections(project):
    with pytest.raises(ConfigError, match="exchange"):
        build_inputs(
            ticker="AAPL",
            market="US",
            exchange=None,
            asof=T0,
            cfg=cfg(),
            secrets=Secrets(),
            deps=deps(),
        )
    d = build_inputs(
        ticker="AAPL",
        market="US",
        exchange="NAS",
        asof=T0,
        cfg=cfg(),
        secrets=Secrets(),
        deps=deps(),
    )
    assert d["meta"]["name"] == "AAPL"
    assert d["sentiment"]["investor_flow"]["error"] == "not available for US"
    assert d["fundamental"]["ratios"]["error"] == "no edgar client"
    assert d["errors"] == [
        "fundamental.ratios: no edgar client",
        "fundamental.history: no edgar client",
    ]


def test_dart_corp_lookup_failure_is_section_error(project):
    """DART 고유번호 조회 자체가 실패해도 00_inputs 는 만들어지고 절 오류로 남는다 (실기 확인)."""

    class BrokenDart(FakeDart):
        def corp_code(self, ticker):
            raise RuntimeError("DART status 020")

        def corp_name(self, ticker):
            raise RuntimeError("DART status 020")

    d = build_inputs(
        ticker="005930",
        market="KR",
        exchange=None,
        asof=T0,
        cfg=cfg(),
        secrets=Secrets(),
        deps=deps(dart=BrokenDart()),
    )
    assert d["meta"]["name"] == "005930"
    assert (
        d["fundamental"]["ratios"]["data"] is None and "020" in d["fundamental"]["ratios"]["error"]
    )
    assert d["fundamental"]["disclosures"]["data"] is None
    assert any(e.startswith("fundamental.ratios") for e in d["errors"])


def test_sub_source_failures_are_collected(project, monkeypatch):
    """뉴스·커뮤니티 절 안의 소스 단위 실패(네이버 401, Reddit 429)도 errors 에 보인다 (리뷰 I1)."""
    monkeypatch.setattr(
        inputs,
        "collect_news",
        lambda *a, **k: {
            "stock": [],
            "macro": [],
            "sources": {"google_rss": "ok", "naver": "HTTPStatusError: 401"},
        },
    )
    monkeypatch.setattr(
        inputs,
        "collect_community",
        lambda *a, **k: {
            "posts": [],
            "sources": {"reddit": "not available for KR", "clien": "ConnectError: x"},
        },
    )
    c = SourcesConfig.model_validate(
        {"edgar": {"user_agent": "x y@z"}, "macro": {"enabled": False}}
    )
    d = build_inputs(
        ticker="005930", market="KR", exchange=None, asof=T0, cfg=c, secrets=Secrets(), deps=deps()
    )
    assert "news_macro.news.naver: HTTPStatusError: 401" in d["errors"]
    assert "sentiment.community.clien: ConnectError: x" in d["errors"]
    assert not any("reddit" in e or "google_rss" in e for e in d["errors"])


def test_macro_item_failures_are_collected(project, monkeypatch):
    from trader.sources.base import run_section, skipped_section

    def fake_macro(*a, **k):
        return {
            "kospi": run_section(
                lambda: (_ for _ in ()).throw(RuntimeError("KIS down")), now=lambda: T0
            ),
            "vix": skipped_section("no api key", now=lambda: T0),
        }

    monkeypatch.setattr(inputs, "collect_macro", fake_macro)
    c = SourcesConfig.model_validate({"edgar": {"user_agent": "x y@z"}, "news": {"enabled": False}})
    d = build_inputs(
        ticker="005930", market="KR", exchange=None, asof=T0, cfg=c, secrets=Secrets(), deps=deps()
    )
    assert "news_macro.macro.kospi: RuntimeError: KIS down" in d["errors"]
    assert "news_macro.macro.vix: no api key" in d["errors"]


def test_name_fallback_to_market_and_edgar(project):
    """DART 가 죽어도 KIS 에서 종목명을, US 는 EDGAR 티커 맵에서 회사명을 얻는다 (리뷰 I5)."""

    class BrokenDart(FakeDart):
        def corp_name(self, ticker):
            raise RuntimeError("DART status 800")

    class NamedMarket(FakeMarket):
        def get_name_kr(self, ticker):
            self._rec("get_name_kr")
            return "삼성전자"

    class FakeEdgar:
        def company_name(self, symbol):
            return "Apple Inc."

        def cik(self, symbol):
            return None

    d = build_inputs(
        ticker="005930",
        market="KR",
        exchange=None,
        asof=T0,
        cfg=cfg(),
        secrets=Secrets(),
        deps=deps(dart=BrokenDart(), market=NamedMarket()),
    )
    assert d["meta"]["name"] == "삼성전자"
    d = build_inputs(
        ticker="AAPL",
        market="US",
        exchange="NAS",
        asof=T0,
        cfg=cfg(),
        secrets=Secrets(),
        deps=deps(edgar=FakeEdgar()),
    )
    assert d["meta"]["name"] == "Apple Inc."


def test_no_market_client(project):
    d = build_inputs(
        ticker="005930",
        market="KR",
        exchange=None,
        asof=T0,
        cfg=cfg(),
        secrets=Secrets(),
        deps=deps(market=None),
    )
    assert d["technical"]["quote"]["error"] == "no market client"
    assert all(e.startswith("technical.") or e.startswith("sentiment.") for e in d["errors"])


def test_phase6_placeholders_not_in_errors(project):
    d = build_inputs(
        ticker="005930",
        market="KR",
        exchange=None,
        asof=T0,
        cfg=cfg(),
        secrets=Secrets(),
        deps=deps(),
    )
    expected = {"short_interest": "phase6", "trends": "phase8", "manual_inputs": "no manual input"}
    for key, reason in expected.items():
        assert d["sentiment"][key]["error"] == reason
    assert not any("phase" in e or "manual" in e for e in d["errors"])


# --- Phase 6 보강 ------------------------------------------------------------------


def test_future_asof_rejected(project):
    with pytest.raises(ConfigError, match="미래"):
        collect_and_save(
            ticker="005930",
            market="KR",
            exchange=None,
            asof=T0 + timedelta(hours=25),
            cfg=cfg(),
            secrets=Secrets(),
            deps=deps(),
        )


def test_events_and_channel_sections_present(project):
    d = build_inputs(
        ticker="005930",
        market="KR",
        exchange=None,
        asof=T0,
        cfg=cfg(),
        secrets=Secrets(),
        deps=deps(),
    )
    ev = d["news_macro"]["events"]
    assert ev["error"] is None and isinstance(ev["data"]["recent"], list)
    assert ev["data"]["recent"][0]["title"] == "공시" and ev["data"]["upcoming"] == []
    assert d["sentiment"]["manual_inputs"]["error"] == "no manual input"
    assert d["errors"] == []


def test_events_upcoming_from_saveticker_calendar(project):
    """Phase 21: 세이브티커 폴더가 있으면 events.upcoming 에 7일 경제 일정이 들어간다."""
    import shutil
    from datetime import UTC, datetime

    from helpers_sources import FIXTURES

    dst = project / "state" / "external" / "saveticker"
    shutil.copytree(FIXTURES / "saveticker", dst)
    asof = datetime(2026, 10, 30, 3, 0, tzinfo=UTC)  # +7일 창에 11/2 일정이 든다
    d = build_inputs(
        ticker="005930",
        market="KR",
        exchange=None,
        asof=asof,
        cfg=cfg(),
        secrets=Secrets(),
        deps=deps(now=lambda: asof),
    )
    up = d["news_macro"]["events"]["data"]["upcoming"]
    assert up and up[0]["kind"] == "macro" and up[0]["source"] == "saveticker"
    assert "세이브티커" in d["news_macro"]["events"]["data"]["note"]


def test_manual_inputs_section_reads_file(project):
    md = project / "state" / "manual_inputs"
    md.mkdir()
    (md / "005930.md").write_text("관찰 메모", encoding="utf-8")
    d = build_inputs(
        ticker="005930",
        market="KR",
        exchange=None,
        asof=T0,
        cfg=cfg(),
        secrets=Secrets(),
        deps=deps(),
    )
    assert d["sentiment"]["manual_inputs"]["data"]["text"] == "관찰 메모"


def test_channels_collected_into_community_sources(project, monkeypatch):
    import httpx as _httpx

    c = SourcesConfig.model_validate(
        {
            "edgar": {"user_agent": "trader test@example.com"},
            "news": {"enabled": False},
            "macro": {"enabled": False},
            "community": {"enabled": True, "reddit": False, "clien": False},
            "channels": {"youtube": ["UCabc"], "telegram": ["chan"]},
        }
    )
    from trader.sources import community as community_mod

    monkeypatch.setattr(
        community_mod,
        "collect_channels",
        lambda channels, **kw: {
            "sources": {
                "youtube/UCabc": [{"title": "t", "source": "youtube/UCabc", "posted_at": None}],
                "telegram/chan": [],
            },
            "errors": {"telegram/chan": "HTTP 500"},
        },
    )
    d = build_inputs(
        ticker="005930",
        market="KR",
        exchange=None,
        asof=T0,
        cfg=c,
        secrets=Secrets(),
        deps=deps(http=_httpx.Client()),
    )
    comm = d["sentiment"]["community"]["data"]
    assert (
        comm["sources"]["youtube/UCabc"] == "ok" and comm["sources"]["telegram/chan"] == "HTTP 500"
    )
    assert any(p["source"] == "youtube/UCabc" for p in comm["posts"])
    assert "sentiment.community.telegram/chan: HTTP 500" in d["errors"]


def test_events_use_dart_rcept_dt_and_seven_day_window():
    from trader.sources import inputs as inputs_mod

    fundamental = {
        "disclosures": {
            "data": [
                {"title": "실적 공시", "rcept_dt": "2026-10-07", "url": "u1"},
                {"title": "오래된 공시", "rcept_dt": "2026-09-20", "url": "u2"},
            ],
            "received_at": T0.isoformat(),
            "error": None,
        }
    }
    ev = inputs_mod._events(fundamental, T0, lambda: T0)
    recent = ev["data"]["recent"]
    assert [r["title"] for r in recent] == ["실적 공시"]
    assert recent[0]["date"] == "2026-10-07" and recent[0]["kind"] == "earnings"


def test_technical_sections_respect_asof(project):
    """과거 asof 재수집(24시간 안)에서 asof 뒤 날짜의 일봉·분봉·수급이 들어가면 룩어헤드다."""
    from datetime import date

    from helpers_sources import make_candles

    m = FakeMarket()
    # 10-07(수) 10-08 10-09 10-12 10-13 — asof 는 10-09 15:00 KST 이전인 10-08 15:00 KST
    m._candles = make_candles(5, start=date(2026, 10, 7))
    asof = T0 - timedelta(days=1)
    d = build_inputs(
        ticker="005930",
        market="KR",
        exchange=None,
        asof=asof,
        cfg=cfg(),
        secrets=Secrets(),
        deps=deps(market=m),
    )
    daily = d["technical"]["daily"]["data"]
    assert daily and daily[-1]["date"] == "2026-10-08" and len(daily) == 2
    assert d["meta"]["last_candle_date"] == "2026-10-08"
    assert all(r["date"] <= "2026-10-08" for r in d["sentiment"]["investor_flow"]["data"])


def test_build_inputs_runs_sections_concurrently(project):
    """fundamental·news_macro·sentiment 는 서로 독립이라 동시에 돈다 (KR 수집 1회 HTTP 27~31회)."""
    import threading
    import time

    from trader.sources import inputs as inputs_mod

    active = {"n": 0, "max": 0}
    lock = threading.Lock()

    def slow(name):
        def wrapper(*a, **kw):
            with lock:
                active["n"] += 1
                active["max"] = max(active["max"], active["n"])
            time.sleep(0.05)
            with lock:
                active["n"] -= 1
            return {}

        return wrapper

    import pytest

    mp = pytest.MonkeyPatch()
    mp.setattr(inputs_mod, "_fundamental", slow("fundamental"))
    mp.setattr(inputs_mod, "_news_macro", slow("news"))
    mp.setattr(inputs_mod, "_sentiment", slow("sentiment"))
    try:
        build_inputs(
            ticker="005930",
            market="KR",
            exchange=None,
            asof=T0,
            cfg=cfg(),
            secrets=Secrets(),
            deps=deps(),
        )
    finally:
        mp.undo()
    assert active["max"] >= 2


def test_collect_and_save_uses_market_run_date_for_us(project):
    """US 는 KST 자정을 넘겨 장이 열리므로 00_inputs 디렉터리 날짜도 run_date_for(뉴욕 운영일)여야
    데몬 monitor·재평가(run_date_for)와 같은 state/runs/<date> 를 본다. KST 날짜면 01:00 KST
    재수집이 다음 날 디렉터리에 들어가고, 다음 날 analyze 가 그 수집본을 재사용한다."""
    from datetime import UTC, datetime

    asof = datetime(2026, 10, 9, 16, 0, tzinfo=UTC)  # 12:00 ET 10-09 = 01:00 KST 10-10
    p = collect_and_save(
        ticker="AAPL",
        market="US",
        exchange="NAS",
        asof=asof,
        cfg=cfg(),
        secrets=Secrets(),
        deps=deps(now=lambda: asof),
    )
    assert p.parent.parent.parent.name == "2026-10-09"


def _seed_peers(project, market: str, roes: dict[str, float]):
    """피처 스토어 + sectors/universe 설정을 임시 프로젝트에 둔다 (동종업 peer 절용)."""
    import pandas as pd
    import yaml

    from trader.config import SectorMap
    from trader.ml import features

    key = market.lower()
    sectors = SectorMap(sectors=["반도체", "기타"], **{key: dict.fromkeys(roes, "반도체")})
    (project / "config" / "sectors.yaml").write_text(
        yaml.safe_dump(sectors.model_dump(), allow_unicode=True), encoding="utf-8"
    )
    if market == "KR":
        uni = {"kr": list(roes)}
    else:
        uni = {"us": [{"symbol": t, "exchange": "NAS"} for t in roes]}
    (project / "config" / "universe.yaml").write_text(yaml.safe_dump(uni), encoding="utf-8")
    n = len(roes)
    features.write_rows(
        pd.DataFrame(
            {
                "date": [T0.date()] * n,
                "ticker": list(roes),
                "market": [market] * n,
                "close": [100.0] * n,
                "volume": [1000] * n,
                "roe": list(roes.values()),
                "op_margin": [0.1] * n,
                "debt_ratio": [1.0] * n,
            }
        ),
        sectors=sectors,
    )


def test_fundamental_peer_section_from_feature_store(project):
    """스펙 §3 '동종업 중앙값 대비': 피처 스토어 최신 행으로 같은 섹터 중앙값·내 값·차이."""
    _seed_peers(project, "US", {"AAPL": 0.3, "NVDA": 0.1, "AMD": 0.2})
    d = build_inputs(
        ticker="AAPL",
        market="US",
        exchange="NAS",
        asof=T0,
        cfg=cfg(),
        secrets=Secrets(),
        deps=deps(),
    )
    peer = d["fundamental"]["peer"]
    assert peer["error"] is None
    assert peer["data"]["sector"] == "반도체" and peer["data"]["n"] == 3
    assert peer["data"]["median"]["ROE"] == pytest.approx(0.2)
    assert peer["data"]["diff"]["ROE"] == pytest.approx(0.1)  # 내 ROE 0.3 − 중앙값 0.2
    assert peer["data"]["dates"] == {"min": T0.date().isoformat(), "max": T0.date().isoformat()}
    assert "fundamental.peer" not in " ".join(d["errors"])


def test_fundamental_peer_skipped_without_sector_or_peers(project):
    d = build_inputs(
        ticker="005930",
        market="KR",
        exchange=None,
        asof=T0,
        cfg=cfg(),
        secrets=Secrets(),
        deps=deps(),
    )
    assert d["fundamental"]["peer"]["error"] == "no sector peers"
    assert not any(e.startswith("fundamental.peer") for e in d["errors"])  # 건너뜀은 집계하지 않음
    _seed_peers(project, "KR", {"005930": 0.1, "000660": 0.2})  # 2종목 → 중앙값 없음
    d = build_inputs(
        ticker="005930",
        market="KR",
        exchange=None,
        asof=T0,
        cfg=cfg(),
        secrets=Secrets(),
        deps=deps(),
    )
    assert d["fundamental"]["peer"]["error"] == "no sector peers"


def test_fundamental_peer_uses_market_local_date(project):
    """룩어헤드 규칙은 시장 현지 날짜: asof 10-20 08:00 KST 는 뉴욕 10-19 라 10-20 행을 보면 안 된다
    (리뷰 #7)."""
    from datetime import UTC, datetime

    import pandas as pd

    from trader.config import SectorMap
    from trader.ml import features

    _seed_peers(project, "US", {"AAPL": 0.1, "NVDA": 0.2, "AMD": 0.3})  # T0 = 10-09 KST 행
    sectors = SectorMap(
        sectors=["반도체", "기타"], us=dict.fromkeys(["AAPL", "NVDA", "AMD"], "반도체")
    )
    future = pd.DataFrame(
        {
            "date": [T0.date().replace(day=10)] * 3,
            "ticker": ["AAPL", "NVDA", "AMD"],
            "market": ["US"] * 3,
            "close": [100.0] * 3,
            "volume": [1000] * 3,
            "roe": [0.9, 0.9, 0.9],
            "op_margin": [0.1] * 3,
            "debt_ratio": [1.0] * 3,
        }
    )
    features.write_rows(future, sectors=sectors)
    asof = datetime(2026, 10, 9, 23, 0, tzinfo=UTC)  # 10-10 08:00 KST, NY 10-09 19:00 → 현지 10-09
    d = build_inputs(
        ticker="AAPL",
        market="US",
        exchange="NAS",
        asof=asof,
        cfg=cfg(),
        secrets=Secrets(),
        deps=deps(),
    )
    assert d["fundamental"]["peer"]["data"]["median"]["ROE"] == pytest.approx(0.2)  # 10-09 행만


def test_us_history_section_from_edgar(project):
    """스펙 §3: US 도 EDGAR 로 3개년 + 최근 분기 history 를 받는다 (Phase 13)."""
    from helpers_sources import FakeEdgar

    d = build_inputs(
        ticker="AAPL",
        market="US",
        exchange="NAS",
        asof=T0,  # 2026-10-09
        cfg=cfg(),
        secrets=Secrets(),
        deps=deps(edgar=FakeEdgar()),
    )
    h = d["fundamental"]["history"]
    assert h["error"] is None
    assert {str(k) for k in h["data"]["years"]} == {"2023", "2024", "2025"}
    assert h["data"]["latest_quarter"]["form"] == "10-Q"
    assert h["data"]["latest_quarter"]["end"] == "2026-03-31"
    assert d["fundamental"]["ratios"]["error"] is None  # 같은 companyfacts 로 비율도 채움


def test_us_fundamental_fetches_companyfacts_once_without_process_memo(project):
    """비율·history 가 companyfacts 를 한 번만 받되, 클라이언트에 프로세스 수명 메모를 두지 않는다
    (데몬은 EdgarClient 를 기동 때 한 번 만들어 재사용 → 새 10-Q 가 영영 반영되지 않음, 리뷰 #1)."""
    from helpers_sources import FakeEdgar

    e = FakeEdgar()
    kw = dict(ticker="AAPL", market="US", exchange="NAS", asof=T0, cfg=cfg(), secrets=Secrets())
    build_inputs(**kw, deps=deps(edgar=e))
    assert e.calls == 1
    kw = dict(ticker="AAPL", market="US", exchange="NAS", asof=T0, cfg=cfg(), secrets=Secrets())
    build_inputs(**kw, deps=deps(edgar=e))
    assert e.calls == 2  # 수집마다 새로 받는다
    assert not hasattr(e, "_facts")


def test_market_manual_notes_merge_into_manual_inputs(project):
    from datetime import timedelta

    from trader.sources import manual_inputs

    c = SourcesConfig.model_validate(
        {
            "edgar": {"user_agent": "trader test@example.com"},
            "news": {"enabled": False},
            "macro": {"enabled": False},
            "community": {"enabled": False},
        }
    )
    manual_inputs.append_market("KR", title="시장 메모", now=T0 - timedelta(hours=1))
    manual_inputs.append_market("KR", title="미래 메모", now=T0 + timedelta(hours=1))
    d = build_inputs(
        ticker="005930", market="KR", exchange=None, asof=T0, cfg=c, secrets=Secrets(), deps=deps()
    )
    mi = d["sentiment"]["manual_inputs"]
    assert mi["error"] is None and mi["data"]["format"] == "market"
    assert [n["title"] for n in mi["data"]["market_notes"]] == ["시장 메모"]


def test_macro_is_memoized_per_market_and_day(project, monkeypatch):
    """거시 지표는 종목과 무관하다 — 같은 시장·운영일의 수집은 15분 동안 한 번만 외부를 부른다."""
    from trader.sources.base import Section

    calls = []

    def fake_macro(cfg_, secrets, *, market_client, asof, http, now):
        calls.append(asof)
        return {"kospi": Section(data={"value": 1.0}, received_at=T0.isoformat(), error=None)}

    monkeypatch.setattr(inputs, "collect_macro", fake_macro)
    c = SourcesConfig.model_validate(
        {
            "edgar": {"user_agent": "t t@e.com"},
            "news": {"enabled": False},
            "macro": {"enabled": True},
            "community": {"enabled": False},
        }
    )
    for ticker in ("005930", "000660"):
        build_inputs(
            ticker=ticker,
            market="KR",
            exchange=None,
            asof=T0,
            cfg=c,
            secrets=Secrets(),
            deps=deps(),
        )
    assert len(calls) == 1
    later = deps(now=lambda: T0 + timedelta(minutes=16))
    build_inputs(
        ticker="005930", market="KR", exchange=None, asof=T0, cfg=c, secrets=Secrets(), deps=later
    )
    assert len(calls) == 2  # TTL 지남
    # 전부 실패한 결과는 메모하지 않는다
    monkeypatch.setattr(
        inputs,
        "collect_macro",
        lambda *a, **k: (
            calls.append("err")
            or {"kospi": Section(data=None, received_at=T0.isoformat(), error="boom")}
        ),
    )
    us = deps()
    for _ in range(2):
        build_inputs(
            ticker="AAPL", market="US", exchange="NAS", asof=T0, cfg=c, secrets=Secrets(), deps=us
        )
    assert calls.count("err") == 2


def test_stock_news_merges_store_items_tagged_for_ticker(project, conn):
    """저장소(news_items)에 이 종목으로 태그된 뉴스가 있으면 종목 뉴스에 합친다(URL 중복 제거)."""
    from trader.sources import news_store

    news_store.insert_items(
        conn,
        [
            {
                "title": "삼성전자 대형 수주",
                "url": "https://s/1",
                "published_at": (T0 - timedelta(hours=5)).isoformat(),
                "received_at": (T0 - timedelta(hours=5)).isoformat(),
            }
        ],
        market="KR",
        source="google_rss",
        query="수주",
        lang="ko",
        names={"005930": "삼성전자"},
    )
    c = SourcesConfig.model_validate(
        {
            "edgar": {"user_agent": "t t@e.com"},
            "news": {"enabled": True, "google_rss": False, "naver": False},
            "macro": {"enabled": False},
            "community": {"enabled": False},
        }
    )
    d = build_inputs(
        ticker="005930",
        market="KR",
        exchange=None,
        asof=T0,
        cfg=c,
        secrets=Secrets(),
        deps=deps(
            conn_factory=lambda: __import__("trader.ledger.db", fromlist=["connect"]).connect()
        ),
    )
    stock = d["news_macro"]["news"]["data"]["stock"]
    assert [s["title"] for s in stock] == ["삼성전자 대형 수주"]
    assert stock[0]["source"] == "store:google_rss"
    assert d["news_macro"]["news"]["data"]["sources"]["store"] == "1건 합침"
    # 다른 종목에는 들어가지 않는다
    d2 = build_inputs(
        ticker="000660",
        market="KR",
        exchange=None,
        asof=T0,
        cfg=c,
        secrets=Secrets(),
        deps=deps(
            conn_factory=lambda: __import__("trader.ledger.db", fromlist=["connect"]).connect()
        ),
    )
    assert d2["news_macro"]["news"]["data"]["stock"] == []


def test_sentiment_saveticker_section_from_local_dir(project, monkeypatch):
    """세이브티커 로컬 폴더가 있으면 sentiment.saveticker 에 종목 언급·트렌드가 들어간다."""
    import shutil

    from helpers_sources import FIXTURES

    shutil.copytree(FIXTURES / "saveticker", project / "state" / "external" / "saveticker")
    late = T0.replace(year=2026, month=10, day=25, hour=3)
    d = build_inputs(
        ticker="QQQ",
        market="US",
        exchange="NAS",
        asof=late,
        cfg=cfg(),
        secrets=Secrets(),
        deps=deps(now=lambda: late),
    )
    sec = d["sentiment"]["saveticker"]
    assert sec["error"] is None and sec["data"]["mentions"]["community_top5"][0]["tickers"] == [
        "QQQ"
    ]
    assert sec["data"]["trends"][0]["keyword"] == "트럼프"
    d2 = build_inputs(
        ticker="005930",
        market="KR",
        exchange=None,
        asof=late,
        cfg=cfg(),
        secrets=Secrets(),
        deps=deps(now=lambda: late),
    )
    assert d2["sentiment"]["saveticker"]["data"]["mentions"] == {}
    shutil.rmtree(project / "state" / "external")
    d3 = build_inputs(
        ticker="005930",
        market="KR",
        exchange=None,
        asof=T0,
        cfg=cfg(),
        secrets=Secrets(),
        deps=deps(),
    )
    assert d3["sentiment"]["saveticker"]["error"] == "no saveticker dir"


def test_build_inputs_returns_at_shared_deadline_without_waiting_stuck_section(project):
    """전체 검토 P7: 멈춘 절을 기다리지 않고 공통 기한에 돌아온다(예전 `with` 풀은 닫힐 때 대기)."""
    import threading
    import time

    import pytest

    from trader.sources import inputs as inputs_mod

    release = threading.Event()

    def stuck(*a, **kw):
        release.wait(5)
        return {}

    mp = pytest.MonkeyPatch()
    mp.setattr(inputs_mod, "SECTION_TIMEOUT_S", 0.3)
    mp.setattr(inputs_mod, "_fundamental", lambda *a, **kw: {})
    mp.setattr(inputs_mod, "_news_macro", stuck)
    mp.setattr(inputs_mod, "_sentiment", stuck)
    t0 = time.monotonic()
    try:
        d = build_inputs(
            ticker="005930",
            market="KR",
            exchange=None,
            asof=T0,
            cfg=cfg(),
            secrets=Secrets(),
            deps=deps(),
        )
    finally:
        mp.undo()
        release.set()
    assert time.monotonic() - t0 < 2.0  # 절마다 0.3초씩 따로 기다리지도 않는다
    assert "timeout" in d["news_macro"]["error"] and "timeout" in d["sentiment"]["error"]
    for _ in range(100):  # 풀어 준 뒤 멈춘 작업이 끝나면 수가 돌아온다
        if not any(inputs_mod._STUCK.values()):
            break
        time.sleep(0.02)
    assert not any(inputs_mod._STUCK.values())


def test_stuck_sections_are_not_resubmitted_beyond_cap(project):
    """시간 초과 뒤에도 안 끝난 절이 상한만큼 쌓이면 그 절은 새로 띄우지 않는다."""
    import threading

    import pytest

    from trader.sources import inputs as inputs_mod

    release = threading.Event()
    started = {"n": 0}

    def stuck(*a, **kw):
        started["n"] += 1
        release.wait(5)
        return {}

    mp = pytest.MonkeyPatch()
    mp.setattr(inputs_mod, "SECTION_TIMEOUT_S", 0.1)
    mp.setattr(inputs_mod, "_fundamental", lambda *a, **kw: {})
    mp.setattr(inputs_mod, "_news_macro", stuck)
    mp.setattr(inputs_mod, "_sentiment", lambda *a, **kw: {})
    kw = dict(
        ticker="005930",
        market="KR",
        exchange=None,
        asof=T0,
        cfg=cfg(),
        secrets=Secrets(),
        deps=deps(),
    )
    try:
        for _ in range(inputs_mod.MAX_STUCK_PER_SECTION):
            build_inputs(**kw)
        d = build_inputs(**kw)
    finally:
        mp.undo()
        release.set()
    assert started["n"] == inputs_mod.MAX_STUCK_PER_SECTION
    assert "skipped" in d["news_macro"]["error"]
    # 역할 단위 실패도 errors 집계에 들어간다(PR #37 리뷰)
    assert any(e.startswith("news_macro:") and "skipped" in e for e in d["errors"])
    import time

    for _ in range(100):
        if not any(inputs_mod._STUCK.values()):
            break
        time.sleep(0.02)
    assert not any(inputs_mod._STUCK.values())
