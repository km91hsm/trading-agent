"""운영 점검(Phase 23 A): 영역별 단계·머리 단계·격상 알림·헤드라인."""

from datetime import UTC, datetime

from conftest import make_account

from trader import daemon
from trader.config import Settings
from trader.control import control
from trader.ops import health

NOW = datetime(2026, 10, 23, 8, 30, tzinfo=UTC)  # 17:30 KST 금요일 (대조 유예 뒤)
ACCTS = [make_account("paper-main", "paper", "auto", ["KR"])]


def test_check_ok_when_ledger_empty_except_daemon_dead(conn, project):
    r = health.check(conn, Settings(), ACCTS, now=NOW, state=project / "state")
    keys = {i["key"] for i in r["items"]}
    # 데몬 lock 이 없으니 critical, 대조도 아직 안 돌았으니 watch
    assert r["level"] == "critical" and "jobs.daemon" in keys
    assert "reconcile.paper-main.KR" in keys and r["areas"]["reconcile"] == "watch"
    assert r["areas"]["loss"] == "ok" and r["areas"]["llm"] == "ok"
    assert all(a["link"] for a in r["actions"])


def test_orders_llm_and_reconcile_items(conn, project):
    conn.execute(
        "INSERT INTO orders(account_id, ticker, market, side, qty, order_type, status, mode, "
        "created_at) VALUES ('paper-main','005930','KR','BUY',1,'LIMIT','unknown','auto',?)",
        (NOW.isoformat(),),
    )
    conn.execute(
        "INSERT INTO orders(account_id, ticker, market, side, qty, order_type, status, mode, "
        "created_at) VALUES ('paper-main','000660','KR','BUY',1,'LIMIT','accepted','auto',"
        "'2026-10-22T01:00:00+00:00')"
    )
    for i in range(4):
        conn.execute(
            "INSERT INTO agent_calls(agent, model, status, created_at) VALUES "
            "('orchestrator','sonnet',?,?)",
            ("error" if i < 3 else "ok", NOW.isoformat()),
        )
    control.set_flag(
        conn,
        "reconcile:paper-main:KR:2026-10-23",
        {"ok": False, "mismatches": [{"ticker": "x"}], "open_orders": []},
    )
    control.set_flag(conn, "fill_ambiguous:KR:2026-10-23", {"candidates": 2})
    conn.commit()
    r = health.check(conn, Settings(), ACCTS, now=NOW, state=project / "state")
    by = {i["key"]: i for i in r["items"]}
    assert by["orders.unknown"]["level"] == "warning" and by["orders.unknown"]["count"] == 1
    assert by["orders.stale"]["count"] == 1
    assert by["orders.fill_ambiguous.KR"]["level"] == "warning"
    assert by["reconcile.paper-main.KR"]["level"] == "warning"
    assert by["llm.errors"]["level"] == "critical" and r["areas"]["llm"] == "critical"


def test_escalate_only_when_worse_and_headline(conn, project):
    r_ok = {"level": "ok", "label": "정상", "at": NOW.isoformat(), "items": []}
    assert health.escalate(conn, r_ok) is None
    r_warn = {
        "level": "warning",
        "label": "주의",
        "at": NOW.isoformat(),
        "items": [{"text": "unknown 1건"}],
    }
    msg = health.escalate(conn, r_warn)
    assert msg and msg.startswith("[점검 격상] 정상 → 주의") and "unknown 1건" in msg
    assert health.escalate(conn, r_warn) is None  # 같은 단계면 다시 알리지 않는다
    assert health.escalate(conn, r_ok) is None  # 좋아지면 기록만
    assert control.get_flag(conn, health.LEVEL_FLAG)["level"] == "ok"
    assert health.headline(r_ok) == "점검 정상 · 17:30"
    assert health.headline(r_warn).startswith("점검 주의 · 1건")
    assert health.headline(None) == "점검 기록 없음"


def test_daemon_ops_job_registered_and_body_records_result(conn, project):
    from test_daemon import SCHED, _deps

    from trader.broker.fake import FakeMarket

    jobs = {j.id: j for j in daemon.build_jobs(SCHED, Settings(), markets=["KR"])}
    assert jobs["ops_check"].interval_s == 1800
    deps = _deps(conn, FakeMarket(), now=lambda: NOW)
    sent = []
    deps.notifier = type("N", (), {"send": lambda self, t, **k: sent.append(t) or True})()
    out = daemon._ops_body(deps)
    assert out["level"] == "critical" and out["notified"] is True
    assert control.get_flag(conn, health.RESULT_FLAG)["level"] == "critical"
    assert sent and sent[0].startswith("[점검 격상]")
    assert daemon._ops_body(deps)["notified"] is False
