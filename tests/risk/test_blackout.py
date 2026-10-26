"""권리 일정 블랙아웃(Stock D-113): 수집·창 판정·기본 끔·executor 가 BUY 만 막는다."""

from datetime import date

import httpx
import respx

from trader.config import BlackoutCfg
from trader.risk import blackout

D = date(2026, 10, 27)


class Market:
    def __init__(self, fail=()):
        self.fail = set(fail)

    def ksd_schedule(self, kind, start, end):
        if kind in self.fail:
            raise RuntimeError("x")
        return [{"ticker": "005930", "kind": kind, "date": "2026-10-30", "name": "삼성전자"}]


def test_collect_and_reason_window(conn):
    on = BlackoutCfg(enabled=True, kinds=["paidin", "rev_split"])
    r = blackout.collect(Market(fail=["rev_split"]), on, today=D)
    assert len(r["events"]) == 1 and "rev_split" in r["errors"]
    blackout.store(conn, r)
    assert blackout.reason(conn, "005930", D, on) == "권리 일정: 유상증자 기준일 2026-10-30"
    assert blackout.reason(conn, "005930", date(2026, 10, 24), on) is None  # 6일 전
    assert blackout.reason(conn, "005930", date(2026, 11, 1), on) is not None  # 2일 뒤
    assert blackout.reason(conn, "005930", date(2026, 11, 2), on) is None
    assert blackout.reason(conn, "000660", D, on) is None
    assert blackout.reason(conn, "005930", D, BlackoutCfg()) is None  # 기본 끔


@respx.mock
def test_kis_ksd_schedule_parses_and_pages(project):
    from trader.broker.kis_auth import KisAuth
    from trader.broker.kis_client import KisClient
    from trader.broker.kis_market import KisMarket

    respx.post("https://openapi.koreainvestment.com:9443/oauth2/tokenP").mock(
        return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 86400})
    )
    route = respx.get(
        "https://openapi.koreainvestment.com:9443/uapi/domestic-stock/v1/ksdinfo/paidin-capin"
    ).mock(
        side_effect=[
            httpx.Response(
                200,
                headers={"tr_cont": "F"},
                json={
                    "rt_cd": "0",
                    "cts": "NEXT",
                    "output1": [{"record_date": "20261030", "sht_cd": "005930", "isin_name": "가"}],
                },
            ),
            httpx.Response(
                200,
                json={
                    "rt_cd": "0",
                    "output1": [
                        {"record_date": "20261105", "sht_cd": "000660", "isin_name": "나"},
                        {"record_date": "", "sht_cd": "x"},
                    ],
                },
            ),
        ]
    )
    m = KisMarket(KisClient("real", KisAuth("real", "k", "s")))
    got = m.ksd_schedule("paidin", D, date(2026, 11, 26))
    assert [g["ticker"] for g in got] == ["005930", "000660"]
    assert got[0] == {"ticker": "005930", "kind": "paidin", "date": "2026-10-30", "name": "가"}
    second = route.calls[1].request
    assert second.url.params["CTS"] == "NEXT" and second.headers["tr_cont"] == "N"
    assert route.calls[0].request.headers["tr_id"] == "HHKDB669100C0"
    assert route.calls[0].request.url.params["GB1"] == "1"


def test_executor_blocks_buy_in_window_when_enabled(conn, project):
    from decimal import Decimal

    from conftest import make_account
    from helpers_agents import ok_decision

    from trader.agents.schemas import Decision
    from trader.broker.fake import FakeBroker
    from trader.broker.models import Cash
    from trader.config import Settings
    from trader.orchestrator import executor

    blackout.store(conn, {"events": [{"ticker": "005930", "kind": "paidin", "date": "2026-10-30"}]})
    acct = make_account("paper-main", "paper", "auto")
    cash = {"KR": Cash(currency="KRW", available=Decimal(10_000_000), total=Decimal(10_000_000))}
    for enabled, blocked in ((False, False), (True, True)):
        ctx = executor.ExecContext(
            settings=Settings(event_blackout={"enabled": enabled}),
            accounts=[acct],
            conn=conn,
            brokers={"paper-main": FakeBroker(acct, cash=cash)},
        )
        res = executor.execute(
            ctx,
            Decision(**ok_decision(action="BUY", target_weight=0.1, ticker="005930")),
            date=D,
            market="KR",
            price=Decimal("70000"),
            dry_run=True,
        )
        assert any("권리 일정" in r for r in res[0]["reasons"]) is blocked
