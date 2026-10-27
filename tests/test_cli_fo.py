"""Phase 26 단계 1: CLI — 브로커 asset 디스패치·`kis ping` 파생 잔고·`fo verify`·`fo order-test`·
거부 목록."""

from __future__ import annotations

import json
from dataclasses import replace as dc_replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import yaml
from typer.testing import CliRunner

from trader import cli, config
from trader.broker import fo_master
from trader.broker.accounts import KisBroker
from trader.broker.fake import FakeBroker, FakeFoBroker, FakeMarket
from trader.broker.fo import CONTRACT_VERSION, KisFoBroker
from trader.broker.models import Cash, FoBalance, FoCash, FoPosition, FoQuote
from trader.cli import app
from trader.control import control
from trader.ledger import db

T0 = datetime(2026, 10, 27, 1, 15, 30, tzinfo=UTC)
FIX = Path(__file__).parent / "broker" / "fixtures" / "fo_master_sample.mst"


def _accounts(project, *, fo_env: str = "paper", fo_mode: str = "auto"):
    data = {
        "accounts": [
            {
                "id": "paper-main",
                "env": "paper",
                "cano": "00000000",
                "markets": ["KR"],
                "mode": "auto",
            },
            {
                "id": f"{fo_env}-fo",
                "env": fo_env,
                "cano": "00000000",
                "acnt_prdt_cd": "03",
                "markets": ["KR"],
                "mode": fo_mode,
                "asset": "fo",
            },
        ]
    }
    (project / "config" / "accounts.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")


def _fo_cash(nav: Decimal | None = Decimal("10000000")) -> FoCash:
    return FoCash(
        deposit=Decimal("10000000"),
        orderable_cash=Decimal("8000000"),
        orderable_total=Decimal("8000000"),
        margin_total=Decimal("0"),
        add_margin=Decimal("0"),
        nav=nav,
        realized_today=Decimal("0"),
        unrealized=Decimal("0"),
        requested_at=T0,
        received_at=T0,
    )


def _pos(code: str, kind: str, side: str) -> FoPosition:
    return FoPosition(
        code=code,
        name=None,
        kind=kind,
        contract_side=side,
        qty=1,
        avg_price=Decimal("1"),
        current_price=Decimal("1"),
        pnl=Decimal("0"),
        sellable_qty=1,
    )


def _quote(code: str, kind: str = "FUTURES") -> FoQuote:
    return FoQuote(
        code=code,
        kind=kind,
        price=Decimal("350.5"),
        bid=Decimal("350.48"),
        ask=Decimal("350.52"),
        change_pct=0.0,
        volume=1,
        open_interest=1,
        basis=Decimal("0.7"),
        upper=Decimal("380"),
        lower=Decimal("320"),
        last_trade_date=None,
        days_left=16,
        hist_vol=15.0,
        iv=None,
        delta=None,
        gamma=None,
        theta=None,
        vega=None,
        strike=None,
        underlying=Decimal("349.8"),
        underlying_asof=T0,
        quote_asof=T0,
        received_at=T0,
    )


class _Client:
    def __init__(self, env):
        self.env = env


def test_build_brokers_asset_dispatch(project, monkeypatch):
    """asset fo 계좌는 KisFoBroker(상품은 settings.fo), stock 은 KisBroker."""
    _accounts(project)
    accounts = config.load_accounts()
    secrets = config.Secrets(KIS_PAPER_APP_KEY="k", KIS_PAPER_APP_SECRET="s")
    monkeypatch.setattr(cli, "_client_for", lambda creds: _Client(creds.env))
    brokers = cli._build_brokers(secrets, accounts, settings=config.load_settings())
    assert [type(b) for b in brokers] == [KisBroker, KisFoBroker]
    assert brokers[1].account.id == "paper-fo"
    assert brokers[1].product.futures_prefix == "105"  # 기본 상품 mini


def _fakes(balance: FoBalance | None = None, *, market_fail=()):
    market = FakeMarket(
        prices={"005930": Decimal("71500"), "AAPL": Decimal("227.55")},
        fo_quotes={"105W11": _quote("105W11")},
        kospi200=(Decimal("349.8"), T0),
        fail=set(market_fail),
    )
    bal = balance or FoBalance(positions=[], unverified=[], cash=_fo_cash())
    made: list = []

    def brokers(secrets, accounts, *, settings=None):
        out = []
        for a in accounts:
            if a.asset == "fo":
                out.append(FakeFoBroker(a, balance=bal, now=lambda: T0))
            else:
                out.append(
                    FakeBroker(
                        a,
                        cash={
                            "KR": Cash(currency="KRW", available=1, total=1),
                            "US": Cash(currency="USD", available=1, total=1),
                        },
                    )
                )
        made.extend(out)
        return out

    return market, brokers, made


def test_kis_ping_includes_fo_balance(project, monkeypatch):
    _accounts(project)
    market, brokers, made = _fakes()
    monkeypatch.setattr(cli, "_build_market", lambda s, a=None: market)
    monkeypatch.setattr(cli, "_build_brokers", brokers)
    r = CliRunner().invoke(app, ["kis", "ping"])
    assert r.exit_code == 0, r.stdout
    assert "paper-main KR 현금" in r.stdout
    assert "paper-fo 파생 잔고" in r.stdout and "실패 0건" in r.stdout
    fo = next(b for b in made if isinstance(b, FakeFoBroker))
    assert fo.calls == ["get_balance"]


def _run_verify(project, monkeypatch, balance: FoBalance, *args: str):
    market, brokers, made = _fakes(balance)
    monkeypatch.setattr(cli, "_build_market", lambda s, a=None: market)
    monkeypatch.setattr(cli, "_build_brokers", brokers)
    monkeypatch.setattr(fo_master, "load", lambda **kw: fo_master.parse(FIX.read_bytes()))
    r = CliRunner().invoke(app, ["fo", "verify", "--account", "paper-fo", *args])
    assert r.exit_code == 0, r.stdout + r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1]), made


def _flags(project) -> dict[str, int]:
    conn = db.connect()
    try:
        rows = conn.execute("SELECT key, value FROM control WHERE key LIKE 'fo_verified:%'")
        return {k: json.loads(v) for k, v in rows.fetchall()}
    finally:
        conn.close()


def test_fo_verify_marks_only_verified_vehicles(project, monkeypatch):
    _accounts(project)
    # 빈 잔고: 조회는 성공해도 어떤 수단도 검증되지 않는다(빈 응답은 검증이 아니다, R18)
    out, made = _run_verify(
        project, monkeypatch, FoBalance(positions=[], unverified=[], cash=_fo_cash())
    )
    assert out["verified"] == [] and _flags(project) == {}
    assert out["balance"]["positions"] == 0 and out["balance"]["nav"] is True
    assert out["quote"]["code"] == "105W11" and out["orderable"]["ord_qty"] >= 1
    fo = next(b for b in made if isinstance(b, FakeFoBroker))
    assert fo.orders == [] and fo.cancelled == []  # 조회 전용

    # 선물 롱 + 콜 매도 보유 → FUTURES·OPTION_SELL 만, OPTION_BUY 는 닫힌 채
    bal = FoBalance(
        positions=[_pos("105W11", "FUTURES", "LONG"), _pos("205W11350", "CALL", "SHORT")],
        unverified=[],
        cash=_fo_cash(),
    )
    out, _ = _run_verify(project, monkeypatch, bal)
    assert out["verified"] == ["FUTURES", "OPTION_SELL"]
    assert _flags(project) == {
        "fo_verified:paper:paper-fo:mini:FUTURES": CONTRACT_VERSION,
        "fo_verified:paper:paper-fo:mini:OPTION_SELL": CONTRACT_VERSION,
    }

    # NAV 없음(prsm_dpast_amt 비면 None)이면 보유가 있어도 검증 아님
    bal2 = FoBalance(
        positions=[_pos("305W11347", "PUT", "LONG")], unverified=[], cash=_fo_cash(None)
    )
    out, _ = _run_verify(project, monkeypatch, bal2, "--vehicle", "OPTION_BUY")
    assert out["verified"] == [] and "fo_verified:paper:paper-fo:mini:OPTION_BUY" not in _flags(
        project
    )


def test_fo_verify_vehicle_filter(project, monkeypatch):
    _accounts(project)
    bal = FoBalance(
        positions=[_pos("105W11", "FUTURES", "LONG"), _pos("205W11350", "CALL", "LONG")],
        unverified=[],
        cash=_fo_cash(),
    )
    out, _ = _run_verify(project, monkeypatch, bal, "--vehicle", "OPTION_BUY")
    assert out["verified"] == ["OPTION_BUY"]
    assert list(_flags(project)) == ["fo_verified:paper:paper-fo:mini:OPTION_BUY"]


def test_fo_verify_rejects_stock_account(project, monkeypatch):
    _accounts(project)
    r = CliRunner().invoke(app, ["fo", "verify", "--account", "paper-main"])
    assert r.exit_code == 1 and "asset" in (r.stdout + r.stderr)


def test_fo_order_test_refuses_real(project, monkeypatch):
    _accounts(project, fo_env="real", fo_mode="off")
    market, brokers, _ = _fakes()
    monkeypatch.setattr(cli, "_build_brokers", brokers)
    r = CliRunner().invoke(
        app, ["fo", "order-test", "--account", "real-fo", "--vehicle", "FUTURES"]
    )
    assert r.exit_code == 1 and "모의" in (r.stdout + r.stderr)


def test_fo_order_test_paper_not_wired_yet(project, monkeypatch):
    """단계 1: 명령 틀만 — 모의 계좌라도 주문 없이 안내하고 끝낸다(실제 왕복은 단계 2 executor)."""
    _accounts(project)
    market, brokers, made = _fakes()
    monkeypatch.setattr(cli, "_build_brokers", brokers)
    r = CliRunner().invoke(
        app, ["fo", "order-test", "--account", "paper-fo", "--vehicle", "FUTURES"]
    )
    assert r.exit_code == 2
    assert all(b.orders == [] for b in made if isinstance(b, FakeFoBroker))


def test_fo_contracts_lists_front_month_and_strikes(project, monkeypatch):
    monkeypatch.setattr(fo_master, "load", lambda **kw: fo_master.parse(FIX.read_bytes()))
    r = CliRunner().invoke(app, ["fo", "contracts", "--today", "2026-10-27"])
    assert r.exit_code == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["product"] == "mini" and out["futures"]["front"]["code"] == "105W11"
    assert "105W12" in [c["code"] for c in out["futures"]["months"]]
    assert out["options"]["front"]["expiry_ym"] == "202611"
    assert Decimal("350") in {Decimal(s) for s in out["options"]["front"]["strikes"]}


def test_settings_json_denies_fo_order_test_and_close():
    deny = json.loads(
        (Path(__file__).parents[1] / ".claude" / "settings.json").read_text(encoding="utf-8")
    )["permissions"]["deny"]
    for cmd in ("fo order-test", "fo close"):
        assert f"Bash(python -m trader {cmd}*)" in deny
        assert f"Bash(uv run trader {cmd}*)" in deny


def test_session_brokers_fake_fo(project, monkeypatch):
    _accounts(project)
    monkeypatch.setenv("FAKE_CLAUDE", "1")
    brokers = cli._session_brokers(config.load_accounts())
    assert isinstance(brokers["paper-main"], FakeBroker)
    fo = brokers["paper-fo"]
    assert isinstance(fo, FakeFoBroker)
    b = fo.get_balance()
    assert b.positions == [] and b.cash.nav == Decimal("10000000")


def test_market_creds_ignores_fo_accounts(project):
    """시세 키 폴백은 현물 실전 계좌만 본다(fo 실전 계좌의 전용 키는 시세 폴백에 쓰지 않음)."""
    _accounts(project, fo_env="real", fo_mode="off")
    secrets = config.Secrets(KIS_REAL_FO_APP_KEY="fk", KIS_REAL_FO_APP_SECRET="fs")
    assert not cli._market_creds(secrets).app_key


def test_readiness_cmd_uses_stock_accounts_only(project, monkeypatch):
    _accounts(project)
    seen = {}

    def fake_check(conn, *, accounts, settings, state, today):
        seen["ids"] = [a.id for a in accounts]
        return {"ready": False, "items": []}

    from trader import readiness

    monkeypatch.setattr(readiness, "check", fake_check)
    r = CliRunner().invoke(app, ["readiness"])
    assert r.exit_code == 0, r.stdout + r.stderr
    assert seen["ids"] == ["paper-main"]


def test_fo_verify_no_flag_when_a_step_fails(project, monkeypatch):
    """Codex 단계 1 P1-4: 마스터·시세·주문가능 중 하나라도 실패하면 보유 행이 있어도 플래그를
    적지 않고 exit 1."""
    _accounts(project)
    bal = FoBalance(positions=[_pos("105W11", "FUTURES", "LONG")], unverified=[], cash=_fo_cash())
    market, brokers, _ = _fakes(bal, market_fail={"get_kospi200"})
    monkeypatch.setattr(cli, "_build_market", lambda s, a=None: market)
    monkeypatch.setattr(cli, "_build_brokers", brokers)
    monkeypatch.setattr(fo_master, "load", lambda **kw: fo_master.parse(FIX.read_bytes()))
    r = CliRunner().invoke(app, ["fo", "verify", "--account", "paper-fo"])
    assert r.exit_code == 1, r.stdout + (r.stderr or "")
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["verified"] == [] and out["errors"] and _flags(project) == {}


def test_control_and_status_see_fo_account(project):
    """Codex 단계 1 P1-5: 계좌를 가리지 않는 소비자(control·STATUS)는 all_accounts 를 받는다."""
    _accounts(project)
    runner = CliRunner()
    r = runner.invoke(app, ["control", "mode", "paper-fo", "off"])
    assert r.exit_code == 0, r.stdout + (r.stderr or "")
    conn = db.connect()
    try:
        assert control.get_flag(conn, "mode:paper-fo") == "off"
    finally:
        conn.close()
    r = runner.invoke(app, ["control", "show"])
    assert r.exit_code == 0 and "paper-fo" in r.stdout
    r = runner.invoke(app, ["status"])
    assert r.exit_code == 0, r.stdout + (r.stderr or "")
    text = (project / "state" / "STATUS.md").read_text(encoding="utf-8")
    assert "paper-fo" in text and "paper-main" in text


def test_fo_verify_no_flag_when_no_front_month(project, monkeypatch):
    """Claude 리뷰 1: 마스터에 roll 창 밖 선물 월물이 없으면 시세·주문가능 단계가 돌지 않으므로
    검증 실패(errors, exit 1, 플래그 없음)로 센다 — 조용히 건너뛰고 플래그를 적지 않는다."""
    _accounts(project)
    bal = FoBalance(positions=[_pos("105W11", "FUTURES", "LONG")], unverified=[], cash=_fo_cash())
    market, brokers, _ = _fakes(bal)
    monkeypatch.setattr(cli, "_build_market", lambda s, a=None: market)
    monkeypatch.setattr(cli, "_build_brokers", brokers)
    no_futs = [c for c in fo_master.parse(FIX.read_bytes()) if c.kind != "FUTURES"]
    monkeypatch.setattr(fo_master, "load", lambda **kw: no_futs)
    r = CliRunner().invoke(app, ["fo", "verify", "--account", "paper-fo"])
    assert r.exit_code == 1, r.stdout + (r.stderr or "")
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["front"] is None and any("front" in e for e in out["errors"])
    assert out["verified"] == [] and _flags(project) == {}
    assert "get_fo_price" not in market.calls  # 시세 단계는 돌지 않았다(그래서 실패로 센다)


def test_fo_verify_no_flag_when_master_empty(project, monkeypatch):
    _accounts(project)
    market, brokers, _ = _fakes()
    monkeypatch.setattr(cli, "_build_market", lambda s, a=None: market)
    monkeypatch.setattr(cli, "_build_brokers", brokers)
    monkeypatch.setattr(fo_master, "load", lambda **kw: [])
    r = CliRunner().invoke(app, ["fo", "verify", "--account", "paper-fo"])
    assert r.exit_code == 1
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert any("master" in e for e in out["errors"]) and _flags(project) == {}


def _chain_market(**kw):
    q = _quote("205W11350", "CALL").model_copy(update={"strike": Decimal("350"), "iv": 18.0})
    return FakeMarket(
        prices={},
        fo_quotes={"205W11350": q},
        kospi200=(Decimal("349.8"), T0),
        **kw,
    )


def test_fo_chain_atm_window_and_errors(project, monkeypatch):
    """Claude 리뷰 3·4: ATM ± n 행사가만 묻고 시세 없는 계약은 errors 로, 행사가가 없는 월물은
    ConfigError(exit 1, 트레이스백 없음)."""
    market = _chain_market()
    monkeypatch.setattr(cli, "_build_market", lambda s, a=None: market)
    monkeypatch.setattr(fo_master, "load", lambda **kw: fo_master.parse(FIX.read_bytes()))
    r = CliRunner().invoke(app, ["fo", "chain", "--today", "2026-10-27", "--strikes", "1"])
    assert r.exit_code == 0, r.stdout + (r.stderr or "")
    out = json.loads(r.stdout)
    assert out["expiry_ym"] == "202611" and out["underlying"] == "349.8"
    assert len(out["strikes"]) <= 3 and Decimal("350") in {Decimal(k) for k in out["strikes"]}
    assert [q["code"] for q in out["quotes"]] == ["205W11350"]
    assert out["errors"] and all(c != "205W11350" for c in out["errors"])
    # 행사가 없는 월물(옵션 계약의 strike 가 전부 None)
    stripped = [
        dc_replace(c, strike=None) if c.kind != "FUTURES" else c
        for c in fo_master.parse(FIX.read_bytes())
    ]
    monkeypatch.setattr(fo_master, "load", lambda **kw: stripped)
    r = CliRunner().invoke(app, ["fo", "chain", "--today", "2026-10-27"])
    assert r.exit_code == 1 and "행사가" in (r.stdout + (r.stderr or ""))
    assert "Traceback" not in (r.stdout + (r.stderr or ""))


def test_fo_key_fallback_logs_info_not_warning(project, caplog):
    """Claude 리뷰 7: 파생 계좌의 공용 모의 키 폴백(KIS_PAPER_FO_* 없음)은 설계상 허용이라 부트마다
    WARNING 을 내지 않는다(INFO)."""
    import logging

    _accounts(project)
    accounts = config.load_accounts()
    secrets = config.Secrets(KIS_PAPER_APP_KEY="k", KIS_PAPER_APP_SECRET="s")
    with caplog.at_level(logging.INFO, logger="trader.kis"):
        cli._build_brokers(secrets, accounts, settings=config.load_settings())
    shared = [r for r in caplog.records if "같은 앱키" in r.message]
    assert shared and all(r.levelno == logging.INFO for r in shared)
