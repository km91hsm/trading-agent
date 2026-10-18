"""검토(2026-10-18)로 통합한 공용 헬퍼. 각자 흩어져 있던 사본의 동작을 그대로 보존해야 한다."""

from decimal import Decimal

import numpy as np
import pandas as pd

from trader import config


def test_config_kst_is_market_tz_kr():
    assert config.KST is config.MARKET_TZ["KR"] and str(config.KST) == "Asia/Seoul"


def test_broker_parse_helpers():
    from trader.broker._parse import dec, rows, to_int

    assert dec("71,500".replace(",", "")) == Decimal("71500") and dec("") == Decimal("0")
    assert dec(None) == Decimal("0") and dec("x", default="1") == Decimal("1")
    assert to_int(" 3 ") == 3 and to_int(None) == 0
    assert rows([{"a": 1}, {}, "x", {"b": 2}]) == [{"a": 1}, {"b": 2}]
    assert rows({"a": 1}) == [{"a": 1}] and rows({}) == [] and rows(None) == []


def test_sources_strip_tags_unescapes_twice():
    from trader.sources.base import strip_tags, utc_now

    assert strip_tags("&lt;b&gt;삼성&lt;/b&gt; &amp;amp; 전자") == "삼성 & 전자"
    assert strip_tags("<p>a\xa0b</p>") == "a b" and strip_tags(None) == ""
    assert utc_now().tzinfo is not None


def test_zscore_keeps_or_fills_nan():
    from trader.ml.features import zscore

    s = pd.Series([1.0, 2.0, np.nan, 3.0])
    z = zscore(s)
    assert np.isnan(z.iloc[2]) and abs(z.iloc[1]) < 1e-9
    assert zscore(s, fill=0.0).iloc[2] == 0.0
    assert (zscore(pd.Series([2.0, 2.0])) == 0).all()
    assert zscore(pd.Series(["1", "x", "3"]), fill=0.0).tolist() == [-1.0, 0.0, 1.0]


def test_checkpoint_payload_envelope():
    from trader.agents import schemas
    from trader.orchestrator.run_store import checkpoint_payload

    assert checkpoint_payload({"a": 1}) == {
        "schema_version": schemas.CHECKPOINT_SCHEMA_VERSION,
        "data": {"a": 1},
        "call": None,
    }
    assert checkpoint_payload({"a": 1}, call={"agent": "x"})["call"] == {"agent": "x"}


def test_quiet_httpx_is_shared_and_idempotent():
    import logging

    from trader.logging_setup import quiet_httpx

    quiet_httpx()
    quiet_httpx()
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING
