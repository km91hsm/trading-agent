import pytest
from helpers_memory import seed_matured

from trader.ml import registry
from trader.ml.rl import shadow
from trader.ml.rl.env import OBS_SIZE
from trader.ml.rl.policy import FixedPolicy
from trader.ml.rl.report import comparison


def test_comparison_without_model_and_with_shadow(conn, project):
    assert comparison(conn, None, account_id="paper-main")["model"] is None
    seed_matured(conn, 2)
    dec_ids = [r[0] for r in conn.execute("SELECT decision_id FROM memory_entries")]
    for d in dec_ids:
        shadow.record(
            conn,
            decision_id=d,
            account_id="paper-main",
            model_version="v",
            obs=[0.0] * OBS_SIZE,
            llm_weight=0.1,
            proposed_weight=0.2,
            applied=False,
        )
    registry.save(
        "rl_sizer",
        FixedPolicy(0.1),
        {"summary": {"rl": {"return_mean": 0.01}}, "backend": "cem", "source": "features"},
    )
    r = comparison(conn, None, account_id="paper-main", weeks=8)
    assert r["weeks"] == 8 and r["model"]["backend"] == "cem"
    assert r["shadow"]["n"] == 2 and r["shadow"]["matured"] == 2
    assert r["shadow"]["avg_proposed_weight"] == pytest.approx(0.2)
    assert r["shadow"]["realized_rl_sum"] == pytest.approx(0.2 * 0.03 * 2)
