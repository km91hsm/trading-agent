"""체결통보 리스너(Stock D-060): HTS ID 로 국내·해외 통보 구독, 통보 프레임 → 체결 동기화 요청(5초에
한 번), 핑퐁, HTS ID 거절이면 멈춤, 접속키 거절이면 새 키, 접속 실패 백오프."""

import json

from trader import daemon, jobs
from trader.orchestrator.notice import NoticeListener


class Conn:
    def __init__(self, inbox=()):
        self.sent, self.inbox, self.closed = [], list(inbox), False

    def send(self, t):
        self.sent.append(t)

    def recv(self, timeout=None):
        if self.inbox:
            return self.inbox.pop(0)
        raise TimeoutError

    def close(self):
        self.closed = True


def _listener(conns, *, env="paper"):
    clock = [0.0]
    fired, invalid = [], []
    made = []

    def connect(url):
        c = conns.pop(0)
        if isinstance(c, Exception):
            raise c
        made.append((url, c))
        return c

    lst = NoticeListener(
        env=env,
        hts_id="myhts",
        approval=lambda: "ak",
        invalidate=lambda: invalid.append(1),
        on_notice=fired.append,
        connect=connect,
        clock=lambda: clock[0],
    )
    return lst, clock, fired, invalid, made


def test_subscribes_both_notice_trs_and_fires_debounced():
    c = Conn(["1|H0STCNI9|001|ENCRYPTED", "1|H0STCNI9|001|ENCRYPTED2"])
    lst, clock, fired, _, made = _listener([c])
    lst.step()
    assert made[0][0].endswith(":31000")  # 모의 웹소켓
    subs = [json.loads(m)["body"]["input"] for m in c.sent]
    assert subs == [
        {"tr_id": "H0STCNI9", "tr_key": "myhts"},
        {"tr_id": "H0GSCNI9", "tr_key": "myhts"},
    ]
    lst.step()
    lst.step()  # 같은 5초 안 두 번째 통보 → 동기화 요청은 한 번
    assert fired == ["paper"] and lst.notices == 2
    c.inbox.append("1|H0GSCNI9|001|X")
    clock[0] = 6.0
    lst.step()
    assert fired == ["paper", "paper"]


def test_pingpong_and_hts_rejection_stops():
    ping = json.dumps({"header": {"tr_id": "PINGPONG"}})
    bad = json.dumps(
        {
            "header": {"tr_id": "H0STCNI0", "tr_key": "x"},
            "body": {"rt_cd": "1", "msg1": "invalid htsid"},
        }
    )
    c = Conn([ping, bad])
    lst, _, fired, _, made = _listener([c], env="real")
    lst.step()
    lst.step()
    assert c.sent[-1] == ping and made[0][0].endswith(":21000")
    lst.step()
    assert lst.rejected == "invalid htsid" and c.closed and not lst.step()


def test_approval_rejected_and_connect_backoff():
    bad = json.dumps({"header": {"tr_id": "H0STCNI9"}, "body": {"rt_cd": "1", "msg1": "NOT FOUND"}})
    lst, clock, _, invalid, made = _listener([OSError("x"), Conn([bad]), Conn()])
    assert not lst.step() and not made  # 실패 → 1초 백오프
    assert not lst.step()
    clock[0] = 1.5
    lst.step()  # 접속
    lst.step()  # 접속키 거절 → 무효화
    assert invalid == [1] and lst.status()["connected"] is False


def test_daemon_handler_enqueues_fills_sync_once(conn):
    h = daemon.notice_handler(lambda: __import__("trader.ledger.db", fromlist=["db"]).connect())
    h("paper")
    h("paper")  # 이미 대기 중이면 그것으로 충분
    rows = conn.execute("SELECT kind, source FROM job_requests").fetchall()
    assert [(r["kind"], r["source"]) for r in rows] == [("fills_sync", "notice:paper")]
    assert "fills_sync" in jobs.FAST_KINDS


def test_trailing_notice_is_not_lost_and_running_sync_retries():
    """디바운스로 미룬 마지막 통보도 간격이 지나면 동기화하고, 동기화가 실행 중이면 다시 요청."""
    c = Conn(["1|H0STCNI9|001|A"])
    lst, clock, fired, _, _ = _listener([c])
    answers = [True, False, True]
    lst._on_notice = lambda env: fired.append(env) or answers.pop(0)
    lst.step()  # 접속
    lst.step()  # 첫 통보 → 즉시
    clock[0] = 4.0
    c.inbox.append("1|H0STCNI9|001|B")
    lst.step()  # 5초 안 → 미룸
    assert len(fired) == 1
    clock[0] = 5.5
    lst.step()  # 수신 없음(타임아웃)이어도 미룬 것을 보낸다 — 실행 중(False) 이라 남김
    clock[0] = 11.0
    lst.step()
    assert len(fired) == 3 and not lst._pending


def test_transient_error_reconnects_ping_failure_and_ack_catches_up():
    err = json.dumps(
        {"header": {"tr_id": "H0STCNI9"}, "body": {"rt_cd": "1", "msg1": "SERVER BUSY"}}
    )
    ok = json.dumps(
        {"header": {"tr_id": "H0STCNI9"}, "body": {"rt_cd": "0", "msg1": "SUBSCRIBE SUCCESS"}}
    )

    class PingFail(Conn):
        def send(self, t):
            if "PINGPONG" in t:
                raise ConnectionError("x")
            super().send(t)

    ping = json.dumps({"header": {"tr_id": "PINGPONG"}})
    lst, clock, fired, _, made = _listener([Conn([err]), PingFail([ping]), Conn([ok])])
    lst.step()
    lst.step()  # 일시 오류 → 영구 거절 아님, 다시 접속
    assert lst.rejected is None and not lst.status()["connected"]
    clock[0] = 2.0
    lst.step()
    lst.step()  # 핑 응답 실패 → 끊김으로 처리(스레드 유지)
    assert not lst.status()["connected"]
    clock[0] = 10.0
    lst.step()
    lst.step()  # 구독 성공 응답 → 끊긴 동안 체결 보충 동기화
    assert fired == ["paper"]


def test_subscribe_send_failure_closes_socket():
    class SendFail(Conn):
        def send(self, t):
            raise ConnectionError("x")

    c = SendFail()
    lst, _, _, _, _ = _listener([c])
    assert not lst.step() and c.closed


def test_real_listener_connects_only_when_enabled_and_yields_session():
    """Codex(PR #46): KIS 는 앱키당 웹소켓 세션 하나 — 실전 리스너는 실전 계좌가 주문을 낼 수
    있을 때만 붙고, 그동안만 holds_session() 이 참(같은 프로세스 시세 허브가 비켜선다)."""
    from trader.orchestrator import notice

    on = [False]
    c = Conn()
    clock = [0.0]
    made = []
    lst = NoticeListener(
        env="real",
        hts_id="myhts",
        approval=lambda: "ak",
        invalidate=lambda: None,
        on_notice=lambda env: True,
        connect=lambda url: made.append(c) or c,
        clock=lambda: clock[0],
        enabled=lambda: on[0],
    )
    assert lst.step() is False and made == [] and lst.holds_session() is False
    on[0] = True
    lst.step()  # 캐시(ENABLED_TTL_S) 안이라 아직 끔
    assert made == []
    clock[0] += notice.ENABLED_TTL_S
    lst.step()
    assert made == [c] and lst.holds_session() is True
    on[0] = False
    clock[0] += notice.ENABLED_TTL_S
    lst.step()
    assert c.closed and lst.holds_session() is False


def test_appkey_in_use_backs_off_instead_of_ignoring():
    busy = json.dumps(
        {"header": {"tr_id": "H0STCNI0"}, "body": {"rt_cd": "1", "msg1": "ALREADY IN USE appkey"}}
    )
    c1, c2 = Conn([busy]), Conn()
    lst, clock, _, _, made = _listener([c1, c2], env="real")
    lst.step()  # 접속·구독
    lst.step()  # 사용 중 → 끊고 백오프
    assert c1.closed and lst.rejected is None
    lst.step()
    assert len(made) == 1  # 백오프 동안 다시 붙지 않는다
    clock[0] += 2
    lst.step()
    assert len(made) == 2
