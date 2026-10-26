"""Phase 25 단계 4: KIS 순위 API 3종 (응답 필드명 미확인 → 관대한 파서). 합성 응답만 쓴다."""

import httpx
import pytest

from trader.broker.kis_market import KisMarket
from trader.broker.tr_ids import path, tr_id

REAL = "https://openapi.koreainvestment.com:9443"


@pytest.fixture
def market(real_client):
    return KisMarket(real_client)


def _ok(rows):
    return httpx.Response(
        200, json={"rt_cd": "0", "msg_cd": "MCA00000", "msg1": "정상", "output": rows}
    )


def test_rank_parser_known_fields(market, respx_mock):
    respx_mock.get(REAL + path("VOLUME_RANK")).mock(
        return_value=_ok(
            [
                {
                    "mksc_shrn_iscd": "111111",
                    "hts_kor_isnm": "가나전자",
                    "data_rank": "1",
                    "prdy_ctrt": "12.50",
                    "acml_vol": "1000",
                },
                {
                    "stck_shrn_iscd": "222222",
                    "hts_kor_isnm": "다라",
                    "data_rank": "2",
                    "prdy_ctrt": "-1.0",
                    "acml_vol": "500",
                },
            ]
        )
    )
    rows = market.volume_rank_kr(limit=5)
    assert [(r.ticker, r.name, r.rank, r.volume) for r in rows] == [
        ("111111", "가나전자", 1, 1000),
        ("222222", "다라", 2, 500),
    ]
    assert rows[0].change_pct == pytest.approx(0.125)


def test_foreign_institution_net_buy_sums(market, respx_mock):
    respx_mock.get(REAL + path("FRGN_INST_TOTAL")).mock(
        return_value=_ok(
            [
                {
                    "mksc_shrn_iscd": "111111",
                    "hts_kor_isnm": "가나",
                    "frgn_ntby_qty": "300",
                    "orgn_ntby_qty": "-100",
                }
            ]
        )
    )
    (r,) = market.foreign_institution_kr()
    assert r.net_buy == 200 and r.rank == 1  # 순위 필드가 없으면 응답 순서


def test_rank_parser_unknown_fields_returns_empty(market, respx_mock):
    respx_mock.get(REAL + path("FLUCT_RANK")).mock(return_value=_ok([{"foo": "1", "bar": "x"}]))
    assert market.fluctuation_rank_kr() == []


def test_rank_paper_env_unsupported():
    from trader.broker.errors import KisApiError

    for name in ("VOLUME_RANK", "FLUCT_RANK", "FRGN_INST_TOTAL"):
        assert tr_id(name, "real").startswith("FH")
        with pytest.raises(KisApiError):
            tr_id(name, "paper")
