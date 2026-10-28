"""텔레그램 명령 (/approve /reject /status /pause /resume /mode /watch /run /kill). CLI·데몬 공용.

실전 계좌 모드는 텔레그램으로 off(하향)만 바꿀 수 있다. 상향은 CLI `control mode` 만."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from trader import heartbeat, names
from trader.control import control
from trader.ledger import repo
from trader.notify import messages
from trader.notify.telegram import parse_command
from trader.orchestrator import executor
from trader.platform import paths

logger = logging.getLogger("trader.telegram")


def _mode_label(conn, account) -> str:
    """STATUS·웹과 같은 실효 모드 (실전 auto + 확인 기록 없음 = off, 강등 표시)."""
    mode, note = executor.effective_mode(conn, account)
    env = messages.ENV.get(account.env, account.env)
    return f"{account.id}({env}) {messages.MODE.get(mode, mode)}" + (" · 강등" if note else "")


def approval_line(a: dict, env: str | None = None) -> str:
    """'#12 real-main(실전) 매수 삼성전자(005930) 10주 @71,500원 · 10:42 만료' (KST)."""
    o = a["order"]
    shown = names.label(str(o.get("ticker")), o.get("name"))
    market = o.get("market")
    who = a["account_id"] + (f"({messages.ENV[env]})" if env in messages.ENV else "")
    price = f"@{messages.money(o.get('price'), market)}" if o.get("price") else "시장가"
    side = messages.SIDE.get(str(o.get("side")), o.get("side"))
    return (
        f"#{a['id']} {who} {side} {shown} {o.get('qty')}주 {price} · "
        f"{messages.kst_hhmm(a.get('expires_at'))} 만료"
    )


HELP_LINE = (
    "/status · /approve <id> [실전] · /reject <id> · /pause · /resume · "
    "/mode <계좌> <auto|approve|off> · /watch add|remove|list <종목> · "
    "/run <작업> [KR|US] [종목] · /daily · /fo(파생 포지션) · /kill(신규 매수 중단, 해제는 CLI)"
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
        return messages.event("일시정지", "모든 계좌의 주문을 멈췄어요")
    if name == "resume":
        control.set_flag(conn, "paused", False)
        return messages.event("재개", "주문을 다시 내요")
    if name == "kill":
        # 켜기만 한다 — 해제는 원인 메모와 함께 CLI 로만(Stock D-049)
        from trader.risk import killswitch

        killswitch.engage(conn, reason="텔레그램 /kill", source="telegram")
        return messages.killswitch_on("텔레그램 /kill")
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
        return messages.mode_changed(account=acct.id, mode=mode)
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
    return messages.event("관심종목", ", ".join(current) or "없음")


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
    return messages.event(f"요청 #{rid}", f"{jobq.label(kind, p)} — 데몬이 순서대로 실행해요")


# 보낸 사람을 확인할 수 없어도 처리하는 명령: **조회만**(2-T2). 일시정지·킬스위치도 쓰기다 —
# 킬스위치 해제는 CLI 로만 되므로 그룹 구성원 한 명의 실수가 매매 중단으로 이어졌다.
UNTRUSTED_OK = frozenset({"status", "daily", "fo"})  # /fo 는 원장 조회만(Phase 26)
HELP_FLAG = "telegram_help_at"  # 마지막 도움말 발송 시각(폴링 사이에도 유지)
HELP_EVERY_S = 60


def _help_due(ectx: Any, conn) -> bool:
    """모르는 /xxx 마다 도움말을 보내지 않는다 — 분당 1회(2-T2). 시각은 control 에 남긴다."""
    now = ectx.now()
    last = control.get_flag(conn, HELP_FLAG)
    if last:
        try:
            if (now - datetime.fromisoformat(str(last))).total_seconds() < HELP_EVERY_S:
                return False
        except ValueError:
            pass
    control.set_flag(conn, HELP_FLAG, now.isoformat())
    return True


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
            if u.text.strip().startswith("/"):
                # 오타·모르는 명령을 조용히 버리지 않는다(전체 검토 T2) — 단 분당 1회(2-T2)
                if _help_due(ectx, conn):
                    ectx.notifier.send(messages.event("명령 안내", HELP_LINE))
                control.set_flag(conn, "telegram_offset", int(u.update_id) + 1)
            continue
        handled += 1
        if not getattr(u, "trusted", True) and cmd[0] not in UNTRUSTED_OK:
            # 그룹 채팅의 아무 구성원이 승인·재개·모드를 바꾸지 못하게(전체 검토 2026-10-27)
            ectx.notifier.send(
                messages.event(
                    "명령 거부",
                    f"/{cmd[0]} 은 1:1 채팅 또는 허용 사용자"
                    "(TELEGRAM_ALLOWED_USERS)만 쓸 수 있어요",
                )
            )
            control.set_flag(conn, "telegram_offset", int(u.update_id) + 1)
            continue
        try:
            _handle_command(ectx, conn, cmd, echo)
        except Exception as e:  # noqa: BLE001 - 명령 하나의 실패가 나머지·오프셋을 막지 않는다
            failed += 1
            logger.exception("텔레그램 명령 처리 실패: %s", u.text)
            try:
                ectx.notifier.send(messages.failure("명령 실패", f"{u.text}", e))
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
        if name == "daily":
            from trader.ledger import daily_report
            from trader.orchestrator.run_store import run_date_for

            r = daily_report.compute(
                conn,
                ectx.accounts,
                markets=sorted({m for a in ectx.accounts for m in a.markets}),
                today_of=lambda m: run_date_for(m, ectx.now()),
            )
            ectx.notifier.send(
                daily_report.message(r)
                if r["items"]
                else messages.event("일일 리포트", "오늘 잔고 스냅샷이 아직 없어요")
            )
            echo("daily")
            return
        if name == "fo":
            from trader.fo import status as fo_status

            ectx.notifier.send(messages.event("파생", *fo_status.lines(conn, ectx.now())))
            echo("fo")
            return
        if name == "status":
            pending = repo.pending_approvals(conn)
            modes = ", ".join(_mode_label(conn, a) for a in ectx.accounts)
            envs = {a.id: a.env for a in ectx.accounts}
            body = "\n".join(approval_line(a, envs.get(a["account_id"])) for a in pending) or "없음"
            paused = "일시정지" if control.is_paused(conn) else "실행 중"
            from trader.risk import regime as regime_mod
            from trader.status import scan_lines

            regime_line = regime_mod.line(conn, list(ectx.settings.markets))
            scan = "\n".join(scan_lines(ectx.settings, ectx.now(), ectx.state))
            from trader.ops import health

            ops = health.headline(control.get_flag(conn, health.RESULT_FLAG))
            ectx.notifier.send(
                messages.event(
                    "상태",
                    f"{paused} · 계좌 모드: {modes}",
                    daemon_line(ectx),
                    ops,
                    f"국면: {regime_line}",
                    f"오늘 스캔:\n{scan}",
                    f"미승인 주문:\n{body}",
                )
            )
            echo(f"status → 미승인 {len(pending)}건")
            return
        if name in ("pause", "resume", "mode", "watch", "kill"):
            ectx.notifier.send(_control_command(ectx, name, arg))
            echo(f"{name} {arg or ''}".strip())
            return
        if name == "run":
            ectx.notifier.send(run_command(ectx, arg))
            echo(f"run {arg or ''}".strip())
            return
        parts = (arg or "").split()
        if not parts or not parts[0].isdigit():
            ectx.notifier.send(f"사용법: /{name} <승인 id>")
            return
        arg = parts[0]
        if name == "approve":
            # 실전 계좌 승인은 확인어를 붙여야 한다 — 웹의 확인창과 같은 한 번 더 묻기(전체 검토 T2)
            pa = repo.get_approval(conn, int(arg))
            acct = next((x for x in ectx.accounts if pa and x.id == pa["account_id"]), None)
            if acct is not None and acct.env != "paper" and messages.REAL_CONFIRM not in parts[1:]:
                ectx.notifier.send(
                    messages.event(
                        f"확인 필요 #{arg}",
                        f"{acct.id} 은(는) 실전 계좌예요 — 실제 돈으로 주문해요.",
                        f"진행하려면: /approve {arg} {messages.REAL_CONFIRM}",
                    )
                )
                echo(f"approve #{arg} → 실전 확인어 없음")
                return
        r = (
            executor.approve(ectx, int(arg))
            if name == "approve"
            else executor.reject(ectx, int(arg))
        )
        echo(f"{name} #{arg} → {'ok' if r['ok'] else r.get('error') or '실패'}")
        order = r.get("order") or {}
        if not r["ok"] and order.get("status") == "unknown":
            # 응답 없음 — executor 가 이미 [주문 상태 미확인] 을 보냈다(실패로 덧알리면 재주문 유도)
            return
        if not r["ok"]:
            label = "승인" if name == "approve" else "거부"
            ectx.notifier.send(
                messages.failure(
                    f"{label} 실패 #{arg}",
                    "",
                    r.get("error") or order.get("message") or "알 수 없음",
                )
            )
