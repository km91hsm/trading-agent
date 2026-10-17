"""텔레그램 명령 처리 (/approve /reject /status). CLI `telegram poll|loop` 와 데몬이 같이 쓴다."""

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
