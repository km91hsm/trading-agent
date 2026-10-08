import logging
from datetime import date
from decimal import Decimal

import httpx
import pytest
from helpers_sources import T0, corpcode_zip, load

from trader.sources.dart import DartClient, basic_ratios, ratios_kr

BASE = "https://opendart.fss.or.kr/api"


@pytest.fixture
def dart(project, respx_mock):
    respx_mock.get(BASE + "/corpCode.xml").mock(
        return_value=httpx.Response(200, content=corpcode_zip())
    )
    return DartClient("DART-KEY-SECRET", project / "state" / "cache", now=lambda: T0)


def test_corp_code_cached(dart, respx_mock, project):
    assert dart.corp_code("005930") == "00126380"
    assert dart.corp_name("005930") == "삼성전자"
    assert dart.corp_code("999999") is None
    assert respx_mock.calls.call_count == 1
    assert (project / "state" / "cache" / "dart_corpcode.json").exists()
    dart2 = DartClient("DART-KEY-SECRET", project / "state" / "cache", now=lambda: T0)
    assert dart2.corp_code("000660") == "00164779"
    assert respx_mock.calls.call_count == 1


def test_financials_parsed(dart, respx_mock):
    route = respx_mock.get(BASE + "/fnlttSinglAcntAll.json").mock(
        return_value=httpx.Response(200, json=load("dart_fnltt.json"))
    )
    fin = dart.financials("00126380", 2025)
    assert fin["매출액"] == Decimal("330000000000000")
    assert fin["영업이익"] == Decimal("33000000000000")
    assert fin["당기순이익"] == Decimal("40000000000000")
    assert fin["자본총계"] == Decimal("400000000000000")
    assert fin["매출액_전기"] == Decimal("300000000000000")
    p = route.calls[0].request.url.params
    assert p["reprt_code"] == "11011" and p["fs_div"] == "CFS" and p["bsns_year"] == "2025"
    assert "DART-KEY-SECRET" not in str(route.calls[0].request.url).replace(
        "crtfc_key=DART-KEY-SECRET", ""
    )


def test_no_financials_gives_none_ratios(dart, respx_mock):
    respx_mock.get(BASE + "/fnlttSinglAcntAll.json").mock(
        return_value=httpx.Response(
            200, json={"status": "013", "message": "조회된 데이타가 없습니다."}
        )
    )
    respx_mock.get(BASE + "/stockTotqySttus.json").mock(
        return_value=httpx.Response(200, json={"status": "013", "message": "없음"})
    )
    r = ratios_kr(dart, "005930", date(2026, 10, 9), Decimal("70000"))
    assert set(r) == {"매출성장률", "영업이익률", "ROE", "부채비율", "PER", "PBR", "PSR"}
    assert all(v is None for v in r.values())


def test_ratios_kr_computed(dart, respx_mock):
    respx_mock.get(BASE + "/fnlttSinglAcntAll.json").mock(
        return_value=httpx.Response(200, json=load("dart_fnltt.json"))
    )
    respx_mock.get(BASE + "/stockTotqySttus.json").mock(
        return_value=httpx.Response(200, json=load("dart_shares.json"))
    )
    r = ratios_kr(dart, "005930", date(2026, 10, 9), Decimal("70000"))
    assert r["매출성장률"] == pytest.approx(0.1)
    assert r["영업이익률"] == pytest.approx(0.1)
    assert r["ROE"] == pytest.approx(0.1)
    assert r["부채비율"] == pytest.approx(0.25)
    mcap = 70000 * 5969782550
    assert r["PER"] == pytest.approx(mcap / 40e12)
    assert r["PBR"] == pytest.approx(mcap / 400e12)


def test_ratios_use_previous_year_before_april(dart, respx_mock):
    """3월에는 전년도 사업보고서가 아직 없으므로 2년 전 연도를 조회한다."""
    route = respx_mock.get(BASE + "/fnlttSinglAcntAll.json").mock(
        return_value=httpx.Response(200, json=load("dart_fnltt.json"))
    )
    respx_mock.get(BASE + "/stockTotqySttus.json").mock(
        return_value=httpx.Response(200, json=load("dart_shares.json"))
    )
    ratios_kr(dart, "005930", date(2026, 3, 15), Decimal("70000"))
    assert route.calls[0].request.url.params["bsns_year"] == "2024"


def test_disclosures_filtered_by_asof(dart, respx_mock):
    respx_mock.get(BASE + "/list.json").mock(
        return_value=httpx.Response(200, json=load("dart_list.json"))
    )
    items = dart.disclosures("00126380", T0, days=30)
    assert [i["rcept_dt"] for i in items] == ["2026-10-07", "2026-09-25"]
    assert items[0]["title"].startswith("연결재무제표") and items[0]["url"].endswith(
        "20261007000002"
    )


def test_error_status_raises_without_key(dart, respx_mock, caplog):
    respx_mock.get(BASE + "/fnlttSinglAcntAll.json").mock(
        return_value=httpx.Response(200, json={"status": "020", "message": "요청 제한 초과"})
    )
    with (
        caplog.at_level(logging.WARNING, logger="trader.sources"),
        pytest.raises(RuntimeError) as e,
    ):
        dart.financials("00126380", 2025)
    assert "020" in str(e.value) and "DART-KEY-SECRET" not in str(e.value)
    assert "DART-KEY-SECRET" not in caplog.text


def test_corpcode_non_zip_error_is_dart_error(project, respx_mock):
    """실기 확인: 키 오류·한도 초과 시 corpCode.xml 이 zip 이 아니라 오류 XML 로 온다."""
    respx_mock.get(BASE + "/corpCode.xml").mock(
        return_value=httpx.Response(
            200,
            content=(
                '<?xml version="1.0"?><result><status>020</status>'
                "<message>요청 한도 초과</message></result>"
            ).encode(),
        )
    )
    dart = DartClient("DART-KEY-SECRET", project / "state" / "cache", now=lambda: T0)
    with pytest.raises(RuntimeError) as e:
        dart.corp_code("005930")
    assert "020" in str(e.value) and "DART-KEY-SECRET" not in str(e.value)


def test_ofs_fallback_when_cfs_empty(dart, respx_mock):
    """연결(CFS)이 013 이면 개별(OFS)로 1회 재조회하고 _fs_div 로 표시한다."""
    route = respx_mock.get(BASE + "/fnlttSinglAcntAll.json").mock(
        side_effect=[
            httpx.Response(200, json={"status": "013", "message": "없음"}),
            httpx.Response(200, json=load("dart_fnltt.json")),
        ]
    )
    fin = dart.financials("00126380", 2025)
    assert fin["매출액"] == Decimal("330000000000000") and fin["_fs_div"] == "OFS"
    assert [c.request.url.params["fs_div"] for c in route.calls] == ["CFS", "OFS"]


def test_financial_history_three_years_and_quarter(dart, respx_mock):
    route = respx_mock.get(BASE + "/fnlttSinglAcntAll.json").mock(
        return_value=httpx.Response(200, json=load("dart_fnltt.json"))
    )
    h = dart.financial_history("00126380", date(2026, 10, 9))
    assert sorted(h["years"]) == [2023, 2024, 2025]
    assert h["years"][2025]["영업이익"] == Decimal("33000000000000")
    q = h["latest_quarter"]
    assert q["reprt_code"] == "11012" and q["year"] == 2026 and q["영업이익"] is not None
    calls = [
        (c.request.url.params["bsns_year"], c.request.url.params["reprt_code"]) for c in route.calls
    ]
    assert ("2026", "11012") in calls and ("2023", "11011") in calls


def test_financial_history_quarter_before_june_uses_prev_year_q3(dart, respx_mock):
    respx_mock.get(BASE + "/fnlttSinglAcntAll.json").mock(
        return_value=httpx.Response(200, json=load("dart_fnltt.json"))
    )
    h = dart.financial_history("00126380", date(2026, 3, 15))
    assert h["latest_quarter"]["reprt_code"] == "11014" and h["latest_quarter"]["year"] == 2025
    assert sorted(h["years"]) == [2022, 2023, 2024]


def test_basic_ratios_shared():
    fin = {
        "매출액": Decimal(200),
        "매출액_전기": Decimal(100),
        "영업이익": Decimal(20),
        "당기순이익": Decimal(10),
        "자본총계": Decimal(50),
        "부채총계": Decimal(25),
    }
    r = basic_ratios(fin, shares=10, price=Decimal(5))
    assert r["매출성장률"] == 1.0 and r["PSR"] == pytest.approx(50 / 200)
    assert basic_ratios({}, shares=None, price=None)["PER"] is None
