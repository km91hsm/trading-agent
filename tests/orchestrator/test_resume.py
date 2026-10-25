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


def test_resume_stage_writes_round_specific_risk_and_order_checkpoints(conn):
    """재평가(31_decision_r<n>)의 판정·주문 체크포인트는 오늘 분석의 40_/50_ 을 덮어쓰지 않는다."""
    ctx, accounts, brokers = _prepare(conn)
    d = run_dir(D, "KR", "005930")
    before = (d / "40_risk_check_paper-main.json").read_text(encoding="utf-8")
    r = tools.submit_decision(
        ctx,
        ticker="005930",
        market="KR",
        date=D,
        stage="31_decision_r1",
        payload=ok_decision(action="SELL", target_weight=0.0),
    )
    assert r["ok"]
    stages = list_checkpoints(d)
    assert "41_risk_check_r1_paper-main" in stages
    assert (d / "40_risk_check_paper-main.json").read_text(encoding="utf-8") == before


def test_resume_uses_trigger_price_for_stop_loss_check_and_gate(conn):
    """재평가는 장중 트리거 가격이 현재가다. 아침 00_inputs 시세로 손절선을 검사하면 '현재가 아래'
    규칙을 지킨 트레이더의 결정(아침 가격보다 높은 손절선)이 거부된다."""
    ctx, accounts, brokers = _prepare(conn)  # 아침 시세 71500
    dec_id = repo.decision_id(conn, D, "KR", "005930")
    repo.insert_trigger(
        conn,
        market="KR",
        ticker="005930",
        kind="take_profit",
        detail={},
        price=Decimal("80000"),
        decision_id=dec_id,
        account_id="paper-main",
        created_at=NOW.isoformat(),
    )
    r = tools.submit_decision(
        ctx,
        ticker="005930",
        market="KR",
        date=D,
        stage="31_decision_r1",
        payload=ok_decision(action="HOLD", target_weight=0.0, stop_loss=75000.0),
    )
    assert r["ok"], r
    assert r.get("price") == "80000"


def test_trader_bundle_carries_previous_decision_in_resume(conn):
    """트리거 재평가의 트레이더 입력에는 원래 결정(30_decision·최신 31_)이 들어가야 한다.
    md 가 '원래 결정을 전제로' 라고 하는데 묶음에 없으면 트레이더가 아침 리포트로 다시 추측한다."""
    ctx, accounts, brokers = _prepare(conn)
    b = tools.checkpoint_bundle(ctx, ticker="005930", market="KR", date=D, kind="trader")
    assert b["ok"] and b["data"]["previous_decision"]["action"] == "BUY"
    tools.submit_decision(
        ctx,
        ticker="005930",
        market="KR",
        date=D,
        stage="31_decision_r1",
        payload=ok_decision(action="TRIM", target_weight=0.05),
    )
    b2 = tools.checkpoint_bundle(ctx, ticker="005930", market="KR", date=D, kind="trader")
    assert b2["data"]["previous_decision"]["action"] == "TRIM"
    assert b2["data"]["previous_decision_stage"] == "31_decision_r1"


def test_reevaluate_news_trigger_without_price_uses_inputs_quote(conn, monkeypatch):
    """Phase 23: 뉴스 트리거(kind=news, price 없음)도 재평가 세션이 돌고 31_decision_r1 을 남긴다.
    현재가는 트리거 가격이 아니라 00_inputs 시세다."""
    ctx, accounts, brokers = _prepare(conn)
    monkeypatch.setenv("FAKE_CLAUDE", "1")
    tid = repo.insert_trigger(
        conn,
        market="KR",
        ticker="005930",
        kind="news",
        detail={"news": [{"id": 1, "title": "삼성전자 수주"}], "count": 1},
        price=None,
        decision_id=1,
    )
    assert repo.latest_trigger_price(conn, "KR", "005930", D) is None
    res = resume.reevaluate(
        Settings(),
        conn,
        accounts,
        market="KR",
        date=D,
        ticker="005930",
        exchange=None,
        trigger_id=tid,
        trigger={"kind": "news", "price": None, "detail": {"count": 1}},
        brokers=brokers,
        now=lambda: NOW,
        dry_run=False,
    )
    assert res.ok and "31_decision_r1" in list_checkpoints(run_dir(D, "KR", "005930"))
    assert conn.execute("SELECT handled_at FROM triggers WHERE id=?", (tid,)).fetchone()[0]
