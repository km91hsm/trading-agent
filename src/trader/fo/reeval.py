"""파생 장중 재평가(Phase 26 단계 4, 스펙 §5). 엔진이 트리거를 잡으면 `request` 가 재평가 요청
(`_fo_reeval_r<n>_<계좌>`)을 남기고 빠른 대기열 `fo_reevaluate` 로 넘긴다 → `run` 이 `fo_resume`
세션을 띄운다 → 세션의 `submit-fo-decision --stage _fo_decision_r<n>_<계좌>` 가 `apply` 를 부른다.

`apply` 는 **늦은 재평가 CAS**: 봉투의 `expected_version` 이 지금 포지션 버전과 같고 상태가
`held` 일 때만 적용한다(그 사이 손절·감액·체결이 있었으면 버린다). 실행은 `executor.run_leg`
하나를 지나므로 게이트·예약 트랜잭션·단조(손절·배수·보호 배수)가 아침 결정과 같다. REPLACE·반대
방향 OPEN 은 청산을 먼저 내고 새 진입 봉투를 `control.fo_replace:<계좌>:<포지션>` 에 두며, 다음
틱(`continue_replace`)이 보유 0 ∧ 살아 있는 다리 0 일 때 운영일 1회 진입한다."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date
from typing import Any

from trader.config import AccountConfig
from trader.control import control
from trader.fo import executor, rules, store
from trader.fo.executor import FoExecContext
from trader.fo.schemas import FoDecisionEnvelope
from trader.ledger import repo

log = logging.getLogger("trader.fo")

REPLACE_FLAG = "fo_replace:"
MARKET = "KR"


def replace_key(account_id: str, position_id: int) -> str:
    return f"{REPLACE_FLAG}{account_id}:{position_id}"


def _tool_ctx(ctx: FoExecContext):
    from trader.orchestrator import tools

    return tools.ToolContext(
        settings=ctx.settings, accounts=[], conn=ctx.conn, state=ctx.state, now=ctx.now, fo=ctx,
        dry_run=ctx.dry_run,
    )  # fmt: skip


def request(
    ctx: FoExecContext,
    *,
    account_id: str,
    position_id: int,
    trigger_id: int,
    trigger: dict[str, Any],
    today: date,
) -> dict[str, Any]:
    """재평가 요청 체크포인트를 남기고 대기열 파라미터를 돌려준다. 같은 포지션·버전의 미결 요청이
    있으면 그 라운드를 재사용한다(`tools.fo_reeval_request`)."""
    from trader.orchestrator import tools

    req = tools.fo_reeval_request(
        _tool_ctx(ctx), date=today, account_id=account_id, position_id=position_id,
        trigger={**trigger, "id": trigger_id},
    )  # fmt: skip
    if not req.get("ok"):
        return {"ok": False, "error": req.get("error")}
    pos = req["position"]
    return {
        "ok": True, "reused": bool(req.get("reused")),
        "params": {
            "account_id": account_id, "position_id": position_id,
            "expected_version": pos["expected_version"], "request_id": pos["request_id"],
            "round": req["round"], "trigger_id": trigger_id, "date": today.isoformat(),
        },
    }  # fmt: skip


def run(
    ctx: FoExecContext,
    params: dict[str, Any],
    *,
    session_runner: Callable[..., Any] | None = None,
    cancel: Any | None = None,
) -> dict[str, Any]:
    """대기열 잡 본체: 트리거가 이미 처리됐거나(청산으로 폐기) 포지션이 바뀌었으면 세션 없이
    끝내고, 아니면 `fo_resume` 세션 1회 → `triggers.handled_at`·`session_id`(실패는 '실패:')."""
    from trader.orchestrator import resume
    from trader.orchestrator import session as session_mod
    from trader.orchestrator.run_store import market_dir, read_checkpoint

    conn = ctx.conn
    now = ctx.now()
    tid = int(params["trigger_id"])
    trig = repo.get_trigger(conn, tid)
    if trig is None or trig.get("handled_at"):
        return {"skipped": "트리거가 이미 처리됨(폐기 포함)", "trigger_id": tid}
    pos = store.get_position(conn, int(params["position_id"]))
    if pos is None or pos.version != int(params["expected_version"]) or pos.status != "held":
        repo.mark_trigger_handled(conn, tid, "폐기: 포지션 변경", handled_at=now.isoformat())
        return {"skipped": "포지션이 바뀌어 재평가 생략", "trigger_id": tid}
    day = date.fromisoformat(params["date"])
    mdir = market_dir(day, MARKET, ctx.state)
    stage = f"_fo_reeval_r{int(params['round'])}_{params['account_id']}"
    req = (read_checkpoint(mdir, stage) or {}).get("data") or {}
    if read_checkpoint(mdir, "_fo_inputs") is None or read_checkpoint(mdir, "_fo_summary") is None:
        # 재평가는 그날 아침 세션의 입력·리포트 위에서만 결정을 받을 수 있다(제출 전제조건) —
        # 아침 세션이 없던 날은 세션 비용을 쓰지 않고 버린다. 보호선·유지 조건은 엔진이 계속 본다
        repo.mark_trigger_handled(
            conn, tid, "폐기: 오늘 파생 분석 없음", handled_at=now.isoformat()
        )
        return {"skipped": "오늘 아침 파생 세션의 입력·요약이 없어 재평가 생략", "trigger_id": tid}
    if req.get("request_id") != params.get("request_id"):
        repo.mark_trigger_handled(conn, tid, "폐기: 요청 불일치", handled_at=now.isoformat())
        return {"skipped": f"재평가 요청 {stage} 이 없거나 다름", "trigger_id": tid}
    spec = session_mod.SessionSpec(
        market=MARKET, date=day, tickers=[], dry_run=ctx.dry_run, kind="fo_resume",
        position=req, trigger={"kind": trig["kind"], "detail": trig.get("detail") or {}},
    )  # fmt: skip
    runner = session_runner or session_mod.run_session
    try:
        extra = {"cancel": cancel} if cancel is not None else {}
        res = runner(
            spec, ctx.settings, conn, [], state=ctx.state, now=ctx.now, brokers=ctx.brokers,
            notifier=ctx.notifier, fo=ctx, **extra,
        )  # fmt: skip
    except session_mod.PipelineError as e:
        repo.mark_trigger_handled(conn, tid, f"실패: {e}"[:200], handled_at=ctx.now().isoformat())
        return {"skipped": str(e), "trigger_id": tid}
    # 처리 시각은 세션이 **끝난** 시각(간격 제한이 세션 길이만큼 짧아지지 않게)
    repo.mark_trigger_handled(
        conn, tid, resume.handled_marker(res), handled_at=ctx.now().isoformat()
    )
    return {"trigger_id": tid, "ok": res.ok, "error": res.error, "session_id": res.session_id}


def apply(ctx: FoExecContext, env: FoDecisionEnvelope, *, round_n: int) -> dict[str, Any]:
    """재평가 결정 적용(`submit_fo_decision` 의 `_r<n>` 분기가 부른다)."""
    conn = ctx.conn
    account = executor.account_of(ctx, env.account_id)
    if account is None:
        return {"discarded": f"파생 계좌 {env.account_id} 없음"}
    pos = store.get_position(conn, env.position_id) if env.position_id is not None else None
    if pos is None or pos.account_id != account.id:
        return {"discarded": f"포지션 {env.position_id} 없음"}
    if pos.version != env.expected_version or pos.status != "held":
        return {
            "discarded": "포지션이 바뀌어 재평가 결과 폐기",
            "version": pos.version, "expected_version": env.expected_version,
            "status": pos.status,
        }  # fmt: skip
    d = env.decision
    opposite = d.intent == "OPEN" and (d.direction != pos.direction or d.vehicle != pos.vehicle)
    if d.intent == "REPLACE" or opposite:
        _, today = executor._today(ctx)
        new_dec = d.model_copy(update={"intent": "OPEN"})
        new_env = env.model_copy(update={
            "decision": new_dec, "account_id": None, "position_id": None,
            "expected_version": None,
        })  # fmt: skip
        if d.direction != "FLAT":
            control.set_flag(
                conn, replace_key(account.id, pos.id),
                {"date": today.isoformat(), "round": round_n,
                 "envelope": new_env.model_dump(mode="json")},
            )  # fmt: skip
        close_env = env.model_copy(update={"decision": d.model_copy(update={"intent": "CLOSE"})})
        out = executor.run_leg(ctx, account, close_env, round=round_n, source="reeval")
        return {"applied": "REPLACE", "results": out, "replace_pending": d.direction != "FLAT"}
    out = executor.run_leg(ctx, account, env, round=round_n, source="reeval")
    mode, _ = executor.fo_mode(conn, account)
    hold = executor._record_only(ctx, mode, control.is_paused(conn))
    if d.intent == "HOLD" and d.take_profit_pt is not None and hold is None:
        cur = store.get_position(conn, pos.id)
        tp = rules._d(d.take_profit_pt)
        if cur is not None and cur.tp_pt != tp and cur.status == "held":
            with store.tx(conn):
                store.transition(conn, cur.id, expected_version=cur.version, tp_pt=tp)
    return {"applied": d.intent, "results": out}


def _replace_flags(conn, account_id: str) -> list[tuple[str, dict]]:
    import json

    rows = conn.execute(
        "SELECT key, value FROM control WHERE key LIKE ?", (f"{REPLACE_FLAG}{account_id}:%",)
    ).fetchall()
    out = []
    for r in rows:
        v = json.loads(r["value"])
        if isinstance(v, dict):
            out.append((r["key"], v))
    return out


def continue_replace(ctx: FoExecContext, account: AccountConfig) -> list[dict[str, Any]]:
    """REPLACE 의 두 번째 다리: 옛 포지션이 보유 0 ∧ 살아 있는 다리 0 이 되면 저장된 새 진입
    봉투를 운영일 1회 실행한다. 다른 날 것·unknown 이 된 포지션의 것은 버린다. 진입 창 밖이면
    기다린다(오늘 cutoff 가 지나면 버림)."""
    conn = ctx.conn
    now, today = executor._today(ctx)
    out: list[dict[str, Any]] = []
    for key, flag in _replace_flags(conn, account.id):
        pid = int(key.rsplit(":", 1)[1])
        pos = store.get_position(conn, pid)
        if flag.get("date") != today.isoformat() or pos is None or pos.status == "unknown":
            control.set_flag(conn, key, None)
            out.append({"position_id": pid, "discarded": "다른 날·대조 필요"})
            continue
        if pos.qty_held > 0 or store.live_legs(conn, position_id=pid):
            continue  # 청산이 아직 진행 중
        if not rules._in_entry_window(now, ctx.settings.fo):
            from trader.config import KST

            local = now.astimezone(KST).time()
            if local.strftime("%H:%M") >= ctx.settings.fo.entry_window.cutoff:
                control.set_flag(conn, key, None)
                out.append({"position_id": pid, "discarded": "진입 창 마감"})
            continue
        control.set_flag(conn, key, None)
        env = FoDecisionEnvelope.model_validate(flag["envelope"])
        res = executor.run_leg(ctx, account, env, round=int(flag.get("round") or 0),
                               source="replace")  # fmt: skip
        out.append({"position_id": pid, "opened": res})
    return out
