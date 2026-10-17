"""텔레그램 명령 (/approve /reject /status /pause /resume /mode /watch). CLI·데몬 공용.

실전 계좌 모드는 텔레그램으로 off(하향)만 바꿀 수 있다. 상향은 CLI `control mode` 만."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from trader.control import control
from trader.ledger import repo
from trader.notify.telegram import parse_command
from trader.orchestrator import executor


def approval_line(a: dict) -> str:
    o = a["order"]
    return (
        f"#{a['id']} {a['account_id']} {o.get('side')} {o.get('ticker')} {o.get('qty')}주 "
        f"@{o.get('price') or '시장가'} 만료 {a['expires_at']}"
    )


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
            current = control.set_watchlist(conn, [t for t in current if t != parts[1].upper()])
        elif sub != "list":
            return "사용법: /watch add|remove <ticker> | /watch list"
    except ValueError as e:
        return f"거부: {e}"
    return "[관심종목] " + (", ".join(current) or "없음")


def poll_once(ectx: Any, receiver: Any, *, echo: Callable[[str], None] = lambda s: None) -> dict:
    """getUpdates 1회: 명령을 처리하고 오프셋을 control `telegram_offset` 에 저장한다."""
    conn = ectx.conn
    offset = control.get_flag(conn, "telegram_offset")
    updates = receiver.poll(offset)
    executor.expire_approvals(ectx)
    handled = 0
    for u in updates:
        cmd = parse_command(u.text)
        if cmd is None:
            continue
        name, arg = cmd
        handled += 1
        if name == "status":
            pending = repo.pending_approvals(conn)
            modes = ", ".join(f"{a.id}={control.get_account_mode(conn, a)}" for a in ectx.accounts)
            body = "\n".join(approval_line(a) for a in pending) or "없음"
            paused = "일시정지" if control.is_paused(conn) else "실행 중"
            ectx.notifier.send(f"[상태] {paused} · 계좌 모드: {modes}\n미승인 주문:\n{body}")
            echo(f"status → 미승인 {len(pending)}건")
            continue
        if name in ("pause", "resume", "mode", "watch"):
            ectx.notifier.send(_control_command(ectx, name, arg))
            echo(f"{name} {arg or ''}".strip())
            continue
        if arg is None or not arg.isdigit():
            ectx.notifier.send(f"사용법: /{name} <승인 id>")
            continue
        r = (
            executor.approve(ectx, int(arg))
            if name == "approve"
            else executor.reject(ectx, int(arg))
        )
        echo(f"{name} #{arg} → {'ok' if r['ok'] else r.get('error') or '실패'}")
        if not r["ok"]:
            ectx.notifier.send(f"[{name} 실패] {r.get('error') or r.get('order')}")
    if updates:
        control.set_flag(conn, "telegram_offset", max(u.update_id for u in updates) + 1)
    echo(f"수신 {len(updates)}건, 명령 {handled}건")
    return {"received": len(updates), "handled": handled}
