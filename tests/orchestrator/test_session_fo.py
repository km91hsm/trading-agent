"""Phase 26 단계 3: 파생 세션 종류(fo·fo_resume) — stdin 봉투·run_kind·상한 예약·에이전트 선택."""

import json
from datetime import date

import pytest
from helpers_sources import T0

from trader.agents import registry
from trader.orchestrator import session, tools
from trader.orchestrator.session import SessionSpec

D = date(2026, 10, 27)


def test_stdin_payload_fo_resume_has_position():
    pos = {"account_id": "paper-fo", "position_id": 3, "expected_version": 2, "request_id": "r1"}
    spec = SessionSpec(
        market="KR", date=D, tickers=[], kind="fo_resume", position=pos,
        trigger={"kind": "move", "detail": {}},
    )  # fmt: skip
    p = json.loads(session._stdin_payload(spec, T0))
    assert p["kind"] == "fo_resume" and p["position"] == pos and p["trigger"]["kind"] == "move"
    spec2 = SessionSpec(market="KR", date=D, tickers=[], kind="fo")
    p2 = json.loads(session._stdin_payload(spec2, T0))
    assert "position" not in p2 and p2["tickers"] == []


def test_run_kind_mapping():
    assert session.run_kind_of("fo") == "fo"
    assert session.run_kind_of("fo_resume") == "monitor"
    assert session.run_kind_of("resume") == "monitor"
    assert session.run_kind_of("analyze") == "analyze"
    assert session.agents_for("fo") == registry.FO_AGENTS
    assert session.agents_for("fo_resume") == registry.FO_AGENTS
    assert session.agents_for("analyze") == registry.AGENTS


def test_fo_caps_reserved_in_one_transaction(conn):
    """fo 상한은 일일 상한과 같은 쓰기 트랜잭션에서 검사한다. `runs.kind='fo'` 인 오늘 세션
    (실패 포함)이 상한에 닿으면 PipelineError 이고 행은 남지 않는다."""
    from trader.ledger import db

    spec = SessionSpec(market="KR", date=D, tickers=[], kind="fo")
    for i in range(2):
        run_id = db.start_run(conn, "fo", "KR")
        cid = session._start_call(
            conn, run_id, spec, session_id=f"s{i}", model="sonnet", cap=10,
            extra_cap=("fo", 2), now=T0,
        )  # fmt: skip
        assert cid is not None
        db.finish_run(conn, run_id, "error" if i else "ok", None)
    assert tools.sessions_today(conn, T0, kind="fo") == 2
    run_id = db.start_run(conn, "fo", "KR")
    with pytest.raises(session.PipelineError, match="fo"):
        session._start_call(
            conn, run_id, spec, session_id="s9", model="sonnet", cap=10, extra_cap=("fo", 2),
            now=T0,
        )  # fmt: skip
    assert conn.execute("SELECT COUNT(*) AS n FROM agent_calls").fetchone()["n"] == 2
    # 다른 종류는 fo 상한과 무관
    assert tools.sessions_today(conn, T0, kind="analyze") == 0
    conn.execute("SELECT 1").fetchone()


def test_fake_fo_writes_flat_decision(project, conn, monkeypatch):
    """FAKE: fo 세션은 애널리스트 3 → r0 토론 → 요약 → FLAT HOLD 봉투(request_id 는 입력의 것),
    fo_resume 는 stdin 봉투(계좌·포지션·버전·request_id)를 그대로 돌려
    `_fo_decision_r<n>_<계좌>`."""
    from orchestrator.test_tools_fo import _fo
    from trader.fo import store
    from trader.orchestrator.run_store import list_checkpoints, market_dir, read_checkpoint

    monkeypatch.setenv("FAKE_CLAUDE", "1")
    fo, inp = _fo(conn)
    d0 = inp["meta"]["date"]
    D0 = date.fromisoformat(d0)
    spec = SessionSpec(market="KR", date=D0, tickers=[], kind="fo", dry_run=False, asof=T0)
    res = session.run_session(spec, fo.settings, conn, [], fo=fo, now=lambda: T0)
    assert res.ok and res.fake and "_fo_decision" in res.stages_written
    stages = list_checkpoints(market_dir(D0, "KR"))
    assert {"_fo_analyst_technical", "_fo_analyst_sentiment", "_fo_analyst_news_macro",
            "_fo_debate_r0_bull", "_fo_debate_r0_bear", "_fo_summary",
            "_fo_decision"} <= set(stages)
    dec = read_checkpoint(market_dir(D0, "KR"), "_fo_decision")["data"]
    assert dec["decision"]["intent"] == "HOLD" and dec["decision"]["direction"] == "FLAT"
    assert dec["request_id"] == inp["meta"]["request_id"] and dec["account_id"] is None
    rc = read_checkpoint(market_dir(D0, "KR"), "_fo_risk_check_paper-fo")["data"]
    assert "관망" in rc["note"]
    assert conn.execute("SELECT COUNT(*) AS n FROM runs WHERE kind='fo'").fetchone()["n"] == 1
    # 재평가: 가짜 포지션 + 요청 → 봉투를 그대로 되돌린다
    with store.tx(conn):
        pid = store.insert_position(
            conn, account_id="paper-fo", code="105W11", kind="FUTURES", contract_side="LONG",
            direction="LONG", vehicle="FUTURES", status="held", qty_held=1, qty_target=1,
            stop_pt=345, nav_at_open=100000000, opened_at=T0.isoformat(),
        )  # fmt: skip
    ctx = tools.ToolContext(settings=fo.settings, accounts=[], conn=conn, now=lambda: T0, fo=fo)
    req = tools.fo_reeval_request(
        ctx, date=D0, account_id="paper-fo", position_id=pid, trigger={"kind": "move"}
    )
    spec2 = SessionSpec(
        market="KR", date=D0, tickers=[], kind="fo_resume", dry_run=False, asof=T0,
        position=req["position"], trigger={"kind": "move"},
    )  # fmt: skip
    applied = []
    fo.reeval_apply = lambda e, r: applied.append(r) or {"applied": True}
    res2 = session.run_session(spec2, fo.settings, conn, [], fo=fo, now=lambda: T0)
    assert res2.ok and "_fo_decision_r1_paper-fo" in res2.stages_written and applied == [1]
    r1 = read_checkpoint(market_dir(D0, "KR"), "_fo_decision_r1_paper-fo")["data"]
    ver = store.get_position(conn, pid).version
    assert (r1["account_id"], r1["position_id"], r1["expected_version"]) == ("paper-fo", pid, ver)
    assert r1["request_id"] == req["position"]["request_id"] and r1["decision"]["intent"] == "HOLD"
    assert conn.execute("SELECT COUNT(*) AS n FROM runs WHERE kind='monitor'").fetchone()["n"] == 1
