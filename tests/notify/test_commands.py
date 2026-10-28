"""텔레그램 제어 명령 (/pause /resume /mode /watch): 실전 auto 는 거부, 모드는 control 테이블."""

from conftest import make_account

from trader.config import Settings
from trader.control import control
from trader.notify import commands
from trader.notify.telegram import Update, parse_command
from trader.orchestrator.executor import ExecContext


class Sent:
    def __init__(self):
        self.msgs: list[str] = []

    def send(self, text, *, escape=True):
        self.msgs.append(text)
        return True


class Recv:
    def __init__(self, texts):
        self.texts = texts

    def poll(self, offset=None):
        return [Update(update_id=i + 1, text=t, chat_id="1") for i, t in enumerate(self.texts)]


def _ctx(conn, sent):
    accounts = [
        make_account("paper-main", "paper", "auto"),
        make_account("real-main", "real", "off"),
    ]
    return ExecContext(settings=Settings(), accounts=accounts, conn=conn, notifier=sent)


def test_parse_multiword_commands():
    assert parse_command("/mode paper-main approve") == ("mode", "paper-main approve")
    assert parse_command("/watch add 000660") == ("watch", "add 000660")
    assert parse_command("/pause") == ("pause", None)
    assert parse_command("/resume@bot") == ("resume", None)
    assert parse_command("hello") is None


def test_telegram_pause_resume_mode_watch(conn):
    sent = Sent()
    ctx = _ctx(conn, sent)
    r = commands.poll_once(ctx, Recv(["/pause"]))
    assert r["handled"] == 1 and control.is_paused(conn)
    commands.poll_once(ctx, Recv(["/resume"]))
    assert not control.is_paused(conn)
    commands.poll_once(ctx, Recv(["/mode paper-main approve"]))
    assert control.get_account_mode(conn, ctx.accounts[0]) == "approve"
    commands.poll_once(ctx, Recv(["/mode nope auto", "/mode paper-main fast"]))
    assert any("사용법" in m or "알 수 없는" in m for m in sent.msgs[-2:])
    commands.poll_once(ctx, Recv(["/watch add 000660", "/watch add AAPL", "/watch add 000660"]))
    assert control.get_watchlist(conn) == ["000660", "AAPL"]
    commands.poll_once(ctx, Recv(["/watch remove 000660", "/watch list"]))
    assert control.get_watchlist(conn) == ["AAPL"]
    assert "AAPL" in sent.msgs[-1]
    commands.poll_once(ctx, Recv(["/watch add bad/ticker"]))
    assert control.get_watchlist(conn) == ["AAPL"]


def test_real_auto_via_telegram_rejected(conn):
    sent = Sent()
    ctx = _ctx(conn, sent)
    commands.poll_once(ctx, Recv(["/mode real-main auto"]))
    assert control.get_account_mode(conn, ctx.accounts[1]) == "off"
    assert "거부" in sent.msgs[-1] and "control mode" in sent.msgs[-1]
    commands.poll_once(ctx, Recv(["/mode real-main approve"]))
    assert control.get_account_mode(conn, ctx.accounts[1]) == "off"
    assert "거부" in sent.msgs[-1]
    control.set_account_mode(conn, ctx.accounts[1], "approve")
    commands.poll_once(ctx, Recv(["/mode real-main off"]))  # 비상 정지(하향)만 허용
    assert control.get_account_mode(conn, ctx.accounts[1]) == "off"


def test_poll_once_advances_offset_past_filtered_updates(conn):
    class FilteredRecv(Recv):
        last_update_id = 50  # 원본 결과엔 50 까지 있었지만 전부 다른 채팅이라 걸러짐

    sent = Sent()
    ctx = _ctx(conn, sent)
    r = commands.poll_once(ctx, FilteredRecv([]))
    assert r["received"] == 0
    assert control.get_flag(conn, "telegram_offset") == 51

    # 유지된 업데이트가 더 뒤면 그것이 우선
    class MixedRecv(Recv):
        last_update_id = 1

    commands.poll_once(ctx, MixedRecv(["/status", "/status"]))  # update_id 1, 2
    assert control.get_flag(conn, "telegram_offset") == 3


def test_status_shows_effective_mode(conn):
    """/status 의 계좌 모드는 executor.effective_mode (실전 auto + 확인 기록 없음 = off)."""
    sent = Sent()
    ctx = _ctx(conn, sent)
    control.set_flag(conn, "mode:real-main", "auto")  # 확인 문자열 없이 직접 쓴 값
    commands.poll_once(ctx, Recv(["/status"]))
    msg = sent.msgs[-1]
    assert "real-main(실전) 끔" in msg and "강등" in msg and "real-main(실전) 자동" not in msg


def test_status_includes_daemon_line(conn, project):
    from datetime import UTC, datetime

    from trader import heartbeat

    sent = Sent()
    ctx = _ctx(conn, sent)
    commands.poll_once(ctx, Recv(["/status"]))
    assert "데몬 기록 없음" in sent.msgs[-1]
    hb = heartbeat.Heartbeat(project / "state" / heartbeat.FILE_NAME)
    hb.start(pid=1, started_at=datetime.now(UTC), jobs=[])
    hb.job_started("kr_monitor", datetime.now(UTC))
    hb.job_finished("kr_monitor", datetime.now(UTC), "ok")
    commands.poll_once(ctx, Recv(["/status"]))
    assert "최근 잡: kr_monitor ok" in sent.msgs[-1]
    assert "국면: KR -" in sent.msgs[-1]
    assert "오늘 스캔:\n- KR " in sent.msgs[-1] and "아직 없음" in sent.msgs[-1]


def test_command_failure_does_not_block_others_or_offset(conn, monkeypatch):
    """명령 하나가 예외로 죽어도 다음 명령은 처리되고 오프셋은 전진한다(영원한 재시도 방지)."""
    sent = Sent()
    ctx = _ctx(conn, sent)

    def boom(*a, **kw):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(commands.executor, "approve", boom)
    r = commands.poll_once(ctx, Recv(["/approve 7", "/pause"]))
    assert r["handled"] == 2 and r["failed"] == 1
    assert control.is_paused(conn)  # 두 번째 명령은 처리됐다
    assert control.get_flag(conn, "telegram_offset") == 3
    assert any("[명령 실패]" in m and "/approve 7" in m for m in sent.msgs)


def test_telegram_run_enqueues_when_daemon_alive(conn, project):
    """Phase 21: `/run <kind> [market]` 는 데몬이 살아 있을 때만 대기열에 넣는다."""
    import json
    import os
    from datetime import UTC, datetime

    from trader import heartbeat, jobs

    sent = Sent()
    ctx = _ctx(conn, sent)
    commands.poll_once(ctx, Recv(["/run"]))
    assert "사용법" in sent.msgs[-1] and "scan_analyze" in sent.msgs[-1]
    commands.poll_once(ctx, Recv(["/run scan_analyze KR"]))
    assert "데몬이 꺼져" in sent.msgs[-1] and jobs.active_count(conn) == 0
    # 데몬 살아 있음: lock(pid) + heartbeat
    (project / "state" / heartbeat.LOCK_NAME).write_text(
        json.dumps({"pid": os.getpid()}), encoding="utf-8"
    )
    hb = heartbeat.Heartbeat(project / "state" / heartbeat.FILE_NAME)
    hb.start(pid=os.getpid(), started_at=datetime.now(UTC), jobs=[])
    commands.poll_once(ctx, Recv(["/run scan_analyze KR"]))
    assert sent.msgs[-1].startswith("[요청 #") and "KR 스캔 → 분석" in sent.msgs[-1]
    assert jobs.active_count(conn) == 1
    commands.poll_once(ctx, Recv(["/run scan_analyze KR"]))
    assert "이미 대기" in sent.msgs[-1]
    commands.poll_once(ctx, Recv(["/run nope", "/run analyze_ticker 005930"]))
    assert "알 수 없는 작업" in sent.msgs[-2] and "파라미터 오류" in sent.msgs[-1]
    commands.poll_once(ctx, Recv(["/run analyze_ticker KR 005930"]))
    assert "종목 분석 005930 (모의)" in sent.msgs[-1]


def test_real_approve_requires_confirm_word_and_unknown_command_gets_help(conn, monkeypatch):
    """전체 검토 T2: 실전 계좌 승인은 '/approve <id> 실전' 이어야 실행, 모르는 /명령은 안내."""
    from trader.ledger import repo
    from trader.orchestrator import executor

    sent = Sent()
    ctx = _ctx(conn, sent)
    aid = repo.create_approval(
        conn,
        decision_id=0,
        account_id="real-main",
        order={
            "ticker": "005930",
            "market": "KR",
            "side": "BUY",
            "qty": 1,
            "order_type": "LIMIT",
            "price": "70000",
            "exchange": None,
        },
        requested_at="2026-10-26T00:00:00+00:00",
        expires_at="2026-10-26T00:30:00+00:00",
    )
    calls = []
    monkeypatch.setattr(executor, "approve", lambda c, i: calls.append(i) or {"ok": True})
    commands.poll_once(ctx, Recv([f"/approve {aid}"]))
    assert calls == [] and f"/approve {aid} 실전" in sent.msgs[-1]
    commands.poll_once(ctx, Recv([f"/approve {aid} 실전"]))
    assert calls == [aid]
    commands.poll_once(ctx, Recv(["/aprove 1"]))
    assert sent.msgs[-1].startswith("[명령 안내]") and "/status" in sent.msgs[-1]


def test_telegram_kill_engages_only(conn):
    from trader.risk import killswitch

    sent = Sent()
    ctx = _ctx(conn, sent)
    commands.poll_once(ctx, Recv(["/kill"]))
    assert killswitch.reason(conn) == "텔레그램 /kill"
    assert sent.msgs[-1].startswith("[킬스위치]") and "control killswitch off" in sent.msgs[-1]


def test_telegram_daily_without_snapshot(conn):
    sent = Sent()
    ctx = _ctx(conn, sent)
    commands.poll_once(ctx, Recv(["/daily"]))
    assert sent.msgs[-1].startswith("[일일 리포트]") and "스냅샷" in sent.msgs[-1]


def test_untrusted_sender_can_only_read(conn):
    """2-T2: 그룹 채팅(허용 목록 없음)·허용 목록 밖 구성원은 조회(/status·/daily)만 —
    /pause·/kill 도 거부한다(킬스위치 해제는 CLI 로만 되므로 구성원 한 명의 실수가 매매
    중단으로 이어졌다)."""
    from trader.control import control
    from trader.notify.commands import poll_once
    from trader.notify.telegram import Update
    from trader.risk import killswitch

    sent = Sent()
    cctx = _ctx(conn, sent)

    class R:
        last_update_id = None

        def __init__(self, text):
            self.text = text

        def poll(self, offset=None):
            return [Update(update_id=1, text=self.text, chat_id="-100", trusted=False)]

    for cmd in ("/resume", "/pause", "/kill"):
        poll_once(cctx, R(cmd))
        assert sent.msgs[-1].startswith("[명령 거부]"), cmd
    assert not control.is_paused(cctx.conn)
    assert killswitch.reason(cctx.conn) is None
    poll_once(cctx, R("/status"))
    assert not sent.msgs[-1].startswith("[명령 거부]")


def test_unknown_command_help_at_most_once_per_minute(conn):
    """2-T2: 모르는 /xxx 마다 도움말을 보내지 않는다 — 분당 1회."""
    from datetime import UTC, datetime, timedelta

    sent = Sent()
    ctx = _ctx(conn, sent)
    t = [datetime(2026, 10, 28, 1, 0, tzinfo=UTC)]
    ctx.now = lambda: t[0]
    commands.poll_once(ctx, Recv(["/aprove 1", "/foo"]))
    assert sum(m.startswith("[명령 안내]") for m in sent.msgs) == 1
    t[0] += timedelta(seconds=61)
    commands.poll_once(ctx, Recv(["/bar"]))
    assert sum(m.startswith("[명령 안내]") for m in sent.msgs) == 2


def _fo(id: str, env: str, mode: str):
    from trader.config import AccountConfig

    return AccountConfig(id=id, env=env, cano="00000000", acnt_prdt_cd="03", markets=["KR"],
                         mode=mode, asset="fo")  # fmt: skip


def test_mode_and_status_reach_fo_accounts(conn):
    """/mode·/status 는 계좌를 가리지 않는 제어 — `control_accounts`(현물 + 파생)를 본다.
    실전 파생은 코드상 항상 off 라 설정이 approve 여도 끔으로 보인다."""
    import dataclasses

    sent = Sent()
    base = _ctx(conn, sent)
    fo = [_fo("paper-fo", "paper", "auto"), _fo("real-fo", "real", "approve")]
    ctx = dataclasses.replace(base, control_accounts=[*base.accounts, *fo])
    commands.poll_once(ctx, Recv(["/mode paper-fo off"]))
    assert control.get_account_mode(conn, fo[0]) == "off"
    assert "알 수 없는" not in sent.msgs[-1]
    commands.poll_once(ctx, Recv(["/status"]))
    msg = sent.msgs[-1]
    assert "paper-fo(모의) 끔" in msg
    assert "real-fo(실전) 끔" in msg and "real-fo(실전) 승인" not in msg


def test_account_mode_real_fo_is_always_off(conn):
    from trader.orchestrator import executor

    assert executor.account_mode(conn, _fo("real-fo", "real", "approve"))[0] == "off"
    assert executor.account_mode(conn, _fo("paper-fo", "paper", "auto"))[0] == "auto"
    assert executor.account_mode(conn, make_account("real-main", "real", "approve"))[0] == "approve"
