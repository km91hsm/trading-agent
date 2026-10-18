from datetime import UTC, date, datetime
from decimal import Decimal

from helpers_agents import ok_decision, ok_report, ok_turn

from trader.agents.schemas import Decision
from trader.broker.fake import FakeBroker
from trader.broker.models import Cash
from trader.config import AccountConfig, Settings
from trader.ledger import repo
from trader.orchestrator import resume, tools
from trader.orchestrator.run_store import list_checkpoints, run_dir

D = date(2026, 10, 9)
NOW = datetime(2026, 10, 9, 2, 0, tzinfo=UTC)


def _prepare(conn):
    accounts = [AccountConfig(id="paper-main", env="paper", cano="1", markets=["KR"], mode="auto")]
    cash = {"KR": Cash(currency="KRW", available=Decimal("10000000"), total=Decimal("10000000"))}
    brokers = {"paper-main": FakeBroker(accounts[0], cash=cash)}
    ctx = tools.ToolContext(
        settings=Settings(),
        accounts=accounts,
        conn=conn,
        now=lambda: NOW,
        brokers=brokers,
        dry_run=False,
    )
    from trader.orchestrator import run_store

    d = run_dir(D, "KR", "005930")
    run_store.write_checkpoint(
        d,
        "00_inputs",
        {
            "meta": {"ticker": "005930", "market": "KR", "asof": NOW.isoformat()},
            "technical": {"quote": {"data": {"price": "71500"}, "received_at": "", "error": None}},
            "fundamental": {},
            "news_macro": {},
            "sentiment": {},
            "errors": [],
        },
    )
    for role in ("technical", "fundamental"):
        tools.checkpoint_write(
            ctx,
            ticker="005930",
            market="KR",
            date=D,
            stage=f"10_analyst_{role}",
            payload=ok_report(role=role),
            agent="a",
        )
    for side in ("bull", "bear"):
        tools.checkpoint_write(
            ctx,
            ticker="005930",
            market="KR",
            date=D,
            stage=f"20_debate_r0_{side}",
            payload=ok_turn(side),
            agent="r",
        )
    tools.checkpoint_summarize(ctx, ticker="005930", market="KR", date=D)
    r = tools.submit_decision(ctx, ticker="005930", market="KR", date=D, payload=ok_decision())
    assert r["ok"] and len(brokers["paper-main"].orders) == 1  # 최초 BUY 14주
    return ctx, accounts, brokers


def test_submit_decision_resume_stage_executes_new_decision(conn):
    ctx, accounts, brokers = _prepare(conn)
    r = tools.submit_decision(
        ctx,
        ticker="005930",
        market="KR",
        date=D,
        stage="31_decision_r1",
        payload=ok_decision(action="SELL", target_weight=0.0),
    )
    assert r["ok"] and r["stage"] == "31_decision_r1"
    assert "31_decision_r1" in list_checkpoints(run_dir(D, "KR", "005930"))
    # 재평가 SELL 은 기존 BUY 주문이 있어도 나간다 (같은 방향만 중복 방지)
    assert len(brokers["paper-main"].orders) == 1  # 보유 없음 → SELL 은 거부(보유하지 않은 종목)
    ex = r["executions"][0]
    assert ex["approved"] is False and any("보유" in s for s in ex["reasons"])
    assert conn.execute("SELECT action FROM decisions").fetchone()["action"] == "SELL"
    bad = tools.submit_decision(
        ctx, ticker="005930", market="KR", date=D, stage="32_x", payload=ok_decision()
    )
    assert bad["ok"] is False


def test_resume_stage_requires_original_decision(conn, project):
    accounts = [AccountConfig(id="paper-main", env="paper", cano="1", markets=["KR"], mode="auto")]
    ctx = tools.ToolContext(settings=Settings(), accounts=accounts, conn=conn, now=lambda: NOW)
    r = tools.submit_decision(
        ctx, ticker="005930", market="KR", date=D, stage="31_decision_r1", payload=ok_decision()
    )
    assert r["ok"] is False and "30_decision" in r["error"]


def test_fake_resume_session_writes_31_decision(conn, monkeypatch):
    ctx, accounts, brokers = _prepare(conn)
    monkeypatch.setenv("FAKE_CLAUDE", "1")
    tid = repo.insert_trigger(
        conn,
        market="KR",
        ticker="005930",
        kind="take_profit",
        detail={"level": "80000"},
        price=Decimal("80000"),
        decision_id=1,
    )
    res = resume.reevaluate(
        Settings(),
        conn,
        accounts,
        market="KR",
        date=D,
        ticker="005930",
        exchange=None,
        trigger_id=tid,
        trigger={"kind": "take_profit", "price": "80000", "detail": {}},
        brokers=brokers,
        now=lambda: NOW,
        dry_run=False,
    )
    assert res.ok and "31_decision_r1" in list_checkpoints(run_dir(D, "KR", "005930"))
    assert (
        conn.execute("SELECT handled_at, session_id FROM triggers").fetchone()["session_id"]
        == "fake"
    )
    res2 = resume.reevaluate(
        Settings(),
        conn,
        accounts,
        market="KR",
        date=D,
        ticker="005930",
        exchange=None,
        trigger_id=tid,
        trigger={"kind": "take_profit", "price": "80000", "detail": {}},
        brokers=brokers,
        now=lambda: NOW,
        dry_run=False,
    )
    assert res2.ok and "31_decision_r2" in list_checkpoints(run_dir(D, "KR", "005930"))


def test_decision_schema_unchanged_for_resume():
    assert Decision(**ok_decision()).action == "BUY"
