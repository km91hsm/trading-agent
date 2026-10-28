"""Phase 26 단계 1: 종목 마스터·코드 정규화·월물·행사가 해석."""

import io
import zipfile
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from trader.broker import fo_codes, fo_master
from trader.config import FoCfg

FIX = Path(__file__).parent / "fixtures" / "fo_master_sample.mst"
MINI = FoCfg().products["mini"]
T0 = datetime(2026, 10, 27, 0, 0, tzinfo=UTC)


@pytest.fixture
def contracts():
    return fo_master.parse(FIX.read_bytes())


def test_parse_master_fixture(contracts):
    assert len(contracts) == 20
    f = next(c for c in contracts if c.code == "105W11")
    assert f.kind == "FUTURES" and f.strike is None and f.expiry_ym == "202611"
    assert f.std_code == "KR4105W10001" and f.underlying == "K200"
    c = next(c for c in contracts if c.code == "205W11347")
    assert c.kind == "CALL" and c.strike == Decimal("347.5") and c.atm_flag == "1"
    p = next(c for c in contracts if c.code == "305W11350")
    assert p.kind == "PUT" and p.weekly is False
    w = next(c for c in contracts if c.code == "209W1A350")
    assert w.weekly is True and w.kind == "CALL"


def test_second_thursday_estimate():
    assert fo_master.second_thursday("202611") == date(2026, 11, 12)
    assert fo_master.second_thursday("202612") == date(2026, 12, 10)


def test_front_month_skips_within_roll_days(contracts):
    ltd = {"105W11": date(2026, 11, 12), "105W12": date(2026, 12, 10), "105X03": date(2027, 3, 11)}

    def ltd_of(c):
        return ltd.get(c.code)

    today = date(2026, 10, 27)
    assert fo_master.front_month(
        contracts, MINI, "FUTURES", today=today, roll_days=2, last_trade_date_of=ltd_of
    ).code == "105W11"
    # D-2: 11/10 은 roll 안 → 다음 월물
    assert fo_master.front_month(
        contracts, MINI, "FUTURES", today=date(2026, 11, 10), roll_days=2, last_trade_date_of=ltd_of
    ).code == "105W12"
    assert fo_master.front_month(
        contracts, MINI, "FUTURES", today=date(2026, 11, 9), roll_days=2, last_trade_date_of=ltd_of
    ).code == "105W11"
    # 시세가 없으면 둘째 목요일 추정
    assert fo_master.front_month(
        contracts, MINI, "FUTURES", today=today, roll_days=2, last_trade_date_of=lambda c: None
    ).code == "105W11"
    # 정규 상품은 미니 코드를 고르지 않는다
    reg = FoCfg().products["regular"]
    assert fo_master.front_month(
        contracts, reg, "FUTURES", today=today, roll_days=2, last_trade_date_of=lambda c: None
    ).code == "101W12"


def _resolve(contracts, **kw):
    base = dict(
        underlying=Decimal("350.8"), today=date(2026, 10, 27), roll_days=2,
        last_trade_date_of=lambda c: None, expiry_rule="front",
    )
    base.update(kw)
    return fo_master.resolve_option(contracts, MINI, **base)


def test_resolve_option_call_otm1_above_put_otm1_below(contracts):
    assert _resolve(contracts, call_put="CALL", strike_rule="ATM").code == "205W11350"
    assert _resolve(contracts, call_put="CALL", strike_rule="OTM1").code == "205W11352"
    assert _resolve(contracts, call_put="CALL", strike_rule="OTM2").code == "205W11355"
    assert _resolve(contracts, call_put="PUT", strike_rule="ATM").code == "305W11350"
    assert _resolve(contracts, call_put="PUT", strike_rule="OTM1").code == "305W11347"
    assert _resolve(contracts, call_put="PUT", strike_rule="OTM2").code == "305W11345"


def test_resolve_option_itm1_direction(contracts):
    assert _resolve(contracts, call_put="CALL", strike_rule="ITM1").code == "205W11347"
    assert _resolve(contracts, call_put="PUT", strike_rule="ITM1").code == "305W11352"


def test_resolve_option_next_expiry_and_missing(contracts):
    nxt = _resolve(contracts, call_put="CALL", strike_rule="ATM", expiry_rule="next")
    assert nxt.code == "205W12350"
    # 다음 월물에 OTM2 가 없으면 None
    assert _resolve(contracts, call_put="CALL", strike_rule="OTM2", expiry_rule="next") is None


def test_weekly_excluded(contracts):
    only_weekly = [c for c in contracts if c.weekly]
    assert _resolve(only_weekly, call_put="CALL", strike_rule="ATM") is None


def test_last_trade_date_prefers_quote(contracts):
    f = next(c for c in contracts if c.code == "105W11")

    class Q:
        last_trade_date = date(2026, 11, 13)

    assert fo_master.last_trade_date(f, Q()) == date(2026, 11, 13)
    assert fo_master.last_trade_date(f, None) == date(2026, 11, 12)


def _zip_bytes():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("fo_idx_code_mts.mst", FIX.read_bytes())
    return buf.getvalue()


class _Http:
    def __init__(self):
        self.calls = 0

    def get(self, url, **kw):
        self.calls += 1

        class R:
            content = _zip_bytes()

            def raise_for_status(self):
                pass

        return R()


def test_cache_reused_within_24h(tmp_path):
    http = _Http()
    a = fo_master.load(cache_dir=tmp_path, http=http, now=lambda: T0)
    b = fo_master.load(cache_dir=tmp_path, http=http, now=lambda: T0)
    assert http.calls == 1 and len(a) == len(b) == 20
    later = datetime(2026, 10, 28, 1, 0, tzinfo=UTC)
    fo_master.load(cache_dir=tmp_path, http=http, now=lambda: later)
    assert http.calls == 2


def test_normalize_std_code_to_short(contracts):
    assert fo_codes.normalize(" 105w11 ") == "105W11"
    assert fo_codes.normalize("KR4205W10347", contracts) == "205W11347"
    assert fo_codes.normalize("KR4999W10000", contracts) == "KR4999W10000"
    assert fo_codes.kind_of("205W11347", MINI) == "CALL"
    assert fo_codes.kind_of("305W11347", MINI) == "PUT"
    assert fo_codes.kind_of("105W11", MINI) == "FUTURES"
    assert fo_codes.kind_of("101W12", MINI) is None


def test_of_product_and_option_front_public(contracts):
    """Claude 리뷰 6: CLI 가 쓰는 공개 헬퍼 — 상품·종류 필터와 roll 창 밖 가장 가까운 옵션 월물."""
    futs = fo_master.of_product(contracts, MINI, "FUTURES")
    assert futs and all(c.kind == "FUTURES" and c.code.startswith("105") for c in futs)
    ym, chain = fo_master.option_front(
        contracts, MINI, today=date(2026, 10, 27), roll_days=2, last_trade_date_of=lambda c: None
    )
    assert ym == "202611"
    assert chain and {c.kind for c in chain} == {"CALL", "PUT"}
    assert all(c.expiry_ym == "202611" for c in chain)
    # 월물이 전부 roll 창 안·만기면 (None, [])
    assert fo_master.option_front(
        contracts, MINI, today=date(2030, 1, 1), roll_days=2, last_trade_date_of=lambda c: None
    ) == (None, [])


# KRX 영문 단축코드 체계(실기 1, 2026-10-28): 선물 A01/A05, 콜 B01/B05, 풋 C01/C05,
# 위클리 B09·C09·BAF·CAF.
# 월물구분 칸은 순번이고 만기월(YYYYMM)은 종목명에 있다. 아래 행은 구조만 본뜬 합성 데이터다.
NEW_ROWS = "\n".join(
    [
        "B|A05611|KR4A056B0006|미니F 202611| |00000.00|1|2001|KOSPI200",
        "B|A05612|KR4A056C0005|미니F 202612| |00000.00|2|2001|KOSPI200",
        "1|A01612|KR4A016C0004|F 202612| |00000.00|1|2001|KOSPI200",
        "D|B05611945|KR4B056B9451|미니C 202611   945.0|2|00945.00| |2001|KOSPI200",
        "E|C05611945|KR4C056B9459|미니P 202611   945.0|3|00945.00| |2001|KOSPI200",
        "5|B01612C10|KR4B016CC100|C 202612 1,010.0|3|01010.00| |2001|KOSPI200",
        "L|B09FJW945|KR4B09FJ9455|위클리C 2610W5   945.0|2|00945.00| |2001|KOSPI200",
        "N|CAFC4W945|KR4CAFC49450|위클리M P 2611W1   945.0|2|00945.00| |2001|KOSPI200",
        "7|A04611|KR4A046B0009|변동성F 202611| |00000.00|1|0503|VKOSPI",
    ]
).encode("cp949")


def test_parse_krx_alpha_codes_and_expiry_from_name():
    cs = {c.code: c for c in fo_master.parse(NEW_ROWS)}
    assert "A04611" not in cs  # 변동성 선물은 대상 아님
    f = cs["A05611"]
    assert f.kind == "FUTURES" and f.expiry_ym == "202611" and not f.weekly
    assert cs["A01612"].expiry_ym == "202612"
    c = cs["B05611945"]
    assert c.kind == "CALL" and c.strike == Decimal("945.0") and c.expiry_ym == "202611"
    p = cs["C05611945"]
    assert p.kind == "PUT" and p.expiry_ym == "202611"
    assert cs["B01612C10"].strike == Decimal("1010.00")
    assert cs["B09FJW945"].weekly and cs["CAFC4W945"].weekly
    assert cs["B09FJW945"].expiry_ym is None


def test_front_month_and_kind_with_krx_alpha_codes():
    cs = fo_master.parse(NEW_ROWS)
    f = fo_master.front_month(
        cs, MINI, "FUTURES", today=date(2026, 10, 28), roll_days=2,
        last_trade_date_of=lambda c: None,
    )  # fmt: skip
    assert f is not None and f.code == "A05611"  # 정규(A01)는 미니 상품에 섞이지 않는다
    ym, chain = fo_master.option_front(
        cs, MINI, today=date(2026, 10, 28), roll_days=2, last_trade_date_of=lambda c: None
    )
    assert ym == "202611" and {c.code for c in chain} == {"B05611945", "C05611945"}
    assert fo_codes.kind_of("A05611", MINI) == "FUTURES"
    assert fo_codes.kind_of("B05611945", MINI) == "CALL"
    assert fo_codes.kind_of("C05611945", MINI) == "PUT"
    assert fo_codes.kind_of("A01612", MINI) is None
    assert fo_codes.kind_of("105W11", MINI) == "FUTURES"  # 옛 숫자 코드도 계속 읽는다
