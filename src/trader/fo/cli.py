"""`trader fo …` 명령(Phase 26 단계 1): 종목 마스터 조회·옵션 체인·브로커 계약 검증·주문 테스트 틀.

조회 전용 명령(`contracts`·`chain`·`verify`)은 어떤 주문도 내지 않는다. `verify` 는 보유 행이
실제로 있어 방향·종류·NAV 가 확인된 수단만 control `fo_verified:<env>:<계좌>:<상품>:<수단>` 에
`CONTRACT_VERSION` 을 적는다(빈 응답은 검증이 아니다). `order-test`·`close` 는 거부 목록(사용자
실행)."""

from __future__ import annotations

import functools
import json
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import typer

from trader import config
from trader.broker import fo_master
from trader.broker.fo import CONTRACT_VERSION
from trader.broker.models import FoBalance, FoPosition
from trader.config import KST
from trader.platform import paths

app = typer.Typer(
    help="선물옵션 레인 (Phase 26): 마스터·체인·브로커 계약 검증", no_args_is_help=True
)

VEHICLES = ("FUTURES", "OPTION_BUY", "OPTION_SELL")


def _config_errors(fn):
    """`cli._config_errors` 를 호출 시점에 적용한다(모듈 import 때는 trader.cli 가 아직
    초기화 중)."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        from trader.cli import _config_errors as deco

        return deco(fn)(*args, **kwargs)

    return wrapper


def _master(settings: config.Settings) -> list[fo_master.Contract]:
    return fo_master.load(cache_dir=paths.state_dir() / "fo")


def _today(value: str | None) -> date:
    return date.fromisoformat(value) if value else datetime.now(KST).date()


def _no_quote(_: fo_master.Contract) -> date | None:
    return None  # 시세 없이는 둘째 목요일 규칙(휴일 앞당김 미반영)


def _front_futures(
    contracts: list[fo_master.Contract],
    product: config.FoProductCfg,
    *,
    today: date,
    roll_days: int,
) -> fo_master.Contract | None:
    return fo_master.front_month(
        contracts,
        product,
        "FUTURES",
        today=today,
        roll_days=roll_days,
        last_trade_date_of=_no_quote,
    )


def _option_front(
    contracts: list[fo_master.Contract],
    product: config.FoProductCfg,
    *,
    today: date,
    roll_days: int,
) -> tuple[str | None, list[fo_master.Contract]]:
    """roll 창 밖의 가장 가까운 옵션 월물과 그 월물의 콜·풋 계약."""
    calls = fo_master._of_product(contracts, product, "CALL")
    yms = fo_master._expiries(calls, today=today, roll_days=roll_days, last_trade_date_of=_no_quote)
    if not yms:
        return None, []
    ym = yms[0]
    puts = fo_master._of_product(contracts, product, "PUT")
    return ym, [c for c in calls + puts if c.expiry_ym == ym]


def _vehicle_of(p: FoPosition) -> str:
    if p.kind == "FUTURES":
        return "FUTURES"
    return "OPTION_BUY" if p.contract_side == "LONG" else "OPTION_SELL"


def verified_vehicles(balance: FoBalance) -> list[str]:
    """잔고에서 방향·종류가 확정된 보유 행(`FoPosition`)이 있고 NAV 가 있을 때, 그 행이 속한 수단.
    `UnverifiedHolding` 과 빈 잔고는 아무것도 검증하지 않는다."""
    if balance.cash.nav is None:
        return []
    seen = {_vehicle_of(p) for p in balance.positions}
    return [v for v in VEHICLES if v in seen]


@app.command("contracts")
@_config_errors
def contracts_cmd(
    product: str | None = typer.Option(
        None, "--product", help="mini|regular (기본 settings.fo.product)"
    ),
    today: str | None = typer.Option(None, "--today", help="기준일 YYYY-MM-DD (기본 오늘 KST)"),
) -> None:
    """종목 마스터(24h 캐시)에서 선물 월물·옵션 행사가 표를 JSON 으로. 주문·기록 없음."""
    from trader.cli import _boot

    settings = _boot(settings=True).settings
    name = product or settings.fo.product
    if name not in settings.fo.products:
        raise config.ConfigError(f"fo.products 에 없는 상품: {name}")
    pcfg = settings.fo.products[name]
    d = _today(today)
    cs = _master(settings)
    futs = fo_master._of_product(cs, pcfg, "FUTURES")
    front = _front_futures(cs, pcfg, today=d, roll_days=settings.fo.roll_days)
    ym, chain = _option_front(cs, pcfg, today=d, roll_days=settings.fo.roll_days)
    months = sorted({c.expiry_ym for c in futs if c.expiry_ym})
    out = {
        "product": name,
        "today": d.isoformat(),
        "contracts": len(cs),
        "futures": {
            "front": _contract_json(front) if front else None,
            "months": [_contract_json(next(c for c in futs if c.expiry_ym == m)) for m in months],
        },
        "options": {
            "front": {
                "expiry_ym": ym,
                "last_trade_date": fo_master.second_thursday(ym).isoformat() if ym else None,
                "strikes": sorted(
                    {str(c.strike) for c in chain if c.strike is not None}, key=Decimal
                ),
                "calls": sum(1 for c in chain if c.kind == "CALL"),
                "puts": sum(1 for c in chain if c.kind == "PUT"),
            }
            if ym
            else None
        },
    }
    typer.echo(json.dumps(out, ensure_ascii=False))


def _contract_json(c: fo_master.Contract) -> dict[str, Any]:
    return {
        "code": c.code,
        "name": c.name,
        "kind": c.kind,
        "expiry_ym": c.expiry_ym,
        "last_trade_date": fo_master.second_thursday(c.expiry_ym).isoformat()
        if c.expiry_ym
        else None,
        "strike": str(c.strike) if c.strike is not None else None,
    }


@app.command("chain")
@_config_errors
def chain_cmd(
    product: str | None = typer.Option(None, "--product", help="mini|regular"),
    strikes: int = typer.Option(4, "--strikes", help="ATM 양쪽으로 몇 단계"),
    today: str | None = typer.Option(None, "--today", help="기준일 YYYY-MM-DD"),
) -> None:
    """가까운 월물 옵션 체인(ATM ± n 행사가)의 시세·그릭을 JSON 으로(실전 시세 클라이언트,
    조회만)."""
    from trader.cli import _boot, _build_market

    boot = _boot(settings=True, secrets=True)
    settings = boot.settings
    name = product or settings.fo.product
    if name not in settings.fo.products:
        raise config.ConfigError(f"fo.products 에 없는 상품: {name}")
    pcfg = settings.fo.products[name]
    d = _today(today)
    cs = _master(settings)
    ym, chain = _option_front(cs, pcfg, today=d, roll_days=settings.fo.roll_days)
    if not ym:
        raise config.ConfigError("roll 창 밖의 옵션 월물이 없습니다")
    market = _build_market(boot.secrets)
    underlying, asof = market.get_kospi200()
    ks = sorted({c.strike for c in chain if c.strike is not None})
    atm = min(range(len(ks)), key=lambda i: abs(ks[i] - underlying))
    wanted = ks[max(0, atm - strikes) : atm + strikes + 1]
    quotes, errors = market.get_option_chain(chain, strikes=wanted)
    out = {
        "product": name,
        "expiry_ym": ym,
        "underlying": str(underlying),
        "underlying_asof": asof.isoformat() if asof else None,
        "strikes": [str(k) for k in wanted],
        "quotes": [q.model_dump(mode="json") for q in quotes],
        "errors": errors,
    }
    typer.echo(json.dumps(out, ensure_ascii=False))


def _find_fo_account(accounts: list[config.AccountConfig], account: str) -> config.AccountConfig:
    acct = next((a for a in accounts if a.id == account), None)
    if acct is None:
        raise config.ConfigError(f"계좌 id 를 찾을 수 없습니다: {account}")
    if acct.asset != "fo":
        raise config.ConfigError(f"계좌 {account} 은 asset fo 가 아닙니다(현물 계좌)")
    return acct


def _mask_cash(cash) -> dict[str, bool]:
    """금액은 값이 아니라 '있음/없음' 만 남긴다(비식별 스냅샷)."""
    return {
        "nav": cash.nav is not None,
        "deposit": cash.deposit is not None,
        "orderable_cash": cash.orderable_cash is not None,
        "orderable_total": cash.orderable_total is not None,
        "margin_total": cash.margin_total is not None,
        "add_margin": cash.add_margin is not None,
        "realized_today": cash.realized_today is not None,
        "unrealized": cash.unrealized is not None,
    }


@app.command("verify")
@_config_errors
def verify_cmd(
    account: str = typer.Option(..., "--account", help="선물옵션 계좌 id (asset: fo)"),
    vehicle: str | None = typer.Option(
        None, "--vehicle", help="FUTURES|OPTION_BUY|OPTION_SELL 하나만 검증(기본 전부)"
    ),
) -> None:
    """조회 전용 계약 검증: 마스터·코스피200·선물 시세·주문가능·잔고를 읽고 비식별 스냅샷 JSON 을
    출력한다. 보유 행으로 방향·종류·NAV 가 확인된 수단만 `fo_verified:*` 플래그를 적는다."""
    from trader.cli import _boot, _build_brokers, _build_market
    from trader.control import control

    if vehicle is not None and vehicle not in VEHICLES:
        raise config.ConfigError(f"--vehicle 은 {'|'.join(VEHICLES)} 중 하나")
    boot = _boot(settings=True, accounts=True, secrets=True, conn=True)
    settings, conn = boot.settings, boot.conn
    try:
        acct = _find_fo_account(boot.all_accounts, account)
        errors: list[str] = []
        out: dict[str, Any] = {
            "account": acct.id,
            "env": acct.env,
            "product": settings.fo.product,
            "contract_version": CONTRACT_VERSION,
        }

        def step(name: str, fn: Callable[[], Any]) -> Any:
            try:
                r = fn()
            except Exception as e:  # noqa: BLE001 — 점검은 모든 실패를 보고한다
                errors.append(f"{name}: {type(e).__name__}: {e}")
                out[name] = None
                return None
            out[name] = r
            return r

        pcfg = settings.fo.product_cfg()
        d = datetime.now(KST).date()
        cs = step("master", lambda: _master(settings)) or []
        out["master"] = {"contracts": len(cs)} if cs else out["master"]
        front = _front_futures(cs, pcfg, today=d, roll_days=settings.fo.roll_days) if cs else None
        out["front"] = _contract_json(front) if front else None

        market = step("market_client", lambda: _build_market(boot.secrets))
        out["market_client"] = market is not None
        if market is not None:
            step("kospi200", lambda: _k200_json(market.get_kospi200()))
            if front is not None:
                quote = step(
                    "quote", lambda: _quote_json(market.get_fo_price(front.code, "FUTURES"))
                )
            else:
                quote = None
        else:
            quote = None

        brokers = _build_brokers(boot.secrets, [acct], settings=settings)
        broker = next((b for b in brokers if b.account.id == acct.id), None)
        if broker is None:
            errors.append("broker: 앱키가 없어 브로커를 만들지 못했습니다")
            out["orderable"] = out["balance"] = None
            balance = None
        else:
            if front is not None:
                price = Decimal(quote["price"]) if quote and quote.get("price") else None
                step(
                    "orderable",
                    lambda: _orderable_json(
                        broker.orderable(front.code, "BUY", price, "LIMIT" if price else "MARKET")
                    ),
                )
            else:
                out["orderable"] = None
            balance = step("balance_raw", lambda: broker.get_balance())
            out.pop("balance_raw", None)
            out["balance"] = _balance_json(balance) if balance is not None else None

        verified: list[str] = []
        if balance is not None:
            for v in verified_vehicles(balance):
                if vehicle is not None and v != vehicle:
                    continue
                control.set_flag(
                    conn,
                    f"fo_verified:{acct.env}:{acct.id}:{settings.fo.product}:{v}",
                    CONTRACT_VERSION,
                )
                verified.append(v)
        out["verified"] = verified
        out["errors"] = errors
    finally:
        conn.close()
    typer.echo(json.dumps(out, ensure_ascii=False))
    if errors:
        raise typer.Exit(code=1)


def _k200_json(r: tuple[Decimal, datetime | None]) -> dict[str, Any]:
    px, asof = r
    return {"price": str(px), "asof": asof.isoformat() if asof else None}


def _quote_json(q) -> dict[str, Any]:
    return {
        "code": q.code,
        "kind": q.kind,
        "price": str(q.price),
        "bid": q.bid is not None,
        "ask": q.ask is not None,
        "basis": q.basis is not None,
        "last_trade_date": q.last_trade_date.isoformat() if q.last_trade_date else None,
        "days_left": q.days_left,
        "underlying": q.underlying is not None,
        "quote_asof": q.quote_asof.isoformat() if q.quote_asof else None,
    }


def _orderable_json(o) -> dict[str, Any]:
    return {
        "tot_qty": o.tot_qty,
        "lqd_qty": o.lqd_qty,
        "ord_qty": o.ord_qty,
        "cash": o.cash is not None,
        "bass_idx": o.bass_idx is not None,
    }


def _balance_json(b: FoBalance) -> dict[str, Any]:
    return {
        "positions": len(b.positions),
        "unverified": len(b.unverified),
        "rows": [
            {"code": p.code, "kind": p.kind, "side": p.contract_side, "qty": p.qty}
            for p in b.positions
        ],
        "unverified_reasons": [u.reason for u in b.unverified],
        **_mask_cash(b.cash),
    }


@app.command("order-test")
@_config_errors
def order_test_cmd(
    account: str = typer.Option(..., "--account", help="모의 선물옵션 계좌 id (실전 거부)"),
    vehicle: str = typer.Option(..., "--vehicle", help="FUTURES|OPTION_BUY|OPTION_SELL"),
) -> None:
    """실기 2 전용(거부 목록): 1계약 진입 → 잔고로 방향·종류·NAV·증거금 검증 → 즉시 청산 →
    체결 확정. 단계 1 에서는 명령 틀만 있고 실제 왕복은 단계 2 의 fo executor 로 연결된다."""
    from trader.cli import _boot

    if vehicle not in VEHICLES:
        raise config.ConfigError(f"--vehicle 은 {'|'.join(VEHICLES)} 중 하나")
    boot = _boot(settings=True, accounts=True, secrets=True)
    acct = _find_fo_account(boot.all_accounts, account)
    if acct.env != "paper":
        typer.echo("오류: fo order-test 는 모의(paper) 계좌에서만 실행합니다", err=True)
        raise typer.Exit(code=1)
    typer.echo(
        "fo order-test 는 아직 연결되지 않았습니다(Phase 26 단계 2 executor 뒤 활성). "
        "주문은 내지 않았습니다",
        err=True,
    )
    raise typer.Exit(code=2)
