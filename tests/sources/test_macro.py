from decimal import Decimal

import httpx
from helpers_sources import T0, load

from trader.config import MacroCfg, Secrets
from trader.sources.macro import Ecos, Fred, collect_macro

FRED = "https://api.stlouisfed.org/fred/series/observations"
ECOS = "https://ecos.bok.or.kr/api/StatisticSearch/"


class FakeIndexMarket:
    def __init__(self, fail=False):
        self.fail = fail

    def get_index_kr(self, code="0001"):
        if self.fail:
            raise RuntimeError("KIS down")
        from trader.broker.models import Quote

        return Quote(
            ticker=code, market="KR", price=Decimal("3250.12"), change_pct=0.0038, volume=1, asof=T0
        )


def test_fred_latest_skips_missing(respx_mock):
    route = respx_mock.get(url__startswith=FRED).mock(
        return_value=httpx.Response(200, json=load("macro_fred.json"))
    )
    v = Fred("FRED-KEY-SECRET", httpx.Client()).latest("DGS10")
    assert v == {"value": 4.12, "date": "2026-10-07"}
    p = route.calls[0].request.url.params
    assert p["series_id"] == "DGS10" and p["file_type"] == "json" and p["sort_order"] == "desc"


def test_ecos_latest(respx_mock):
    route = respx_mock.get(url__startswith=ECOS).mock(
        return_value=httpx.Response(200, json=load("macro_ecos.json"))
    )
    v = Ecos("ECOS-KEY-SECRET", httpx.Client(), now=lambda: T0).latest("722Y001", "0101000")
    assert v == {"value": 2.5, "date": "2026-10-08"}
    assert "/ECOS-KEY-SECRET/json/kr/" in str(route.calls[0].request.url)


def test_ecos_error_result_raises(respx_mock):
    respx_mock.get(url__startswith=ECOS).mock(
        return_value=httpx.Response(200, json={"RESULT": {"CODE": "INFO-200", "MESSAGE": "없음"}})
    )
    try:
        Ecos("ECOS-KEY-SECRET", httpx.Client(), now=lambda: T0).latest("722Y001", "0101000")
    except RuntimeError as e:
        assert "INFO-200" in str(e) and "ECOS-KEY-SECRET" not in str(e)
    else:
        raise AssertionError("RuntimeError 가 나야 한다")


def test_no_key_error_section(respx_mock):
    out = collect_macro(
        MacroCfg(),
        Secrets(),
        market_client=FakeIndexMarket(),
        asof=T0,
        http=httpx.Client(),
        now=lambda: T0,
    )
    assert out["kospi"]["data"]["value"] == 3250.12 and out["kospi"]["error"] is None
    for key in ("sp500", "usdkrw", "us10y", "vix", "base_rate_us"):
        assert out[key]["error"] == "no api key", key
    assert out["base_rate_kr"]["error"] == "no api key"


def test_partial_failure_keeps_others(respx_mock):
    respx_mock.get(url__startswith=FRED).mock(
        side_effect=lambda req: (
            httpx.Response(500, text="boom")
            if req.url.params["series_id"] == "VIXCLS"
            else httpx.Response(200, json=load("macro_fred.json"))
        )
    )
    respx_mock.get(url__startswith=ECOS).mock(
        return_value=httpx.Response(200, json=load("macro_ecos.json"))
    )
    secrets = Secrets(FRED_API_KEY="f", ECOS_API_KEY="e")
    out = collect_macro(
        MacroCfg(),
        secrets,
        market_client=FakeIndexMarket(fail=True),
        asof=T0,
        http=httpx.Client(),
        now=lambda: T0,
    )
    assert out["vix"]["data"] is None and out["vix"]["error"]
    assert out["us10y"]["data"]["value"] == 4.12
    assert out["base_rate_kr"]["data"]["value"] == 2.5
    assert out["kospi"]["error"].startswith("RuntimeError")
    assert all(s["received_at"] == T0.isoformat() for s in out.values())


def test_disabled_sources(respx_mock):
    cfg = MacroCfg(enabled=True, fred=False, ecos=False)
    out = collect_macro(
        cfg,
        Secrets(FRED_API_KEY="f", ECOS_API_KEY="e"),
        market_client=None,
        asof=T0,
        http=httpx.Client(),
        now=lambda: T0,
    )
    assert out["us10y"]["error"] == "disabled" and out["base_rate_kr"]["error"] == "disabled"
    assert out["kospi"]["error"] == "no market client"


def test_collect_macro_closes_self_made_client(monkeypatch):
    made: list[httpx.Client] = []
    real = httpx.Client

    class Spy(real):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            made.append(self)

    monkeypatch.setattr(httpx, "Client", Spy)
    collect_macro(MacroCfg(), Secrets(), market_client=None, asof=T0, now=lambda: T0)
    assert len(made) == 1 and made[0].is_closed
