"""Phase 26 단계 3 Task 3.5: `fo/session.run` — 입력 저장 → fo 세션 1회 → `_fo_decision` 읽기.
상한·오늘 결정 멱등·enabled=false 생략·과거 asof 강제 dry-run·CLI `fo session --dry-run` JSON."""

from __future__ import annotations

import json
from datetime import timedelta

import yaml
from helpers_sources import T0
from typer.testing import CliRunner

from fo.test_executor import account, balance, settings
from fo.test_inputs import _market, _master_with_chain
from trader.broker.fake import FakeFoBroker
from trader.fo import session as fo_session
from trader.fo import store
from trader.ledger import db
from trader.orchestrator import session as session_mod
from trader.orchestrator.run_store import list_checkpoints, market_dir, read_checkpoint

D = T0.date()


def _deps(conn, *, now=T0, enabled=True):
    acct = account()
    broker = FakeFoBroker(acct, balance=balance(), now=lambda: now)
    return dict(
        settings=settings(enabled=enabled), conn=conn, fo_accounts=[acct],
        brokers={acct.id: broker}, market_client=_market(), master=_master_with_chain(),
        date=D, now=lambda: now, dry_run=False, state=None,
    )  # fmt: skip


def test_fo_session_writes_inputs_and_decision_fake(project, conn, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE", "1")
    out = fo_session.run(**_deps(conn))
    assert out["skipped"] is None and out["session"]["ok"] and out["session"]["fake"]
    assert out["inputs_path"].endswith("_fo_inputs.json")
    stages = set(list_checkpoints(market_dir(D, "KR")))
    assert {"_fo_inputs", "_fo_analyst_technical", "_fo_analyst_sentiment",
            "_fo_analyst_news_macro", "_fo_debate_r0_bull", "_fo_debate_r0_bear",
            "_fo_summary", "_fo_decision", "_fo_risk_check_paper-fo"} <= stages
    assert out["decision"]["decision"]["direction"] == "FLAT"
    assert conn.execute("SELECT COUNT(*) AS n FROM runs WHERE kind='fo'").fetchone()["n"] == 1
    # 같은 날 다시 부르면 세션 없이 기존 결정을 돌려준다
    again = fo_session.run(**_deps(conn))
    assert again["skipped"] and "결정" in again["skipped"] and again["session"] is None
    assert again["decision"]["decision"]["direction"] == "FLAT"
    assert conn.execute("SELECT COUNT(*) AS n FROM runs WHERE kind='fo'").fetchone()["n"] == 1


def test_fo_session_cap_skips(project, conn, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE", "1")
    deps = _deps(conn)
    deps["settings"] = settings(enabled=True, session_cap=1)
    # 오늘 fo 세션(실패 포함, FAKE 제외)이 상한에 닿으면 세션 없이 생략
    run_id = db.start_run(conn, "fo", "KR")
    spec = session_mod.SessionSpec(market="KR", date=D, tickers=[], kind="fo")
    session_mod._start_call(conn, run_id, spec, session_id="s0", model="sonnet", now=T0)
    db.finish_run(conn, run_id, "error", None)
    out = fo_session.run(**deps)
    assert out["skipped"] and "상한" in out["skipped"] and out["session"] is None
    assert "_fo_decision" not in list_checkpoints(market_dir(D, "KR"))


def test_fo_session_disabled_without_positions_skips(project, conn, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE", "1")
    out = fo_session.run(**_deps(conn, enabled=False))
    assert out["skipped"] and "enabled" in out["skipped"] and out["inputs_path"] is None
    # 보유가 있으면 꺼져 있어도 세션은 돈다(청산 의견)
    with store.tx(conn):
        store.insert_position(
            conn, account_id="paper-fo", code="105W11", kind="FUTURES", contract_side="LONG",
            direction="LONG", vehicle="FUTURES", status="held", qty_held=1, qty_target=1,
            stop_pt=345, nav_at_open=100000000, opened_at=T0.isoformat(),
        )  # fmt: skip
    out2 = fo_session.run(**_deps(conn, enabled=False))
    assert out2["skipped"] is None and out2["session"]["ok"]


def test_fo_session_historical_asof_forces_dry_run(project, conn):
    """asof 가 now − max_input_age_s 보다 과거면 세션은 dry_run 강제 + 입력 historical."""
    seen = {}

    def runner(spec, *a, **kw):
        seen["spec"] = spec
        return session_mod.SessionResult(ok=True, session_id="x", fake=False)

    deps = _deps(conn)
    old = T0 - timedelta(hours=3)
    out = fo_session.run(**deps, session_runner=runner, asof=old)
    assert seen["spec"].kind == "fo" and seen["spec"].dry_run is True
    assert seen["spec"].asof == old
    inp = read_checkpoint(market_dir(D, "KR"), "_fo_inputs")["data"]
    assert inp["meta"]["historical"] is True and out["forced_dry_run"] is True


def test_cli_fo_session_dry_run_json(project, conn, monkeypatch):
    from test_cli_fo import _accounts, _fakes

    from trader import cli
    from trader.broker import fo_master

    _accounts(project)
    (project / "config" / "settings.yaml").write_text(
        yaml.safe_dump({"fo": {"enabled": True}}), encoding="utf-8"
    )
    monkeypatch.setenv("FAKE_CLAUDE", "1")
    _, brokers, _ = _fakes()
    monkeypatch.setattr(cli, "_build_market", lambda s, a=None: _market())
    monkeypatch.setattr(cli, "_build_brokers", brokers)
    monkeypatch.setattr(fo_master, "load", lambda **kw: _master_with_chain())
    r = CliRunner().invoke(cli.app, ["fo", "session", "--dry-run", "--asof", T0.isoformat()])
    assert r.exit_code == 0, r.stdout + r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["session"]["fake"] and out["decision"]["decision"]["intent"] == "HOLD"
    assert out["dry_run"] is True
