"""Phase 26 설정: FoCfg·AccountConfig.asset·split_accounts·schedule fo."""

from decimal import Decimal

import pydantic
import pytest
import yaml

from trader import config


def acc(id, env, mode, markets=("KR",), **extra):
    d = {
        "id": id,
        "env": env,
        "cano": "00000000",
        "acnt_prdt_cd": "01",
        "markets": list(markets),
        "mode": mode,
    }
    d.update(extra)
    return d


def write_yaml(project, name, data):
    (project / "config" / name).write_text(
        yaml.safe_dump(data, allow_unicode=True), encoding="utf-8"
    )


def test_fo_defaults_disabled_and_mini():
    fo = config.FoCfg()
    assert fo.enabled is False and fo.product == "mini"
    p = fo.product_cfg()
    assert p.multiplier == 50000 and p.futures_prefix == ("A05", "105")
    assert len(p.option_ticks) == 3 and p.option_ticks[-1][0] is None
    assert p.futures_tick == Decimal("0.02")
    assert fo.vehicles == ["FUTURES", "OPTION_BUY", "OPTION_SELL"]
    assert fo.max_leverage == 2.0 and fo.max_positions == 1 and fo.max_contracts == 4
    assert fo.option.sell_stop_mult.default == 2.0
    assert fo.margin_rate.futures == 0.12 and fo.margin_rate.max_age_days == 30
    assert fo.stress.index_pct == [0.03, 0.06]


def test_fo_forbid_unknown_key():
    with pytest.raises(pydantic.ValidationError, match="foo"):
        config.FoCfg(foo=1)


def test_sell_stop_mult_order():
    with pytest.raises(pydantic.ValidationError, match="sell_stop_mult"):
        config.FoCfg(option={"sell_stop_mult": {"min": 2.5, "default": 2.0, "max": 3.0}})
    with pytest.raises(pydantic.ValidationError, match="sell_stop_mult"):
        config.FoCfg(option={"sell_stop_mult": {"min": 1.5, "default": 3.5, "max": 3.0}})


def test_regime_cap_zero_vs_null():
    fo = config.FoCfg(max_leverage=2.0, leverage_by_regime={"bear": 0, "neutral": None})
    assert fo.regime_cap("bear") == 0.0
    assert fo.regime_cap("neutral") == 2.0
    assert fo.regime_cap("bull") == 2.0
    fo2 = config.FoCfg(max_leverage=2.0, leverage_by_regime={"bull": 3.0})
    assert fo2.regime_cap("bull") == 2.0  # 설정 한도보다 넓어지지 않는다


def test_max_leverage_range():
    with pytest.raises(pydantic.ValidationError):
        config.FoCfg(max_leverage=0)
    with pytest.raises(pydantic.ValidationError):
        config.FoCfg(max_leverage=5.5)


def test_settings_has_fo_block(project):
    s = config.load_settings()
    assert s.fo.enabled is False and s.fo.product == "mini"
    assert s.fo.products["regular"].multiplier == 250000


def test_split_accounts():
    stock = config.AccountConfig(id="paper-main", env="paper", cano="0", mode="auto")
    fo = config.AccountConfig(
        id="paper-fo", env="paper", cano="0", acnt_prdt_cd="03", markets=["KR"],
        mode="auto", asset="fo",
    )
    assert stock.asset == "stock"
    s, f = config.split_accounts([stock, fo])
    assert [a.id for a in s] == ["paper-main"] and [a.id for a in f] == ["paper-fo"]


def test_fo_account_validation(project):
    write_yaml(
        project, "accounts.yaml",
        {"accounts": [acc("p-fo", "paper", "auto", markets=("KR", "US"), asset="fo")]},
    )
    with pytest.raises(config.ConfigError, match="asset"):
        config.load_accounts()
    write_yaml(
        project, "accounts.yaml",
        {"accounts": [acc("r-fo", "real", "auto", asset="fo")]},
    )
    with pytest.raises(config.ConfigError, match="실전"):
        config.load_accounts()
    write_yaml(
        project, "accounts.yaml",
        {"accounts": [acc("p-fo", "paper", "auto", asset="fo", acnt_prdt_cd="03")]},
    )
    a = config.load_accounts()[0]
    assert a.asset == "fo" and a.acnt_prdt_cd == "03"


def test_schedule_fo_defaults(project):
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    sch = config.load_schedule(repo / "config" / "schedule.yaml")
    assert sch.fo.kr.session == "08:20"
    assert sch.fo.kr.monitor.start == "08:45" and sch.fo.kr.monitor.end == "15:45"
    assert sch.fo.kr.monitor.interval_s == 60


def test_option_ticks_validation():
    """Codex 단계 1 P2: tick 은 양수, 상한은 엄격한 오름차순, inf 는 마지막에만."""
    base = dict(
        futures_prefix="105", call_prefix="205", put_prefix="305", multiplier=50000,
        futures_tick="0.02", name="x",
    )
    bad = [
        [["3", "0"], ["inf", "0.05"]],
        [["3", "-0.01"], ["inf", "0.05"]],
        [["10", "0.01"], ["3", "0.02"], ["inf", "0.05"]],
        [["3", "0.01"], ["3", "0.02"], ["inf", "0.05"]],
        [["inf", "0.01"], ["3", "0.02"], ["inf", "0.05"]],
        [["3", "0.01"]],
    ]
    for ticks in bad:
        with pytest.raises(ValueError):
            config.FoProductCfg(**base, option_ticks=ticks)
    ok = config.FoProductCfg(**base, option_ticks=[["3", "0.01"], ["10", "0.02"], ["inf", "0.05"]])
    assert ok.option_ticks[0] == (Decimal("3"), Decimal("0.01"))
