"""텔레그램 명령 (/approve /reject /status /pause /resume /mode /watch). CLI·데몬 공용.

실전 계좌 모드는 텔레그램으로 off(하향)만 바꿀 수 있다. 상향은 CLI `control mode` 만."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from trader import heartbeat, names
from trader.control import control
from trader.ledger import repo
from trader.notify.telegram import parse_command
from trader.orchestrator import executor
from trader.platform import paths


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
            modes = ", ".join(_mode_label(conn, a) for a in ectx.accounts)
            body = "\n".join(approval_line(a) for a in pending) or "없음"
            paused = "일시정지" if control.is_paused(conn) else "실행 중"
            ectx.notifier.send(
                f"[상태] {paused} · 계좌 모드: {modes}\n{daemon_line(ectx)}\n미승인 주문:\n{body}"
            )
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
    # 걸러진(다른 채팅) 업데이트까지 포함한 최대 id 로 오프셋을 전진시킨다
    ids = [u.update_id for u in updates]
    last_raw = getattr(receiver, "last_update_id", None)
    if last_raw is not None:
        ids.append(int(last_raw))
    if ids:
        control.set_flag(conn, "telegram_offset", max(ids) + 1)
    echo(f"수신 {len(updates)}건, 명령 {handled}건")
    return {"received": len(updates), "handled": handled}
