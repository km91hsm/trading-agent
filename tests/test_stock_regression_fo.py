"""Phase 26 Task 4.7: 현물 회귀 스냅샷. 파생 레인(asset·fo 계좌·원장 v26)을 더한 뒤에도 현물 기본
경로의 판정·체크포인트·주문 행이 Phase 25(main `a39e102`)와 같다. 스냅샷은 같은 시나리오
(`stock_regression_scenario.run`)를 main 에서 돌려 만든 합성 데이터다."""

from __future__ import annotations

import json
from pathlib import Path

import stock_regression_scenario as scenario

SNAPSHOT = Path(__file__).parent / "fixtures" / "stock_regression_phase25.json"


def test_stock_paths_unchanged(project):
    got = json.loads(json.dumps(scenario.run(), ensure_ascii=False, sort_keys=True, default=str))
    assert got == json.loads(SNAPSHOT.read_text(encoding="utf-8"))


def test_stock_orders_tagged_stock_and_accounts_default(project, conn):
    from trader.config import AccountConfig

    scenario.run()
    assets = {r[0] for r in conn.execute("SELECT asset FROM orders")}
    assert assets == {"stock"}
    acct = AccountConfig(id="x", env="paper", cano="1", markets=["KR"], mode="auto")
    assert acct.asset == "stock"
