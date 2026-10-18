from datetime import UTC, datetime

import pandas as pd
from helpers_sources import FakeDart, FakeMarket

from trader.config import Universe
from trader.ml import features
from trader.orchestrator.run_store import run_dir, write_checkpoint

T0 = datetime(2026, 10, 17, 6, 0, tzinfo=UTC)


def test_snapshot_fills_counts_from_inputs(project):
    d = run_dir(T0.astimezone(features.KST).date(), "KR", "005930")
    write_checkpoint(
        d,
        "00_inputs",
        {
            "meta": {"ticker": "005930"},
            "news_macro": {
                "news": {
                    "data": {"items": [{"t": 1}, {"t": 2}, {"t": 3}], "sources": {}},
                    "received_at": "",
                    "error": None,
                }
            },
            "sentiment": {
                "community": {
                    "data": {"posts": [{"p": 1}], "sources": {}},
                    "received_at": "",
                    "error": None,
                }
            },
        },
    )
    n = features.snapshot(
        market_client=FakeMarket(),
        dart=FakeDart(),
        universe=Universe(kr=["005930"]),
        markets=["KR"],
        asof=T0,
        now=lambda: T0,
    )
    assert n == 1
    got = pd.read_parquet(next((project / "state" / "features").glob("*.parquet")))
    assert got.iloc[0]["news_count"] == 3 and got.iloc[0]["community_count"] == 1
    assert features.counts_from_inputs("000660", "KR", T0) == (None, None)  # 00_inputs 없음
