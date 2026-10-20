"""유니버스 점검: KIS 가 시세를 주는 코드만 남기고, US 는 데이터가 오는 거래소 코드를 알린다."""

from decimal import Decimal

from typer.testing import CliRunner

from trader.broker.fake import FakeMarket
from trader.cli import app
from trader.config import Universe, UsSymbol
from trader.scan import universe_check


def test_check_kr_flags_codes_without_price():
    uni = Universe(kr=["005930", "999999"])
    market = FakeMarket(prices={"005930": Decimal("71500")})
    rows = universe_check.check(uni, market, market="KR")
    by = {r["ticker"]: r for r in rows}
    assert by["005930"]["ok"] is True and by["999999"]["ok"] is False
    assert "가격" in by["999999"]["note"] or "없" in by["999999"]["note"]


def test_check_us_suggests_exchange_when_declared_is_empty():
    """WMT 처럼 선언 거래소(NYS)로는 빈 응답이고 NAS 로만 오는 종목을 잡아낸다."""

    class Market(FakeMarket):
        def get_price_us(self, symbol, exchange):
            q = super().get_price_us(symbol, exchange)
            if symbol == "WMT" and exchange != "NAS":
                return q.model_copy(update={"price": Decimal(0)})
            return q

    uni = Universe(
        us=[UsSymbol(symbol="WMT", exchange="NYS"), UsSymbol(symbol="JPM", exchange="NYS")]
    )
    market = Market(prices={"WMT": Decimal("100"), "JPM": Decimal("200")})
    rows = {r["ticker"]: r for r in universe_check.check(uni, market, market="US")}
    assert rows["JPM"]["ok"] is True and rows["JPM"]["suggested_exchange"] is None
    assert rows["WMT"]["ok"] is False and rows["WMT"]["suggested_exchange"] == "NAS"


def test_cli_universe_check_lists_problems_only(project, monkeypatch):
    import yaml

    from trader import cli

    (project / "config" / "universe.yaml").write_text(
        yaml.safe_dump({"kr": ["005930", "999999"], "us": [], "exclude": [], "watch": []}),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "_build_market", lambda s: FakeMarket(prices={"005930": Decimal("1")}))
    r = CliRunner().invoke(app, ["universe", "check", "--market", "KR"])
    assert r.exit_code == 1, r.stdout  # 문제가 있으면 exit 1
    assert "999999" in r.stdout and "005930" not in r.stdout
    r = CliRunner().invoke(app, ["universe", "check", "--market", "KR", "--all"])
    assert "005930" in r.stdout


def test_check_kr_distinguishes_transient_api_error_from_no_price():
    """KIS 일시 오류(한도·네트워크)는 상장폐지·오코드와 다르다 → note 에 코드와 '재실행'."""
    from trader.broker.errors import KisApiError

    class Market(FakeMarket):
        def get_price_kr(self, ticker):
            if ticker == "000660":
                raise KisApiError("EGW00201", "초당 거래건수를 초과하였습니다", "FHKST01010100")
            return super().get_price_kr(ticker)

    uni = Universe(kr=["005930", "000660", "999999"])
    market = Market(prices={"005930": Decimal("1")})
    rows = {r["ticker"]: r for r in universe_check.check(uni, market, market="KR")}
    assert rows["000660"]["ok"] is False and "EGW00201" in rows["000660"]["note"]
    assert "재실행" in rows["000660"]["note"]
    assert rows["999999"]["ok"] is False and "재실행" not in rows["999999"]["note"]
