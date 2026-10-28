"""`trader fo …` 명령(Phase 26 단계 1): 종목 마스터 조회·옵션 체인·브로커 계약 검증·주문 테스트 틀.

조회 전용 명령(`contracts`·`chain`·`verify`)은 어떤 주문도 내지 않는다. `verify` 는 보유 행이
실제로 있어 방향·종류·NAV 가 확인된 수단만 control `fo_verified:<env>:<계좌>:<상품>:<수단>` 에
`CONTRACT_VERSION` 을 적는다(빈 응답은 검증이 아니다). `order-test`·`close` 는 거부 목록(사용자
실행)."""

from __future__ import annotations

import functools
import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import typer

from trader import config
from trader.broker import fo_master
from trader.broker.fo import CONTRACT_VERSION
from trader.broker.models import FoBalance, FoPosition
from trader.config import KST
from trader.fo import executor
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
    return fo_master.option_front(
        contracts, product, today=today, roll_days=roll_days, last_trade_date_of=_no_quote
    )


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
    futs = fo_master.of_product(cs, pcfg, "FUTURES")
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
    if not ks:
        raise config.ConfigError(f"옵션 월물 {ym} 에 행사가가 있는 계약이 없습니다")
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
    """금액은 값이 아니라 '있음/없음' 만 남긴다(비식별 스냅샷). 모델이 강제하는 필드는 늘 있으므로
    선택 필드 NAV 만 보고한다."""
    return {"nav": cash.nav is not None}


@app.command("verify")
@_config_errors
def verify_cmd(
    account: str = typer.Option(..., "--account", help="선물옵션 계좌 id (asset: fo)"),
    vehicle: str | None = typer.Option(
        None, "--vehicle", help="FUTURES|OPTION_BUY|OPTION_SELL 하나만 검증(기본 전부)"
    ),
) -> None:
    """조회 전용 계약 검증: 마스터·코스피200·선물 시세·주문가능·잔고를 읽고 비식별 스냅샷 JSON 을
    출력한다. 모든 단계가 성공했고 보유 행으로 방향·종류·NAV 가 확인된 수단만 `fo_verified:*`
    플래그를 적는다(한 단계라도 실패하면 플래그 없이 exit 1)."""
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
        if not cs and not errors:
            errors.append("master: 계약이 없습니다")
        front = _front_futures(cs, pcfg, today=d, roll_days=settings.fo.roll_days) if cs else None
        out["front"] = _contract_json(front) if front else None
        if cs and front is None:
            # 월물이 없으면 시세·주문가능 단계를 못 돌리므로 검증 실패로 센다(플래그 없음)
            errors.append("front: roll 창 밖 선물 월물이 없습니다")

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
        # 마스터·시세·주문가능·잔고 중 하나라도 실패하면 검증 성공 상태를 남기지 않는다(Codex P1-4)
        if balance is not None and not errors:
            for v in verified_vehicles(balance):
                if vehicle is not None and v != vehicle:
                    continue
                control.set_flag(conn, executor.verified_key(settings, acct, v), CONTRACT_VERSION)
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


@app.command("session")
@_config_errors
def session_cmd(
    dry_run: bool = typer.Option(False, "--dry-run", help="판정·기록만(주문 없음)"),
    asof: str | None = typer.Option(
        None, "--asof", help="기준 시각 ISO (now − fo.max_input_age_s 보다 과거면 dry-run 강제)"
    ),
) -> None:
    """파생 세션 1회(Phase 26 단계 3): `_fo_inputs` 수집·저장 → fo 세션 → `_fo_decision`.
    `FAKE_CLAUDE=1` 이면 subprocess 없이 고정 FLAT HOLD. 결과 JSON 한 줄, 세션 실패면 exit 1."""
    from trader.cli import _boot, _build_brokers, _build_market
    from trader.fo import session as fo_session
    from trader.notify.telegram import make_notifier

    boot = _boot(settings=True, accounts=True, secrets=True, conn=True, log_name="fo")
    settings, conn = boot.settings, boot.conn
    at = datetime.fromisoformat(asof) if asof else None
    if at is not None and at.tzinfo is None:
        at = at.replace(tzinfo=KST)
    try:
        fo_accounts = list(boot.fo_accounts or [])
        brokers = {
            b.account.id: b for b in _build_brokers(boot.secrets, fo_accounts, settings=settings)
        }
        spot_accounts = list(boot.accounts or [])
        try:  # 현물 브로커는 거시 절의 헤지 보유 읽기용(없어도 세션은 돈다)
            spot_brokers = {
                b.account.id: b
                for b in _build_brokers(boot.secrets, spot_accounts, settings=settings)
            }
        except Exception as e:  # noqa: BLE001
            typer.echo(f"경고: 현물 브로커 없음({type(e).__name__}) — 헤지 보유 생략", err=True)
            spot_brokers = {}
        try:
            market = _build_market(boot.secrets)
        except Exception as e:  # noqa: BLE001 — 시세 없이도 입력 오류 절로 남기고 세션은 돈다
            typer.echo(
                f"경고: 시세 클라이언트 없음({type(e).__name__}) — 입력 절 일부 누락", err=True
            )
            market = None
        try:
            master = _master(settings)
        except Exception as e:  # noqa: BLE001
            typer.echo(f"경고: 마스터 적재 실패({type(e).__name__}) — 빈 목록", err=True)
            master = []
        out = fo_session.run(
            settings, conn, fo_accounts=fo_accounts, brokers=brokers, market_client=market,
            master=master, dry_run=dry_run, notifier=make_notifier(boot.secrets), asof=at,
            spot_accounts=spot_accounts, spot_brokers=spot_brokers, secrets=boot.secrets,
        )  # fmt: skip  (운영일은 asof 의 KST 날짜)
    finally:
        conn.close()
    typer.echo(json.dumps(out, ensure_ascii=False, default=str))
    if out.get("error"):
        raise typer.Exit(code=1)


@app.command("order-test")
@_config_errors
def order_test_cmd(
    account: str = typer.Option(..., "--account", help="모의 선물옵션 계좌 id (실전 거부)"),
    vehicle: str = typer.Option(..., "--vehicle", help="FUTURES|OPTION_BUY|OPTION_SELL"),
    wait_s: float = typer.Option(30.0, "--wait", help="체결 확정 대기(초)"),
) -> None:
    """실기 2 전용(거부 목록): 1계약 진입 → 체결 확정 → 잔고로 방향·종류·NAV·증거금 필드 확인 →
    즉시 청산 → 체결 확정. 전부 통과하면 그 수단의 `fo_verified` 플래그를 연다. 원장 미기록."""
    from trader.cli import _boot, _build_brokers, _build_market
    from trader.control import control
    from trader.fo import order_test

    if vehicle not in VEHICLES:
        raise config.ConfigError(f"--vehicle 은 {'|'.join(VEHICLES)} 중 하나")
    boot = _boot(settings=True, accounts=True, secrets=True, conn=True, log_name="fo")
    settings, conn = boot.settings, boot.conn
    try:
        acct = _find_fo_account(boot.all_accounts, account)
        if acct.env != "paper":
            typer.echo("오류: fo order-test 는 모의(paper) 계좌에서만 실행합니다", err=True)
            raise typer.Exit(code=1)
        broker = next(iter(_build_brokers(boot.secrets, [acct], settings=settings)), None)
        if broker is None:
            raise config.ConfigError("앱키가 없어 파생 브로커를 만들지 못했습니다")
        out = order_test.run(
            broker, _build_market(boot.secrets), _master(settings), settings, vehicle,
            today=datetime.now(KST).date(), wait_s=wait_s,
        )  # fmt: skip
        if out["ok"]:
            key = executor.verified_key(settings, acct, vehicle)
            control.set_flag(conn, key, CONTRACT_VERSION)
            out["verified_flag"] = key
    finally:
        conn.close()
    typer.echo(json.dumps(out, ensure_ascii=False, default=str))
    if not out["ok"]:
        raise typer.Exit(code=1)


def _exec_ctx(boot, *, dry_run: bool):
    """파생 실행 컨텍스트(CLI 용): 파생 계좌·브로커·시세·마스터·알림·국면/시장 뷰."""
    from trader.cli import _build_brokers, _build_market
    from trader.fo.executor import FoExecContext, context_sources
    from trader.notify.telegram import make_notifier

    settings = boot.settings
    fo_accounts = list(boot.fo_accounts or [])
    brokers = {
        b.account.id: b for b in _build_brokers(boot.secrets, fo_accounts, settings=settings)
    }
    try:
        market = _build_market(boot.secrets)
    except Exception as e:  # noqa: BLE001 — 시세 없이도 축소는 시장가로 간다
        typer.echo(f"경고: 시세 클라이언트 없음({type(e).__name__})", err=True)
        market = None
    try:
        master = _master(settings)
    except Exception as e:  # noqa: BLE001
        typer.echo(f"경고: 마스터 적재 실패({type(e).__name__}) — 빈 목록", err=True)
        master = []
    return FoExecContext(
        settings=settings, accounts=fo_accounts, conn=boot.conn, brokers=brokers,
        market_client=market, master=master, notifier=make_notifier(boot.secrets),
        dry_run=dry_run, secrets=boot.secrets,
        **context_sources(boot.conn, lambda: datetime.now(UTC)),
    )  # fmt: skip


@app.command("status")
@_config_errors
def status_cmd() -> None:
    """파생 포지션·오늘 결정·한도 상태(원장만 읽음, 텔레그램 `/fo` 와 같은 줄)."""
    from trader.cli import _boot
    from trader.fo import status as fo_status

    boot = _boot(settings=True, conn=True)
    try:
        typer.echo("\n".join(fo_status.lines(boot.conn, datetime.now(UTC))))
    finally:
        boot.conn.close()


@app.command("monitor")
@_config_errors
def monitor_cmd(
    once: bool = typer.Option(False, "--once", help="한 번만 돌고 끝"),
    dry_run: bool = typer.Option(False, "--dry-run", help="판정·기록만(주문·취소 없음)"),
    interval: int = typer.Option(60, "--interval", min=5, help="반복 간격(초)"),
) -> None:
    """파생 모니터(Phase 26 단계 4, 거부 목록 — 자동 주문). 데몬 `kr_fo_monitor` 와 같은
    `fo/engine.tick`. 반복 모드는 파생 정규장(08:45~15:45) 밖에서 쉰다."""
    import time

    from trader.cli import _boot
    from trader.fo import engine
    from trader.market_hours import is_fo_open

    boot = _boot(settings=True, accounts=True, secrets=True, conn=True, log_name="fo")
    try:
        ctx = _exec_ctx(boot, dry_run=dry_run)
        while True:
            if once or is_fo_open(ctx.now()):
                out = engine.tick(ctx)
                typer.echo(json.dumps(out, ensure_ascii=False, default=str))
            if once:
                break
            time.sleep(interval)
    finally:
        boot.conn.close()


@app.command("adopt")
@_config_errors
def adopt_cmd(
    account: str = typer.Option(..., "--account"),
    position: int = typer.Option(..., "--position", help="unknown 포지션 id"),
    side: str = typer.Option(..., "--side", help="LONG|SHORT (계약 보유 방향)"),
    kind: str = typer.Option(..., "--kind", help="FUTURES|CALL|PUT"),
    qty: int = typer.Option(..., "--qty", min=1),
    stop_pt: float = typer.Option(..., "--stop-pt", help="기초지수 손절선"),
) -> None:
    """사람의 승계: 브로커 잔고 행(코드·방향·종류·수량)과 **일치할 때만** unknown → held."""
    from trader.cli import _boot, _build_brokers
    from trader.fo import positions

    boot = _boot(settings=True, accounts=True, secrets=True, conn=True, log_name="fo")
    try:
        acct = _find_fo_account(boot.all_accounts, account)
        broker = next(iter(_build_brokers(boot.secrets, [acct], settings=boot.settings)), None)
        if broker is None:
            raise config.ConfigError("앱키가 없어 파생 브로커를 만들지 못했습니다")
        from trader.fo import store

        pos = store.get_position(boot.conn, position)
        contract = None
        try:
            contract = next((c for c in _master(boot.settings) if pos and c.code == pos.code), None)
        except Exception as e:  # noqa: BLE001 — 마스터 없으면 최종거래일 없이 승계(경고)
            typer.echo(f"경고: 마스터 없음({type(e).__name__}) — 최종거래일 미기록", err=True)
        ltd = fo_master.last_trade_date(contract, None) if contract is not None else None
        ok = positions.adopt(
            boot.conn, position_id=position, side=side, kind=kind, qty=qty,
            stop_pt=Decimal(str(stop_pt)), balance=broker.get_balance(), account_id=acct.id,
            last_trade_date=ltd,
        )  # fmt: skip
        boot.conn.commit()
    finally:
        boot.conn.close()
    typer.echo(json.dumps({"position_id": position, "adopted": ok}, ensure_ascii=False))
    if not ok:
        raise typer.Exit(code=1)


@app.command("close")
@_config_errors
def close_cmd(
    account: str = typer.Option(..., "--account"),
    position: int = typer.Option(..., "--position"),
    confirm: str = typer.Option("", "--confirm", help="CLOSE 를 적어야 실행"),
) -> None:
    """수동 청산(거부 목록 — 사용자만): 진입 회수 → 잔고 재조회 → 청산 다리. unknown 포지션은
    먼저 `fo adopt` 로 승계해야 한다."""
    from trader.cli import _boot
    from trader.fo import store

    if confirm != "CLOSE":
        raise config.ConfigError("--confirm CLOSE 가 있어야 청산합니다")
    boot = _boot(settings=True, accounts=True, secrets=True, conn=True, log_name="fo")
    try:
        ctx = _exec_ctx(boot, dry_run=False)
        acct = executor.account_of(ctx, account)
        pos = store.get_position(boot.conn, position)
        if acct is None or pos is None or pos.account_id != acct.id:
            raise config.ConfigError(f"계좌 {account} 의 포지션 {position} 이 없습니다")
        out = executor.reduce(ctx, acct, pos, intent="CLOSE", qty=None, reason="수동 청산(CLI)")
    finally:
        boot.conn.close()
    typer.echo(json.dumps(out, ensure_ascii=False, default=str))
