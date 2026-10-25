"""텔레그램 명령 (/approve /reject /status /pause /resume /mode /watch /run). CLI·데몬 공용.

실전 계좌 모드는 텔레그램으로 off(하향)만 바꿀 수 있다. 상향은 CLI `control mode` 만."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from trader import heartbeat, names
from trader.control import control
from trader.ledger import repo
from trader.notify.telegram import parse_command
from trader.orchestrator import executor
from trader.platform import paths

logger = logging.getLogger("trader.telegram")


def _mode_label(conn, account) -> str:
    """STATUS·웹과 같은 실효 모드 (실전 auto + 확인 기록 없음 = off, 강등 표시)."""
    mode, note = executor.effective_mode(conn, account)
    return f"{account.id}={mode}" + (" (강등)" if note else "")


def approval_line(a: dict) -> str:
    o = a["order"]
    shown = names.label(str(o.get("ticker")), o.get("name"))
    return (
        f"#{a['id']} {a['account_id']} {o.get('side')} {shown} {o.get('qty')}주 "
        f"@{o.get('price') or '시장가'} 만료 {a['expires_at']}"
    )


def daemon_line(ectx: Any) -> str:
    """하트비트 요약 한 줄: 가동 시간 + 최근에 돈 잡 3개 (/status 응답이 온다 = 데몬이 산 것)."""
    base = ectx.state if ectx.state is not None else paths.state_dir()
    st = heartbeat.read_status(base, ectx.now(), [])
    hb = heartbeat._read_json(base / heartbeat.FILE_NAME) or {}
    jobs = hb.get("jobs") if isinstance(hb.get("jobs"), dict) else {}
    recent = sorted(
        (
            (j.get("last_end") or j.get("last_start") or "", jid, j.get("last_status"))
            for jid, j in jobs.items()
            if j.get("last_start")
        ),
        reverse=True,
    )[:3]
    up = st["uptime_s"]
    head = (
        f"데몬 pid {st['pid']} · 가동 {up // 3600}시간 {up % 3600 // 60}분"
        if st["alive"] and up is not None
        else "데몬 기록 없음"
    )
    tail = ", ".join(f"{jid} {status}" for _, jid, status in recent) or "잡 기록 없음"
    return f"{head} · 최근 잡: {tail}"


def _control_command(ectx: Any, name: str, arg: str | None) -> str:
    conn = ectx.conn
    if name == "pause":
        control.set_flag(conn, "paused", True)
        return "[일시정지] 모든 계좌 주문 중단"
    if name == "resume":
        control.set_flag(conn, "paused", False)
        return "[재개] 실행 중"
    if name == "mode":
        parts = (arg or "").split()
        if len(parts) != 2:
            return "사용법: /mode <계좌 id> <auto|approve|off>"
        acct_id, mode = parts
        acct = next((a for a in ectx.accounts if a.id == acct_id), None)
        if acct is None:
            return f"알 수 없는 계좌: {acct_id}"
        if acct.env == "real" and mode != "off":
            return (
                f"거부: 실전 계좌 {acct.id} 는 텔레그램으로 off(비상 정지)만 가능합니다. "
                "상향은 CLI `trader control mode <계좌> approve|auto --confirm LIVE` 로만"
            )
        try:
            control.set_account_mode(conn, acct, mode)
        except (ValueError, control.ConfirmationRequired) as e:
            return f"거부: {e}"
        return f"[모드] {acct.id} = {mode}"
    parts = (arg or "").split()
    sub = parts[0] if parts else "list"
    current = control.get_watchlist(conn)
    try:
        if sub == "add" and len(parts) == 2:
            current = control.set_watchlist(conn, [*current, parts[1]])
        elif sub == "remove" and len(parts) == 2:
            current = control.remove_watch(conn, parts[1])
        elif sub != "list":
            return "사용법: /watch add|remove <ticker> | /watch list"
    except ValueError as e:
        return f"거부: {e}"
    return "[관심종목] " + (", ".join(current) or "없음")


def run_command(ectx: Any, arg: str | None) -> str:
    """`/run <kind> [KR|US] [ticker]` → 작업 대기열 enqueue(실행은 데몬 job_queue 틱). 데몬이 꺼져
    있으면 넣지 않고 안내한다(웹의 409 와 같은 규칙)."""
    from trader import jobs as jobq

    parts = (arg or "").split()
    if not parts:
        kinds = ", ".join(jobq.KINDS)
        return f"사용법: /run <작업> [KR|US] [종목]\n작업: {kinds}"
    kind = parts[0]
    params: dict = {}
    for tok in parts[1:]:
        up = tok.upper()
        if up in ("KR", "US"):
            params["market"] = up
        else:
            params["ticker"] = up
    base = ectx.state if ectx.state is not None else paths.state_dir()
    if not heartbeat.read_status(base, ectx.now(), [])["alive"]:
        return "거부: 데몬이 꺼져 있어 요청을 실행할 주체가 없어요 (`uv run trader daemon`)"
    try:
        p = jobq.validate(kind, params)
        rid = jobq.enqueue(ectx.conn, kind, p, source="telegram", now=ectx.now())
    except jobq.DuplicateRequest as e:
        return f"거부: {e}"
    except ValueError as e:
        return f"거부: {e}"
    return f"[요청 #{rid}] {jobq.label(kind, p)} — 데몬이 순서대로 실행해요"


def poll_once(ectx: Any, receiver: Any, *, echo: Callable[[str], None] = lambda s: None) -> dict:
    """getUpdates 1회: 명령을 처리하고 오프셋을 control `telegram_offset` 에 저장한다."""
    conn = ectx.conn
    offset = control.get_flag(conn, "telegram_offset")
    updates = receiver.poll(offset)
    executor.expire_approvals(ectx)
    handled = 0
    failed = 0
    for u in updates:
        cmd = parse_command(u.text)
        if cmd is None:
            continue
        handled += 1
        try:
            _handle_command(ectx, conn, cmd, echo)
        except Exception as e:  # noqa: BLE001 - 명령 하나의 실패가 나머지·오프셋을 막지 않는다
            failed += 1
            logger.exception("텔레그램 명령 처리 실패: %s", u.text)
            try:
                ectx.notifier.send(f"[명령 실패] {u.text}: {type(e).__name__}: {e}")
            except Exception:  # noqa: BLE001
                logger.debug("명령 실패 알림 실패", exc_info=True)
        # 업데이트마다 오프셋을 바로 전진시킨다 — 뒤 명령이 예외로 끊겨도 같은 명령을 다시 받지 않음
        control.set_flag(conn, "telegram_offset", int(u.update_id) + 1)
    # 걸러진(다른 채팅) 업데이트까지 포함한 최대 id 로 오프셋을 전진시킨다
    ids = [u.update_id for u in updates]
    last_raw = getattr(receiver, "last_update_id", None)
    if last_raw is not None:
        ids.append(int(last_raw))
    if ids:
        control.set_flag(conn, "telegram_offset", max(ids) + 1)
    echo(f"수신 {len(updates)}건, 명령 {handled}건" + (f", 실패 {failed}건" if failed else ""))
    return {"received": len(updates), "handled": handled, "failed": failed}


def _handle_command(ectx: Any, conn: Any, cmd: tuple[str, str | None], echo: Callable) -> None:
    name, arg = cmd
    if True:
        if name == "status":
            pending = repo.pending_approvals(conn)
            modes = ", ".join(_mode_label(conn, a) for a in ectx.accounts)
            body = "\n".join(approval_line(a) for a in pending) or "없음"
            paused = "일시정지" if control.is_paused(conn) else "실행 중"
            from trader.risk import regime as regime_mod
            from trader.status import scan_lines

            regime_line = regime_mod.line(conn, list(ectx.settings.markets))
            scan = "\n".join(scan_lines(ectx.settings, ectx.now(), ectx.state))
            from trader.ops import health

            ops = health.headline(control.get_flag(conn, health.RESULT_FLAG))
            ectx.notifier.send(
                f"[상태] {paused} · 계좌 모드: {modes}\n{daemon_line(ectx)}\n{ops}\n"
                f"국면: {regime_line}\n오늘 스캔:\n{scan}\n미승인 주문:\n{body}"
            )
            echo(f"status → 미승인 {len(pending)}건")
            return
        if name in ("pause", "resume", "mode", "watch"):
            ectx.notifier.send(_control_command(ectx, name, arg))
            echo(f"{name} {arg or ''}".strip())
            return
        if name == "run":
            ectx.notifier.send(run_command(ectx, arg))
            echo(f"run {arg or ''}".strip())
            return
        if arg is None or not arg.isdigit():
            ectx.notifier.send(f"사용법: /{name} <승인 id>")
            return
        r = (
            executor.approve(ectx, int(arg))
            if name == "approve"
            else executor.reject(ectx, int(arg))
        )
        echo(f"{name} #{arg} → {'ok' if r['ok'] else r.get('error') or '실패'}")
        if not r["ok"]:
            ectx.notifier.send(f"[{name} 실패] {r.get('error') or r.get('order')}")
