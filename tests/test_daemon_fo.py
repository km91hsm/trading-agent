"""Phase 26 Task 4.4: 데몬·대기열·텔레그램·STATUS 의 파생 연결."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from fo.test_executor import ACCT, _held_position, balance, make_ctx
from fo.test_executor import account as fo_account
from test_daemon import SCHED, _deps

from trader import daemon
from trader import jobs as jobq
from trader.broker.fake import FakeFoBroker, FakeMarket
from trader.config import KST, Settings
from trader.control import control
from trader.notify.telegram import parse_command


def at(h, m=0, d=27):  # 2026-10-27 화요일
    return datetime(2026, 10, d, h, m, tzinfo=KST).astimezone(UTC)


def _fo_deps(conn, now):
    deps = _deps(conn, FakeMarket(), now=lambda: now)
    acct = fo_account()
    deps.fo_accounts = [acct]
    deps.fo_brokers = {acct.id: FakeFoBroker(acct, balance=balance(), now=lambda: now)}
    return deps


def test_build_jobs_has_fo_session_and_monitor():
    jobs = {j.id: j for j in daemon.build_jobs(SCHED, Settings(), markets=["KR"], fo=True)}
    assert jobs["kr_fo_session"].cron["hour"] == 8 and jobs["kr_fo_session"].cron["minute"] == 20
    assert jobs["kr_fo_monitor"].interval_s == 60
    plain = {j.id for j in daemon.build_jobs(SCHED, Settings(), markets=["KR"])}
    assert "kr_fo_session" not in plain and "kr_fo_monitor" not in plain


def test_fo_monitor_window_0845_1545_not_stock_window(conn):
    for now, fo_open, stock_open in (
        (at(8, 50), True, False), (at(15, 40), True, False), (at(15, 50), False, False),
        (at(8, 40), False, False), (at(10, 0), True, True),
    ):  # fmt: skip
        deps = _fo_deps(conn, now)
        assert daemon._fo_window(deps) is fo_open, now
        assert daemon._in_market_window(deps, "KR") is stock_open, now


def test_fo_monitor_job_skips_without_accounts_or_outside_window(conn):
    deps = _deps(conn, FakeMarket(), now=lambda: at(10, 0))
    assert daemon._fo_monitor_job(deps) == {"skipped": "파생 계좌 없음"}
    deps = _fo_deps(conn, at(16, 0))
    assert daemon._fo_monitor_job(deps) == {"skipped": "파생 창 밖"}


def test_auto_gate_fo_kinds_use_fo_clock(conn):
    req = {"kind": "fo_reevaluate", "source": "daemon", "params": {}}
    assert daemon._auto_gate(_fo_deps(conn, at(15, 40)), req) is None
    assert daemon._auto_gate(_fo_deps(conn, at(15, 50)), req) == "파생 장 밖"
    stock = {"kind": "reevaluate", "source": "daemon", "params": {"market": "KR"}}
    assert daemon._auto_gate(_fo_deps(conn, at(15, 40)), stock) == "장 마감"


def test_queue_fo_reevaluate_fast_lane_priority(conn):
    assert "fo_reevaluate" in jobq.REEVAL_KINDS and "fo_session" in jobq.SLOW_KINDS
    assert "fo_close" in jobq.FAST_KINDS
    params = {"account_id": ACCT, "position_id": 1, "expected_version": 0, "request_id": "abc",
              "round": 1, "trigger_id": 1, "date": "2026-10-27"}  # fmt: skip
    rid = jobq.enqueue(conn, "fo_reevaluate", params, source="daemon")
    row = conn.execute("SELECT priority FROM job_requests WHERE id = ?", (rid,)).fetchone()
    assert row["priority"] == 10


def test_daemon_deps_account_lists_by_consumer(conn, monkeypatch):
    from trader.fo import engine
    from trader.orchestrator import balance as bal_mod

    deps = _fo_deps(conn, at(10, 0))
    seen: dict = {}

    def spy_refresh(conn_, *, accounts, brokers, market, **kw):
        seen.setdefault("balance", []).append(sorted(a.id for a in accounts))
        seen["brokers"] = sorted(brokers)
        return {}

    monkeypatch.setattr(bal_mod, "refresh", spy_refresh)
    daemon._balance_body(deps, force=True)
    assert seen["balance"][0] == ["paper-fo", "paper-main"]
    assert seen["brokers"] == ["paper-fo", "paper-main"]

    def spy_tick(ctx, **kw):
        seen["fo_tick"] = [a.id for a in ctx.accounts]
        seen["spot"] = [a.id for a in ctx.spot_accounts]
        return {"accounts": {}}

    monkeypatch.setattr(engine, "tick", spy_tick)
    daemon._fo_monitor_body(deps)
    assert seen["fo_tick"] == ["paper-fo"] and seen["spot"] == ["paper-main"]
    assert [a.id for a in deps.accounts] == ["paper-main"]  # 현물 잡 목록은 그대로


def test_fo_close_rechecks_mode_and_session(conn):
    deps = _fo_deps(conn, at(16, 0))
    assert daemon._fo_close_request(deps, {"account_id": ACCT, "position_id": 1}) == {
        "skipped": "파생 장 밖"
    }
    deps = _fo_deps(conn, at(10, 0))
    ctx = make_ctx(conn, now=at(10, 0))
    pos = _held_position(conn, ctx, qty=2)
    deps.fo_brokers = ctx.brokers
    control.set_flag(conn, "paused", True)
    out = daemon._fo_close_request(deps, {"account_id": ACCT, "position_id": pos.id})
    assert out["status"] == "recorded" and ctx.brokers[ACCT].orders == []
    control.set_flag(conn, "paused", False)
    out = daemon._fo_close_request(deps, {"account_id": ACCT, "position_id": pos.id})
    assert out["status"] == "accepted" and ctx.brokers[ACCT].orders[0].side == "SELL"


def test_telegram_fo_status_line(conn):
    from trader.notify import commands

    assert parse_command("/fo") == ("fo", None)
    sent: list[str] = []
    ectx = SimpleNamespace(notifier=SimpleNamespace(send=lambda t: sent.append(t) or True),
                           now=lambda: at(10, 0))  # fmt: skip
    ctx = make_ctx(conn, now=at(10, 0))
    _held_position(conn, ctx, qty=2)
    commands._handle_command(ectx, conn, ("fo", None), lambda s: None)
    assert sent and "105W11" in sent[0] and "2계약" in sent[0]
    assert "fo" in commands.UNTRUSTED_OK and "/fo" in commands.HELP_LINE


def test_status_fo_line(conn, project):
    from trader.status import build_status

    ctx = make_ctx(conn, now=at(10, 0))
    _held_position(conn, ctx, qty=2)
    text = build_status(conn, Settings(), [], project / "docs" / "TODO.md", now=at(10, 0))
    assert "## 파생(선물옵션)" in text and "105W11" in text
