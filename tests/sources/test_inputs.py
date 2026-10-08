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
