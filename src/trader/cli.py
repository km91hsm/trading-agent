"""`trader` 명령행 진입점 (typer)."""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import typer

from trader import config
from trader.broker.accounts import KisBroker
from trader.broker.kis_auth import KisAuth
from trader.broker.kis_client import KisClient
from trader.broker.kis_market import KisMarket
from trader.logging_setup import configure
from trader.notify.telegram import make_notifier
from trader.platform import paths

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


def _build_client(secrets: config.Secrets, env: config.Env) -> KisClient:
    key = secrets.KIS_PAPER_APP_KEY if env == "paper" else secrets.KIS_REAL_APP_KEY
    secret = secrets.KIS_PAPER_APP_SECRET if env == "paper" else secrets.KIS_REAL_APP_SECRET
    if not key or not secret:
        raise config.ConfigError(f"{env} 앱키·시크릿이 secrets/.env 에 없습니다")
    return KisClient(env, KisAuth(env, key, secret))


def _build_market(secrets: config.Secrets) -> KisMarket:
    """시세 클라이언트 (실전 env 전용)."""
    return KisMarket(_build_client(secrets, "real"))


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
    for broker in _build_brokers(secrets, accounts):
        for m in broker.account.markets:
            check(
                f"{broker.account.id} {m} 현금",
                lambda b=broker, m=m: f"{b.get_cash(m).available} {b.get_cash(m).currency}",
            )
    typer.echo(f"실패 {failures}건")
    if failures:
        raise typer.Exit(code=1)


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
