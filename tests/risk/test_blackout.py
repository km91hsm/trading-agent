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


def test_store_keeps_last_success_per_kind_and_reports_stale(conn):
    """2-O9: 종류별 마지막 성공분을 합친다 — 실패한 종류는 지난 일정·수집 시각 유지, 성공한 종류만
    교체. `stale_kinds` 는 수집 시각이 STALE_DAYS 넘은 종류(없으면 None 일수)."""
    from datetime import UTC, datetime, timedelta

    from trader.control import control

    on = BlackoutCfg(enabled=True, kinds=["paidin", "rev_split"])
    t1 = datetime(2026, 10, 27, 22, 5, tzinfo=UTC)
    r1 = blackout.collect(Market(), on, today=D, now=t1)
    blackout.store(conn, r1)
    r2 = blackout.collect(Market(fail=["rev_split"]), on, today=D, now=t1 + timedelta(days=1))
    blackout.store(conn, r2)
    data = control.get_flag(conn, blackout.FLAG)
    assert sorted(e["kind"] for e in data["events"]) == ["paidin", "rev_split"]
    assert data["fetched_at"] == {"paidin": r2["at"], "rev_split": r1["at"]}
    assert blackout.reason(conn, "005930", D, on) is not None
    assert blackout.stale_kinds(data, on, now=t1 + timedelta(days=3)) == {}
    assert blackout.stale_kinds(data, on, now=t1 + timedelta(days=4)) == {"rev_split": 4}
    # 설정에서 빠진 종류의 지난 일정은 버리고, 수집 기록이 없는 종류는 None
    only = BlackoutCfg(enabled=True, kinds=["paidin"])
    blackout.store(conn, blackout.collect(Market(), only, today=D, now=t1))
    data = control.get_flag(conn, blackout.FLAG)
    assert [e["kind"] for e in data["events"]] == ["paidin"]
    assert blackout.stale_kinds(data, on, now=t1) == {"rev_split": None}
