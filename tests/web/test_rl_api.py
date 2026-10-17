from trader.ml import registry
from trader.ml.rl.policy import FixedPolicy


def test_rl_experiments_and_shadow_endpoints(client, conn):
    assert client.get("/api/rl/experiments").json() == {"experiments": [], "active": None}
    v = registry.save(
        "rl_sizer",
        FixedPolicy(0.1),
        {"summary": {}, "backend": "cem", "source": "features", "steps": 10, "seeds": 3},
    )
    assert client.get("/api/rl/experiments").json()["experiments"][0]["version"] == v
    assert client.get(f"/api/rl/experiments/{v}").status_code == 200
    assert client.get("/api/rl/experiments/bad").status_code == 400
    assert client.get("/api/rl/experiments/19990101_000000").status_code == 404
    assert client.get("/api/rl/shadow").status_code == 400
    assert client.get("/api/rl/shadow?account_id=nope").status_code == 400
    assert client.get("/api/rl/shadow?account_id=paper-main").json() == {"rows": []}
    assert client.get("/api/rl/report?account_id=paper-main").json()["shadow"]["n"] == 0
