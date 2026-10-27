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
