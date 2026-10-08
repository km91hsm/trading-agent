from trader.agents.anonymize import Alias, anonymize, deanonymize


def test_replaces_in_nested_strings_only_values():
    a = Alias(ticker="005930", name="삼성전자")
    out = anonymize(
        {
            "meta": {"ticker": "005930", "name": "삼성전자"},
            "news": [{"title": "삼성전자 실적 발표"}],
            "005930": "키는 그대로",
        },
        a,
    )
    assert out["meta"] == {"ticker": "TICKER_X", "name": "종목X"}
    assert out["news"][0]["title"] == "종목X 실적 발표"
    assert "005930" in out  # 키는 바꾸지 않는다


def test_roundtrip_restores():
    a = Alias(ticker="AAPL", name="Apple Inc.")
    src = {"ticker": "AAPL", "x": "Apple Inc. 호재", "n": 1, "d": 2.5, "b": None}
    assert deanonymize(anonymize(src, a), a) == src


def test_ticker_boundary_not_inside_other_tokens():
    a = Alias(ticker="005930", name="삼성전자")
    assert anonymize({"v": "10059300"}, a)["v"] == "10059300"
    assert anonymize({"v": "코드 005930 입니다"}, a)["v"] == "코드 TICKER_X 입니다"


def test_input_not_mutated():
    a = Alias(ticker="005930", name="삼성전자")
    src = {"t": "삼성전자"}
    anonymize(src, a)
    assert src == {"t": "삼성전자"}


def test_short_name_not_replaced():
    a = Alias(ticker="034730", name="SK")
    assert anonymize({"v": "RISK ASK"}, a)["v"] == "RISK ASK"
    assert anonymize({"v": "SK 그룹"}, a)["v"] == "종목X 그룹"
