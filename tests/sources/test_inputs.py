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
    assert d["errors"] == ["fundamental.ratios: no edgar client"]


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
    for key in ("short_interest", "trends", "manual_inputs"):
        assert d["sentiment"][key]["error"] == "phase6"
    assert not any("phase6" in e for e in d["errors"])
