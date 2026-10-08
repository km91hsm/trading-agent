from datetime import date
from decimal import Decimal

import httpx
import pytest
from helpers_sources import T0, load

from trader.sources.dart import RATIO_KEYS
from trader.sources.edgar import EdgarClient, ratios_us

TICKERS = "https://www.sec.gov/files/company_tickers.json"
FACTS = "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json"


@pytest.fixture
def edgar(project, respx_mock):
    respx_mock.get(TICKERS).mock(return_value=httpx.Response(200, json=load("edgar_tickers.json")))
    respx_mock.get(FACTS).mock(
        return_value=httpx.Response(200, json=load("edgar_companyfacts.json"))
    )
    return EdgarClient("trader test@example.com", project / "state" / "cache", now=lambda: T0)


def test_cik_lookup_cached(edgar, respx_mock, project):
    assert edgar.cik("AAPL") == "0000320193"
    assert edgar.cik("msft") == "0000789019"
    assert edgar.cik("ZZZZ") is None
    assert (project / "state" / "cache" / "edgar_tickers.json").exists()
    again = EdgarClient("trader test@example.com", project / "state" / "cache", now=lambda: T0)
    assert again.cik("AAPL") == "0000320193"
    assert respx_mock.get(TICKERS).call_count == 1


def test_user_agent_header(edgar, respx_mock):
    edgar.cik("AAPL")
    req = respx_mock.get(TICKERS).calls[0].request
    assert req.headers["user-agent"] == "trader test@example.com"


def test_ratios_same_keys_as_kr(edgar):
    r = ratios_us(edgar, "AAPL", date(2026, 10, 9), Decimal("227.55"))
    assert set(r) == set(RATIO_KEYS)
    assert r["매출성장률"] == pytest.approx((416 - 391) / 391)
    assert r["영업이익률"] == pytest.approx(133 / 416)
    assert r["ROE"] == pytest.approx(112 / 74)
    assert r["부채비율"] == pytest.approx(285 / 74)
    mcap = 227.55 * 14_800_000_000
    assert r["PER"] == pytest.approx(mcap / 112e9)
    assert r["PBR"] == pytest.approx(mcap / 74e9)


def test_filed_after_asof_excluded(edgar):
    """2025-10-31 제출 10-K 는 2025-10-15 시점엔 못 본다 → 2024 회계연도 값."""
    r = ratios_us(edgar, "AAPL", date(2025, 10, 15), Decimal("200"))
    assert r["영업이익률"] == pytest.approx(123 / 391)
    assert r["ROE"] == pytest.approx(93.7 / 57)
    assert r["매출성장률"] is None  # 2023 값은 픽스처에 없음


def test_unknown_symbol_all_none(edgar):
    r = ratios_us(edgar, "ZZZZ", date(2026, 10, 9), Decimal("1"))
    assert all(v is None for v in r.values())
