"""현물 회귀 스냅샷 시나리오(Phase 26 Task 4.7). 파생 레인을 더한 뒤에도 현물 기본 경로
(asset 기본값)의 판정·체크포인트·주문 행이 Phase 25 와 같은지 본다. `run()` 은 현재
TRADER_HOME 의 원장·state 를 쓰고 시각·id 같은 바뀌는 값을 뺀 dict 를 돌려준다(main 에서도
같은 함수로 스냅샷을 만들었다)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

D = date(2026, 10, 9)
T0 = datetime(2026, 10, 9, 1, 0, tzinfo=UTC)
VOLATILE = {"created_at", "updated_at", "received_at", "requested_at", "id", "decided_at", "ts"}


def _clean(v):
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in sorted(v.items()) if k not in VOLATILE}
    if isinstance(v, list):
        return [_clean(x) for x in v]
    return v


def run() -> dict:
    from helpers_agents import ok_decision

    from trader.agents.schemas import Decision
    from trader.broker.fake import FakeBroker
    from trader.broker.models import Cash
    from trader.config import AccountConfig, Settings
    from trader.ledger import db, repo
    from trader.orchestrator import executor
    from trader.orchestrator.run_store import read_checkpoint, run_dir

    conn = db.connect()
    db.init_schema(conn)
    accounts = [
        AccountConfig(id="paper-main", env="paper", cano="1", markets=["KR", "US"], mode="auto"),
        AccountConfig(id="real-main", env="real", cano="2", markets=["KR"], mode="off"),
    ]
    cash = {
        "KR": Cash(currency="KRW", available=Decimal("10000000"), total=Decimal("10000000")),
        "US": Cash(currency="USD", available=Decimal("10000"), total=Decimal("10000")),
    }
    ectx = executor.ExecContext(
        settings=Settings(), accounts=accounts, conn=conn,
        brokers={a.id: FakeBroker(a, cash=dict(cash)) for a in accounts}, now=lambda: T0,
    )  # fmt: skip
    out: dict = {}
    for name, dec in (
        ("buy", ok_decision()),
        ("hold", ok_decision(ticker="000660", action="HOLD", target_weight=0.0)),
    ):
        d = Decision(**dec)
        repo.ingest_checkpoint(conn, D, "KR", d.ticker, "30_decision", {"data": d.model_dump()})
        res = executor.execute(
            ectx, d, date=D, market="KR", price=Decimal("71500"), dry_run=False
        )
        out[name] = {
            "results": _clean(res),
            "checkpoints": {
                st: _clean((read_checkpoint(run_dir(D, "KR", d.ticker), st) or {}).get("data"))
                for st in ("40_risk_check_paper-main", "40_risk_check_real-main",
                           "50_order_paper-main")
            },
        }  # fmt: skip
    out["orders"] = [
        dict(r)
        for r in conn.execute(
            "SELECT account_id, ticker, market, side, qty, price, order_type, status, mode, "
            "order_no FROM orders ORDER BY id"
        )
    ]
    out["risk_checks"] = conn.execute("SELECT COUNT(*) FROM risk_checks").fetchone()[0]
    conn.close()
    return out
