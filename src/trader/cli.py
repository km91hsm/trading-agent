"""`trader` 명령행 진입점 (typer)."""

from __future__ import annotations

import logging
import os
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import typer

from trader import config
from trader.broker.accounts import KisBroker
from trader.broker.kis_auth import KisAuth
from trader.broker.kis_client import KisClient
from trader.broker.kis_market import KisMarket
from trader.logging_setup import configure
from trader.notify.telegram import make_notifier, make_receiver
from trader.orchestrator.session import run_session
from trader.platform import paths
from trader.platform.claude_bin import ClaudeNotFound
from trader.sources.inputs import InputDeps, collect_and_save

app = typer.Typer(help="LLM 멀티 에이전트 주식 자동매매", no_args_is_help=True)
kis_app = typer.Typer(help="KIS 브로커 도구", no_args_is_help=True)
app.add_typer(kis_app, name="kis")

_KST = ZoneInfo("Asia/Seoul")


def _force_utf8() -> None:
    """콘솔 출력을 UTF-8 로 고정한다 (Windows cp949 대비)."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


@app.callback()
def main() -> None:
    _force_utf8()


# --- 조립 함수 (테스트에서 monkeypatch 로 교체) ------------------------------


_clients: dict[tuple[config.Env, str], KisClient] = {}


def _build_client(secrets: config.Secrets, env: config.Env) -> KisClient:
    """env 별 KisClient. 같은 프로세스 안에서는 하나만 만들어 레이트리미터·토큰을 공유한다."""
    key = secrets.KIS_PAPER_APP_KEY if env == "paper" else secrets.KIS_REAL_APP_KEY
    secret = secrets.KIS_PAPER_APP_SECRET if env == "paper" else secrets.KIS_REAL_APP_SECRET
    if not key or not secret:
        raise config.ConfigError(f"{env} 앱키·시크릿이 secrets/.env 에 없습니다")
    cache_key = (env, key)
    if cache_key not in _clients:
        _clients[cache_key] = KisClient(env, KisAuth(env, key, secret))
    return _clients[cache_key]


def _build_market(secrets: config.Secrets) -> KisMarket:
    """시세 클라이언트 (실전 env 전용)."""
    return KisMarket(_build_client(secrets, "real"))


def _session_brokers(
    secrets: config.Secrets, accounts: list[config.AccountConfig], *, dry_run: bool
) -> dict:
    """세션용 브로커 맵. FAKE 모드만 가짜 브로커(현금 1천만원/1만달러)를 준다."""
    from trader.orchestrator.session import is_fake

    if is_fake():
        from decimal import Decimal

        from trader.broker.fake import FakeBroker
        from trader.broker.models import Cash

        cash = {
            "KR": Cash(currency="KRW", available=Decimal("10000000"), total=Decimal("10000000")),
            "US": Cash(currency="USD", available=Decimal("10000"), total=Decimal("10000")),
        }
        return {a.id: FakeBroker(a, cash=cash) for a in accounts}
    return {}  # 실제 세션은 tool 서브프로세스가 _tool_ctx 로 자기 브로커를 만든다


def _build_brokers(
    secrets: config.Secrets, accounts: list[config.AccountConfig]
) -> list[KisBroker]:
    """계좌별 브로커. 앱키가 없는 env 의 계좌는 경고 후 건너뛴다."""
    clients: dict[str, KisClient] = {}
    brokers: list[KisBroker] = []
    for a in accounts:
        if a.env not in clients:
            try:
                clients[a.env] = _build_client(secrets, a.env)
            except config.ConfigError as e:
                logging.getLogger("trader.kis").warning("계좌 %s 건너뜀: %s", a.id, e)
                continue
        brokers.append(KisBroker(a, clients[a.env]))
    return brokers


# --- 명령 -------------------------------------------------------------------


@app.command()
def status() -> None:
    """설정·원장·TODO 를 읽어 state/STATUS.md 를 재생성한다."""
    from trader import status as status_mod
    from trader.ledger import db

    try:
        configure(paths.state_dir())
        settings = config.load_settings()
        accounts = config.load_accounts()
        conn = db.connect()
        try:
            db.init_schema(conn)
            db.sync_accounts(conn, accounts)
            text = status_mod.build_status(conn, settings, accounts, paths.docs_dir() / "TODO.md")
        finally:
            conn.close()
        out = status_mod.write_status(text)
    except (config.ConfigError, paths.ProjectRootNotFound) as e:
        typer.echo(f"오류: {e}", err=True)
        raise typer.Exit(code=1) from e
    typer.echo(f"저장: {out}")


@kis_app.command("ping")
def kis_ping(
    account: str | None = typer.Option(None, "--account", help="특정 계좌 id 만 점검"),
) -> None:
    """KIS 시세(005930·AAPL)·휴장일·계좌별 현금을 점검한다. 실패가 있으면 exit 1."""
    try:
        configure(paths.state_dir())
        secrets = config.load_secrets()
        accounts = config.load_accounts()
    except (config.ConfigError, paths.ProjectRootNotFound) as e:
        typer.echo(f"오류: {e}", err=True)
        raise typer.Exit(code=1) from e
    if account:
        accounts = [a for a in accounts if a.id == account]
        if not accounts:
            typer.echo(f"오류: 계좌 id 를 찾을 수 없습니다: {account}", err=True)
            raise typer.Exit(code=1)

    failures = 0

    def check(label: str, fn) -> None:
        nonlocal failures
        try:
            result = fn()
        except Exception as e:  # noqa: BLE001 - 점검 도구는 모든 실패를 보고한다
            failures += 1
            typer.echo(f"[실패] {label}: {type(e).__name__}: {e}")
            return
        typer.echo(f"[OK] {label}: {result}")

    try:
        market = _build_market(secrets)
    except Exception as e:  # noqa: BLE001
        failures += 1
        typer.echo(f"[실패] 시세 클라이언트 생성: {type(e).__name__}: {e}")
        market = None
    if market is not None:
        today = datetime.now(_KST).date()
        check(f"휴장일 {today}", lambda: "휴장" if market.is_holiday(today) else "개장")
        check("005930 현재가", lambda: market.get_price_kr("005930").price)
        check("AAPL 현재가", lambda: market.get_price_us("AAPL", "NAS").price)

    def cash_label(b: KisBroker, m: str) -> str:
        c = b.get_cash(m)  # 모의 2/s 한도를 고려해 한 번만 호출한다
        return f"{c.available} {c.currency}"

    brokers = _build_brokers(secrets, accounts)
    built = {b.account.id for b in brokers}
    for a in accounts:
        if a.id not in built:
            failures += 1
            typer.echo(f"[실패] {a.id}: {a.env} 앱키·시크릿이 없어 점검하지 못했습니다")
    for broker in brokers:
        for m in broker.account.markets:
            check(f"{broker.account.id} {m} 현금", lambda b=broker, m=m: cash_label(b, m))
    typer.echo(f"실패 {failures}건")
    if failures:
        raise typer.Exit(code=1)


def _tick_size_kr(price: Decimal) -> Decimal:
    """KRX 호가단위 (2023-01 개편 기준)."""
    for limit, tick in (
        (2000, 1),
        (5000, 5),
        (20000, 10),
        (50000, 50),
        (200000, 100),
        (500000, 500),
    ):
        if price < limit:
            return Decimal(tick)
    return Decimal(1000)


@kis_app.command("order-test")
def kis_order_test(
    account: str = typer.Option(..., "--account", help="모의 계좌 id (실전 거부)"),
    ticker: str = typer.Option("005930", "--ticker", help="KR 종목코드"),
) -> None:
    """Phase 4 실기 전용: 모의 계좌에 현재가 10% 아래 1주 지정가 매수 → 즉시 취소 (원장 미기록)."""
    try:
        configure(paths.state_dir())
        secrets = config.load_secrets()
        accounts = config.load_accounts()
    except (config.ConfigError, paths.ProjectRootNotFound) as e:
        typer.echo(f"오류: {e}", err=True)
        raise typer.Exit(code=1) from e
    acct = next((a for a in accounts if a.id == account), None)
    if acct is None:
        typer.echo(f"오류: 계좌 id 를 찾을 수 없습니다: {account}", err=True)
        raise typer.Exit(code=1)
    if acct.env != "paper":
        typer.echo("오류: order-test 는 모의(paper) 계좌에서만 실행합니다", err=True)
        raise typer.Exit(code=1)
    broker = next((b for b in _build_brokers(secrets, [acct]) if b.account.id == acct.id), None)
    if broker is None:
        typer.echo("오류: 모의 앱키가 없어 브로커를 만들지 못했습니다", err=True)
        raise typer.Exit(code=1)
    from trader.broker.models import OrderRequest

    try:
        price = _build_market(secrets).get_price_kr(ticker).price
        tick = _tick_size_kr(price)
        limit = (price * Decimal("0.9") // tick) * tick
        req = OrderRequest(
            ticker=ticker,
            market="KR",
            side="BUY",
            qty=1,
            order_type="LIMIT",
            price=limit,
            exchange=None,
        )
        res = broker.place_order(req)
        typer.echo(
            f"접수: {ticker} 1주 @{limit} (현재가 {price}) 주문번호 {res.order_no} {res.message}"
        )
    except Exception as e:  # noqa: BLE001 - 실기 점검은 모든 실패를 보고한다
        typer.echo(f"주문 실패: {type(e).__name__}: {e}")
        raise typer.Exit(code=1) from e
    try:
        cres = broker.cancel_order(res.order_no or "", market="KR", org_no=res.org_no)
        typer.echo(f"취소: 주문번호 {cres.order_no} {cres.message}")
    except Exception as e:  # noqa: BLE001
        typer.echo(
            f"취소 실패(미체결 주문이 남아 있을 수 있음, HTS/앱에서 확인): {type(e).__name__}: {e}"
        )
        raise typer.Exit(code=1) from e


def _build_input_deps(secrets: config.Secrets, cfg: config.SourcesConfig) -> InputDeps:
    """수집용 클라이언트를 조립한다. 시세 클라이언트를 못 만들면 해당 절만 건너뛴다."""
    from trader.sources.dart import DartClient
    from trader.sources.edgar import EdgarClient

    cache = paths.state_dir() / "cache"
    try:
        market = _build_market(secrets)
    except Exception as e:  # noqa: BLE001
        logging.getLogger("trader.sources").warning("시세 클라이언트 생성 실패: %s", e)
        market = None
    dart = DartClient(secrets.DART_API_KEY, cache) if secrets.DART_API_KEY else None
    edgar = EdgarClient(cfg.edgar.user_agent, cache) if cfg.edgar.enabled else None
    return InputDeps(market=market, dart=dart, edgar=edgar)


@app.command("collect")
def collect(
    ticker: str = typer.Option(..., "--ticker", help="종목코드/심볼"),
    market: str = typer.Option("KR", "--market", help="KR 또는 US"),
    exchange: str | None = typer.Option(None, "--exchange", help="미국 거래소 코드 (NAS/NYS/AMS)"),
    asof: str | None = typer.Option(
        None, "--asof", help="기준 시각 ISO8601 (기본: 현재 UTC, 24시간 넘은 과거는 거부)"
    ),
    force: bool = typer.Option(False, "--force", help="기존 00_inputs 를 덮어씀"),
) -> None:
    """종목의 00_inputs.json 을 수집·저장한다. 일부 절 실패는 정상(exit 0)."""
    import json

    try:
        configure(paths.state_dir())
        if market not in ("KR", "US"):
            raise config.ConfigError(f"시장은 KR 또는 US 여야 합니다: {market}")
        cfg = config.load_sources()
        secrets = config.load_secrets()
        when = datetime.fromisoformat(asof) if asof else datetime.now(UTC)
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        deps = _build_input_deps(secrets, cfg)
        from trader.orchestrator.run_store import read_checkpoint as _rc
        from trader.orchestrator.run_store import run_dir as _rd

        existed = (
            not force
            and _rc(_rd(when.astimezone(_KST).date(), market, ticker), "00_inputs") is not None  # type: ignore[arg-type]
        )
        path = collect_and_save(
            ticker=ticker,
            market=market,
            exchange=exchange,
            asof=when,
            cfg=cfg,  # type: ignore[arg-type]
            secrets=secrets,
            deps=deps,
            force=force,
        )
        errors = json.loads(path.read_text(encoding="utf-8")).get("errors", [])
    except (config.ConfigError, paths.ProjectRootNotFound, ValueError) as e:
        typer.echo(f"오류: {e}", err=True)
        raise typer.Exit(code=1) from e
    if existed:
        typer.echo(f"건너뜀(이미 존재, --force 로 덮어씀): {path}")
    else:
        typer.echo(f"저장: {path}")
    for e in errors:
        typer.echo(f"  - {e}")
    typer.echo(f"오류 절 {len(errors)}건")


features_app = typer.Typer(help="피처 스토어", no_args_is_help=True)
app.add_typer(features_app, name="features")


def _build_dart(secrets: config.Secrets):
    from trader.sources.dart import DartClient

    cache = paths.state_dir() / "cache"
    return DartClient(secrets.DART_API_KEY, cache) if secrets.DART_API_KEY else None


def _run_features(action: str, market: str | None, **kwargs) -> None:
    from trader.ml import features

    try:
        configure(paths.state_dir())
        secrets = config.load_secrets()
        universe = config.load_universe()
        markets = [market] if market else ["KR", "US"]
        if any(m not in ("KR", "US") for m in markets):
            raise config.ConfigError(f"시장은 KR 또는 US 여야 합니다: {market}")
        market_client = _build_market(secrets)
        dart = _build_dart(secrets)
        fn = features.backfill if action == "backfill" else features.snapshot
        n = fn(
            market_client=market_client,
            dart=dart,
            universe=universe,
            markets=markets,  # type: ignore[arg-type]
            progress=lambda s: typer.echo(f"  {s}"),
            **kwargs,
        )
    except (config.ConfigError, paths.ProjectRootNotFound) as e:
        typer.echo(f"오류: {e}", err=True)
        raise typer.Exit(code=1) from e
    except Exception as e:  # noqa: BLE001 - 시세 클라이언트 생성 실패 등
        typer.echo(f"오류: {type(e).__name__}: {e}", err=True)
        raise typer.Exit(code=1) from e
    typer.echo(f"{action}: {n}행 저장")


@features_app.command("snapshot")
def features_snapshot(
    market: str | None = typer.Option(None, "--market", help="KR 또는 US (기본: 둘 다)"),
    asof: str | None = typer.Option(
        None, "--asof", help="기준 시각 ISO8601 (24시간 넘은 과거는 거부)"
    ),
) -> None:
    """유니버스 전체의 오늘 피처 행을 저장한다."""
    when = datetime.fromisoformat(asof) if asof else None
    if when is not None and when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    _run_features("snapshot", market, asof=when)


@features_app.command("backfill")
def features_backfill(
    years: int = typer.Option(3, "--years", help="과거 적재 연수 (최대 약 4년=1000봉)"),
    market: str | None = typer.Option(None, "--market", help="KR 또는 US (기본: 둘 다)"),
) -> None:
    """유니버스 전체의 과거 피처를 재구성해 저장한다."""
    _run_features("backfill", market, years=years)


# --- analyze -----------------------------------------------------------------


@app.command("analyze")
def analyze(
    ticker: str = typer.Option(..., "--ticker", help="종목코드/심볼"),
    market: str = typer.Option("KR", "--market", help="KR 또는 US"),
    exchange: str | None = typer.Option(None, "--exchange", help="미국 거래소 코드 (NAS/NYS/AMS)"),
    asof: str | None = typer.Option(None, "--asof", help="기준 시각 ISO8601 (기본: 현재 UTC)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="판정·기록까지만, 주문·승인 요청 없음"),
    skip_collect: bool = typer.Option(False, "--skip-collect", help="00_inputs 수집 생략"),
) -> None:
    """오케스트레이터 세션을 띄워 종목 하나를 분석하고 30_decision.json 을 만든다."""
    import json
    from zoneinfo import ZoneInfo

    from trader.ledger import db
    from trader.orchestrator.run_store import read_checkpoint, run_dir
    from trader.orchestrator.session import PipelineError, SessionSpec

    try:
        configure(paths.state_dir())
        if market not in ("KR", "US"):
            raise config.ConfigError(f"시장은 KR 또는 US 여야 합니다: {market}")
        if market == "US" and not exchange:
            raise config.ConfigError("미국 종목은 --exchange(거래소 코드)가 필요합니다")
        settings = config.load_settings()
        accounts = config.load_accounts()
        secrets = config.load_secrets()
        when = datetime.fromisoformat(asof) if asof else datetime.now(UTC)
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        day = when.astimezone(ZoneInfo("Asia/Seoul")).date()
        d = run_dir(day, market, ticker)  # type: ignore[arg-type]
        if read_checkpoint(d, "00_inputs") is None:
            if skip_collect:
                raise config.ConfigError(f"00_inputs 가 없습니다: {d} (--skip-collect 해제)")
            cfg = config.load_sources()
            collect_and_save(
                ticker=ticker,
                market=market,
                exchange=exchange,
                asof=when,
                cfg=cfg,  # type: ignore[arg-type]
                secrets=secrets,
                deps=_build_input_deps(secrets, cfg),
            )
        conn = db.connect()
        db.init_schema(conn)
        try:
            from trader.orchestrator import tools as _tools

            _tctx = _tools.ToolContext(settings=settings, accounts=accounts, conn=conn)
            _tools.ensure_quant(_tctx, ticker=ticker, market=market, date=day)  # type: ignore[arg-type]
            _tools.ensure_calibration(_tctx, ticker=ticker, market=market, date=day)  # type: ignore[arg-type]
            res = run_session(
                SessionSpec(
                    market=market,
                    date=day,
                    tickers=[{"ticker": ticker, "exchange": exchange}],
                    dry_run=dry_run,
                    asof=when,
                ),
                settings,
                conn,
                accounts,
                brokers=_session_brokers(secrets, accounts, dry_run=dry_run),
                notifier=make_notifier(secrets),
            )
        finally:
            conn.close()
    except (
        config.ConfigError,
        paths.ProjectRootNotFound,
        PipelineError,
        ValueError,
        ClaudeNotFound,
    ) as e:
        typer.echo(f"오류: {e}", err=True)
        raise typer.Exit(code=1) from e
    if not res.ok:
        typer.echo(f"세션 실패: {res.error}", err=True)
        raise typer.Exit(code=1)
    mode = "FAKE" if res.fake else f"session={res.session_id}"
    typer.echo(f"세션 완료 ({mode}) 비용 ${res.cost_usd or 0:.2f}")
    if res.stages_written:
        typer.echo("새로 쓴 단계: " + ", ".join(res.stages_written))
    decision = read_checkpoint(d, "30_decision")
    if decision is None:
        typer.echo("결정 없음 (30_decision.json 이 만들어지지 않았습니다)", err=True)
        raise typer.Exit(code=1)
    data = decision["data"]
    typer.echo(
        f"결정: {data['action']} target_weight={data['target_weight']} "
        f"confidence={data['confidence']}"
    )
    typer.echo(f"저장: {d / '30_decision.json'}")
    errors = (read_checkpoint(d, "00_inputs") or {}).get("errors", [])
    if errors:
        typer.echo(f"입력 오류 절 {len(errors)}건: {json.dumps(errors, ensure_ascii=False)[:300]}")


# --- tool: LLM 오케스트레이터 전용 (stdout 에 JSON 만) ---------------------

tool_app = typer.Typer(help="오케스트레이터 세션용 도구 (출력은 JSON 한 줄)", no_args_is_help=True)
app.add_typer(tool_app, name="tool")
checkpoint_app = typer.Typer(help="체크포인트 도구", no_args_is_help=True)
tool_app.add_typer(checkpoint_app, name="checkpoint")


def _tool_ctx():
    from trader.ledger import db
    from trader.orchestrator.tools import ToolContext

    configure(paths.state_dir())
    logging.getLogger().handlers = [
        h
        for h in logging.getLogger().handlers
        if not isinstance(h, logging.StreamHandler) or isinstance(h, logging.FileHandler)
    ]  # stdout 은 JSON 전용이므로 콘솔 핸들러 제거
    conn = db.connect()
    db.init_schema(conn)
    secrets = config.load_secrets()
    accounts = config.load_accounts()
    # 세션이 env 로 넘긴 값. 없으면 안전하게 dry_run(주문·승인 요청 없음).
    dry_run = os.environ.get("TRADER_DRY_RUN", "1") != "0"
    # dry_run 도 잔고로 판정은 한다(주문·승인 요청만 생략). 브로커 생성은 네트워크를 쓰지 않는다.
    brokers = {b.account.id: b for b in _build_brokers(secrets, accounts)}
    try:
        holiday_fn = _build_market(secrets).is_holiday  # 7일 캐시, 보유 거래일 계산용
    except Exception:  # noqa: BLE001 - 실전 앱키가 없으면 휴장일 없이 평일 수로 계산
        holiday_fn = None
    return ToolContext(
        settings=config.load_settings(),
        accounts=accounts,
        conn=conn,
        brokers=brokers,
        notifier=make_notifier(secrets),
        dry_run=dry_run,
        holiday_fn=holiday_fn,
    )


def _emit(result: dict) -> None:
    import json

    typer.echo(json.dumps(result, ensure_ascii=False, default=str))
    if not result.get("ok", False):
        raise typer.Exit(code=1)


def _tool_run(fn, **kwargs) -> None:
    import json
    from datetime import date as _date

    try:
        ctx = _tool_ctx()
        if "date" in kwargs and isinstance(kwargs["date"], str):
            kwargs["date"] = _date.fromisoformat(kwargs["date"])
        if "payload" in kwargs and isinstance(kwargs["payload"], str):
            kwargs["payload"] = json.loads(kwargs["payload"])
        result = fn(ctx, **kwargs)
    except Exception as e:  # noqa: BLE001 - 도구는 항상 JSON 으로 끝난다
        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    _emit(result)


def _payload_arg(json_text: str | None) -> str:
    if json_text is not None:
        return json_text
    return sys.stdin.read()


@tool_app.command("status")
def tool_status(
    market: str = typer.Option(..., "--market"), date: str = typer.Option(..., "--date")
) -> None:
    """오늘 종목별 체크포인트 목록과 세션 수."""
    from trader.orchestrator import tools

    _tool_run(tools.status, market=market, date=date)


@tool_app.command("collect")
def tool_collect(
    ticker: str = typer.Option(..., "--ticker"),
    market: str = typer.Option("KR", "--market"),
    exchange: str | None = typer.Option(None, "--exchange"),
    asof: str | None = typer.Option(None, "--asof"),
) -> None:
    """00_inputs 수집 (있으면 건너뜀). JSON {ok, path, errors}."""
    import json

    try:
        ctx = _tool_ctx()
        cfg = config.load_sources()
        secrets = config.load_secrets()
        when = datetime.fromisoformat(asof) if asof else ctx.now()
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        path = collect_and_save(
            ticker=ticker,
            market=market,
            exchange=exchange,
            asof=when,
            cfg=cfg,  # type: ignore[arg-type]
            secrets=secrets,
            deps=_build_input_deps(secrets, cfg),
        )
        errors = json.loads(path.read_text(encoding="utf-8")).get("errors", [])
        from trader.orchestrator import tools

        day_ = when.astimezone(_KST).date()
        q = tools.ensure_quant(ctx, ticker=ticker, market=market, date=day_)  # type: ignore[arg-type]
        tools.ensure_calibration(ctx, ticker=ticker, market=market, date=day_)  # type: ignore[arg-type]
        result = {"ok": True, "path": str(path), "errors": errors, "quant": bool(q)}
    except Exception as e:  # noqa: BLE001
        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    _emit(result)


@checkpoint_app.command("list")
def tool_checkpoint_list(
    ticker: str = typer.Option(..., "--ticker"),
    market: str = typer.Option(..., "--market"),
    date: str = typer.Option(..., "--date"),
) -> None:
    from trader.orchestrator import tools

    _tool_run(tools.checkpoint_list, ticker=ticker, market=market, date=date)


@checkpoint_app.command("read")
def tool_checkpoint_read(
    ticker: str = typer.Option(..., "--ticker"),
    market: str = typer.Option(..., "--market"),
    date: str = typer.Option(..., "--date"),
    stage: str = typer.Option(..., "--stage"),
    section: str | None = typer.Option(None, "--section"),
) -> None:
    from trader.orchestrator import tools

    _tool_run(
        tools.checkpoint_read, ticker=ticker, market=market, date=date, stage=stage, section=section
    )


@checkpoint_app.command("write")
def tool_checkpoint_write(
    ticker: str = typer.Option(..., "--ticker"),
    market: str = typer.Option(..., "--market"),
    date: str = typer.Option(..., "--date"),
    stage: str = typer.Option(..., "--stage"),
    agent: str | None = typer.Option(None, "--agent"),
    json_text: str | None = typer.Option(None, "--json", help="페이로드 JSON (없으면 stdin)"),
    model: str | None = typer.Option(None, "--model", help="서브에이전트 모델명 (call.model 기록)"),
) -> None:
    from trader.orchestrator import tools

    _tool_run(
        tools.checkpoint_write,
        model=model,
        ticker=ticker,
        market=market,
        date=date,
        stage=stage,
        payload=_payload_arg(json_text),
        agent=agent,
    )


@checkpoint_app.command("bundle")
def tool_checkpoint_bundle(
    ticker: str = typer.Option(..., "--ticker"),
    market: str = typer.Option(..., "--market"),
    date: str = typer.Option(..., "--date"),
    kind: str = typer.Option(..., "--kind", help="researcher | trader"),
    round: int | None = typer.Option(None, "--round"),
    side: str | None = typer.Option(None, "--side"),
) -> None:
    """서브에이전트 입력 묶음(리포트+턴 또는 리포트+요약+컨텍스트)을 JSON 으로."""
    from trader.orchestrator import tools

    _tool_run(
        tools.checkpoint_bundle,
        ticker=ticker,
        market=market,
        date=date,
        kind=kind,
        round=round,
        side=side,
    )


@checkpoint_app.command("summarize")
def tool_checkpoint_summarize(
    ticker: str = typer.Option(..., "--ticker"),
    market: str = typer.Option(..., "--market"),
    date: str = typer.Option(..., "--date"),
) -> None:
    from trader.orchestrator import tools

    _tool_run(tools.checkpoint_summarize, ticker=ticker, market=market, date=date)


@tool_app.command("submit-decision")
def tool_submit_decision(
    ticker: str = typer.Option(..., "--ticker"),
    market: str = typer.Option(..., "--market"),
    date: str = typer.Option(..., "--date"),
    json_text: str | None = typer.Option(None, "--json", help="Decision JSON (없으면 stdin)"),
    stage: str | None = typer.Option(None, "--stage", help="재평가면 31_decision_r<n>"),
) -> None:
    from trader.orchestrator import tools

    _tool_run(
        tools.submit_decision,
        ticker=ticker,
        market=market,
        date=date,
        payload=_payload_arg(json_text),
        stage=stage,
    )


# --- 승인 (Phase 4) ------------------------------------------------------------

approvals_app = typer.Typer(help="approve 모드 계좌의 승인 요청 조회·승인·거부")
app.add_typer(approvals_app, name="approvals")
telegram_app = typer.Typer(help="텔레그램 수신 (승인 명령 1회 폴링)")
app.add_typer(telegram_app, name="telegram")


def _exec_ctx():
    """CLI 용 ExecContext. 브로커는 앱키가 있는 계좌만 (없으면 승인 시 실패로 기록)."""
    from trader.ledger import db
    from trader.orchestrator.executor import ExecContext

    configure(paths.state_dir())
    secrets = config.load_secrets()
    accounts = config.load_accounts()
    conn = db.connect()
    db.init_schema(conn)
    return ExecContext(
        settings=config.load_settings(),
        accounts=accounts,
        conn=conn,
        brokers={b.account.id: b for b in _build_brokers(secrets, accounts)},
        notifier=make_notifier(secrets),
    ), secrets


def _approval_line(a: dict) -> str:
    o = a["order"]
    return (
        f"#{a['id']} {a['account_id']} {o.get('side')} {o.get('ticker')} {o.get('qty')}주 "
        f"@{o.get('price') or '시장가'} 만료 {a['expires_at']}"
    )


@approvals_app.command("list")
def approvals_list() -> None:
    """미처리 승인 요청 목록 (만료분은 expired 로 정리)."""
    from trader.ledger import repo
    from trader.orchestrator import executor

    try:
        ectx, _ = _exec_ctx()
    except (config.ConfigError, paths.ProjectRootNotFound) as e:
        typer.echo(f"오류: {e}", err=True)
        raise typer.Exit(code=1) from e
    try:
        expired = executor.expire_approvals(ectx)
        pending = repo.pending_approvals(ectx.conn)
    finally:
        ectx.conn.close()
    if expired:
        typer.echo(f"만료 처리: {', '.join(f'#{i}' for i in expired)}")
    if not pending:
        typer.echo("미승인 주문 없음")
    for a in pending:
        typer.echo(_approval_line(a))


def _decide(approval_id: int, action: str) -> None:
    from trader.orchestrator import executor

    try:
        ectx, _ = _exec_ctx()
    except (config.ConfigError, paths.ProjectRootNotFound) as e:
        typer.echo(f"오류: {e}", err=True)
        raise typer.Exit(code=1) from e
    try:
        r = (
            executor.approve(ectx, approval_id)
            if action == "approve"
            else executor.reject(ectx, approval_id)
        )
    finally:
        ectx.conn.close()
    if not r["ok"]:
        typer.echo(f"실패: {r.get('error') or r.get('order')}", err=True)
        raise typer.Exit(code=1)
    if action == "approve":
        o = r["order"]
        typer.echo(f"승인 #{approval_id} → 주문 접수번호 {o.get('order_no')} {o.get('message')}")
    else:
        typer.echo(f"거부 #{approval_id}")


@approvals_app.command("approve")
def approvals_approve(approval_id: int = typer.Argument(..., help="승인 요청 id")) -> None:
    """승인 요청을 주문으로 실행한다 (30분 만료, 1회만)."""
    _decide(approval_id, "approve")


@approvals_app.command("reject")
def approvals_reject(approval_id: int = typer.Argument(..., help="승인 요청 id")) -> None:
    """승인 요청을 거부한다."""
    _decide(approval_id, "reject")


@telegram_app.command("poll")
def telegram_poll() -> None:
    """getUpdates 1회: /approve <id>·/reject <id>·/status 를 처리하고 오프셋을 저장한다."""
    _telegram_run(interval=0, max_iterations=1)


@telegram_app.command("loop")
def telegram_loop(
    interval: int = typer.Option(30, "--interval", help="폴링 주기(초)"),
    max_iterations: int | None = typer.Option(None, "--max-iterations", hidden=True),
) -> None:
    """텔레그램 명령을 주기적으로 처리한다 (데몬 밖에서 단독 실행용)."""
    _telegram_run(interval=interval, max_iterations=max_iterations)


def _telegram_run(*, interval: int, max_iterations: int | None) -> None:
    import time

    from trader.notify.commands import poll_once

    try:
        ectx, secrets = _exec_ctx()
    except (config.ConfigError, paths.ProjectRootNotFound) as e:
        typer.echo(f"오류: {e}", err=True)
        raise typer.Exit(code=1) from e
    receiver = make_receiver(secrets)
    if receiver is None:
        ectx.conn.close()
        typer.echo("오류: 텔레그램 토큰·chat_id 가 없습니다", err=True)
        raise typer.Exit(code=1)
    n = 0
    try:
        while True:
            poll_once(ectx, receiver, echo=typer.echo)
            n += 1
            if max_iterations is not None and n >= max_iterations:
                break
            time.sleep(interval)
    finally:
        ectx.conn.close()


def _daemon_snapshot_fn(secrets: config.Secrets, universe, market_client):
    """데몬 reflect 잡 앞에서 매일 피처 스냅샷을 찍는 함수 (시세 클라이언트 없으면 None)."""
    if market_client is None:
        return None
    from trader.ml import features

    def run(markets: list[str]) -> int:
        return features.snapshot(
            market_client=market_client,
            dart=_build_dart(secrets),
            universe=universe,
            markets=markets,  # type: ignore[arg-type]
            progress=lambda s: None,
        )

    return run


@app.command("daemon")
def daemon_cmd(
    no_web: bool = typer.Option(False, "--no-web", help="웹 서버를 띄우지 않음"),
    dry_run: bool = typer.Option(False, "--dry-run", help="세션·monitor 를 dry_run 으로"),
) -> None:
    """스케줄 데몬: schedule.yaml 대로 scan/analyze/monitor·텔레그램·체결 동기화 + web (§10.1)."""
    from trader import daemon as daemon_mod
    from trader.ledger import db

    try:
        configure(paths.state_dir())
        settings = config.load_settings()
        schedule = config.load_schedule()
        accounts = config.load_accounts()
        secrets = config.load_secrets()
        universe = config.load_universe()
        sources_cfg = config.load_sources()
    except (config.ConfigError, paths.ProjectRootNotFound) as e:
        typer.echo(f"오류: {e}", err=True)
        raise typer.Exit(code=1) from e
    try:
        market_client = _build_market(secrets)
    except Exception as e:  # noqa: BLE001
        logging.getLogger("trader.daemon").warning(
            "시세 클라이언트 없음(휴장일·monitor 비활성): %s", e
        )
        market_client = None
    conn = db.connect()
    db.init_schema(conn)
    conn.close()

    def web_app():
        from trader.web.app import create_app

        return create_app(_web_deps())

    deps = daemon_mod.DaemonDeps(
        settings=settings,
        schedule=schedule,
        accounts=accounts,
        brokers={b.account.id: b for b in _build_brokers(secrets, accounts)},
        market_client=market_client,
        conn_factory=db.connect,
        notifier=make_notifier(secrets),
        receiver=make_receiver(secrets),
        secrets=secrets,
        universe=universe,
        sources_cfg=sources_cfg,
        input_deps=_build_input_deps(secrets, sources_cfg),
        web_app_factory=web_app,
        dry_run=dry_run,
        snapshot_fn=_daemon_snapshot_fn(secrets, universe, market_client),
    )
    r = daemon_mod.run(deps, web=not no_web)
    if not r.get("ok"):
        typer.echo(f"오류: {r.get('error')}", err=True)
        raise typer.Exit(code=1)
    typer.echo("데몬 종료")


# --- 체결 동기화 (Phase 6) ------------------------------------------------------

fills_app = typer.Typer(help="체결 조회로 fills 를 적재하고 주문 상태를 갱신한다")
app.add_typer(fills_app, name="fills")


@fills_app.command("sync")
def fills_sync(
    account: str | None = typer.Option(None, "--account", help="특정 계좌 id 만"),
    date: str | None = typer.Option(None, "--date", help="YYYY-MM-DD (기본 KST 오늘)"),
) -> None:
    """계좌별 체결을 원장에 적재한다 (멱등)."""
    from datetime import date as _date

    from trader.ledger import db
    from trader.orchestrator.fills import sync_fills

    try:
        configure(paths.state_dir())
        secrets = config.load_secrets()
        accounts = config.load_accounts()
        d = _date.fromisoformat(date) if date else datetime.now(_KST).date()
    except (config.ConfigError, paths.ProjectRootNotFound, ValueError) as e:
        typer.echo(f"오류: {e}", err=True)
        raise typer.Exit(code=1) from e
    if account:
        accounts = [a for a in accounts if a.id == account]
    conn = db.connect()
    db.init_schema(conn)
    try:
        for b in _build_brokers(secrets, accounts):
            r = sync_fills(conn, b, b.account, d)
            typer.echo(
                f"{r['account_id']} {r['date']}: 체결 {r['inserted']}건 적재, "
                f"주문 {r['orders_updated']}건 갱신"
            )
    finally:
        conn.close()


# --- 런타임 제어 (control 테이블) -----------------------------------------------

control_app = typer.Typer(help="일시정지·계좌 모드 (SQLite control 테이블, yaml 보다 우선)")
app.add_typer(control_app, name="control")


def _control_conn():
    from trader.ledger import db

    configure(paths.state_dir())
    accounts = config.load_accounts()
    conn = db.connect()
    db.init_schema(conn)
    return conn, accounts


@control_app.command("show")
def control_show() -> None:
    """일시정지 여부와 계좌별 실효 모드를 보여준다."""
    from trader.control import control

    try:
        conn, accounts = _control_conn()
    except (config.ConfigError, paths.ProjectRootNotFound) as e:
        typer.echo(f"오류: {e}", err=True)
        raise typer.Exit(code=1) from e
    try:
        typer.echo(f"일시정지: {'예' if control.is_paused(conn) else '아니오'}")
        for a in accounts:
            mode = control.get_account_mode(conn, a)
            src = "control" if control.get_flag(conn, f"mode:{a.id}") is not None else "yaml"
            typer.echo(f"{a.id} ({a.env}): {mode} [{src}]")
    finally:
        conn.close()


@control_app.command("mode")
def control_mode(
    account: str = typer.Argument(..., help="계좌 id"),
    mode: str = typer.Argument(..., help="auto | approve | off"),
    confirm: str | None = typer.Option(None, "--confirm", help="실전 auto 전환 확인 문자열(LIVE)"),
) -> None:
    """계좌 실행 모드를 바꾼다. 실전 auto 는 --confirm LIVE 가 있어야 하며 확인 기록을 남긴다."""
    from trader.control import control

    try:
        conn, accounts = _control_conn()
    except (config.ConfigError, paths.ProjectRootNotFound) as e:
        typer.echo(f"오류: {e}", err=True)
        raise typer.Exit(code=1) from e
    try:
        acct = next((a for a in accounts if a.id == account), None)
        if acct is None:
            typer.echo(f"오류: 계좌 id 를 찾을 수 없습니다: {account}", err=True)
            raise typer.Exit(code=1)
        try:
            control.set_account_mode(conn, acct, mode, confirm=confirm)
        except control.ConfirmationRequired as e:
            typer.echo(f"오류: {e}", err=True)
            raise typer.Exit(code=1) from e
        except ValueError as e:
            typer.echo(f"오류: {e}", err=True)
            raise typer.Exit(code=1) from e
        typer.echo(f"{account}: {mode}")
    finally:
        conn.close()


@control_app.command("pause")
def control_pause() -> None:
    """paused 플래그를 켠다 (Phase 6 데몬이 확인)."""
    from trader.control import control

    conn, _ = _control_conn()
    try:
        control.set_flag(conn, "paused", True)
    finally:
        conn.close()
    typer.echo("일시정지: 예")


@control_app.command("resume")
def control_resume() -> None:
    """paused 플래그를 끈다."""
    from trader.control import control

    conn, _ = _control_conn()
    try:
        control.set_flag(conn, "paused", False)
    finally:
        conn.close()
    typer.echo("일시정지: 아니오")


# --- 웹 UI (Phase 5) ------------------------------------------------------------


def _web_deps():
    from trader.ledger import db
    from trader.web.app import WebDeps

    configure(paths.state_dir())
    settings = config.load_settings()
    accounts = config.load_accounts()
    secrets = config.load_secrets()
    log = logging.getLogger("trader.web")
    try:
        market = _build_market(secrets)
    except Exception as e:  # noqa: BLE001 - 시세 없이도 조회 페이지는 뜬다
        log.warning("시세 클라이언트 생성 실패(차트 비활성): %s", e)
        market = None
    if not secrets.WEB_TOKEN:
        log.warning("WEB_TOKEN 미설정: 제어 API 비활성(401)")
    conn = db.connect()
    db.init_schema(conn)
    conn.close()
    return WebDeps(
        settings=settings,
        accounts=accounts,
        conn_factory=db.connect,
        brokers={b.account.id: b for b in _build_brokers(secrets, accounts)},
        market=market,
        token=secrets.WEB_TOKEN or None,
        dist=paths.project_root() / "web" / "dist",
        notifier=make_notifier(secrets),
    )


@app.command("web")
def web(
    host: str | None = typer.Option(None, "--host", help="바인드 주소 (기본 settings.web.bind)"),
    port: int | None = typer.Option(None, "--port", help="포트 (기본 settings.web.port)"),
) -> None:
    """웹 UI 서버를 띄운다 (조회는 토큰 없음, 제어는 Bearer WEB_TOKEN)."""
    import uvicorn

    from trader.web.app import create_app

    try:
        deps = _web_deps()
    except (config.ConfigError, paths.ProjectRootNotFound) as e:
        typer.echo(f"오류: {e}", err=True)
        raise typer.Exit(code=1) from e
    bind = host or deps.settings.web.bind
    if bind not in ("127.0.0.1", "localhost", "::1"):
        logging.getLogger("trader.web").warning(
            "루프백이 아닌 주소(%s)에 바인드: 조회 API 는 토큰 없이 열립니다", bind
        )
        deps.extra_hosts.append(bind)
    uvicorn.run(
        create_app(deps),
        host=bind,
        port=port or deps.settings.web.port,
        log_level="info",
    )


@tool_app.command("submit-screen")
def tool_submit_screen(
    market: str = typer.Option(..., "--market"),
    date: str = typer.Option(..., "--date"),
    json_text: str | None = typer.Option(None, "--json", help="Screened JSON (없으면 stdin)"),
) -> None:
    from trader.orchestrator import tools

    _tool_run(tools.submit_screen, market=market, date=date, payload=_payload_arg(json_text))


@app.command("scan")
def scan(
    market: str = typer.Option("KR", "--market", help="KR 또는 US"),
    asof: str | None = typer.Option(None, "--asof", help="기준 시각 ISO8601 (기본: 현재 UTC)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="screener 세션 없이 정량 상위만"),
) -> None:
    """정량 필터 → screener 세션 → 오늘 분석 대상 목록 (_scan.json, stdout JSON)."""
    import json

    from trader.control import control
    from trader.ledger import db
    from trader.orchestrator import tools
    from trader.orchestrator.run_store import write_checkpoint
    from trader.orchestrator.session import PipelineError
    from trader.scan import quant, screener

    try:
        configure(paths.state_dir())
        if market not in ("KR", "US"):
            raise config.ConfigError(f"시장은 KR 또는 US 여야 합니다: {market}")
        settings = config.load_settings()
        accounts = config.load_accounts()
        secrets = config.load_secrets()
        universe = config.load_universe()
        when = datetime.fromisoformat(asof) if asof else datetime.now(UTC)
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        day = when.astimezone(_KST).date()
        from trader.ml import ranker

        ml = ranker.current()
        conn = db.connect()
        db.init_schema(conn)
        try:
            watch = [*universe.watch, *control.get_watchlist(conn)]
            cands = quant.candidates(
                universe,
                market,
                rank_fn=ml[0] if ml else None,  # type: ignore[arg-type]
                watch_extra=control.get_watchlist(conn),
            )
            cap_hit = dry_run or tools.sessions_today(conn, when) >= settings.llm.daily_session_cap
            brokers = {b.account.id: b for b in _build_brokers(secrets, accounts)}
            r = screener.run_screen(
                settings,
                conn,
                accounts,
                market=market,
                date=day,
                candidates=cands,
                cap_hit=cap_hit,
                brokers=brokers,
                watch=watch,
                asof=when,
            )
        finally:
            conn.close()
        out = {
            "market": market,
            "date": day.isoformat(),
            "candidates": cands,
            "picks": r["picks"],
            "analyze_targets": r["analyze_targets"],
            "session": r["session"],
            "cap_hit": cap_hit,
            "ml_model": ml[1] if ml else None,
        }
        write_checkpoint(screener.market_dir(day, market), "_scan", out)
    except (config.ConfigError, paths.ProjectRootNotFound, PipelineError, ValueError) as e:
        typer.echo(f"오류: {e}", err=True)
        raise typer.Exit(code=1) from e
    typer.echo(json.dumps(out, ensure_ascii=False, default=str))


@app.command("monitor")
def monitor_cmd(
    market: str = typer.Option("KR", "--market", help="KR 또는 US"),
    once: bool = typer.Option(False, "--once", help="1회만 점검하고 종료"),
    interval: int = typer.Option(60, "--interval", help="반복 주기(초)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="손절 주문·재평가 주문 없이 기록만"),
    max_iterations: int | None = typer.Option(None, "--max-iterations", hidden=True),
) -> None:
    """보유종목 장중 점검: 손절 즉시 매도, 트리거 → 재평가 세션 (스펙 §5.2)."""
    import json
    import time

    from trader.ledger import db
    from trader.orchestrator import monitor as monitor_mod
    from trader.orchestrator.session import run_session

    try:
        configure(paths.state_dir())
        if market not in ("KR", "US"):
            raise config.ConfigError(f"시장은 KR 또는 US 여야 합니다: {market}")
        settings = config.load_settings()
        accounts = config.load_accounts()
        secrets = config.load_secrets()
        universe = config.load_universe()
        market_client = _build_market(secrets)
    except (config.ConfigError, paths.ProjectRootNotFound) as e:
        typer.echo(f"오류: {e}", err=True)
        raise typer.Exit(code=1) from e
    except Exception as e:  # noqa: BLE001 - 시세 클라이언트 없이는 monitor 를 돌릴 수 없다
        typer.echo(f"오류: 시세 클라이언트 생성 실패: {e}", err=True)
        raise typer.Exit(code=1) from e
    conn = db.connect()
    db.init_schema(conn)
    ctx = monitor_mod.MonitorContext(
        settings=settings,
        accounts=accounts,
        conn=conn,
        brokers={b.account.id: b for b in _build_brokers(secrets, accounts)},
        market_client=market_client,
        notifier=make_notifier(secrets),
        session_runner=run_session,
        dry_run=dry_run,
        exchanges={u.symbol: u.exchange for u in universe.us},
    )
    n = 0
    try:
        while True:
            today = datetime.now(_KST).date()
            r = monitor_mod.monitor_once(ctx, market=market, today=today)
            typer.echo(
                json.dumps({"market": market, "date": today.isoformat(), **r}, ensure_ascii=False)
            )
            n += 1
            if once or (max_iterations is not None and n >= max_iterations):
                break
            time.sleep(interval)
    finally:
        conn.close()


@app.command("install")
def install_cmd(
    dry_run: bool = typer.Option(False, "--dry-run", help="파일을 쓰지 않고 내용·명령만 출력"),
    uninstall: bool = typer.Option(False, "--uninstall", help="등록 해제"),
) -> None:
    """로그온 시 데몬 자동 시작 등록 (macOS launchd / Windows 작업 스케줄러)."""
    from trader.platform import installer

    try:
        if uninstall:
            r = installer.uninstall()
            typer.echo(f"해제: {r['path']} ({' '.join(r['command'])})")
            return
        from trader.platform.claude_bin import find_claude

        extra = []
        try:
            extra.append(find_claude(config.load_settings().claude_bin).parent)
        except Exception as e:  # noqa: BLE001 - claude 가 없으면 PATH 에 넣지 않고 경고만
            logging.getLogger("trader.daemon").warning("claude 실행파일을 찾지 못함: %s", e)
        r = installer.install(dry_run=dry_run, extra_paths=extra)
    except config.ConfigError as e:
        typer.echo(f"오류: {e}", err=True)
        raise typer.Exit(code=1) from e
    typer.echo(("[dry-run] " if dry_run else "") + f"파일: {r['path']}")
    typer.echo("명령: " + " ".join(r["command"]))
    if dry_run:
        typer.echo(r["content"])


# --- ML (Phase 7) ----------------------------------------------------------------

ml_app = typer.Typer(help="퀀트 모델 학습·레지스트리")
app.add_typer(ml_app, name="ml")


@ml_app.command("train")
def ml_train(
    market: str | None = typer.Option(None, "--market", help="KR|US (기본 전체)"),
    min_rows: int = typer.Option(100, "--min-rows", help="학습 최소 라벨 행 수"),
) -> None:
    """피처 스토어로 퀀트 모델을 워크포워드 학습·평가·저장하고 승격 기준을 판정한다."""
    import json

    from trader.ml import train as train_mod

    try:
        configure(paths.state_dir())
        cfg = config.load_ml()
        if market is not None and market not in ("KR", "US"):
            raise config.ConfigError(f"시장은 KR 또는 US: {market}")
    except (config.ConfigError, paths.ProjectRootNotFound) as e:
        typer.echo(f"오류: {e}", err=True)
        raise typer.Exit(code=1) from e
    r = train_mod.train_quant(cfg, market=market, min_rows=min_rows)
    typer.echo(json.dumps(r, ensure_ascii=False, default=str))


@ml_app.command("models")
def ml_models() -> None:
    """레지스트리의 quant 모델 목록."""
    from trader.ml import registry

    configure(paths.state_dir())
    metas = registry.list_models("quant")
    if not metas:
        typer.echo("모델 없음")
        return
    for m in metas:
        met = m.get("metrics") or {}
        typer.echo(
            f"{m['version']}  정확도 {met.get('accuracy', '-')}  "
            f"기준선 {met.get('baseline', '-')}  알파 {met.get('alpha', '-')}  "
            f"행 {m.get('rows', '-')}  {'승격' if m.get('promoted') else '미승격'}  "
            f"{m.get('promote_reason') or ''}"
        )


@ml_app.command("promote")
def ml_promote(
    version: str = typer.Argument(..., help="YYYYMMDD_HHMMSS"),
    reason: str = typer.Option("manual", "--reason"),
) -> None:
    """모델을 수동 승격한다 (이후 05_quant·scan 랭킹에 쓰인다)."""
    from trader.ml import registry

    configure(paths.state_dir())
    if not any(m.get("version") == version for m in registry.list_models("quant")):
        typer.echo(f"오류: 모델 없음: {version}", err=True)
        raise typer.Exit(code=1)
    m = registry.promote("quant", version, reason=reason)
    typer.echo(f"승격: quant {m['version']} ({reason})")


@ml_app.command("demote")
def ml_demote(
    version: str = typer.Argument(..., help="YYYYMMDD_HHMMSS"),
    reason: str = typer.Option("manual", "--reason"),
) -> None:
    """승격을 해제한다 (다른 승격 버전이 없으면 05_quant·ML 랭킹 비활성)."""
    from trader.ml import registry

    configure(paths.state_dir())
    if not any(m.get("version") == version for m in registry.list_models("quant")):
        typer.echo(f"오류: 모델 없음: {version}", err=True)
        raise typer.Exit(code=1)
    m = registry.demote("quant", version, reason=reason)
    typer.echo(f"승격 해제: quant {m['version']} ({reason})")


rl_app = typer.Typer(help="RL 사이저 학습·레지스트리 (실험, 모의 계좌 한정, 승격 수동)")
app.add_typer(rl_app, name="rl")


def _rl_version_exists(version: str) -> bool:
    from trader.ml import registry

    return any(m.get("version") == version for m in registry.list_models("rl_sizer"))


@rl_app.command("train")
def rl_train(
    market: str | None = typer.Option(None, "--market", help="KR|US (기본 전체)"),
    seeds: int | None = typer.Option(None, "--seeds", help="시드 수 (기본 ml.yaml)"),
) -> None:
    """스텝 데이터로 사이저를 워크포워드×시드 학습·기준선 비교·저장한다 (자동 승격 없음)."""
    import json

    from trader.ledger import db
    from trader.ml.rl import train as rl_train_mod

    try:
        configure(paths.state_dir())
        cfg = config.load_ml()
        if market is not None and market not in ("KR", "US"):
            raise config.ConfigError(f"시장은 KR 또는 US: {market}")
        if seeds is not None:
            rl_cfg = cfg.models.rl_sizer.model_copy(update={"seeds": seeds})
            cfg = cfg.model_copy(
                update={"models": cfg.models.model_copy(update={"rl_sizer": rl_cfg})}
            )
        conn = db.connect()
        db.init_schema(conn)
        try:
            r = rl_train_mod.train_sizer(cfg, conn, market=market)
        finally:
            conn.close()
    except (config.ConfigError, paths.ProjectRootNotFound) as e:
        typer.echo(f"오류: {e}", err=True)
        raise typer.Exit(code=1) from e
    typer.echo(json.dumps(r, ensure_ascii=False, default=str))


@rl_app.command("models")
def rl_models() -> None:
    """레지스트리의 rl_sizer 모델 목록."""
    from trader.ml import registry

    configure(paths.state_dir())
    metas = registry.list_models("rl_sizer")
    if not metas:
        typer.echo("모델 없음")
        return
    for m in metas:
        s = m.get("summary") or {}
        rl, fx = s.get("rl") or {}, s.get("fixed") or {}
        typer.echo(
            f"{m['version']}  {m.get('backend', '-')}/{m.get('source', '-')}  "
            f"RL {rl.get('return_mean', '-')}±{rl.get('return_std', '-')}  "
            f"고정 {fx.get('return_mean', '-')}  스텝 {m.get('steps', '-')}  "
            f"{'승격' if m.get('promoted') else '미승격'}"
        )


@rl_app.command("promote")
def rl_promote(
    version: str = typer.Argument(..., help="YYYYMMDD_HHMMSS"),
    reason: str = typer.Option("manual", "--reason"),
) -> None:
    """사이저를 수동 승격한다 (이후 ml.yaml rl_sizer.enabled 가 켜져 있으면 모의 계좌 섀도)."""
    from trader.ml import registry

    configure(paths.state_dir())
    if not _rl_version_exists(version):
        typer.echo(f"오류: 모델 없음: {version}", err=True)
        raise typer.Exit(code=1)
    m = registry.promote("rl_sizer", version, reason=reason)
    typer.echo(f"승격: rl_sizer {m['version']} ({reason})")


@rl_app.command("demote")
def rl_demote(
    version: str = typer.Argument(..., help="YYYYMMDD_HHMMSS"),
    reason: str = typer.Option("manual", "--reason"),
) -> None:
    """승격을 해제한다 (다른 승격 버전이 없으면 섀도 비활성)."""
    from trader.ml import registry

    configure(paths.state_dir())
    if not _rl_version_exists(version):
        typer.echo(f"오류: 모델 없음: {version}", err=True)
        raise typer.Exit(code=1)
    m = registry.demote("rl_sizer", version, reason=reason)
    typer.echo(f"승격 해제: rl_sizer {m['version']} ({reason})")


@tool_app.command("submit-reflection")
def tool_submit_reflection(
    market: str = typer.Option(..., "--market"),
    date: str = typer.Option(..., "--date"),
    json_text: str | None = typer.Option(
        None, "--json", help="Reflection 배열 JSON (없으면 stdin)"
    ),
) -> None:
    from trader.orchestrator import tools

    _tool_run(tools.submit_reflection, market=market, date=date, payload=_payload_arg(json_text))


@app.command("reflect")
def reflect_cmd(
    market: str = typer.Option("KR", "--market", help="KR 또는 US"),
    dry_run: bool = typer.Option(False, "--dry-run", help="결과 채우기까지만 (세션 없음)"),
) -> None:
    """만기 결정의 결과를 채우고(피처 스토어 종가) reflector 세션으로 복기를 받는다 (스펙 §6.5)."""
    from trader.ledger import db
    from trader.memory import outcomes, store
    from trader.orchestrator.reflect import pending, run_reflect
    from trader.orchestrator.session import PipelineError

    try:
        configure(paths.state_dir())
        if market not in ("KR", "US"):
            raise config.ConfigError(f"시장은 KR 또는 US 여야 합니다: {market}")
        settings = config.load_settings()
        accounts = config.load_accounts()
    except (config.ConfigError, paths.ProjectRootNotFound) as e:
        typer.echo(f"오류: {e}", err=True)
        raise typer.Exit(code=1) from e
    conn = db.connect()
    db.init_schema(conn)
    try:
        today = datetime.now(_KST).date()
        matured = outcomes.mature(conn, state=None, today=today)
        typer.echo(f"결과 채움 {len(matured)}건")
        n_pending = len(pending(conn, market=market))
        if dry_run:
            typer.echo(f"대기 중 반성 {n_pending}건 (--dry-run: 세션 생략)")
            return
        res = run_reflect(settings, conn, accounts, market=market, date=today, dry_run=True)
        if not res.ok:
            typer.echo(f"세션 실패: {res.error}", err=True)
            raise typer.Exit(code=1)
        pr = store.promote_and_prune(conn, now=datetime.now(UTC), settings=settings.memory)
        typer.echo(
            f"반성 {n_pending}건 ({'FAKE' if res.fake else 'session=' + str(res.session_id)}), "
            f"정리 {pr['deleted']}건 삭제"
        )
    except PipelineError as e:
        typer.echo(f"오류: {e}", err=True)
        raise typer.Exit(code=1) from e
    finally:
        conn.close()


# --- 보정 (Phase 8) -------------------------------------------------------------------

calibration_app = typer.Typer(help="애널리스트 신호 적중률 보정 (결정 60건 미만이면 비활성)")
app.add_typer(calibration_app, name="calibration")


@calibration_app.command("build")
def calibration_build() -> None:
    """현재 메모리 결과로 보정 리포트를 계산해 출력한다 (체크포인트는 analyze 때 종목별로 생성)."""
    import json

    from trader.calibration import model as cal
    from trader.ledger import db

    configure(paths.state_dir())
    cfg = config.load_ml().models.calibration
    conn = db.connect()
    db.init_schema(conn)
    try:
        r = cal.build_calibration(
            conn,
            min_decisions=cfg.min_decisions,
            query_vector=None,
            asof=datetime.now(UTC).isoformat(),
        )
    finally:
        conn.close()
    typer.echo(
        json.dumps(r or {"active": False, "min_decisions": cfg.min_decisions}, ensure_ascii=False)
    )


@app.command("backtest")
def backtest_cmd(
    since: str = typer.Option(..., "--since", help="컷오프 날짜(YYYY-MM-DD). 이후 결정만 집계"),
    market: str | None = typer.Option(None, "--market", help="KR 또는 US (없으면 전체)"),
) -> None:
    """컷오프 이후 결정의 적중률·평균 수익·알파를 메모리 결과/피처 스토어 종가로 집계한다."""
    import json

    from trader import backtest
    from trader.ledger import db

    try:
        configure(paths.state_dir())
        cutoff = date.fromisoformat(since)
    except (ValueError, paths.ProjectRootNotFound) as e:
        typer.echo(f"오류: {e}", err=True)
        raise typer.Exit(code=1) from e
    conn = db.connect()
    db.init_schema(conn)
    try:
        r = backtest.simple_backtest(conn, paths.state_dir(), since=cutoff, market=market)
    finally:
        conn.close()
    typer.echo(json.dumps(r, ensure_ascii=False))


@app.command("readiness")
def readiness_cmd() -> None:
    """실전 전환 준비도 점검(읽기 전용). 모드는 바꾸지 않는다 — docs/runbook-live-transition.md."""
    import json

    from trader import readiness
    from trader.ledger import db

    try:
        configure(paths.state_dir())
        settings = config.load_settings()
        accounts = config.load_accounts()
    except (config.ConfigError, paths.ProjectRootNotFound) as e:
        typer.echo(f"오류: {e}", err=True)
        raise typer.Exit(code=1) from e
    conn = db.connect()
    db.init_schema(conn)
    try:
        r = readiness.check(
            conn,
            accounts=accounts,
            settings=settings,
            state=paths.state_dir(),
            today=datetime.now(_KST).date(),
        )
    finally:
        conn.close()
    typer.echo(json.dumps(r, ensure_ascii=False))


@app.command("notify-test")
def notify_test() -> None:
    """텔레그램으로 테스트 메시지를 보낸다 (미설정이면 NullNotifier 로 성공)."""
    try:
        configure(paths.state_dir())
        secrets = config.load_secrets()
    except (config.ConfigError, paths.ProjectRootNotFound) as e:
        typer.echo(f"오류: {e}", err=True)
        raise typer.Exit(code=1) from e
    notifier = make_notifier(secrets)
    stamp = datetime.now(_KST).strftime("%Y-%m-%d %H:%M KST")
    if notifier.send(f"trader 알림 테스트 {stamp}"):
        typer.echo("전송 성공")
    else:
        typer.echo("전송 실패")
        raise typer.Exit(code=1)
