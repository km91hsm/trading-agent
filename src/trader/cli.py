"""`trader` 명령행 진입점 (typer)."""

from __future__ import annotations

import functools
import logging
import os
import sys
import threading
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

from trader import config
from trader.broker.accounts import KisBroker
from trader.broker.kis_auth import KisAuth
from trader.broker.kis_client import KisClient
from trader.broker.kis_market import KisMarket
from trader.config import KST
from trader.logging_setup import configure
from trader.notify.commands import approval_line
from trader.notify.telegram import make_notifier, make_receiver
from trader.orchestrator import run_store
from trader.orchestrator.session import run_session
from trader.platform import paths
from trader.platform.claude_bin import ClaudeNotFound

if TYPE_CHECKING:
    from trader.sources.inputs import InputDeps

app = typer.Typer(help="LLM 멀티 에이전트 주식 자동매매", no_args_is_help=True)
kis_app = typer.Typer(help="KIS 브로커 도구", no_args_is_help=True)
app.add_typer(kis_app, name="kis")

from trader.saveticker.cli import app as saveticker_app  # noqa: E402

app.add_typer(saveticker_app, name="saveticker")


def _force_utf8() -> None:
    """콘솔 출력을 UTF-8 로 고정한다 (Windows cp949 대비)."""
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _config_errors(fn):
    """설정·프로젝트 루트 오류를 '오류: …' 한 줄과 exit 1 로 바꾼다 (명령마다 반복되던 try/except).

    functools.wraps 로 시그니처를 보존해 typer 옵션 파싱이 그대로 동작한다."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (config.ConfigError, paths.ProjectRootNotFound) as e:
            typer.echo(f"오류: {e}", err=True)
            raise typer.Exit(code=1) from e

    return wrapper


@dataclass
class _Boot:
    settings: config.Settings | None = None
    accounts: list[config.AccountConfig] | None = None
    secrets: config.Secrets | None = None
    conn: Any | None = None


def _boot(
    *,
    settings: bool = False,
    accounts: bool = False,
    secrets: bool = False,
    conn: bool = False,
    log_name: str | None = None,
) -> _Boot:
    """명령 공통 부트스트랩: 로깅 → 요청한 설정·계좌·시크릿 로드 → (요청 시) 스키마 적용 원장 연결.
    명령마다 반복하던 4~6줄을 한 곳에 둔다. conn 은 호출자가 닫는다."""
    from trader.ledger import db

    configure(paths.state_dir(), name=log_name)
    out = _Boot()
    if settings:
        out.settings = config.load_settings()
    if accounts:
        out.accounts = config.load_accounts()
    if secrets:
        out.secrets = config.load_secrets()
    if conn:
        out.conn = db.connect()
        db.init_schema(out.conn)
    return out


def _version_callback(value: bool) -> None:
    if value:
        from importlib.metadata import PackageNotFoundError, version

        try:
            v = version("trader")
        except PackageNotFoundError:
            v = "0.0.0"
        typer.echo(f"trader {v}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", help="버전을 출력하고 끝낸다", callback=_version_callback, is_eager=True
    ),
) -> None:
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
        from trader.broker.kis_client import RATE_PER_SEC
        from trader.broker.rate_store import SharedRateLimiter

        # 데몬·세션 도구 서브프로세스가 같은 1초 창을 나눠 쓴다(교차 프로세스 한도)
        limiter = SharedRateLimiter(RATE_PER_SEC[env], paths.state_dir() / "kis_rate.db", env)
        _clients[cache_key] = KisClient(env, KisAuth(env, key, secret), limiter=limiter)
    return _clients[cache_key]


def _build_market(secrets: config.Secrets) -> KisMarket:
    """시세 클라이언트 (실전 env 전용)."""
    return KisMarket(_build_client(secrets, "real"))


def _session_brokers(accounts: list[config.AccountConfig]) -> dict:
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
@_config_errors
def status() -> None:
    """설정·원장·TODO 를 읽어 state/STATUS.md 를 재생성한다."""
    from trader import status as status_mod
    from trader.ledger import db

    boot = _boot(settings=True, accounts=True, conn=True)
    settings, accounts, conn = boot.settings, boot.accounts, boot.conn
    try:
        db.sync_accounts(conn, accounts)
        text = status_mod.build_status(conn, settings, accounts, paths.docs_dir() / "TODO.md")
    finally:
        conn.close()
    out = status_mod.write_status(text)
    typer.echo(f"저장: {out}")


@kis_app.command("ping")
@_config_errors
def kis_ping(
    account: str | None = typer.Option(None, "--account", help="특정 계좌 id 만 점검"),
) -> None:
    """KIS 시세(005930·AAPL)·휴장일·계좌별 현금을 점검한다. 실패가 있으면 exit 1."""
    boot = _boot(secrets=True, accounts=True)
    secrets, accounts = boot.secrets, boot.accounts
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
        today = datetime.now(KST).date()
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


@kis_app.command("order-test")
@_config_errors
def kis_order_test(
    account: str = typer.Option(..., "--account", help="모의 계좌 id (실전 거부)"),
    ticker: str = typer.Option("005930", "--ticker", help="KR 종목코드"),
) -> None:
    """Phase 4 실기 전용: 모의 계좌에 현재가 10% 아래 1주 지정가 매수 → 즉시 취소 (원장 미기록)."""
    boot = _boot(secrets=True, accounts=True)
    secrets, accounts = boot.secrets, boot.accounts
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
        from trader.risk.ticks import tick_size

        price = _build_market(secrets).get_price_kr(ticker).price
        tick = tick_size("KR", price)  # 호가단위 정본 (체결되지 않게 내림)
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


def _collect_and_save():
    from trader.sources.inputs import collect_and_save

    return collect_and_save


def _build_input_deps(secrets: config.Secrets, cfg: config.SourcesConfig) -> InputDeps:
    """수집용 클라이언트를 조립한다. 시세 클라이언트를 못 만들면 해당 절만 건너뛴다.

    `trader.sources.inputs` 는 pandas/pyarrow 를 끌어와 import 에 0.6 초가 들므로 수집 명령 안에서만
    불러온다 (`python -m trader tool …` 서브프로세스가 세션당 20여 회 뜬다)."""
    from trader.sources.dart import DartClient
    from trader.sources.inputs import InputDeps

    cache = paths.state_dir() / "cache"
    try:
        market = _build_market(secrets)
    except Exception as e:  # noqa: BLE001
        logging.getLogger("trader.sources").warning("시세 클라이언트 생성 실패: %s", e)
        market = None
    from trader.ledger import db

    dart = DartClient(secrets.DART_API_KEY, cache) if secrets.DART_API_KEY else None
    return InputDeps(market=market, dart=dart, edgar=_build_edgar(cfg), conn_factory=db.connect)


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
        from trader.orchestrator.run_store import run_date_for as _rdf
        from trader.orchestrator.run_store import run_dir as _rd

        existed = (
            not force and _rc(_rd(_rdf(market, when), market, ticker), "00_inputs") is not None  # type: ignore[arg-type]
        )
        path = _collect_and_save()(
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


def _build_edgar(cfg: config.SourcesConfig | None = None):
    """EDGAR 클라이언트 (수집·US 비율 피처 공용).

    `sources.yaml` edgar.enabled 가 꺼져 있으면 None."""
    from trader.sources.edgar import EdgarClient

    cfg = cfg if cfg is not None else config.load_sources()
    if not cfg.edgar.enabled:
        return None
    return EdgarClient(cfg.edgar.user_agent, paths.state_dir() / "cache")


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
            edgar=_build_edgar(),
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
@_config_errors
def features_snapshot(
    market: str | None = typer.Option(None, "--market", help="KR 또는 US (기본: 둘 다)"),
    asof: str | None = typer.Option(
        None, "--asof", help="기준 시각 ISO8601 (24시간 넘은 과거는 거부)"
    ),
) -> None:
    """유니버스 전체의 오늘 피처 행을 저장한다."""
    try:
        when = datetime.fromisoformat(asof) if asof else None
    except ValueError as e:
        raise config.ConfigError(f"--asof 는 ISO8601 이어야 합니다: {asof!r}") from e
    if when is not None and when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    _run_features("snapshot", market, asof=when)


@features_app.command("backfill")
@_config_errors
def features_backfill(
    years: int = typer.Option(3, "--years", help="과거 적재 연수 (최대 약 4년=1000봉)"),
    market: str | None = typer.Option(None, "--market", help="KR 또는 US (기본: 둘 다)"),
) -> None:
    """유니버스 전체의 과거 피처를 재구성해 저장한다."""
    _run_features("backfill", market, years=years)


@features_app.command("derive")
@_config_errors
def features_derive() -> None:
    """모든 피처 파일의 파생 열(`*_vs_sector`, config/sectors.yaml)을 다시 계산한다.

    KIS 호출 없음 — 섹터 맵을 바꾸거나 옛 스토어를 올릴 때."""
    from trader.ml import features

    configure(paths.state_dir())
    n = features.rederive(progress=lambda s: typer.echo(f"  {s}"))
    typer.echo(f"derive: {n}개 파일 갱신")


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

    from trader.orchestrator.run_store import read_checkpoint, run_dir
    from trader.orchestrator.session import PipelineError, SessionSpec

    try:
        configure(paths.state_dir())
        if market not in ("KR", "US"):
            raise config.ConfigError(f"시장은 KR 또는 US 여야 합니다: {market}")
        if market == "US" and not exchange:
            raise config.ConfigError("미국 종목은 --exchange(거래소 코드)가 필요합니다")
        boot = _boot(settings=True, accounts=True, secrets=True, conn=True)
        settings, accounts, secrets, conn = boot.settings, boot.accounts, boot.secrets, boot.conn
        when = datetime.fromisoformat(asof) if asof else datetime.now(UTC)
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        from trader.orchestrator.run_store import run_date_for

        day = run_date_for(market, when)  # US 는 뉴욕 운영일 (데몬·monitor 와 같은 디렉터리)
        d = run_dir(day, market, ticker)  # type: ignore[arg-type]
        if read_checkpoint(d, "00_inputs") is None:
            if skip_collect:
                raise config.ConfigError(f"00_inputs 가 없습니다: {d} (--skip-collect 해제)")
            cfg = config.load_sources()
            _collect_and_save()(
                ticker=ticker,
                market=market,
                exchange=exchange,
                asof=when,
                cfg=cfg,  # type: ignore[arg-type]
                secrets=secrets,
                deps=_build_input_deps(secrets, cfg),
            )
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
                brokers=_session_brokers(accounts),
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
    from trader.orchestrator.tools import ToolContext

    boot = _boot(secrets=True, accounts=True, conn=True)
    logging.getLogger().handlers = [
        h
        for h in logging.getLogger().handlers
        if not isinstance(h, logging.StreamHandler) or isinstance(h, logging.FileHandler)
    ]  # stdout 은 JSON 전용이므로 콘솔 핸들러 제거
    conn, secrets, accounts = boot.conn, boot.secrets, boot.accounts
    # 세션이 env 로 넘긴 값. 없으면 안전하게 dry_run(주문·승인 요청 없음).
    dry_run = os.environ.get("TRADER_DRY_RUN", "1") != "0"
    # dry_run 도 잔고로 판정은 한다(주문·승인 요청만 생략). 브로커 생성은 네트워크를 쓰지 않는다.
    brokers = {b.account.id: b for b in _build_brokers(secrets, accounts)}
    try:
        market_client = _build_market(secrets)
        holiday_fn = market_client.is_holiday  # 7일 캐시, 보유 거래일 계산용
    except Exception:  # noqa: BLE001 - 실전 앱키가 없으면 휴장일 없이 평일 수로 계산
        market_client = None
        holiday_fn = None
    return ToolContext(
        settings=config.load_settings(),
        accounts=accounts,
        conn=conn,
        brokers=brokers,
        notifier=make_notifier(secrets),
        dry_run=dry_run,
        holiday_fn=holiday_fn,
        market_client=market_client,
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
        path = _collect_and_save()(
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
        from trader.orchestrator.run_store import run_date_for

        day_ = run_date_for(market, when)
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
    from trader.orchestrator.executor import ExecContext

    boot = _boot(settings=True, secrets=True, accounts=True, conn=True)
    secrets, accounts, conn = boot.secrets, boot.accounts, boot.conn
    return ExecContext(
        settings=boot.settings,
        accounts=accounts,
        conn=conn,
        brokers={b.account.id: b for b in _build_brokers(secrets, accounts)},
        notifier=make_notifier(secrets),
    ), secrets


@approvals_app.command("list")
@_config_errors
def approvals_list() -> None:
    """미처리 승인 요청 목록 (만료분은 expired 로 정리)."""
    from trader.ledger import repo
    from trader.orchestrator import executor

    ectx, _ = _exec_ctx()
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
        typer.echo(approval_line(a))


@_config_errors
def _decide(approval_id: int, action: str) -> None:
    from trader.orchestrator import executor

    ectx, _ = _exec_ctx()
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


@_config_errors
def _telegram_run(*, interval: int, max_iterations: int | None) -> None:
    import time

    from trader.notify.commands import poll_once

    ectx, secrets = _exec_ctx()
    configure(paths.state_dir(), name="telegram")  # 데몬과 다른 파일 (오래 사는 명령)
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


def _daemon_features_fn(secrets: config.Secrets, universe, market_client):
    """데몬의 피처 스토어 작업 함수 `(action, markets, *, progress, **kw) -> 행 수`.

    대기열 요청(features_backfill·snapshot·derive)과 reflect 앞 스냅샷이 같은 재료(DART·EDGAR 는
    호출마다 새로 조립)를 쓴다. derive 는 KIS 호출이 없어 시세 클라이언트가 없어도 돈다."""
    from trader.ml import features

    def run(action: str, markets: list[str], *, progress=lambda s: None, **kw) -> int:
        if action == "derive":
            return features.rederive(progress=progress)
        if market_client is None:
            raise config.ConfigError("시세 클라이언트 없음(피처 백필·스냅샷 불가)")
        fn = features.backfill if action == "backfill" else features.snapshot
        return fn(
            market_client=market_client,
            dart=_build_dart(secrets),
            edgar=_build_edgar(),
            universe=universe,
            markets=markets,  # type: ignore[arg-type]
            progress=progress,
            **kw,
        )

    return run


def _daemon_snapshot_fn(secrets: config.Secrets, universe, market_client, notifier=None):
    """데몬 reflect 잡 앞에서 매일 피처 스냅샷을 찍는 함수 (시세 클라이언트 없으면 None).
    수정주가 소급 변경으로 재백필한 종목은 알린다(Stock D-034)."""
    if market_client is None:
        return None
    from trader.notify import messages

    features_fn = _daemon_features_fn(secrets, universe, market_client)

    def on_adjusted(market: str, tickers: list[str]) -> None:
        if notifier is not None:
            notifier.send(messages.adjusted_prices(market, tickers))

    return lambda markets: features_fn("snapshot", markets, on_adjusted=on_adjusted)


@app.command("daemon")
@_config_errors
def daemon_cmd(
    no_web: bool = typer.Option(False, "--no-web", help="웹 서버를 띄우지 않음"),
    dry_run: bool = typer.Option(False, "--dry-run", help="세션·monitor 를 dry_run 으로"),
) -> None:
    """스케줄 데몬: schedule.yaml 대로 scan/analyze/monitor·텔레그램·체결 동기화 + web (§10.1)."""
    from trader import daemon as daemon_mod

    deps = _daemon_deps(dry_run=dry_run)
    r = daemon_mod.run(deps, web=not no_web)
    if not r.get("ok"):
        typer.echo(f"오류: {r.get('error')}", err=True)
        raise typer.Exit(code=1)
    typer.echo("데몬 종료")


def _daemon_deps(*, dry_run: bool = False):
    """데몬 의존성 조립 (daemon·app 명령 공용)."""
    from trader import daemon as daemon_mod
    from trader.ledger import db

    boot = _boot(settings=True, accounts=True, secrets=True, conn=True)
    settings, accounts, secrets = boot.settings, boot.accounts, boot.secrets
    boot.conn.close()  # 스키마 마이그레이션만 (잡은 conn_factory 로 각자 연다)
    schedule = config.load_schedule()
    universe = config.load_universe()
    sources_cfg = config.load_sources()
    try:
        market_client = _build_market(secrets)
    except Exception as e:  # noqa: BLE001
        logging.getLogger("trader.daemon").warning(
            "시세 클라이언트 없음(휴장일·monitor 비활성): %s", e
        )
        market_client = None

    def web_app():
        from trader.web.app import create_app

        wd = _web_deps()
        extra = _hosts_for_bind(wd.settings.web.bind)  # 단독 `trader web` 과 같은 규칙
        if extra:
            logging.getLogger("trader.web").warning(
                "루프백이 아닌 주소(%s)에 바인드: 조회 API 가 토큰 없이 열리고 Host 제한이 꺼짐",
                wd.settings.web.bind,
            )
            wd.extra_hosts.extend(extra)
        return create_app(wd)

    return daemon_mod.DaemonDeps(
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
        snapshot_fn=_daemon_snapshot_fn(
            secrets, universe, market_client, notifier=make_notifier(secrets)
        ),
        features_fn=_daemon_features_fn(secrets, universe, market_client),
    )


@app.command("init")
@_config_errors
def init_cmd() -> None:
    """첫 실행 초기화: config/ secrets/ state/ docs/ 를 만들고 템플릿 중 없는 파일만 채운다."""
    from trader.platform import bundle

    created = bundle.first_run_init(paths.project_root())
    typer.echo("생성: " + (", ".join(created) or "없음 (이미 초기화됨)"))
    if not paths.secrets_path().exists():
        typer.echo(
            "다음: secrets/.env.example 을 secrets/.env 로 복사해 키를 넣고, "
            "config/accounts.yaml 의 계좌번호를 채우세요"
        )


@app.command("app")
@_config_errors
def app_cmd(
    no_browser: bool = typer.Option(False, "--no-browser", help="브라우저를 열지 않음"),
    browser_delay: float = typer.Option(1.5, "--browser-delay", help="웹 기동 뒤 브라우저까지 초"),
    dry_run: bool = typer.Option(False, "--dry-run", help="세션·monitor 를 dry_run 으로"),
    tray: bool = typer.Option(
        False, "--tray", help="메뉴바/작업표시줄 아이콘으로 제어 (데몬은 백그라운드 스레드)"
    ),
) -> None:
    """번들 더블클릭 진입점: 초기화 → 데몬(스케줄 + 웹 UI) → 브라우저 (§10.2)."""
    import webbrowser

    from trader import daemon as daemon_mod
    from trader.platform import bundle

    created = bundle.first_run_init(paths.project_root())
    if created:
        typer.echo("초기화: " + ", ".join(created))
    deps = _daemon_deps(dry_run=dry_run)
    bind, port = deps.settings.web.bind, deps.settings.web.port
    url = f"http://{bind}:{port}"
    typer.echo(f"웹 UI: {url}  (종료: Ctrl+C)")
    finished = threading.Event()

    def open_when_ready() -> None:
        # 첫 실행은 무거운 import 로 1.5초를 훌쩍 넘기므로 포트가 열릴 때까지(최대 20초) 기다린다.
        # 데몬이 먼저 끝났으면(잠금 실패 등) 열지 않는다.
        if browser_delay > 0:
            finished.wait(browser_delay)
        if finished.is_set():
            return
        if _wait_for_port(bind, port, timeout_s=20.0, stop=finished):
            webbrowser.open(url)

    if not no_browser:
        threading.Thread(target=open_when_ready, name="trader-open-browser", daemon=True).start()
    if tray:
        r = _run_with_tray(deps, daemon_mod, url=url, finished=finished)
    else:
        try:
            r = daemon_mod.run(deps, web=True)
        finally:
            finished.set()
    if not r.get("ok"):
        typer.echo(f"오류: {r.get('error')}", err=True)
        raise typer.Exit(code=1)


def _run_with_tray(deps, daemon_mod, *, url: str, finished: threading.Event) -> dict:
    """데몬을 스레드에서 돌리고 메인 스레드는 트레이(pystray 는 메인 스레드 필수)를 맡는다.

    종료(메뉴·시그널)는 scheduler.shutdown(wait=True) → 데몬 스레드가 잠금을 풀고 끝난 뒤 돌아온다.
    pystray 가 없으면 경고 뒤 데몬 스레드를 기다리는 것으로 대신한다(터미널 모드와 같다)."""
    import signal
    import webbrowser

    from trader.platform import bundle, procs, tray

    holder: dict[str, Any] = {}
    result: dict[str, Any] = {}

    def scheduler_factory():
        holder["scheduler"] = daemon_mod.make_scheduler()
        return holder["scheduler"]

    def run_daemon() -> None:
        try:
            r = daemon_mod.run(
                deps, web=True, install_signals=False, scheduler_factory=scheduler_factory
            )
            result.update(r)
        except Exception as e:  # noqa: BLE001 - 스레드에서 죽으면 트레이가 알아야 한다
            result.update(ok=False, error=str(e))
        finally:
            finished.set()

    worker = threading.Thread(target=run_daemon, name="trader-daemon")
    worker.start()
    # 잠금 실패처럼 바로 끝나는 경우는 트레이를 띄우지 않는다
    finished.wait(1.0)
    if finished.is_set():
        worker.join()
        return result

    quitting = threading.Event()

    def shutdown() -> None:
        if quitting.is_set():
            return
        quitting.set()
        deps.stop_event.set()
        sched = holder.get("scheduler")
        if sched is not None:
            try:
                sched.shutdown(wait=True)
            except Exception:  # noqa: BLE001
                pass

    def on_signal(signum, frame) -> None:
        logging.getLogger("trader.daemon").info("시그널 %s 수신, 트레이·데몬을 종료합니다", signum)
        threading.Thread(target=shutdown, name="trader-shutdown", daemon=True).start()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, on_signal)
        except (ValueError, OSError):
            pass
    # 메인 스레드는 icon.run()(AppKit 런루프)에 막혀 위 핸들러가 돌지 않는다 → wakeup fd 감시
    release_signals = tray.watch_signals(
        lambda signum: on_signal(signum, None), (signal.SIGINT, signal.SIGTERM)
    )

    conn_factory = deps.conn_factory
    try:
        tray.run_tray(
            state_fn=lambda: tray.read_state(conn_factory, url),
            on_toggle_pause=lambda: tray.toggle_pause(conn_factory),
            on_open_web=lambda: webbrowser.open(url),
            on_open_logs=lambda: procs.open_folder(paths.state_dir() / "logs"),
            on_quit=shutdown,
            icon_dir=bundle.icons_dir(),
            stop_event=deps.stop_event,
            finished=finished,
        )
    except tray.TrayUnavailable as e:
        typer.echo(f"트레이 없이 실행합니다 ({e}). 종료는 Ctrl+C.")
        worker.join()
        release_signals()
        return result
    shutdown()
    worker.join()
    release_signals()
    return result


def _wait_for_port(
    host: str, port: int, *, timeout_s: float, stop: threading.Event | None = None
) -> bool:
    """host:port 가 연결을 받을 때까지 기다린다 (최대 timeout_s). stop 이 세워지면 False."""
    import socket
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if stop is not None and stop.is_set():
            return False
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.25)
    return False


@app.command("selfcheck")
def selfcheck_cmd() -> None:
    """무거운 라이브러리가 import 되는지 점검한다 (번들 스모크·설치 확인용, JSON 한 줄)."""
    import importlib
    import json

    from trader.platform import bundle

    mods = [
        "sklearn",
        "pyarrow.parquet",
        "pandas",
        "fastapi",
        "uvicorn",
        "apscheduler",
        "httpx",
        "playwright.sync_api",
        "pypdf",
    ]
    loaded, failed = [], {}
    for m in mods:
        try:
            importlib.import_module(m)
            loaded.append(m.split(".")[0])
        except Exception as e:  # noqa: BLE001 - 어떤 import 실패든 보고한다
            failed[m] = f"{type(e).__name__}: {e}"
    try:  # 세이브티커 수집기: Playwright 드라이버(node)가 번들에 실행 가능하게 들어 있는지
        import os
        from pathlib import Path

        from playwright._impl._driver import compute_driver_executable

        node = Path(compute_driver_executable()[0])
        if not (node.is_file() and os.access(node, os.X_OK)):
            failed["playwright.driver"] = f"실행 불가: {node.name}"
    except Exception as e:  # noqa: BLE001
        failed.setdefault("playwright.driver", f"{type(e).__name__}: {e}")
    optional = {}
    for m in ("lightgbm", "stable_baselines3"):  # 선택 백엔드: 없어도 ok (sklearn·cem 폴백)
        try:
            importlib.import_module(m)
            optional[m] = True
        except Exception:  # noqa: BLE001 - ImportError·OSError(libomp 없음) 모두 "없음"
            optional[m] = False
    out = {
        "ok": not failed,
        "frozen": bundle.is_frozen(),
        "modules": loaded,
        "failed": failed,
        "optional": optional,
    }
    typer.echo(json.dumps(out, ensure_ascii=False))
    if failed:
        raise typer.Exit(code=1)


# --- 체결 동기화 (Phase 6) ------------------------------------------------------

fills_app = typer.Typer(help="체결 조회로 fills 를 적재하고 주문 상태를 갱신한다")
app.add_typer(fills_app, name="fills")


@fills_app.command("sync")
def fills_sync(
    account: str | None = typer.Option(None, "--account", help="특정 계좌 id 만"),
    date: str | None = typer.Option(
        None, "--date", help="YYYY-MM-DD (기본: 시장별 현지 오늘 — US 는 뉴욕 날짜)"
    ),
) -> None:
    """계좌별 체결을 원장에 적재한다 (멱등)."""
    from datetime import date as _date

    from trader.orchestrator.fills import sync_fills

    try:
        boot = _boot(secrets=True, accounts=True, conn=True)
        secrets, accounts, conn = boot.secrets, boot.accounts, boot.conn
        d = _date.fromisoformat(date) if date else datetime.now(UTC)
    except (config.ConfigError, paths.ProjectRootNotFound, ValueError) as e:
        typer.echo(f"오류: {e}", err=True)
        raise typer.Exit(code=1) from e
    if account:
        accounts = [a for a in accounts if a.id == account]
    try:
        for b in _build_brokers(secrets, accounts):
            r = sync_fills(conn, b, b.account, d)
            when = r["date"]
            if isinstance(when, dict):
                when = " ".join(f"{m}:{v}" for m, v in when.items())
            typer.echo(
                f"{r['account_id']} {when}: 체결 {r['inserted']}건 적재, "
                f"주문 {r['orders_updated']}건 갱신"
            )
    finally:
        conn.close()


# --- 런타임 제어 (control 테이블) -----------------------------------------------

control_app = typer.Typer(help="일시정지·계좌 모드 (SQLite control 테이블, yaml 보다 우선)")
app.add_typer(control_app, name="control")
jobs_app = typer.Typer(
    help="작업 대기열 (요청만 넣고 실행은 데몬 job_queue 틱)", no_args_is_help=True
)
app.add_typer(jobs_app, name="jobs")


@jobs_app.command("list")
@_config_errors
def jobs_list(limit: int = typer.Option(20, "--limit", help="표시 건수")) -> None:
    """대기·실행 중 요청(실행 순서)과 최근 종료분을 보여준다."""
    from trader import jobs as jobq

    boot = _boot(conn=True)
    try:
        rows = jobq.list_recent(boot.conn, limit=limit)
    finally:
        boot.conn.close()
    if not rows:
        typer.echo("요청 없음")
        return
    for r in rows:
        extra = r.get("progress") if r["status"] == "running" else r.get("error")
        shown = jobq.label(r["kind"], r["params"])
        typer.echo(
            f"#{r['id']} {r['status']:7} p{r.get('priority', 0)} {shown}"
            f" ({r['source']}, {r['requested_at']})" + (f" — {extra}" if extra else "")
        )


@jobs_app.command("run")
@_config_errors
def jobs_run(
    kind: str = typer.Argument(..., help="작업 종류 (`trader jobs kinds`)"),
    market: str | None = typer.Option(None, "--market", help="KR 또는 US"),
    ticker: str | None = typer.Option(None, "--ticker", help="종목 (analyze_ticker)"),
    exchange: str | None = typer.Option(None, "--exchange", help="US 거래소 NAS|NYS|AMS"),
    live: bool = typer.Option(False, "--live", help="analyze_ticker 를 실제 모드로(dry_run=false)"),
    priority: int | None = typer.Option(None, "--priority", help="우선순위(기본 kind 별)"),
) -> None:
    """요청을 대기열에 넣는다. 데몬이 켜져 있어야 실행된다(꺼져 있으면 거부)."""
    from trader import heartbeat
    from trader import jobs as jobq

    params: dict = {}
    if market:
        params["market"] = market.upper()
    if ticker:
        params["ticker"] = ticker.upper()
    if exchange:
        params["exchange"] = exchange.upper()
    if live:
        params["dry_run"] = False
    try:
        p = jobq.validate(kind, params)
    except ValueError as e:
        raise config.ConfigError(str(e)) from e
    if not heartbeat.read_status(paths.state_dir(), datetime.now(UTC), [])["alive"]:
        raise config.ConfigError("데몬이 꺼져 있어 요청을 실행할 주체가 없습니다 (`trader daemon`)")
    boot = _boot(conn=True)
    try:
        rid = jobq.enqueue(boot.conn, kind, p, source="cli", priority=priority)
    except jobq.DuplicateRequest as e:
        raise config.ConfigError(str(e)) from e
    finally:
        boot.conn.close()
    typer.echo(f"요청 #{rid} {jobq.label(kind, p)} — 데몬이 순서대로 실행합니다")


@jobs_app.command("kinds")
def jobs_kinds() -> None:
    """요청 가능한 작업 종류."""
    from trader import jobs as jobq

    for k in jobq.KINDS:
        typer.echo(f"{k:18} {jobq.label(k, {})}" + ("  (가벼움)" if not jobq.is_heavy(k) else ""))


def _control_conn():

    boot = _boot(accounts=True, conn=True)
    return boot.conn, boot.accounts


@control_app.command("show")
@_config_errors
def control_show() -> None:
    """일시정지 여부와 계좌별 실효 모드를 보여준다."""
    from trader.control import control

    conn, accounts = _control_conn()
    try:
        typer.echo(f"일시정지: {'예' if control.is_paused(conn) else '아니오'}")
        from trader.risk import killswitch

        ks = killswitch.reason(conn)
        typer.echo(f"킬스위치: {'켜짐 — ' + ks if ks else '꺼짐'}")
        from trader.orchestrator.executor import effective_mode

        for a in accounts:
            mode, note = effective_mode(conn, a)
            src = "control" if control.get_flag(conn, f"mode:{a.id}") is not None else "yaml"
            suffix = f" — {note}" if note else ""
            typer.echo(f"{a.id} ({a.env}): {mode} [{src}]{suffix}")
    finally:
        conn.close()


@control_app.command("mode")
@_config_errors
def control_mode(
    account: str = typer.Argument(..., help="계좌 id"),
    mode: str = typer.Argument(..., help="auto | approve | off"),
    confirm: str | None = typer.Option(None, "--confirm", help="실전 auto 전환 확인 문자열(LIVE)"),
) -> None:
    """계좌 실행 모드를 바꾼다. 실전 auto 는 --confirm LIVE 가 있어야 하며 확인 기록을 남긴다."""
    from trader.control import control

    conn, accounts = _control_conn()
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
@_config_errors
def control_pause() -> None:
    """paused 플래그를 켠다 (Phase 6 데몬이 확인)."""
    from trader.control import control

    conn, _ = _control_conn()
    try:
        control.set_flag(conn, "paused", True)
    finally:
        conn.close()
    typer.echo("일시정지: 예")


@app.command("paper-review")
@_config_errors
def paper_review_cmd(days: int = typer.Option(7, "--days", min=1, max=90)) -> None:
    """모의 계좌 결정 주문의 체결률·지정가 대비 슬리피지를 비용 가정과 비교한다(읽기 전용, JSON)."""
    import json
    from datetime import timedelta

    from trader.ledger import paper_review

    boot = _boot(settings=True, accounts=True, conn=True)
    try:
        today = datetime.now(config.KST).date()
        r = paper_review.compute(
            boot.conn, boot.accounts, since=today - timedelta(days=days), until=today
        )
    finally:
        boot.conn.close()
    typer.echo(json.dumps(r, ensure_ascii=False, indent=1))


@control_app.command("killswitch")
@_config_errors
def control_killswitch(
    action: str = typer.Argument(..., help="on | off | show"),
    note: str = typer.Option("", "--note", help="off 때 원인 메모(10자 이상)"),
) -> None:
    """누적 손실 킬스위치(신규 매수 중단). off 는 원인 메모가 필요하고 기준 NAV 를 다시 잡는다."""
    from trader.risk import killswitch

    conn, _ = _control_conn()
    try:
        if action == "on":
            killswitch.engage(conn, reason=note or "수동(CLI)", source="cli")
        elif action == "off":
            try:
                killswitch.release(conn, note=note)
            except ValueError as e:
                typer.echo(f"오류: {e}", err=True)
                raise typer.Exit(code=1) from e
        elif action != "show":
            typer.echo("오류: on | off | show", err=True)
            raise typer.Exit(code=1)
        r = killswitch.reason(conn)
        typer.echo(f"킬스위치: {'켜짐 — ' + r if r else '꺼짐'}")
    finally:
        conn.close()


@control_app.command("resume")
@_config_errors
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


def _web_deps(log_name: str | None = None):
    """log_name="web" 은 단독 `trader web` 용 (데몬 안의 web 스레드는 데몬 로그를 그대로 쓴다)."""
    from trader.ledger import db
    from trader.web.app import WebDeps

    boot = _boot(settings=True, accounts=True, secrets=True, conn=True, log_name=log_name)
    settings, accounts, secrets = boot.settings, boot.accounts, boot.secrets
    boot.conn.close()  # 스키마 마이그레이션만 (라우터는 conn_factory 로 각자 연다)
    log = logging.getLogger("trader.web")
    try:
        market = _build_market(secrets)
    except Exception as e:  # noqa: BLE001 - 시세 없이도 조회 페이지는 뜬다
        log.warning("시세 클라이언트 생성 실패(차트 비활성): %s", e)
        market = None
    if not secrets.WEB_TOKEN:
        log.warning("WEB_TOKEN 미설정: 제어 API 비활성(401)")
    try:
        schedule = config.load_schedule()
    except config.ConfigError as e:  # 예정표만 빠진다
        log.warning("schedule.yaml 을 읽지 못해 잡 예정표를 표시하지 않음: %s", e)
        schedule = None
    return WebDeps(
        settings=settings,
        accounts=accounts,
        conn_factory=db.connect,
        brokers={b.account.id: b for b in _build_brokers(secrets, accounts)},
        market=market,
        token=secrets.WEB_TOKEN or None,
        dist=paths.web_dist(),
        notifier=make_notifier(secrets),
        schedule=schedule,
        quote_hub=_quote_hub(settings, market),
    )


def _quote_hub(settings: config.Settings, market):
    """웹 실시간 시세 허브(`settings.web.realtime_ws`). 첫 장중 요청에 스레드가 뜬다."""
    if not settings.web.realtime_ws:
        return None
    from trader.web.live_quotes import QuoteHub

    return QuoteHub.for_market(market)


@app.command("web")
@_config_errors
def web(
    host: str | None = typer.Option(None, "--host", help="바인드 주소 (기본 settings.web.bind)"),
    port: int | None = typer.Option(None, "--port", help="포트 (기본 settings.web.port)"),
) -> None:
    """웹 UI 서버를 띄운다 (조회는 토큰 없음, 제어는 Bearer WEB_TOKEN)."""
    import uvicorn

    from trader.web.app import create_app

    deps = _web_deps(log_name="web")
    bind = host or deps.settings.web.bind
    extra = _hosts_for_bind(bind)
    if extra:
        logging.getLogger("trader.web").warning(
            "루프백이 아닌 주소(%s)에 바인드: 조회 API 가 토큰 없이 열리고 Host 제한이 꺼집니다",
            bind,
        )
        deps.extra_hosts.extend(extra)
    uvicorn.run(
        create_app(deps),
        host=bind,
        port=port or deps.settings.web.port,
        log_level="info",
    )


def _hosts_for_bind(bind: str) -> list[str]:
    """루프백이면 추가 허용 없음. 그 밖(0.0.0.0·LAN IP)이면 클라이언트가 LAN IP·호스트명을 Host 로
    보내므로 바인드 주소를 허용 목록에 넣는 것으로는 전부 400 이 난다 → 모든 Host 허용."""
    return [] if bind in ("127.0.0.1", "localhost", "::1") else ["*"]


market_tool_app = typer.Typer(help="시장 세션 도구 (Phase 18)", no_args_is_help=True)
tool_app.add_typer(market_tool_app, name="market")


@market_tool_app.command("bundle")
def tool_market_bundle(
    market: str = typer.Option(..., "--market"),
    date: str = typer.Option(..., "--date"),
    kind: str = typer.Option(..., "--kind", help="analyst | researcher | strategist"),
    round: int | None = typer.Option(None, "--round"),
    side: str | None = typer.Option(None, "--side"),
) -> None:
    from trader.orchestrator import tools

    _tool_run(tools.market_bundle, market=market, date=date, kind=kind, round=round, side=side)


@market_tool_app.command("write")
def tool_market_write(
    market: str = typer.Option(..., "--market"),
    date: str = typer.Option(..., "--date"),
    stage: str = typer.Option(..., "--stage", help="_market_analyst | _market_debate_r<n>_<side>"),
    agent: str | None = typer.Option(None, "--agent"),
    json_text: str | None = typer.Option(None, "--json", help="JSON (없으면 stdin)"),
) -> None:
    from trader.orchestrator import tools

    _tool_run(
        tools.market_write,
        market=market,
        date=date,
        stage=stage,
        payload=_payload_arg(json_text),
        agent=agent,
    )


@market_tool_app.command("summarize")
def tool_market_summarize(
    market: str = typer.Option(..., "--market"), date: str = typer.Option(..., "--date")
) -> None:
    from trader.orchestrator import tools

    _tool_run(tools.market_summarize, market=market, date=date)


@tool_app.command("submit-market-view")
def tool_submit_market_view(
    market: str = typer.Option(..., "--market"),
    date: str = typer.Option(..., "--date"),
    json_text: str | None = typer.Option(None, "--json", help="HedgeProposal JSON (없으면 stdin)"),
) -> None:
    from trader.orchestrator import tools

    _tool_run(tools.submit_market_view, market=market, date=date, payload=_payload_arg(json_text))


scan_tool_app = typer.Typer(help="scan 세션 도구 (Phase 19 발굴)", no_args_is_help=True)
tool_app.add_typer(scan_tool_app, name="scan")


@scan_tool_app.command("discovery-inputs")
def tool_scan_discovery_inputs(
    market: str = typer.Option(..., "--market"),
    date: str = typer.Option(..., "--date"),
) -> None:
    from trader.orchestrator import tools

    _tool_run(tools.discovery_inputs, market=market, date=date)


@tool_app.command("submit-discovery")
def tool_submit_discovery(
    market: str = typer.Option(..., "--market"),
    date: str = typer.Option(..., "--date"),
    json_text: str | None = typer.Option(None, "--json", help="Discovered JSON (없으면 stdin)"),
) -> None:
    from trader.orchestrator import tools

    _tool_run(tools.submit_discovery, market=market, date=date, payload=_payload_arg(json_text))


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
    from trader.orchestrator import tools
    from trader.orchestrator.run_store import write_checkpoint
    from trader.orchestrator.session import PipelineError
    from trader.scan import quant, screener

    try:
        boot = _boot(settings=True, accounts=True, secrets=True, conn=True)
        if market not in ("KR", "US"):
            raise config.ConfigError(f"시장은 KR 또는 US 여야 합니다: {market}")
        settings, accounts, secrets, conn = boot.settings, boot.accounts, boot.secrets, boot.conn
        universe = config.load_universe()
        when = datetime.fromisoformat(asof) if asof else datetime.now(UTC)
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        from trader.ml import ranker
        from trader.orchestrator.run_store import run_date_for

        day = run_date_for(market, when)

        ml = ranker.current(market=market)
        try:
            runtime_watch = control.get_watchlist(conn, market=market)
            watch = [*universe.watch, *runtime_watch]
            cands = quant.candidates(
                universe,
                market,
                rank_fn=ml[0] if ml else None,  # type: ignore[arg-type]
                watch_extra=runtime_watch,
                asof=day,  # 과거 시점 스캔이 그 뒤 피처 파일을 보지 않도록
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
                universe=universe,
                discovery=True,
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
            "discovery": r.get("discovery"),
            "cap_hit": cap_hit,
            "ml_model": ml[1] if ml else None,
        }
        write_checkpoint(run_store.market_dir(day, market, create=True), "_scan", out)
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

    from trader.orchestrator import monitor as monitor_mod
    from trader.orchestrator.run_store import run_date_for
    from trader.orchestrator.session import run_session

    try:
        # 데몬과 다른 로그 파일 (오래 사는 명령)
        boot = _boot(settings=True, accounts=True, secrets=True, conn=True, log_name="monitor")
        if market not in ("KR", "US"):
            raise config.ConfigError(f"시장은 KR 또는 US 여야 합니다: {market}")
        settings, accounts, secrets, conn = boot.settings, boot.accounts, boot.secrets, boot.conn
        universe = config.load_universe()
        market_client = _build_market(secrets)
    except (config.ConfigError, paths.ProjectRootNotFound) as e:
        typer.echo(f"오류: {e}", err=True)
        raise typer.Exit(code=1) from e
    except Exception as e:  # noqa: BLE001 - 시세 클라이언트 없이는 monitor 를 돌릴 수 없다
        typer.echo(f"오류: 시세 클라이언트 생성 실패: {e}", err=True)
        raise typer.Exit(code=1) from e
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
            today = run_date_for(market, datetime.now(UTC))
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
@_config_errors
def install_cmd(
    dry_run: bool = typer.Option(False, "--dry-run", help="파일을 쓰지 않고 내용·명령만 출력"),
    uninstall: bool = typer.Option(False, "--uninstall", help="등록 해제"),
    watchdog: bool = typer.Option(
        False, "--watchdog", help="데몬 대신 외부 감시(trader watchdog, 5분 간격)를 등록·해제"
    ),
) -> None:
    """로그온 시 데몬 자동 시작 등록 (macOS launchd / Windows 작업 스케줄러).
    --watchdog 이면 데몬 밖 감시 프로세스를 5분 간격 1회 실행으로 등록한다(Phase 23 C)."""
    from trader.platform import installer

    kind = "watchdog" if watchdog else "daemon"
    try:
        if uninstall:
            r = installer.uninstall(kind=kind)
            typer.echo(f"해제: {r['path']} ({' '.join(r['command'])})")
            return
        if watchdog:
            r = installer.install(dry_run=dry_run, kind="watchdog")
            typer.echo(("[dry-run] " if dry_run else "") + f"파일: {r['path']}")
            typer.echo("명령: " + " ".join(r["command"]))
            if dry_run:
                typer.echo(r["content"])
            return
        from trader.platform.claude_bin import find_claude

        extra = []
        settings = config.load_settings()
        try:
            extra.append(find_claude(settings.claude_bin).parent)
        except Exception as e:  # noqa: BLE001 - claude 가 없으면 PATH 에 넣지 않고 경고만
            logging.getLogger("trader.daemon").warning("claude 실행파일을 찾지 못함: %s", e)
        r = installer.install(
            dry_run=dry_run,
            extra_paths=extra,
            exit_timeout_s=int(settings.llm.timeout_s) + 60,  # 종료 시 analyze 세션을 기다린다
        )
    except config.ConfigError as e:
        typer.echo(f"오류: {e}", err=True)
        raise typer.Exit(code=1) from e
    typer.echo(("[dry-run] " if dry_run else "") + f"파일: {r['path']}")
    typer.echo("명령: " + " ".join(r["command"]))
    if dry_run:
        typer.echo(r["content"])


@app.command("watchdog")
@_config_errors
def watchdog_cmd(
    quiet: bool = typer.Option(False, "--quiet", help="알림 없이 판정만 출력(점검용)"),
) -> None:
    """외부 감시 1회(Phase 23 C): 데몬 생존·하트비트·운영 점검을 지금 판정해 주의 이상이면 텔레그램.
    `trader install --watchdog` 가 5분 간격으로 등록한다. 상태를 바꾸지 않는다."""
    import json

    from trader.ops import watchdog as watchdog_mod

    b = _boot(settings=True, accounts=True, secrets=True, conn=True, log_name="watchdog")
    try:
        notifier = None if quiet else make_notifier(b.secrets)
        try:
            from trader.daemon import build_jobs

            jobs = build_jobs(config.load_schedule(), b.settings, markets=list(b.settings.markets))
        except config.ConfigError:
            jobs = []
        out = watchdog_mod.run_once(b.conn, b.settings, b.accounts, notifier, schedule_jobs=jobs)
    finally:
        b.conn.close()
    typer.echo(json.dumps(out, ensure_ascii=False))


# --- ML (Phase 7) ----------------------------------------------------------------

ml_app = typer.Typer(help="퀀트 모델 학습·레지스트리")
app.add_typer(ml_app, name="ml")


@ml_app.command("train")
@_config_errors
def ml_train(
    market: str | None = typer.Option(None, "--market", help="KR|US (기본 전체)"),
    min_rows: int = typer.Option(100, "--min-rows", help="학습 최소 라벨 행 수"),
    horizon: int | None = typer.Option(
        None,
        "--horizon",
        min=1,
        max=60,
        help="실험용: 이 실행만 label.horizon_days 를 덮어쓴다 (설정 파일 불변, 1~60)",
    ),
) -> None:
    """피처 스토어로 퀀트 모델을 워크포워드 학습·평가·저장하고 승격 기준을 판정한다."""
    import json

    from trader.ml import train as train_mod

    configure(paths.state_dir())
    cfg = config.load_ml()
    if horizon is not None:
        # model_copy(update=) 는 검증을 건너뛴다 → model_validate
        # (0·음수 호라이즌은 과거를 보는 라벨)
        label = config.LabelCfg.model_validate({**cfg.label.model_dump(), "horizon_days": horizon})
        cfg = cfg.model_copy(update={"label": label})
    if market is not None and market not in ("KR", "US"):
        raise config.ConfigError(f"시장은 KR 또는 US: {market}")
    r = train_mod.train_quant(cfg, market=market, min_rows=min_rows)
    typer.echo(json.dumps(r, ensure_ascii=False, default=str))


@ml_app.command("tune")
@_config_errors
def ml_tune(
    market: str | None = typer.Option(None, "--market", help="KR|US (기본 전체)"),
    grid: str = typer.Option("small", "--grid", help="small|wide"),
    min_rows: int = typer.Option(100, "--min-rows", help="학습 최소 라벨 행 수"),
) -> None:
    """하이퍼파라미터 탐색: dev 폴드(마지막 폴드 제외)로 고르고 홀드아웃을 보고한다.

    자동 적용 없음 — 리포트를 보고 ml.yaml 에 반영한다."""
    import json

    from trader.ml import tune as tune_mod

    configure(paths.state_dir())
    cfg = config.load_ml()
    if market is not None and market not in ("KR", "US"):
        raise config.ConfigError(f"시장은 KR 또는 US: {market}")
    r = tune_mod.tune_quant(cfg, market=market, min_rows=min_rows, grid=grid)
    typer.echo(json.dumps(r, ensure_ascii=False, default=str))


@ml_app.command("models")
@_config_errors
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
@_config_errors
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
@_config_errors
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
@_config_errors
def rl_train(
    market: str | None = typer.Option(None, "--market", help="KR|US (기본 전체)"),
    seeds: int | None = typer.Option(None, "--seeds", min=1, max=10, help="시드 수 (기본 ml.yaml)"),
) -> None:
    """스텝 데이터로 사이저를 워크포워드×시드 학습·기준선 비교·저장한다 (자동 승격 없음)."""
    import json

    from trader.ledger import db
    from trader.ml.rl import train as rl_train_mod

    configure(paths.state_dir())
    cfg = config.load_ml()
    if market is not None and market not in ("KR", "US"):
        raise config.ConfigError(f"시장은 KR 또는 US: {market}")
    if seeds is not None:
        rl_cfg = cfg.models.rl_sizer.model_copy(update={"seeds": seeds})
        cfg = cfg.model_copy(update={"models": cfg.models.model_copy(update={"rl_sizer": rl_cfg})})
    conn = db.connect()
    db.init_schema(conn)
    try:
        r = rl_train_mod.train_sizer(cfg, conn, market=market)
    finally:
        conn.close()
    typer.echo(json.dumps(r, ensure_ascii=False, default=str))


@rl_app.command("models")
@_config_errors
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


@rl_app.command("report")
@_config_errors
def rl_report(
    account: str = typer.Option("paper-main", "--account", help="모의 계좌 id"),
    weeks: int = typer.Option(8, "--weeks", min=1, max=52),
    market: str | None = typer.Option(None, "--market", help="KR|US (기본 전체)"),
) -> None:
    """모델 요약 + 최근 N주 섀도 실적(제안 vs LLM 비중의 가상 실현수익) JSON 1줄."""
    import json

    from trader.ml.rl import report

    boot = _boot(accounts=True, conn=True)
    accounts, conn = boot.accounts, boot.conn
    if not any(a.id == account for a in accounts):
        conn.close()
        raise config.ConfigError(f"알 수 없는 계좌: {account}")
    try:
        r = report.comparison(conn, None, account_id=account, market=market, weeks=weeks)
    finally:
        conn.close()
    typer.echo(json.dumps(r, ensure_ascii=False, default=str))


@rl_app.command("promote")
@_config_errors
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
@_config_errors
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
@_config_errors
def reflect_cmd(
    market: str = typer.Option("KR", "--market", help="KR 또는 US"),
    dry_run: bool = typer.Option(False, "--dry-run", help="결과 채우기까지만 (세션 없음)"),
) -> None:
    """만기 결정의 결과를 채우고(피처 스토어 종가) reflector 세션으로 복기를 받는다 (스펙 §6.5)."""
    from trader.memory import outcomes, store
    from trader.orchestrator.reflect import pending, run_reflect
    from trader.orchestrator.session import PipelineError

    boot = _boot(settings=True, accounts=True, conn=True)
    if market not in ("KR", "US"):
        boot.conn.close()
        raise config.ConfigError(f"시장은 KR 또는 US 여야 합니다: {market}")
    settings, accounts, conn = boot.settings, boot.accounts, boot.conn
    try:
        today = datetime.now(KST).date()
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
secrets_app = typer.Typer(help="secrets/.env 관리 (값은 출력하지 않는다)", no_args_is_help=True)
app.add_typer(secrets_app, name="secrets")
universe_app = typer.Typer(help="유니버스 점검", no_args_is_help=True)
app.add_typer(universe_app, name="universe")


@universe_app.command("check")
@_config_errors
def universe_check_cmd(
    market: str = typer.Option("KR", "--market", help="KR 또는 US"),
    show_all: bool = typer.Option(False, "--all", help="문제 없는 종목도 출력"),
) -> None:
    """유니버스 종목이 KIS 시세를 실제로 주는지 점검한다 (US 는 거래소 코드 제안). 문제면 exit 1."""
    from trader.scan import universe_check

    if market not in ("KR", "US"):
        raise config.ConfigError(f"시장은 KR 또는 US 여야 합니다: {market}")
    boot = _boot(secrets=True)
    universe = config.load_universe()
    rows = universe_check.check(universe, _build_market(boot.secrets), market=market)
    bad = [r for r in rows if not r["ok"]]
    for r in rows if show_all else bad:
        mark = "OK " if r["ok"] else "NG "
        ex = f" {r['exchange']}" if r["exchange"] else ""
        hint = f" → {r['suggested_exchange']}" if r["suggested_exchange"] else ""
        typer.echo(f"{mark}{r['ticker']}{ex}{hint} {r['note']}".rstrip())
    typer.echo(f"점검 {len(rows)}종목, 문제 {len(bad)}건")
    if bad:
        raise typer.Exit(code=1)


def _templates_config_dir() -> Path:
    """config/*.yaml 의 사본이 있는 templates/config (개발 체크아웃).

    번들(_internal)에는 쓰지 않는다."""
    return paths.project_root() / "templates" / "config"


@universe_app.command("sectors")
@_config_errors
def universe_sectors_cmd(
    market: str = typer.Option("KR", "--market", help="KR 또는 US"),
    show_all: bool = typer.Option(False, "--all", help="이미 섹터가 있는 종목도 출력"),
    write: bool = typer.Option(
        False, "--write", help="제안을 config/sectors.yaml(+템플릿)에 덧붙인다"
    ),
) -> None:
    """KIS 업종으로 sectors.yaml 에 빠진 종목의 섹터를 제안한다. 표에 없는 업종이 있으면 exit 1."""
    from datetime import date as _date

    from trader.platform import bundle
    from trader.scan import sector_suggest

    if market not in ("KR", "US"):
        raise config.ConfigError(f"시장은 KR 또는 US 여야 합니다: {market}")
    boot = _boot(secrets=True)
    universe = config.load_universe()
    sectors = config.load_sectors()
    rows = sector_suggest.suggest(
        universe, sectors, _build_market(boot.secrets), market=market, only_missing=not show_all
    )
    for r in rows:
        ex = f" {r['exchange']}" if r["exchange"] else ""
        typer.echo(
            f"{r['ticker']}{ex}  업종={r['industry'] or '-'}  현재={r['current'] or '-'}  "
            f"제안={r['suggested'] or '-'}  {r['note']}".rstrip()
        )
    unresolved = [r for r in rows if r["current"] is None and not r["suggested"]]
    if write:
        today = _date.today()
        n = sector_suggest.apply(paths.config_dir() / "sectors.yaml", market, rows, today=today)
        tmpl = _templates_config_dir() / "sectors.yaml"
        wrote_tmpl = False
        if not bundle.is_frozen() and tmpl.is_file():
            # 템플릿은 KIS 제안이 아니라 config 값으로 맞춘다 (config 에만 있던 수동 항목도 함께)
            sector_suggest.sync(paths.config_dir() / "sectors.yaml", tmpl, market, today=today)
            wrote_tmpl = True
        typer.echo(f"기록: {n}종목 (config/sectors.yaml{'·templates' if wrote_tmpl else ''})")
    typer.echo(f"점검 {len(rows)}종목, 제안 없음 {len(unresolved)}건")
    if unresolved:
        raise typer.Exit(code=1)


@secrets_app.command("import")
@_config_errors
def secrets_import(
    src: str = typer.Option(..., "--from", help="원본 .env 경로 (예: ../Stock/.env)"),
    overwrite: bool = typer.Option(False, "--overwrite", help="이미 값이 있는 키도 바꾼다"),
    keys: str | None = typer.Option(None, "--keys", help="대상 키 이름(쉼표 구분). 없으면 전부"),
) -> None:
    """다른 프로젝트 .env 의 키를 이름 매핑(KIS_APP_KEY→KIS_PAPER_APP_KEY 등)으로 옮긴다.
    기본은 비어 있는 키만 채우고, 키 이름만 출력한다."""
    from pathlib import Path as _Path

    from trader import secrets_io

    r = secrets_io.import_env(
        _Path(src).expanduser(),
        paths.secrets_path(),
        overwrite=overwrite,
        keys=[k.strip() for k in keys.split(",") if k.strip()] if keys else None,
    )
    typer.echo("복사: " + (", ".join(r["copied"]) or "없음"))
    typer.echo("유지(이미 값 있음, 원본과 같음): " + (", ".join(r["same"]) or "없음"))
    typer.echo(
        "유지(이미 값 있음, 원본과 다름 → --overwrite 로 교체 가능): "
        + (", ".join(r["differs"]) or "없음")
    )
    typer.echo("원본에 없음: " + (", ".join(r["unavailable"]) or "없음"))
    if r.get("backup"):
        typer.echo(f"백업: secrets/{r['backup']}")


@calibration_app.command("build")
@_config_errors
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
@_config_errors
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


regime_app = typer.Typer(help="시장 국면 (Phase 17 H): 조회·과거 재현", no_args_is_help=True)
app.add_typer(regime_app, name="regime")

scalp_app = typer.Typer(
    help="분 단위 단타 (Phase 23 S, 모의 전용·기본 끔): 1분봉 백테스트·실적 보고",
    no_args_is_help=True,
)
app.add_typer(scalp_app, name="scalp")


@scalp_app.command("backtest")
@_config_errors
def scalp_backtest(
    ticker: str = typer.Option(..., "--ticker", help="종목 코드"),
    market: str = typer.Option("KR", "--market", help="KR|US"),
    exchange: str | None = typer.Option(None, "--exchange", help="US 거래소 NAS|NYS|AMS"),
    days: int = typer.Option(1, "--days", min=1, max=5, help="최근 N일 (KIS 당일 분봉만 — 오늘)"),
) -> None:
    """현재 분봉(KIS 당일, 실전 시세 키 필요)으로 단타 규칙을 재현해 JSON 으로. 설정 불변·주문 없음.
    KIS 분봉은 당일분만 주므로 --days 는 상태 파일이 있는 과거 날의 기록을 더해 보여 준다."""
    import json

    from trader.scalp import engine as scalp_engine
    from trader.scalp import signals

    configure(paths.state_dir())
    settings = config.load_settings()
    schedule = config.load_schedule()
    ms = schedule.kr if market == "KR" else schedule.us
    secrets = config.load_secrets()
    mc = _build_market(secrets)
    bars = (
        mc.get_minute_kr(ticker, count=400)
        if market == "KR"
        else mc.get_minute_us(ticker, exchange or "NAS", count=400)
    )
    trade = signals.backtest_day(
        bars, settings.scalp, open_hm=ms.monitor.start, close_hm=ms.monitor.end
    )
    out = {
        "ticker": ticker,
        "market": market,
        "bars": len(bars),
        "today": trade,
        "config": settings.scalp.model_dump(),
        "report": scalp_engine.report(paths.state_dir(), market=market, days=days),
    }
    typer.echo(json.dumps(out, ensure_ascii=False, default=str))


@scalp_app.command("report")
@_config_errors
def scalp_report(
    market: str = typer.Option("KR", "--market", help="KR|US"),
    days: int = typer.Option(5, "--days", min=1, max=60, help="최근 운영일 수"),
    live: bool = typer.Option(
        False, "--live", help="오늘 NO_GO 판정의 가상 수익을 당일 분봉으로 계산(실전 시세 키)"
    ),
) -> None:
    """단타 상태 파일(state/scalp/<date>/<market>.json)로 승률·사유별 집계 + 판정자 GO vs NO_GO
    사후 비교(Phase 25)를 JSON 으로."""
    import json
    from datetime import UTC, datetime

    from trader.orchestrator.run_store import run_date_for
    from trader.scalp import engine as scalp_engine

    configure(paths.state_dir())
    settings = config.load_settings()
    from trader import market_hours

    try:  # 장중 감시 창의 끝(마감 청산 기준). schedule.yaml 이 없으면 정규장 마감
        sched = config.load_schedule()
        close_hm = (sched.kr if market == "KR" else sched.us).monitor.end
    except config.ConfigError:
        close_hm = market_hours.REGULAR[market][1]
    judge = {"cfg": settings.scalp, "close_hm": close_hm, "minutes_fn": None}
    if live:  # 오늘 NO_GO 신호의 가상 수익 — KIS 당일 분봉(실전 시세 키)
        mc = _build_market(config.load_secrets())
        us_ex: dict[str, str] = {}
        if market == "US":
            try:
                us_ex = {u.symbol: u.exchange for u in config.load_universe().us}
            except config.ConfigError:
                us_ex = {}
        judge["minutes_fn"] = (
            (lambda t, ex=None: mc.get_minute_kr(t, count=400))
            if market == "KR"
            else (lambda t, ex=None: mc.get_minute_us(t, ex or us_ex.get(t, "NAS"), count=400))
        )
    out = scalp_engine.report(
        paths.state_dir(),
        market=market,
        days=days,
        today=run_date_for(market, datetime.now(UTC)),
        judge=judge,
    )
    out["enabled"] = settings.scalp.enabled
    out["judge_enabled"] = settings.scalp.judge
    typer.echo(json.dumps(out, ensure_ascii=False, default=str))


theme_app = typer.Typer(help="테마 열기 감지 (Phase 25): 조회", no_args_is_help=True)
app.add_typer(theme_app, name="theme")


@theme_app.command("show")
@_config_errors
def theme_show(
    market: str = typer.Option("KR", "--market", help="KR|US"),
    live: bool = typer.Option(
        False, "--live", help="저장된 결과 대신 지금 계산(KIS 순위 조회, 저장 안 함)"
    ),
) -> None:
    """오늘 테마 열기 결과(데몬 <시장>_theme 이 남긴 control 값)를 JSON 으로. 상태 불변."""
    import json

    from trader.control import control
    from trader.ledger import db
    from trader.orchestrator.run_store import run_date_for
    from trader.scan import theme

    configure(paths.state_dir())
    settings = config.load_settings()
    conn = db.connect()
    db.init_schema(conn)
    try:
        if live:
            from datetime import UTC, datetime

            from trader.sources import saveticker

            try:
                mc = _build_market(config.load_secrets())
            except Exception:  # noqa: BLE001 - 키가 없으면 순위 없이
                mc = None
            now = datetime.now(UTC)
            out = theme.compute(
                conn,
                settings,
                market=market,
                today=run_date_for(market, now),
                now=now,
                market_client=mc,
                saveticker_root=saveticker.resolve_dir(config.load_sources().news.saveticker_dir),
                state=paths.state_dir(),
            )
        else:
            out = control.get_flag(conn, f"{theme.FLAG}:{market}") or {
                "market": market,
                "themes": [],
                "note": "아직 결과 없음 — 데몬 장중 잡(<시장>_theme) 또는 --live",
            }
    finally:
        conn.close()
    typer.echo(json.dumps(out, ensure_ascii=False, default=str))


@regime_app.command("show")
@_config_errors
def regime_show() -> None:
    """시장별 현재 국면(control 플래그)과 헤지 설정을 JSON 으로."""
    import json

    from trader.ledger import db
    from trader.risk import regime as regime_mod

    configure(paths.state_dir())
    settings = config.load_settings()
    conn = db.connect()
    db.init_schema(conn)
    try:
        out = {
            "hedge": settings.hedge.model_dump(),
            "markets": regime_mod.summary(conn, list(settings.markets)),
        }
    finally:
        conn.close()
    typer.echo(json.dumps(out, ensure_ascii=False, default=str))


@regime_app.command("backtest")
@_config_errors
def regime_backtest(
    market: str = typer.Option("KR", "--market", help="KR|US"),
    days: int = typer.Option(1000, "--days", min=260, max=1000, help="지수 proxy 일봉 수"),
) -> None:
    """지수 proxy 일봉으로 과거 bear 구간을 재현한다 (임계값 확정용, 상태는 바꾸지 않음)."""
    import json

    from trader.risk import regime as regime_mod

    configure(paths.state_dir())
    settings = config.load_settings()
    if market not in ("KR", "US"):
        raise config.ConfigError(f"시장은 KR 또는 US: {market}")
    inst = settings.hedge.index_proxy.get(market)  # type: ignore[arg-type]
    if inst is None:
        raise config.ConfigError(f"{market} index_proxy 미설정")
    mc = _build_market(config.load_secrets())
    candles = (
        mc.get_daily_us(inst.symbol, inst.exchange or "AMS", days=days)
        if market == "US"
        else mc.get_daily_kr(inst.symbol, days=days)
    )
    rows = [(c.date, c.close) for c in candles]
    spans = regime_mod.backtest(rows, settings.hedge)
    out = {
        "market": market,
        "proxy": inst.symbol,
        "candles": len(rows),
        "range": [rows[0][0].isoformat(), rows[-1][0].isoformat()] if rows else None,
        "bear_spans": spans,
        "transitions": len(spans),
    }
    typer.echo(json.dumps(out, ensure_ascii=False, default=str))


@app.command("readiness")
@_config_errors
def readiness_cmd() -> None:
    """실전 전환 준비도 점검(읽기 전용). 모드는 바꾸지 않는다 — docs/runbook-live-transition.md."""
    import json

    from trader import readiness
    from trader.ledger import db

    configure(paths.state_dir())
    settings = config.load_settings()
    accounts = config.load_accounts()
    conn = db.connect()
    db.init_schema(conn)
    try:
        r = readiness.check(
            conn,
            accounts=accounts,
            settings=settings,
            state=paths.state_dir(),
            today=datetime.now(KST).date(),
        )
    finally:
        conn.close()
    typer.echo(json.dumps(r, ensure_ascii=False))


@app.command("notify-test")
@_config_errors
def notify_test() -> None:
    """텔레그램으로 테스트 메시지를 보낸다 (미설정이면 NullNotifier 로 성공)."""
    configure(paths.state_dir())
    secrets = config.load_secrets()
    notifier = make_notifier(secrets)
    stamp = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    if notifier.send(f"trader 알림 테스트 {stamp}"):
        typer.echo("전송 성공")
    else:
        typer.echo("전송 실패")
        raise typer.Exit(code=1)
