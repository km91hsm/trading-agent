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


def test_company_name_from_ticker_map(edgar):
    assert edgar.company_name("AAPL") == "Apple Inc." and edgar.company_name("ZZZZ") is None


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


def test_ratios_by_year_us_uses_filings_known_by_april():
    """사업연도 Y 값은 Y+1 년 4월 1일까지 제출된 10-K 로만.

    지연 제출(FY2025, 5월)은 2026 행에 못 들어간다."""
    from helpers_sources import FakeEdgar

    from trader.sources.edgar import ratios_by_year_us

    e = FakeEdgar()
    out = ratios_by_year_us(e, "AAPL", [2022, 2023, 2024, 2025])
    assert set(out) == {2023, 2024, 2025}  # 2022: 2023-04-01 시점에 10-K 없음
    assert out[2024]["영업이익률"] == pytest.approx(33 / 330)
    # FY2025 10-K 는 2026-04-01 에 미제출 → FY2024 값 유지
    assert out[2025]["영업이익률"] == pytest.approx(33 / 330)
    assert out[2024]["ROE"] == pytest.approx(40 / 400) and out[2024]["PER"] is None
    assert e.calls == 1  # companyfacts 는 종목당 1회


def test_ratios_by_year_us_without_cik_is_empty():
    from helpers_sources import FakeEdgar

    from trader.sources.edgar import ratios_by_year_us

    assert ratios_by_year_us(FakeEdgar(), "ZZZZ", [2024]) == {}


def test_financial_history_us_three_years_and_latest_quarter():
    """KR financial_history 와 같은 꼴: 3개년 10-K(years, end 연도 키) + 최신 10-K 뒤의 최근 10-Q.
    둘 다 asof 까지 제출된 것만 (룩어헤드)."""
    from helpers_sources import FakeEdgar

    from trader.sources.edgar import financial_history_us

    facts = FakeEdgar().companyfacts("0000320193")
    h = financial_history_us(facts, date(2026, 4, 1))
    assert set(h["years"]) == {2023, 2024}  # FY2025 10-K 는 2026-05-15 제출
    assert h["years"][2024]["매출액"] == Decimal(330) and "매출액_전기" not in h["years"][2024]
    q = h["latest_quarter"]
    assert q["form"] == "10-Q" and q["end"] == "2025-09-30" and q["filed"] == "2025-11-01"
    assert q["매출액"] == Decimal(80) and q["자본총계"] == Decimal(410)
    h2 = financial_history_us(facts, date(2026, 6, 1))
    assert set(h2["years"]) == {2023, 2024, 2025}
    assert h2["latest_quarter"]["end"] == "2026-03-31"


def test_financial_history_us_quarter_none_when_not_after_latest_10k_or_after_asof():
    from helpers_sources import FakeEdgar

    from trader.sources.edgar import financial_history_us

    facts = FakeEdgar().companyfacts("0000320193")
    h = financial_history_us(facts, date(2025, 10, 1))  # 10-Q(9/30) 는 11/01 제출 → 아직 없음
    assert set(h["years"]) == {2023, 2024} and h["latest_quarter"] is None
    assert financial_history_us({"facts": {}}, date(2026, 1, 1)) == {
        "years": {},
        "latest_quarter": None,
    }


def test_companyfacts_memoized_per_client(edgar, respx_mock):
    """ratios 와 history 가 같은 종목을 두 번 받지 않는다 (EDGAR 10 req/s 예절)."""
    cik = edgar.cik("AAPL")
    edgar.companyfacts(cik)
    edgar.companyfacts(cik)
    assert respx_mock.get(FACTS).call_count == 1
