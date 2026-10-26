"""화면 실시간 시세 허브: 화면이 요청한 국내 종목만 웹소켓 하나로 구독(최근 요청 순 ≤ 40칸),
요청이 끊기면 해제, 핑퐁 응답, 접속키 거절이면 새 키로 재접속, 장 밖·연결 실패는 조용히."""

import json
from datetime import UTC, datetime

from helpers_kis_ws import frame, trade_values

from trader.web.live_quotes import QuoteHub

NOW = datetime(2026, 10, 27, 1, 0, tzinfo=UTC)  # 화 10:00 KST


class FakeConn:
    def __init__(self):
        self.sent: list[dict | str] = []
        self.inbox: list[str] = []
        self.closed = False

    def send(self, text):
        try:
            self.sent.append(json.loads(text))
        except ValueError:
            self.sent.append(text)

    def recv(self, timeout=None):
        if self.inbox:
            return self.inbox.pop(0)
        raise TimeoutError

    def close(self):
        self.closed = True

    def subs(self):
        """등록(1)·해제(2) 순서대로 (tr_type, 종목)."""
        return [
            (m["header"]["tr_type"], m["body"]["input"]["tr_key"])
            for m in self.sent
            if isinstance(m, dict) and "body" in m
        ]


def make_hub(*, is_open=True, conns=None, keys=None):
    clock = [0.0]
    conns = conns if conns is not None else [FakeConn()]
    made, invalidated = [], []
    keys = keys or ["ak-1", "ak-2", "ak-3"]

    def connect(url):
        if not conns:
            raise OSError("연결 거부")
        c = conns.pop(0)
        if isinstance(c, Exception):
            raise c
        made.append(c)
        return c

    hub = QuoteHub(
        approval=lambda: keys[len(invalidated)],
        invalidate=lambda: invalidated.append(1),
        connect=connect,
        now=lambda: NOW,
        clock=lambda: clock[0],
        is_open=lambda now: is_open,
        autostart=False,
    )
    return hub, clock, made, invalidated


def test_subscribes_requested_and_serves_ticks():
    hub, clock, made, _ = make_hub()
    assert hub.request(["005930", "000660"]) == {}  # 아직 틱 없음 → 호출부가 REST 로
    hub.step()
    conn = made[0]
    assert sorted(conn.subs()) == [("1", "000660"), ("1", "005930")]
    assert all(m["header"]["approval_key"] == "ak-1" for m in conn.sent)
    conn.inbox.append(frame(trade_values("005930", 71600)))
    hub.step()
    got = hub.request(["005930", "000660"])
    assert got == {
        "005930": {
            "price": "71600",
            "change_pct": 1.25,
            "asof": NOW.isoformat(),
            "stale": False,
            "source": "ws",
        }
    }
    assert hub.status()["connected"] and hub.status()["subscribed"] == 2


def test_lease_expiry_unsubscribes_and_drops_tick():
    hub, clock, made, _ = make_hub()
    hub.request(["005930", "000660"])
    hub.step()
    made[0].inbox.append(frame(trade_values("005930", 71600)))
    hub.step()
    clock[0] = 20.0
    hub.request(["005930"])  # 000660 은 화면에서 빠졌다
    clock[0] = 31.0
    hub.step()
    assert ("2", "000660") in made[0].subs()
    made[0].inbox.append(frame(trade_values("005930", 71700)))
    hub.step()
    assert hub.request(["005930", "000660"]).keys() == {"005930"}


def test_capacity_keeps_most_recent_40():
    hub, clock, made, _ = make_hub()
    old = [f"{i:06d}" for i in range(1, 11)]
    new = [f"{i:06d}" for i in range(100, 135)]
    hub.request(old)
    clock[0] = 1.0
    hub.request(new)
    hub.step()
    subscribed = {t for op, t in made[0].subs() if op == "1"}
    assert len(subscribed) == 40 and set(new) <= subscribed


def test_pingpong_echoed():
    hub, _, made, _ = make_hub()
    hub.request(["005930"])
    hub.step()
    ping = json.dumps({"header": {"tr_id": "PINGPONG", "datetime": "20261027100000"}})
    made[0].inbox.append(ping)
    hub.step()
    assert made[0].sent[-1] == json.loads(ping)


def test_approval_rejected_reconnects_with_new_key():
    hub, clock, made, invalidated = make_hub(conns=[FakeConn(), FakeConn()])
    hub.request(["005930"])
    hub.step()
    made[0].inbox.append(
        json.dumps(
            {
                "header": {"tr_id": "H0STCNT0", "tr_key": "005930"},
                "body": {"rt_cd": "1", "msg1": "invalid approval : NOT FOUND"},
            }
        )
    )
    hub.step()
    assert invalidated == [1] and made[0].closed and not hub.status()["connected"]
    clock[0] = 5.0
    hub.step()
    assert len(made) == 2 and made[1].sent[0]["header"]["approval_key"] == "ak-2"


def test_rejected_ticker_not_requested_again():
    hub, _, made, _ = make_hub()
    hub.request(["999999", "005930"])
    hub.step()
    made[0].inbox.append(
        json.dumps(
            {
                "header": {"tr_id": "H0STCNT0", "tr_key": "999999"},
                "body": {"rt_cd": "1", "msg1": "invalid tr_key"},
            }
        )
    )
    hub.step()
    hub.step()
    assert made[0].subs().count(("1", "999999")) == 1 and hub.status()["subscribed"] == 1


def test_connect_failure_backs_off_without_raising():
    hub, clock, made, _ = make_hub(conns=[OSError("x"), FakeConn()])
    hub.request(["005930"])
    hub.step()
    assert not made and not hub.status()["connected"]
    hub.step()  # 백오프 중이라 다시 시도하지 않는다
    assert not made
    clock[0] = 2.0
    hub.step()
    assert made and hub.status()["connected"]


def test_closed_market_never_connects():
    hub, _, made, _ = make_hub(is_open=False)
    assert hub.request(["005930"]) == {}
    hub.step()
    assert not made


def test_stale_tick_falls_back_to_rest():
    """체결이 멈추거나 끊김을 아직 모르면 틱이 낡는다 → 10초 넘으면 돌려주지 않아 REST 가 채운다."""
    hub, clock, made, _ = make_hub()
    hub.request(["005930"])
    hub.step()
    made[0].inbox.append(frame(trade_values("005930", 71600)))
    hub.step()
    clock[0] = 9.0
    assert "005930" in hub.request(["005930"])
    clock[0] = 11.0
    assert hub.request(["005930"]) == {}


def test_release_rejected_reconnects_to_resync():
    """해제가 거절되면 서버에 구독이 남아 41칸이 어긋난다 → 다시 접속해 새로 맞춘다."""
    hub, clock, made, _ = make_hub(conns=[FakeConn(), FakeConn()])
    hub.request(["005930", "000660"])
    hub.step()
    clock[0] = 31.0
    hub.request(["005930"])
    hub.step()  # 000660 해제 요청
    made[0].inbox.append(
        json.dumps(
            {
                "header": {"tr_id": "H0STCNT0", "tr_key": "000660"},
                "body": {"rt_cd": "1", "msg1": "unsubscribe error"},
            }
        )
    )
    hub.step()
    assert made[0].closed and not hub.status()["connected"]
    clock[0] = 40.0
    hub.request(["005930"])
    hub.step()
    assert made[1].subs() == [("1", "005930")]


def test_backoff_grows_when_connection_drops_right_away():
    class Dropping(FakeConn):
        def recv(self, timeout=None):
            raise ConnectionError("끊김")

    hub, clock, made, _ = make_hub(conns=[Dropping(), Dropping(), Dropping(), FakeConn()])
    hub.request(["005930"])
    retries = []
    for t in range(0, 20):
        clock[0] = float(t)
        hub.request(["005930"])
        before = len(made)
        hub.step()
        if len(made) > before:
            retries.append(t)
    assert retries[:4] == [0, 1, 3, 7]  # 1·2·4초로 늘어난다(접속만 되고 바로 끊겨도)


def test_thread_hands_off_request_arriving_while_exiting(monkeypatch):
    """유휴 종료를 정한 직후 들어온 요청은 새 스레드가 이어받는다."""
    import threading

    from trader.web import live_quotes

    hub, clock, made, _ = make_hub(conns=[FakeConn(), FakeConn()])
    hub._autostart = True
    started = []
    real_start = threading.Thread.start

    def start(self):
        started.append(self.name)
        real_start(self)

    monkeypatch.setattr(threading.Thread, "start", start)
    monkeypatch.setattr(live_quotes, "IDLE_S", 0.0)
    clock[0] = 0.0
    hub.request(["005930"])  # 스레드 시작 → 곧 유휴로 끝나지만 마지막 요청이 결정 뒤면 재시작
    for _ in range(50):
        if hub._thread is None:
            break
        threading.Event().wait(0.02)
    hub.stop()
    assert started and hub._thread is None


def test_stop_prevents_restart_and_closes():
    hub, clock, made, _ = make_hub()
    hub.request(["005930"])
    hub.step()
    hub.stop()
    assert made[0].closed and not hub.status()["connected"]
    hub._autostart = True
    hub.request(["005930"])
    assert hub._thread is None  # 내려간 앱에서 다시 띄우지 않는다
