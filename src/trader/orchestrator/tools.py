"""LLM 오케스트레이터가 쓰는 도구 4개의 구현 (스펙 §1.1).

모든 함수는 예외 대신 {"ok": bool, ...} dict 를 돌려준다 (CLI 가 JSON 으로 그대로 출력).
순서 강제는 프롬프트가 아니라 여기의 전제조건이 담당한다:
  submit-decision ← 10_analyst_* 가 2개 이상 and 25_research_summary 존재.
"""

from __future__ import annotations

import html
import json
import logging
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from trader.agents import schemas
from trader.config import KST, AccountConfig, Market, Settings
from trader.ledger import repo
from trader.notify.telegram import NullNotifier
from trader.orchestrator import summary as summary_mod
from trader.orchestrator.run_store import checkpoint_payload as _wrap
from trader.orchestrator.run_store import (
    list_checkpoints,
    market_dir,
    read_checkpoint,
    run_dir,
    write_checkpoint,
)
from trader.platform import paths
from trader.risk import costs

logger = logging.getLogger("trader.orchestrator")

MIN_REPORTS = 2
_ANALYST_RE = re.compile(r"^10_analyst_(technical|fundamental|news_macro|sentiment)$")
_DEBATE_RE = re.compile(r"^20_debate_r(\d+)_(bull|bear)$")
_RESUME_RE = re.compile(r"^31_decision_r(\d+)$")
_TICKER_RE = re.compile(r"^(?!\.+$)[A-Za-z0-9.\-]{1,16}$")  # 점만 있는 값(., ..)은 거부
_STAGE_RE = re.compile(r"^\d{2}_[a-z0-9_\-]{1,40}$")
# LLM 이 checkpoint write 로 쓸 수 있는 단계. 30_decision 은 submit-decision 전용(전제조건 검증).
_LLM_WRITABLE = (_ANALYST_RE, _DEBATE_RE)


class ToolInputError(ValueError):
    """ticker·market·stage 형식 오류 (경로 탈출 방지)."""


def _validate_ids(ticker: str, market: str, stage: str | None = None) -> None:
    if not _TICKER_RE.match(ticker or ""):
        raise ToolInputError(f"ticker 형식 오류: {ticker!r}")
    if market not in ("KR", "US"):
        raise ToolInputError(f"market 은 KR 또는 US: {market!r}")
    if stage is not None and not _STAGE_RE.match(stage):
        raise ToolInputError(f"stage 형식 오류: {stage!r}")


def _unescape(obj: Any) -> Any:
    """Claude Code 가 서브에이전트 출력의 <·>·& 를 이스케이프하므로 문자열 값을 되돌린다."""
    if isinstance(obj, str):
        return html.unescape(obj)
    if isinstance(obj, dict):
        return {k: _unescape(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_unescape(v) for v in obj]
    return obj


def _guarded(fn):
    """ToolInputError 를 {"ok": false} 로 바꾸는 데코레이터."""
    import functools

    @functools.wraps(fn)
    def wrapper(ctx, *, ticker=None, market=None, stage=None, **kw):
        try:
            _validate_ids(ticker, market, stage)
        except ToolInputError as e:
            return {"ok": False, "error": str(e)}
        kwargs = {"ticker": ticker, "market": market, **kw}
        if stage is not None:
            kwargs["stage"] = stage
        return fn(ctx, **kwargs)

    return wrapper


class ToolError(RuntimeError):
    """도구 거부 (FAKE 모드·파이썬 호출자가 예외로 받고 싶을 때)."""


@dataclass
class ToolContext:
    settings: Settings
    accounts: list[AccountConfig]
    conn: sqlite3.Connection | None = None
    state: Path | None = None
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    # Phase 4: submit-decision 이 계좌별 실행에 쓴다. 기본은 dry_run(주문·승인 요청 없음).
    brokers: dict[str, Any] = field(default_factory=dict)
    notifier: Any = None
    dry_run: bool = True
    holiday_fn: Callable[[date], bool] | None = None  # 보유 거래일 계산용 (KIS 휴장일)
    market_client: Any | None = None  # Phase 19 발굴: 유니버스 밖 종목의 시세·거래소 확인
    # Phase 26 파생 레인: fo 계좌·브로커·마스터를 묶은 실행 컨텍스트(`fo/executor.FoExecContext`).
    # 없으면 `tool fo *`·`submit-fo-decision` 은 거부한다
    fo: Any | None = None


def _dir(ctx: ToolContext, ticker: str, market: Market, d: date) -> Path:
    return run_dir(d, market, ticker, ctx.state)


def _call(agent: str | None, meta: dict | None) -> dict | None:
    if agent is None and not meta:
        return None
    base = {"agent": agent or "unknown"}
    base.update({k: v for k, v in (meta or {}).items() if k in schemas.CallMeta.model_fields})
    return schemas.CallMeta.model_validate(base).model_dump()


# --- checkpoint -------------------------------------------------------------


@_guarded
def checkpoint_list(ctx: ToolContext, *, ticker: str, market: Market, date: date) -> dict:
    return {"ok": True, "stages": list_checkpoints(_dir(ctx, ticker, market, date))}


def trader_context(
    ctx: ToolContext,
    inputs: dict,
    market: Market,
    *,
    quant: dict | None = None,
    calibration: dict | None = None,
) -> dict:
    """트레이더 입력의 파이썬 제공분. portfolio 는 계좌별 스냅샷, quant 는 05_quant(없으면 None)."""
    quote = ((inputs.get("technical") or {}).get("quote") or {}).get("data")
    return {
        "quote": quote,
        "risk_limits": ctx.settings.risk.model_dump(),
        "costs_bp": costs.round_trip_bp(market),
        "portfolio": _portfolio_context(ctx, market),
        "quant": quant,
        **_memory_context(ctx, inputs, market),
        "calibration": calibration if calibration_injected() else None,
        "regime": _regime_context(ctx, market),
        "market_view": _market_view_context(ctx, market),
    }


def _market_view_context(ctx: ToolContext, market: Market) -> dict | None:
    """오늘 시장 세션의 결론 요약 (Phase 18). 없으면 None."""
    if ctx.conn is None or not ctx.settings.hedge.enabled:
        return None
    from trader.orchestrator.run_store import run_date_for

    v = market_view_today(ctx.conn, market, run_date_for(market, ctx.now()))
    if not v:
        return None
    return {k: v.get(k) for k in ("stance", "hedge_weight", "leveraged", "confidence", "rationale")}


def _regime_context(ctx: ToolContext, market: Market) -> dict | None:
    """국면(Phase 17 H): {state, count, signals, since}. 기록이 없거나 원장이 없으면 None."""
    if ctx.conn is None or not ctx.settings.hedge.enabled:
        return None
    from trader.risk import regime as regime_mod

    r = regime_mod.load(ctx.conn, market)
    if not r:
        return None
    sig = r.get("signals") or {}
    return {
        "state": r.get("state"),
        "count": r.get("count"),
        "signals": {k: sig.get(k) for k in ("ma_below", "momentum_weak", "vol_high")},
        "since": r.get("since"),
        "note": "bear: 게이트가 신규 BUY/ADD 거부·헤지 ETF 보유; neutral: 한도 축소",
    }


def _record_vix(conn, inputs: dict) -> None:
    """00_inputs 의 news_macro.macro.vix(FRED 최신 관측)를 기록한다 (VIX 급등 브레이커 입력)."""
    from trader.memory import store

    try:
        vix = ((inputs.get("news_macro") or {}).get("macro") or {}).get("vix") or {}
        data = vix.get("data") or {}
        if data.get("value") is not None and data.get("date"):
            store.record_vix(conn, value=float(data["value"]), date=str(data["date"]))
    except Exception as e:  # noqa: BLE001 - VIX 기록 실패는 메모리 검색을 막지 않는다
        logger.warning("VIX 기록 실패: %s", e)


def _memory_context(ctx: ToolContext, inputs: dict, market: Market) -> dict:
    """{memory: [...상위 k 요약], memory_status: {warmup, breaker}}. 원장이 없으면 빈 목록."""
    if ctx.conn is None:
        return {"memory": [], "memory_status": {"warmup": True, "breaker": None}}
    from trader.memory import store
    from trader.memory import vector as vec
    from trader.ml import quant_report

    try:
        _record_vix(ctx.conn, inputs)
        ticker = str((inputs.get("meta") or {}).get("ticker") or "")
        row = quant_report.latest_row(ticker, market, ctx.state) if ticker else None
        qv = vec.situation_vector(row or {}, market)
        r = store.retrieve(
            ctx.conn, vector=qv, now=ctx.now(), settings=ctx.settings.memory, market=market
        )
    except Exception as e:  # noqa: BLE001 - 메모리는 선택 입력
        logger.warning("메모리 검색 실패: %s", e)
        return {"memory": [], "memory_status": {"warmup": True, "breaker": None, "error": str(e)}}
    entries = [
        {
            "id": e["id"],
            "ticker": e["ticker"],
            "decided_at": e["decided_at"],
            "situation_summary": e["situation_summary"],
            "decision": e["decision"],
            "round": e["round"],
            "direction_score": e["direction_score"],
            "avoided_loss": e["avoided_loss"],
            "realized_pnl_pct": e["realized_pnl_pct"],
            "post_fill_return": e.get("post_fill_return"),
            "lesson": (e.get("reflection") or {}).get("lesson") if e.get("reflection") else None,
            "score": e["score"],
        }
        for e in r["entries"]
    ]
    return {"memory": entries, "memory_status": {"warmup": r["warmup"], "breaker": r["breaker"]}}


def calibration_injected() -> bool:
    """`ml.yaml calibration.enabled ∧ inject`(결정 14번). 트레이더 입력에 보정을 넣는 모든
    지점(생성·기존 체크포인트 소비)이 이것을 본다 — 스위치를 끄면 전에 만든 35_calibration 도
    들어가지 않는다."""
    try:
        from trader.config import load_ml

        cfg = load_ml().models.calibration
        return bool(cfg.enabled and cfg.inject)
    except Exception:  # noqa: BLE001 - 설정을 못 읽으면 넣지 않는다
        return False


def ensure_calibration(ctx: ToolContext, *, ticker: str, market: Market, date: date) -> dict | None:
    """결과 엔트리가 min_decisions(60) 이상이고 `calibration.inject` 가 켜졌을 때만
    35_calibration 을 쓴다(있으면 그대로)."""
    from trader.calibration import model as cal
    from trader.config import load_ml
    from trader.memory import vector as vec
    from trader.ml import quant_report

    if ctx.conn is None:
        return None
    d = _dir(ctx, ticker, market, date)
    if not calibration_injected():  # 주입은 L12 개선 뒤 사람이 켠다(결정 14번) — 기존 것도
        return None
    existing = read_checkpoint(d, "35_calibration")
    if existing is not None:
        return existing.get("data")
    try:
        cfg = load_ml().models.calibration
        row = quant_report.latest_row(ticker, market, ctx.state, asof=date)
        qv = vec.situation_vector(row, market) if row else None
        report = cal.build_calibration(
            ctx.conn, min_decisions=cfg.min_decisions, query_vector=qv, asof=ctx.now().isoformat()
        )
    except Exception as e:  # noqa: BLE001 - 보정은 선택 입력
        logger.warning("35_calibration 생략 %s %s: %s", market, ticker, e)
        return None
    if report is None:
        return None
    write_checkpoint(d, "35_calibration", _wrap(report, _call("calibration", None)))
    return report


def ensure_quant(ctx: ToolContext, *, ticker: str, market: Market, date: date) -> dict | None:
    """승격된 퀀트 모델·피처 행이 있으면 05_quant 를 쓴다(이미 있으면 그대로). 없으면 None."""
    from trader.ml import quant_report, registry

    d = _dir(ctx, ticker, market, date)
    existing = read_checkpoint(d, "05_quant")
    if existing is not None:
        return existing.get("data")
    try:
        from trader.config import load_ml

        if not load_ml().models.quant.enabled:
            return None
        # 그 시장 모델 우선·전시장 폴백, 다른 시장 한정 모델은 쓰지 않는다(registry.latest 계약)
        found = registry.latest("quant", state=ctx.state, market=market)
    except Exception as e:  # noqa: BLE001 - ML 은 선택 입력: 어떤 오류도 수집·분석을 막지 않는다
        logger.warning("05_quant 생략(레지스트리 오류): %s", e)
        return None
    if found is None:
        return None
    model, meta = found
    try:
        row = quant_report.latest_row(ticker, market, ctx.state, asof=date)
    except Exception as e:  # noqa: BLE001
        logger.warning("05_quant 생략(피처 읽기 실패) %s %s: %s", market, ticker, e)
        return None
    if row is None:
        logger.info("05_quant 생략 %s %s: 피처 행 없음", market, ticker)
        return None
    try:
        data = quant_report.build(model, meta, row, ticker=ticker, asof=ctx.now().isoformat())
    except Exception as e:  # noqa: BLE001 - 모델 오류는 분석을 막지 않는다
        logger.warning("05_quant 생성 실패 %s %s: %s", market, ticker, e)
        return None
    write_checkpoint(d, "05_quant", _wrap(data, _call("quant", {"model": data["model"]})))
    return data


def _portfolio_context(ctx: ToolContext, market: Market) -> dict | None:
    """계좌별 {mode, date, nav, cash, positions} (positions_snapshot 최신). 원장이 없으면 None."""
    if ctx.conn is None:
        return None
    from trader.control import control
    from trader.ledger import views

    out: dict[str, dict] = {}
    for a in ctx.accounts:
        if market not in a.markets:
            continue
        snap = views.latest_positions(ctx.conn, a.id, market) or {}
        out[a.id] = {
            "env": a.env,
            "mode": control.get_account_mode(ctx.conn, a),
            "date": snap.get("date"),
            "nav": snap.get("nav"),
            "cash": snap.get("cash"),
            "positions": snap.get("positions") or [],
        }
    return out


# 서브에이전트에 넘기는 절은 Claude Code 의 Bash 출력 한도(약 30KB, 초과 시 파일로 치환돼
# LLM 이 못 본다)보다 훨씬 작아야 한다 (실기 확인).
# 지표가 250봉을 요약하므로 원시 봉은 최근 것만 준다.
COMPACT_LIMITS = {
    "daily": 30,
    "minute": 20,
    "orderbook_levels": 5,
    "news_stock": 15,
    "news_macro": 8,
    "news_summary_chars": 120,
    "posts": 20,
    "post_body_chars": 200,
    "post_title_chars": 100,
}


def _section_data(sec: Any) -> Any:
    return sec.get("data") if isinstance(sec, dict) else None


def _with_note(sec: dict, data: Any, note: str) -> dict:
    return {**sec, "data": data, "note": note}


def _trim_news(items: list | None, n: int, chars: int) -> list:
    out = []
    for it in (items or [])[:n]:
        if not isinstance(it, dict):
            continue
        row = {k: v for k, v in it.items() if k != "url"}
        if isinstance(row.get("summary"), str):
            row["summary"] = row["summary"][:chars]
        out.append(row)
    return out


def _compact_section(section: str, body: dict) -> dict:
    """역할별 절을 압축한다. 원본 체크포인트 파일은 바꾸지 않는다."""
    lim = COMPACT_LIMITS
    out = dict(body)
    if section == "technical":
        daily = _section_data(body.get("daily"))
        if isinstance(daily, list) and len(daily) > lim["daily"]:
            out["daily"] = _with_note(
                body["daily"],
                daily[-lim["daily"] :],
                f"최근 {lim['daily']}봉만 (전체 {len(daily)}봉은 indicators 에 반영됨)",
            )
        minute = _section_data(body.get("minute"))
        if isinstance(minute, list) and len(minute) > lim["minute"]:
            out["minute"] = _with_note(
                body["minute"], minute[-lim["minute"] :], f"최근 {lim['minute']}분봉만"
            )
        ob = _section_data(body.get("orderbook"))
        if isinstance(ob, dict):
            n = lim["orderbook_levels"]
            out["orderbook"] = _with_note(
                body["orderbook"],
                {**ob, "asks": (ob.get("asks") or [])[:n], "bids": (ob.get("bids") or [])[:n]},
                f"상위 {n}호가만",
            )
    elif section == "news_macro":
        news = _section_data(body.get("news"))
        if isinstance(news, dict):
            out["news"] = _with_note(
                body["news"],
                {
                    **news,
                    "stock": _trim_news(
                        news.get("stock"), lim["news_stock"], lim["news_summary_chars"]
                    ),
                    "macro": _trim_news(
                        news.get("macro"), lim["news_macro"], lim["news_summary_chars"]
                    ),
                },
                f"종목 {lim['news_stock']}건·거시 {lim['news_macro']}건, "
                f"요약 {lim['news_summary_chars']}자",
            )
    elif section == "sentiment":
        comm = _section_data(body.get("community"))
        if isinstance(comm, dict):
            posts = []
            for p in (comm.get("posts") or [])[: lim["posts"]]:
                if not isinstance(p, dict):
                    continue
                row = {k: v for k, v in p.items() if k != "url"}
                row["title"] = str(row.get("title", ""))[: lim["post_title_chars"]]
                row["body"] = str(row.get("body", ""))[: lim["post_body_chars"]]
                posts.append(row)
            out["community"] = _with_note(
                body["community"], {**comm, "posts": posts}, f"최근 {lim['posts']}건만"
            )
    return out


@_guarded
def checkpoint_read(
    ctx: ToolContext,
    *,
    ticker: str,
    market: Market,
    date: date,
    stage: str,
    section: str | None = None,
) -> dict:
    """절(section)을 지정하면 서브에이전트용으로 압축해 돌려준다. 전체 읽기는 파일 그대로."""
    data = read_checkpoint(_dir(ctx, ticker, market, date), stage)
    if data is None:
        return {"ok": False, "error": f"체크포인트 없음: {stage}"}
    if section is None:
        return {"ok": True, "data": data}
    if stage == "00_inputs" and section == "trader_context":
        dd = _dir(ctx, ticker, market, date)
        q = read_checkpoint(dd, "05_quant")
        c = read_checkpoint(dd, "35_calibration")
        return {
            "ok": True,
            "data": trader_context(
                ctx, data, market, quant=(q or {}).get("data"), calibration=(c or {}).get("data")
            ),
        }
    body = data.get("data", data) if stage != "00_inputs" else data
    if section not in body:
        return {"ok": False, "error": f"절 없음: {section}"}
    if stage != "00_inputs":
        return {"ok": True, "data": {section: body[section]}}
    out = body[section]
    if isinstance(out, dict):
        out = _compact_section(section, out)
        errors = [e for e in data.get("errors", []) if e.startswith(section + ".")]
        out = {**out, "input_errors": errors, "meta": data.get("meta")}
        if section in ("fundamental", "sentiment"):  # md: "함께 오는 quote 는 현재가다"
            out["quote"] = (body.get("technical") or {}).get("quote")
    return {"ok": True, "data": out}


@_guarded
def checkpoint_write(
    ctx: ToolContext,
    *,
    ticker: str,
    market: Market,
    date: date,
    stage: str,
    payload: dict,
    agent: str | None = None,
    call_meta: dict | None = None,
    model: str | None = None,
) -> dict:
    if model:
        call_meta = {**(call_meta or {}), "model": model}
    if stage.startswith("30_decision"):
        return {"ok": False, "error": "30_decision 은 submit-decision 도구로만 제출할 수 있습니다"}
    if not any(p.match(stage) for p in _LLM_WRITABLE):
        return {"ok": False, "error": f"알 수 없는 단계이거나 LLM 이 쓰는 단계가 아닙니다: {stage}"}
    schema = schemas.schema_for(stage)
    if schema is None:
        return {"ok": False, "error": f"알 수 없는 단계이거나 LLM 이 쓰는 단계가 아닙니다: {stage}"}
    try:
        validated = schema.model_validate(_unescape(payload))
    except ValidationError as e:
        msgs = "; ".join(
            f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()
        )
        return {"ok": False, "error": f"스키마 검증 실패({stage}): {msgs}"}
    mismatch = _name_payload_mismatch(ctx, stage, ticker, validated)
    if mismatch:
        return {"ok": False, "error": mismatch}
    path = write_checkpoint(
        _dir(ctx, ticker, market, date),
        stage,
        _wrap(validated.model_dump(), _call(agent, call_meta)),
    )
    return {"ok": True, "path": str(path)}


def _name_payload_mismatch(ctx: ToolContext, stage: str, ticker: str, v: Any) -> str | None:
    m = _ANALYST_RE.match(stage)
    if m:
        if v.role != m.group(1):
            return f"파일명 role({m.group(1)}) 과 페이로드 role({v.role}) 불일치"
        if v.ticker != ticker:
            return f"ticker 불일치: {v.ticker} ≠ {ticker}"
        return None
    m = _DEBATE_RE.match(stage)
    if m:
        rnd, side = int(m.group(1)), m.group(2)
        if v.side != side or v.round != rnd:
            return f"파일명 r{rnd}_{side} 와 페이로드 round={v.round} side={v.side} 불일치"
        if rnd > ctx.settings.debate_rounds:
            return f"라운드 {rnd} 는 설정(debate_rounds={ctx.settings.debate_rounds})을 넘습니다"
    return None


def _load_turns(
    d: Path, pattern: re.Pattern[str] = _DEBATE_RE
) -> dict[int, dict[str, schemas.DebateTurn]]:
    """`<접두>_r<n>_<side>` 토론 턴을 {round: {side: DebateTurn}} 로. 종목(20_debate_*)·시장
    (_market_debate_*)·파생(_fo_debate_*)이 패턴만 다르게 같은 함수를 쓴다(S3 P2 dedup)."""
    turns: dict[int, dict[str, schemas.DebateTurn]] = {}
    for stage in list_checkpoints(d):
        m = pattern.match(stage)
        if not m:
            continue
        data = read_checkpoint(d, stage) or {}
        turns.setdefault(int(m.group(1)), {})[m.group(2)] = schemas.DebateTurn.model_validate(
            data["data"]
        )
    return turns


@_guarded
def checkpoint_summarize(ctx: ToolContext, *, ticker: str, market: Market, date: date) -> dict:
    """20_debate_* 를 모아 25_research_summary 를 만든다 (파이썬이 수렴 감시).

    토론 턴이 하나도 없으면 애널리스트 합의(`summary.analyst_consensus`)를 보고, 합의면 토론 없이
    `skipped_debate: true` 요약을, 아니면 `debate_required: true` 오류를 돌려준다(전체 검토 L17).
    토론 턴이 있으면 합의 여부와 무관하게 정상 요약이다."""
    d = _dir(ctx, ticker, market, date)
    turns = _load_turns(d)
    if not turns:
        return _summarize_without_debate(ctx, d)
    if 0 not in turns or set(turns[0]) != {"bull", "bear"}:
        return {"ok": False, "error": "라운드 0 의 bull·bear 턴이 모두 필요합니다"}
    final_round = max(r for r, sides in turns.items() if set(sides) == {"bull", "bear"})
    evidence_ids = {
        str(e.get("source_id"))
        for r in _reports(d)
        for e in (r.get("evidence") or [])
        if isinstance(e, dict) and e.get("source_id")
    }
    try:
        summ = summary_mod.research_summary(
            turns[0], turns[final_round], rounds=final_round, evidence_ids=evidence_ids
        )
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    path = write_checkpoint(d, "25_research_summary", _wrap(summ, None))
    return {
        "ok": True,
        "path": str(path),
        "sycophantic": summ["sycophantic"],
        "preferred": summ["preferred"],
    }


def _summarize_without_debate(ctx: ToolContext, d: Path) -> dict:
    cfg = ctx.settings.debate
    consensus = None
    if cfg.skip_on_consensus:
        reports = [schemas.AnalystReport.model_validate(r) for r in _reports(d)]
        consensus = summary_mod.analyst_consensus(reports, cfg.consensus_min_conviction)
    if consensus is None:
        return {
            "ok": False,
            "debate_required": True,
            "error": "애널리스트 합의가 아니라 토론이 필요합니다 (라운드 0 의 bull·bear 턴 먼저)",
        }
    summ = summary_mod.skipped_summary(consensus)
    path = write_checkpoint(d, "25_research_summary", _wrap(summ, None))
    return {"ok": True, "path": str(path), "skipped_debate": True, "reason": summ["reason"]}


# --- submit-decision --------------------------------------------------------


# --- bundle: 서브에이전트 입력 묶음 (파이썬이 조립 → 오케스트레이터는 전문 전달만) ---------

BUNDLE_LIMIT_BYTES = 24_000  # Claude Code Bash 출력 한도(30KB, UTF-8 바이트) 아래 (실기 4차)
_TRADER_REPORT_KEYS = (
    "ticker",
    "asof",
    "role",
    "summary",
    "bullets",
    "signal",
    "score",
    "confidence",
    "horizon",
    "key_risks",
    "counter_evidence",
    "data_gaps",
)


def _reports(d: Path) -> list[dict]:
    out = []
    for stage in list_checkpoints(d):
        if _ANALYST_RE.match(stage):
            data = read_checkpoint(d, stage) or {}
            if isinstance(data.get("data"), dict):
                out.append(data["data"])
    return out


def _size(obj: Any) -> int:
    return len(json.dumps(obj, ensure_ascii=False).encode("utf-8"))


def _cut(v: Any, n: int) -> Any:
    if isinstance(v, str):
        return v if len(v) <= n else v[: n - 1] + "…"
    if isinstance(v, list):
        return [_cut(x, n) for x in v]
    if isinstance(v, dict):
        return {k: _cut(x, n) for k, x in v.items()}
    return v


def _trim_lists(v: Any, keep: int) -> Any:
    """`keep` 보다 긴 dict 목록은 마지막 `keep` 건만(시계열은 오름차순이라 꼬리가 최신)."""
    if isinstance(v, list):
        if len(v) > keep and all(isinstance(x, dict) for x in v):
            v = v[-keep:]
        return [_trim_lists(x, keep) for x in v]
    if isinstance(v, dict):
        return {k: _trim_lists(x, keep) for k, x in v.items()}
    return v


def _shrink(obj: dict, limit: int, *, drop_evidence: bool = False) -> dict:
    """UTF-8 바이트 한도를 넘으면 리포트의 evidence → 긴 문자열 순으로 줄인다."""
    if drop_evidence and "reports" in obj:
        obj = {
            **obj,
            "reports": [{k: r[k] for k in _TRADER_REPORT_KEYS if k in r} for r in obj["reports"]],
        }
    if _size(obj) <= limit:
        return obj
    if "reports" in obj and any("evidence" in r for r in obj["reports"]):
        return _shrink(obj, limit, drop_evidence=True)
    # 긴 목록(일봉·분봉 같은 시계열, 오름차순)은 꼬리(최신)만 남긴다 — 문자열을 자르기 전에
    for keep in (120, 60, 30):
        trimmed = _trim_lists(obj, keep)
        if _size(trimmed) <= limit:
            note = f"묶음이 {limit}바이트를 넘어 긴 목록을 최근 {keep}건으로 줄였습니다"
            return {**trimmed, "note": note}
    obj = trimmed
    for n in (300, 200, 140, 100):
        cut = {
            **_cut(obj, n),
            "note": f"묶음이 {limit}바이트를 넘어 긴 문자열을 {n}자에서 잘랐습니다",
        }
        if _size(cut) <= limit:
            return cut
    return cut


def _theme_context(ctx: ToolContext, inputs: dict, ticker: str, market: Market, d: date) -> dict:
    """종목의 오늘 상위 테마·shock 사건 소속(결정 19번). 게이트와 같은 조회(`scan/membership`)를
    입력 기준 시각(`00_inputs.meta.asof`)으로 — 그 뒤 관측은 넣지 않는다."""
    from trader.scan import membership

    raw = ((inputs or {}).get("data") or inputs or {}).get("meta", {}).get("asof")
    try:
        asof = datetime.fromisoformat(str(raw)) if raw else ctx.now()
    except ValueError:
        asof = ctx.now()
    try:
        from trader.config import load_events

        events_on = load_events().enabled
    except Exception:  # noqa: BLE001
        events_on = False
    if ctx.conn is None:
        return {"themes": [], "events": [], "status": None, "observed_at": None}
    res = membership.of(
        ctx.conn,
        market,
        d,
        theme_top_n=ctx.settings.theme.top_n,
        theme_enabled=ctx.settings.theme.enabled,
        events_enabled=events_on,
        asof=asof,
    )
    return membership.for_ticker(res, ticker)


def _holding_context(ctx: ToolContext, ticker: str, market: Market, d: date) -> dict:
    """계좌별 이 종목의 보유 거래일·최소 보유 충족 여부(결정 19번 — 익절가 도달이 최소 보유 전이면
    SELL/TRIM 이 게이트에서 거부될 수 있다). 보유한 계좌만."""
    from trader.ledger import repo
    from trader.orchestrator.executor import _holidays_between
    from trader.risk import rules

    out: dict[str, dict] = {}
    if ctx.conn is None:
        return out
    for a in ctx.accounts:
        if market not in a.markets:
            continue
        since = repo.held_since(ctx.conn, a.id, [ticker]).get(ticker)
        if since is None:
            continue
        days = rules._trading_days_between(since, d, _holidays_between(ctx.holiday_fn, since, d))
        out[a.id] = {
            "held_since": since.isoformat(),
            "held_trading_days": days,
            "min_hold_days": rules.MIN_HOLD_TRADING_DAYS,
            "sell_allowed": days >= rules.MIN_HOLD_TRADING_DAYS,
        }
    return out


EVIDENCE_ROLES = ("technical", "fundamental", "news_macro", "sentiment")
EVIDENCE_DEFINITION = (
    "리포트가 낸 근거 항목 수(evidence)·고유 출처 수(source_id)·data_gaps·counter_evidence·"
    "key_risks 개수와 신호 방향. 파이썬이 센 개수일 뿐 검증된 근거 수가 아니다 — 중복·상충 "
    "근거도 그대로 센다"
)


def evidence_stats(reports: list[dict]) -> dict:
    """트레이더 묶음이 evidence 를 떼기 전에 원본 리포트에서 센 숫자(10-26 검토 L10). 프롬프트의
    '검증 가능한 근거 수' 판단은 이 숫자와 리포트 본문(bullets·summary)을 함께 본다."""
    from trader.orchestrator.summary import signal_direction

    roles: dict[str, dict] = {}
    for r in reports:
        ev = r.get("evidence") or []
        roles[str(r.get("role"))] = {
            "evidence": len(ev),
            "sources": len({e.get("source_id") for e in ev if isinstance(e, dict)}),
            "data_gaps": len(r.get("data_gaps") or []),
            "counter_evidence": len(r.get("counter_evidence") or []),
            "key_risks": len(r.get("key_risks") or []),
            "direction": signal_direction(r.get("signal")),
        }
    directions: dict[str, int] = {}
    for v in roles.values():
        directions[v["direction"]] = directions.get(v["direction"], 0) + 1
    top = max((n for k, n in directions.items() if k != "NEUTRAL"), default=0)
    return {
        "definition": EVIDENCE_DEFINITION,
        "roles": roles,
        "missing_roles": [x for x in EVIDENCE_ROLES if x not in roles],
        "directions": directions,
        # 방향 일치도 = 가장 많은 방향(중립 제외)의 역할 수 ÷ 받은 리포트 수
        "agreement": round(top / len(roles), 4) if roles else 0.0,
    }


def _latest_decision_stage(d: Path) -> str | None:
    """오늘의 최신 결정 단계 (31_decision_r<n> 중 n 최대, 없으면 30_decision, 둘 다 없으면 None)."""
    stages = list_checkpoints(d)
    rounds = [int(m.group(1)) for s in stages if (m := _RESUME_RE.match(s))]
    if rounds:
        return f"31_decision_r{max(rounds)}"
    return "30_decision" if "30_decision" in stages else None


def _brief_turn(t: dict) -> dict:
    """우선순위가 아닌 쪽 라운드의 턴은 핵심만 남긴다 (트레이더 묶음 크기 절감)."""
    return {
        k: t.get(k) for k in ("side", "round", "stance", "valuation_view", "timing", "conviction")
    }


def _trader_summary(summ: dict) -> dict:
    """preferred 쪽 턴은 전문, 반대쪽은 핵심만 (트레이더 md 가 preferred 를 우선한다)."""
    if summ.get("skipped_debate"):
        return dict(summ)  # 토론 생략(합의) 요약은 턴이 없어 줄일 것이 없다
    out = dict(summ)
    other = "final" if summ.get("preferred") == "round0" else "round0"
    if isinstance(summ.get(other), dict):
        out[other] = {
            side: _brief_turn(t) for side, t in summ[other].items() if isinstance(t, dict)
        }
    # issues 는 final 양측 rebuttals 의 복제라 트레이더 묶음에선 뺀다 (실기 4.4KB)
    out.pop("issues", None)
    out["note"] = f"{other} 턴은 핵심만, issues 는 final.rebuttals 와 같아 생략"
    return out


@_guarded
def checkpoint_bundle(
    ctx: ToolContext,
    *,
    ticker: str,
    market: Market,
    date: date,
    kind: str,
    round: int | None = None,
    side: str | None = None,
) -> dict:
    """kind=researcher(round, side) | trader. 서브에이전트 프롬프트에 그대로 붙일 입력 묶음."""
    d = _dir(ctx, ticker, market, date)
    reports = _reports(d)
    if len(reports) < MIN_REPORTS:
        return {"ok": False, "error": f"10_analyst_* 가 {len(reports)}개 (최소 {MIN_REPORTS})"}
    if kind == "researcher":
        if round is None or side not in ("bull", "bear"):
            return {
                "ok": False,
                "error": "researcher 묶음은 --round 와 --side(bull|bear) 가 필요합니다",
            }
        turns = _load_turns(d)
        own = opp = None
        if round > 0:
            prev = turns.get(round - 1, {})
            own = prev.get(side)
            opp = prev.get("bear" if side == "bull" else "bull")
            if own is None or opp is None:
                return {"ok": False, "error": f"라운드 {round - 1} 의 양측 턴이 모두 필요합니다"}
        data = {
            "ticker": ticker,
            "round": round,
            "side": side,
            "reports": reports,
            "own_previous": own.model_dump() if own else None,
            "opponent_previous": opp.model_dump() if opp else None,
        }
        return {"ok": True, "data": _shrink(data, BUNDLE_LIMIT_BYTES)}
    if kind == "trader":
        summ = read_checkpoint(d, "25_research_summary")
        if summ is None:
            return {"ok": False, "error": "25_research_summary 없음 (summarize 먼저)"}
        inputs = read_checkpoint(d, "00_inputs") or {}
        prev_stage = _latest_decision_stage(d)
        prev = (read_checkpoint(d, prev_stage) or {}).get("data") if prev_stage else None
        data = {
            "ticker": ticker,
            "reports": reports,
            "previous_decision": prev,  # 재평가(resume) 때 트레이더가 전제로 삼는 원래·최신 결정
            "previous_decision_stage": prev_stage,
            "research_summary": _trader_summary(summ.get("data", summ)),
            "context": trader_context(
                ctx,
                inputs,
                market,
                quant=(read_checkpoint(d, "05_quant") or {}).get("data"),
                calibration=(read_checkpoint(d, "35_calibration") or {}).get("data"),
            ),
        }
        data["context"]["evidence_stats"] = evidence_stats(reports)  # evidence 를 떼기 전에 센다
        data["context"]["theme"] = _theme_context(ctx, inputs, ticker, market, date)
        data["context"]["holding"] = _holding_context(ctx, ticker, market, date)
        return {"ok": True, "data": _shrink(data, BUNDLE_LIMIT_BYTES, drop_evidence=True)}
    return {"ok": False, "error": f"알 수 없는 kind: {kind} (researcher|trader)"}


@_guarded
def submit_decision(
    ctx: ToolContext,
    *,
    ticker: str,
    market: Market,
    date: date,
    payload: dict,
    agent: str = "trader",
    call_meta: dict | None = None,
    stage: str | None = None,
) -> dict:
    """결정 제출. stage 가 31_decision_r<n> 이면 트리거 재평가(30_decision 이 있어야 함)."""
    d = _dir(ctx, ticker, market, date)
    stages = list_checkpoints(d)
    resume_stage = None
    if stage is not None and stage != "30_decision":
        if not _RESUME_RE.match(stage):
            return {"ok": False, "error": f"stage 는 30_decision 또는 31_decision_r<n>: {stage!r}"}
        if "30_decision" not in stages:
            return {"ok": False, "error": "재평가는 30_decision 이 있어야 합니다"}
        if stage in stages:
            return {"ok": False, "error": f"{stage} 이 이미 있습니다 (다음 번호를 쓰세요)"}
        resume_stage = stage
    if ctx.conn is None:  # 30_decision 을 쓰기 전에 거부해야 재시도가 멱등 분기에 걸리지 않는다
        return {"ok": False, "error": "원장 연결이 없어 계좌별 실행을 할 수 없음"}
    if resume_stage is None and "30_decision" in stages:
        # 같은 날 재제출(세션 재시도·재실행)은 기존 결정을 돌려주고 실행을 반복하지 않는다 (멱등).
        # 단, 판정(40_risk_check_*)이 하나도 없으면 저장 뒤 execute 가 끊긴 것이므로 실행을 마친다
        # (executor 는 살아 있는 주문·승인으로 멱등, memory_entries 는 (decision_id, round) UNIQUE).
        existing = read_checkpoint(d, "30_decision") or {}
        out = {
            "ok": True,
            "already": True,
            "path": str(d / "30_decision.json"),
            "action": (existing.get("data") or {}).get("action"),
            "message": "30_decision 이 이미 있어 기존 결정을 유지함",
        }
        # dry-run(`trader analyze --dry-run`·FAKE·웹 dry_run 요청)이 남긴 40_ 는 실행이 아니다 —
        # 그대로 두면 아침 dry-run 뒤 데몬의 실전 세션이 "이미 실행" 으로 보고 그날 주문을
        # 내지 않는다. 지금도 dry-run 이면 dry-run 판정으로 충분하다(반복하지 않음)
        executed = {
            s[len("40_risk_check_") :]
            for s in stages
            if s.startswith("40_risk_check_")
            and _account_finished(ctx, d, s, stages, date, market, ticker)
        }
        wanted = {a.id for a in ctx.accounts if market in a.markets}
        if executed and not (wanted - executed):
            return out
        try:
            decision = schemas.Decision.model_validate(existing.get("data") or {})
        except ValidationError as e:
            n = e.error_count()
            return out | {"message": f"기존 30_decision 을 해석할 수 없어 실행 생략: 오류 {n}건"}
        inputs = read_checkpoint(d, "00_inputs") or {}
        price = _quote_price(inputs)
        if price is None:
            return out | {"message": "00_inputs 에 현재가가 없어 실행을 재개하지 못함"}
        price, requote, change_pct = _requote(ctx, inputs, market, ticker, price)
        if decision.stop_loss is not None and Decimal(str(decision.stop_loss)) >= price:
            return out | {
                "message": f"손절선 {decision.stop_loss} 이 재시세 {price} 이상이라 실행 생략"
            }
        _ingest_run(ctx, d, date, market, ticker)
        # 판정이 이미 있는 계좌는 다시 돌리지 않는다(한 계좌의 예외로 끊긴 뒤 나머지만 재개)
        executions, memory_entry_id = _execute_and_record(
            ctx,
            d,
            date,
            market,
            decision,
            inputs,
            price,
            skip_accounts=frozenset(executed),
            extra_note=requote,
            change_pct=change_pct,
        )
        logger.warning("미실행 결정의 실행 재개 %s %s: %s", market, ticker, decision.action)
        return out | {
            "resumed_execution": True,
            "message": "30_decision 은 있었지만 판정이 없어 실행을 재개함",
            "executions": executions,
            "memory_entry_id": memory_entry_id,
        }
    reports = [s for s in stages if _ANALYST_RE.match(s)]
    missing: list[str] = []
    if len(reports) < MIN_REPORTS:
        missing.append(f"10_analyst_* {len(reports)}개 (최소 {MIN_REPORTS})")
    if "25_research_summary" not in stages:
        missing.append("25_research_summary 없음")
    if missing:
        return {"ok": False, "error": "전제조건 미충족: " + ", ".join(missing)}
    try:
        decision = schemas.Decision.model_validate(_unescape(payload))
    except ValidationError as e:
        msgs = "; ".join(
            f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()
        )
        return {"ok": False, "error": f"스키마 검증 실패(30_decision): {msgs}"}
    if decision.ticker != ticker:
        return {"ok": False, "error": f"ticker 불일치: {decision.ticker} ≠ {ticker}"}
    inputs = read_checkpoint(d, "00_inputs") or {}
    price = _quote_price(inputs)
    requote: str | None = None
    change_pct = _input_change_pct(inputs)
    if resume_stage:
        # 재평가의 현재가는 아침 시세가 아니라 장중 트리거 가격이다 (트레이더 md 의 "현재가")
        trig = repo.latest_trigger_price(ctx.conn, market, ticker, date)
        if trig is not None:
            price = trig
            # 트리거 가격의 당일 등락률 = 트리거 가격 ÷ 직전 완성 봉 종가 − 1 (2-O6) — None 으로
            # 두면 장중 급등을 쫓는 BUY 가 추격 금지를 지난다
            change_pct = _trigger_change_pct(inputs, trig, date)
        elif price is not None:  # 뉴스 재평가(가격 없는 트리거)는 재수집 입력 + 재시세
            price, requote, change_pct = _requote(ctx, inputs, market, ticker, price)
    elif price is not None:
        price, requote, change_pct = _requote(ctx, inputs, market, ticker, price)
    if price is None:
        return {
            "ok": False,
            "error": "00_inputs 의 technical.quote 에 현재가가 없어 판정할 수 없음",
        }
    if decision.stop_loss is not None and Decimal(str(decision.stop_loss)) >= price:
        # 게이트는 BUY/ADD 주문만 막지만 저장된 결정의 손절선은 monitor 가 모든 action 에 대해
        # 읽어 즉시 전량 매도하므로, 저장 전에 action 과 무관하게 거부한다
        return {
            "ok": False,
            "error": (
                f"손절선 {decision.stop_loss} 이 현재가 {price} 이상: 모니터가 즉시 손절 매도함. "
                "현재가 아래로 다시 제출하거나 stop_loss 를 비우세요"
            ),
        }
    stage_name = resume_stage or "30_decision"
    path = write_checkpoint(d, stage_name, _wrap(decision.model_dump(), _call(agent, call_meta)))
    _ingest_run(ctx, d, date, market, ticker)
    if ctx.conn is not None:  # 수집이 알아낸 종목명·US 거래소를 원장에 남긴다 (표기·만기 스냅샷용)
        meta = inputs.get("meta") or {}
        repo.upsert_ticker_name(ctx.conn, market, ticker, meta.get("name"), "inputs")
        repo.upsert_ticker_exchange(ctx.conn, market, ticker, meta.get("exchange"))
    if resume_stage:  # 재평가 결정이 최신 결정이 되도록 마지막에 적재
        repo.ingest_checkpoint(
            ctx.conn, date, market, ticker, resume_stage, read_checkpoint(d, resume_stage) or {}
        )
    m_round = _RESUME_RE.match(resume_stage) if resume_stage else None
    # 합의로 토론을 생략했는지(L17)를 원장 결정·메모리 행에 남긴다(2-L23: 전후 비교용)
    summ = read_checkpoint(d, "25_research_summary") or {}
    skipped = bool((summ.get("data") or summ).get("skipped_debate"))
    if ctx.conn is not None and (did := repo.decision_id(ctx.conn, date, market, ticker)):
        repo.set_skipped_debate(ctx.conn, did, skipped)
    executions, memory_entry_id = _execute_and_record(
        ctx,
        d,
        date,
        market,
        decision,
        inputs,
        price,
        resume_round=int(m_round.group(1)) if m_round else None,
        extra_note=requote,
        change_pct=change_pct,
        skipped_debate=skipped,
    )
    logger.info("결정 저장 %s %s: %s (계좌 %d)", market, ticker, decision.action, len(executions))
    return {
        "ok": True,
        "path": str(path),
        "stage": stage_name,
        "action": decision.action,
        "price": str(price),
        "requote": requote,
        "dry_run": ctx.dry_run,
        "executions": executions,
        "memory_entry_id": memory_entry_id,
    }


def _execute_and_record(
    ctx: ToolContext,
    d: Path,
    date: date,
    market: Market,
    decision: schemas.Decision,
    inputs: dict,
    price: Decimal,
    *,
    resume_round: int | None = None,
    skip_accounts: frozenset[str] = frozenset(),
    extra_note: str | None = None,
    change_pct: float | None = None,
    skipped_debate: bool | None = None,
) -> tuple[list[dict], int | None]:
    """리스크 게이트·계좌별 실행(주문은 executor 안에서만) + 메모리 엔트리.

    결정은 1회, 판정·주문은 계좌마다. 재평가 라운드는 41_/51_ 체크포인트 이름에 들어간다."""
    from trader.orchestrator import executor
    from trader.orchestrator import session as session_mod

    assert ctx.conn is not None
    ectx = executor.ExecContext(
        settings=ctx.settings,
        accounts=ctx.accounts,
        conn=ctx.conn,
        brokers=ctx.brokers,
        notifier=ctx.notifier or NullNotifier(),
        state=ctx.state,
        now=ctx.now,
        holiday_fn=ctx.holiday_fn,
        rl_sizer=_load_sizer(ctx.state),
        record_portfolio=not session_mod.is_fake(),  # 가짜 브로커 잔고는 기록하지 않는다(2-O7)
    )
    # 메모리의 decided_at 은 실행 직전 시각 — 이 라운드의 주문이 그 뒤에 생겨 체결가 채점 창이 된다
    decided_at = _decided_at(ctx.now(), date, market)
    executions = executor.execute(
        ectx,
        decision,
        date=date,
        market=market,
        price=price,
        exchange=(inputs.get("meta") or {}).get("exchange"),
        dry_run=ctx.dry_run,
        resume_round=resume_round,
        skip_accounts=skip_accounts,
        extra_note=extra_note,
        change_pct=change_pct,
        trade_block=_trade_block(inputs),
    )
    memory_entry_id = _record_memory(
        ctx,
        decision,
        market,
        date,
        round_no=resume_round or 0,
        decided_at=decided_at,
        skipped_debate=skipped_debate,
    )
    return executions, memory_entry_id


def _load_sizer(state: Path | None):
    """ml.yaml rl_sizer.enabled ∧ 승격 모델일 때만 Sizer. 어떤 실패도 결정 제출을 막지 않는다."""
    try:
        from trader.config import load_ml
        from trader.ml.rl import shadow

        return shadow.load(load_ml(), state)
    except Exception as e:  # noqa: BLE001
        logger.warning("RL 사이저 로드 건너뜀: %s", e)
        return None


def _decided_at(now: datetime, run_date: date, market: Market = "KR") -> datetime:
    """운영일(run date)이 지금의 운영일이면 현재 시각, 아니면 그날 장 마감(15:30 KST) 시각(UTC).

    운영일은 시장 기준(`run_date_for`) — US 는 KST 자정을 넘긴 장중 재평가도 같은 운영일이라
    현재 시각을 써야 라운드 순서(체결 귀속 창)가 뒤집히지 않는다."""
    from trader.orchestrator.fills import MARKET_CLOSE
    from trader.orchestrator.run_store import run_date_for

    if run_date_for(market, now) == run_date:
        return now
    return datetime.combine(run_date, MARKET_CLOSE["KR"], KST).astimezone(UTC)


def _record_memory(
    ctx: ToolContext,
    decision: schemas.Decision,
    market: Market,
    date: date,
    *,
    round_no: int = 0,
    decided_at: datetime | None = None,
    skipped_debate: bool | None = None,
) -> int | None:
    """결정 1건(라운드별)을 메모리 엔트리로 기록한다 ((decision_id, round) 멱등).

    결정 시점 보유는 방금 판정이 남긴 잔고 스냅샷(주문 전)으로 정한다. 실패해도 결정 제출을 막지
    않는다."""
    if ctx.conn is None:
        return None
    try:
        from trader.memory import store
        from trader.ml import quant_report

        decision_id = repo.decision_id(ctx.conn, date, market, decision.ticker)
        if decision_id is None:
            return None
        paper = next(
            (a.id for a in ctx.accounts if a.env == "paper"),
            ctx.accounts[0].id if ctx.accounts else "",
        )
        row = quant_report.latest_row(decision.ticker, market, ctx.state, asof=date)
        held = store.held_at(
            ctx.conn, account_id=paper, market=market, ticker=decision.ticker, d=date.isoformat()
        )
        return store.record_decision(
            ctx.conn,
            decision_id=decision_id,
            ticker=decision.ticker,
            market=market,
            account_id=paper,
            decided_at=(decided_at or _decided_at(ctx.now(), date, market)).isoformat(),
            decision=decision.model_dump(),
            row=row,
            horizon_days=decision.horizon_days,
            round_no=round_no,
            held_at_decision=held,
            skipped_debate=skipped_debate,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("메모리 기록 실패 %s %s: %s", market, decision.ticker, e)
        return None


REQUOTE_AFTER_S = 600  # 입력 수집 뒤 이만큼 지났으면 제출 때 현재가를 다시 받는다(Phase 21)


def _trigger_change_pct(inputs: dict, price: Decimal, run_date: date) -> float | None:
    """트리거 가격의 당일 등락률(소수): `00_inputs.technical.daily` 에서 운영일 전 마지막 일봉
    종가 대비. 직전 종가를 모르면 None — 아침 시세의 등락률로 트리거 가격을 판정하지 않는다
    (PR #26 리뷰: 아침 +12% 가 장중 되돌린 가격의 재평가를 막지 않게)."""
    try:
        rows = inputs["technical"]["daily"]["data"] or []
        prev = [r for r in rows if str(r.get("date") or "") < run_date.isoformat()]
        close = Decimal(str(prev[-1]["close"]))
        if close > 0:
            return float(price / close - 1)
    except (KeyError, TypeError, ValueError, IndexError, InvalidOperation, AttributeError):
        pass
    return None


def _input_change_pct(inputs: dict) -> float | None:
    """00_inputs 시세의 당일 등락률(소수). 없으면 None."""
    try:
        v = inputs["technical"]["quote"]["data"].get("change_pct")
        return float(v) if v is not None else None
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def _account_finished(
    ctx: ToolContext,
    d: Path,
    risk_stage: str,
    stages: list[str],
    day: date,
    market: str,
    ticker: str,
) -> bool:
    """재개 판단: 이 계좌의 실행이 끝났는가. 판정(40_)만 있고 **통과했는데 주문·승인 흔적이 없으면**
    (판정 저장 뒤 주문 전에 끊김) 끝나지 않은 것으로 본다 — 예전엔 40_ 만 보고 그날 주문을 영구
    생략했다(전체 검토 O9). 재실행은 executor 의 살아 있는 주문·승인 중복 방지가 지킨다.
    dry-run 판정은 지금 실전이면 실행이 아니다(Phase 21)."""
    v = (read_checkpoint(d, risk_stage) or {}).get("data") or {}
    if v.get("dry_run") and not ctx.dry_run:
        return False
    if not v.get("approved") or v.get("dry_run") or v.get("mode") == "off":
        return True
    acct = risk_stage[len("40_risk_check_") :]
    if f"50_order_{acct}" in stages:
        return True
    if v.get("mode") == "approve" and ctx.conn is not None:
        did = repo.decision_id(ctx.conn, day, market, ticker)
        row = ctx.conn.execute(
            "SELECT 1 FROM approvals WHERE decision_id=? AND account_id=? LIMIT 1", (did, acct)
        ).fetchone()
        return row is not None
    return False


def _requote(
    ctx: ToolContext, inputs: dict, market: Market, ticker: str, price: Decimal
) -> tuple[Decimal, str | None, float | None]:
    """제출 시 재시세: `00_inputs.meta.asof` 가 REQUOTE_AFTER_S 넘게 늙었고 시세 클라이언트가 있으면
    현재가를 다시 받아 게이트(수량·손절 검사)에 쓴다. 실패·0·변동 없음은 입력가 유지. 둘째 값은
    판정 note 에 남길 문구("재시세 71500→71800"), 셋째 값은 당일 등락률(소수, 재시세 값 우선 —
    게이트의 급등 추격 금지, Phase 25)."""
    base_pct = _input_change_pct(inputs)
    if ctx.market_client is None:
        return price, None, base_pct
    asof_raw = (inputs.get("meta") or {}).get("asof")
    try:
        asof = datetime.fromisoformat(str(asof_raw))
        if asof.tzinfo is None:
            asof = asof.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return price, None, base_pct
    if (ctx.now() - asof).total_seconds() < REQUOTE_AFTER_S:
        return price, None, base_pct
    try:
        if market == "KR":
            q = ctx.market_client.get_price_kr(ticker)
        else:
            exch = (inputs.get("meta") or {}).get("exchange") or "NAS"
            q = ctx.market_client.get_price_us(ticker, exch)
        fresh = Decimal(str(getattr(q, "price", None) or 0))
        fresh_pct = getattr(q, "change_pct", None)
    except Exception as e:  # noqa: BLE001 - 재시세 실패는 입력가로 간다
        logger.warning("재시세 실패 %s %s: %s", market, ticker, e)
        return price, None, base_pct
    pct = float(fresh_pct) if fresh_pct is not None else base_pct
    if fresh <= 0 or fresh == price:
        return price, None, pct if fresh > 0 else base_pct
    note = f"재시세 {price}→{fresh}"
    logger.info("%s %s %s (입력 %s)", market, ticker, note, asof_raw)
    return fresh, note, pct


def _trade_block(inputs: dict) -> str | None:
    """00_inputs 시세의 종목상태·시장경고·임시정지로 본 매매 제한 사유(Stock D-030)."""
    from trader.risk import tradability

    try:
        return tradability.block_reason(inputs["technical"]["quote"]["data"])
    except (KeyError, TypeError):
        return None


def _quote_price(inputs: dict) -> Decimal | None:
    try:
        raw = inputs["technical"]["quote"]["data"]["price"]
        return Decimal(str(raw))
    except (KeyError, TypeError, InvalidOperation):
        return None


def _ingest_run(ctx: ToolContext, d: Path, date: date, market: Market, ticker: str) -> None:
    """리포트·토론·결정 체크포인트를 원장 테이블에 멱등 적재한다 (메트릭·UI 용)."""
    if ctx.conn is None:
        return
    for stage in list_checkpoints(d):
        cp = read_checkpoint(d, stage)
        if isinstance(cp, dict):
            repo.ingest_checkpoint(ctx.conn, date, market, ticker, stage, cp)


# --- 시장 세션 (Phase 18): 시장 단위 체크포인트 `_market_*` ---------------------------

MARKET_PROXY_LABEL = {"KR": "KOSPI200", "US": "SPX"}
MARKET_VIEW_FLAG = "market_view:"
_MARKET_DEBATE_RE = re.compile(r"^_market_debate_r(\d+)_(bull|bear)$")
_MARKET_WRITABLE = ("_market_analyst", "_market_debate_r<n>_<bull|bear>")


def _market_turns(d: Path) -> dict[int, dict[str, schemas.DebateTurn]]:
    return _load_turns(d, _MARKET_DEBATE_RE)


def _validate_payload(model: type, payload: dict) -> tuple[Any, dict | None]:
    """pydantic 검증(HTML 엔티티 되돌림 포함). 실패면 (None, {"ok": False, "error"})."""
    try:
        return model.model_validate(_unescape(payload)), None
    except ValidationError as e:
        msgs = "; ".join(
            f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()
        )
        return None, {"ok": False, "error": f"스키마 검증 실패({model.__name__}): {msgs}"}


def _summarize_turns(d: Path, out_stage: str, pattern: re.Pattern[str]) -> dict:
    """토론 턴 → 연구 요약(수렴 감시 포함) 체크포인트. 시장·파생 세션 공용."""
    turns = _load_turns(d, pattern)
    if 0 not in turns or set(turns[0]) != {"bull", "bear"}:
        return {"ok": False, "error": "라운드 0 의 bull·bear 턴이 모두 필요합니다"}
    final_round = max(r for r, sides in turns.items() if set(sides) == {"bull", "bear"})
    try:
        summ = summary_mod.research_summary(turns[0], turns[final_round], rounds=final_round)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    path = write_checkpoint(d, out_stage, _wrap(summ, None))
    return {
        "ok": True, "path": str(path), "rounds": final_round,
        "sycophantic": summ["sycophantic"], "preferred": summ["preferred"],
    }  # fmt: skip


def _market_report(d: Path) -> dict | None:
    cp = read_checkpoint(d, "_market_analyst")
    return cp.get("data") if isinstance(cp, dict) and isinstance(cp.get("data"), dict) else None


def _market_hedge_context(ctx: ToolContext, market: Market, inputs: dict) -> dict:
    h = ctx.settings.hedge
    return {
        "max_weight_pct": h.max_weight_pct,
        "min_weight_pct": h.min_weight_pct,
        "leverage": h.leverage,
        "instruments": {k: v.model_dump() for k, v in h.instruments.get(market, {}).items()},  # type: ignore[arg-type]
        "held": (inputs.get("hedge") or {}).get("held", []),
        "state": (inputs.get("regime") or {}).get("state"),
    }


def market_bundle(
    ctx: ToolContext,
    *,
    market: Market,
    date: date,
    kind: str,
    round: int | None = None,
    side: str | None = None,
) -> dict:
    """시장 세션 서브에이전트 입력 묶음. kind=analyst(`_market_inputs`) |
    researcher(round, side) | strategist(리포트 + 요약 + 국면·헤지·섹터 컨텍스트).
    종목 경로(`@_guarded`)와 분리된 market_dir."""
    if market not in ("KR", "US"):
        return {"ok": False, "error": f"market 은 KR 또는 US: {market!r}"}
    d = market_dir(date, market, ctx.state)
    inputs = (read_checkpoint(d, "_market_inputs") or {}).get("data") or {}
    if not inputs:
        return {"ok": False, "error": "_market_inputs 없음 (파이썬이 세션 앞에서 만든다)"}
    base = {"scope": "market", "market": market, "date": date.isoformat()}
    if kind == "analyst":
        return {"ok": True, "data": _shrink({**base, **inputs}, BUNDLE_LIMIT_BYTES)}
    report = _market_report(d)
    if report is None:
        return {"ok": False, "error": "_market_analyst 없음 (거시 애널리스트 먼저)"}
    if kind == "researcher":
        if round is None or side not in ("bull", "bear"):
            return {
                "ok": False,
                "error": "researcher 묶음은 --round 와 --side(bull|bear) 가 필요합니다",
            }
        turns = _market_turns(d)
        own = opp = None
        if round > 0:
            prev = turns.get(round - 1, {})
            own = prev.get(side)
            opp = prev.get("bear" if side == "bull" else "bull")
            if own is None or opp is None:
                return {"ok": False, "error": f"라운드 {round - 1} 의 양측 턴이 모두 필요합니다"}
        data = {
            **base,
            "proxy": inputs.get("proxy"),
            "round": round,
            "side": side,
            "reports": [report],
            "regime": inputs.get("regime"),
            "own_previous": own.model_dump() if own else None,
            "opponent_previous": opp.model_dump() if opp else None,
        }
        return {"ok": True, "data": _shrink(data, BUNDLE_LIMIT_BYTES)}
    if kind == "strategist":
        summ = read_checkpoint(d, "_market_summary")
        if summ is None:
            return {"ok": False, "error": "_market_summary 없음 (market summarize 먼저)"}
        data = {
            **base,
            "proxy": inputs.get("proxy"),
            "report": report,
            "research_summary": _trader_summary(summ.get("data", summ)),
            "regime": inputs.get("regime"),
            "hedge": _market_hedge_context(ctx, market, inputs),
            "sector_weights": inputs.get("sector_weights") or [],
        }
        return {"ok": True, "data": _shrink(data, BUNDLE_LIMIT_BYTES)}
    return {"ok": False, "error": f"알 수 없는 kind: {kind} (analyst|researcher|strategist)"}


def market_write(
    ctx: ToolContext,
    *,
    market: Market,
    date: date,
    stage: str,
    payload: dict,
    agent: str | None = None,
    call_meta: dict | None = None,
) -> dict:
    """LLM 쓰기 가능 시장 단계: `_market_analyst`(AnalystReport),
    `_market_debate_r<n>_<side>`(DebateTurn)."""
    if market not in ("KR", "US"):
        return {"ok": False, "error": f"market 은 KR 또는 US: {market!r}"}
    m = _MARKET_DEBATE_RE.match(stage)
    if stage == "_market_analyst":
        model: type = schemas.AnalystReport
    elif m:
        model = schemas.DebateTurn
        if int(m.group(1)) > ctx.settings.debate_rounds:
            return {
                "ok": False,
                "error": (
                    f"라운드 {m.group(1)} 는 설정(debate_rounds="
                    f"{ctx.settings.debate_rounds})을 넘습니다"
                ),
            }
    else:
        return {
            "ok": False,
            "error": f"쓸 수 없는 단계: {stage} (허용: {', '.join(_MARKET_WRITABLE)})",
        }
    v, err = _validate_payload(model, payload)
    if err:
        return err
    if m and (v.round != int(m.group(1)) or v.side != m.group(2)):
        return {
            "ok": False,
            "error": (
                f"파일명 r{m.group(1)}_{m.group(2)} 와 페이로드 "
                f"round={v.round} side={v.side} 불일치"
            ),
        }
    d = market_dir(date, market, ctx.state, create=True)
    if read_checkpoint(d, "_market_inputs") is None:
        return {"ok": False, "error": "_market_inputs 없음 (시장 세션 밖에서는 쓸 수 없다)"}
    path = write_checkpoint(d, stage, _wrap(v.model_dump(), _call(agent, call_meta)))
    return {"ok": True, "path": str(path), "stage": stage}


def market_summarize(ctx: ToolContext, *, market: Market, date: date) -> dict:
    """`_market_debate_*` 를 모아 `_market_summary`(수렴 감시 포함)를 만든다."""
    if market not in ("KR", "US"):
        return {"ok": False, "error": f"market 은 KR 또는 US: {market!r}"}
    d = market_dir(date, market, ctx.state)
    return _summarize_turns(d, "_market_summary", _MARKET_DEBATE_RE)


def submit_market_view(
    ctx: ToolContext,
    *,
    market: Market,
    date: date,
    payload: dict,
    agent: str = "strategist",
    call_meta: dict | None = None,
) -> dict:
    """HedgeProposal 검증 → `_market_view.json` + `control.market_view:<market>`(오늘 날짜 포함).
    전제: `_market_analyst` 와 `_market_summary` 가 있어야 한다(순서 강제)."""
    from trader.control import control

    if market not in ("KR", "US"):
        return {"ok": False, "error": f"market 은 KR 또는 US: {market!r}"}
    d = market_dir(date, market, ctx.state)
    if _market_report(d) is None or read_checkpoint(d, "_market_summary") is None:
        return {
            "ok": False,
            "error": "_market_analyst 와 _market_summary 가 있어야 제출할 수 있습니다",
        }
    try:
        view = schemas.HedgeProposal.model_validate(_unescape(payload))
    except ValidationError as e:
        msgs = "; ".join(
            f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()
        )
        return {"ok": False, "error": f"스키마 검증 실패(HedgeProposal): {msgs}"}
    if view.market != market:
        return {
            "ok": False,
            "error": f"payload.market={view.market} 이 --market {market} 과 다릅니다",
        }
    call = _call(agent, call_meta)
    path = write_checkpoint(d, "_market_view", _wrap(view.model_dump(), call))
    if ctx.conn is not None:
        from trader.orchestrator import market_view as mv

        control.set_flag(
            ctx.conn,
            f"{MARKET_VIEW_FLAG}{market}",
            {"date": date.isoformat(), **view.model_dump(), "call": call},
        )
        mv.record_view(ctx.conn, today=date, market=market, view=view.model_dump())
    logger.info("시장 뷰 저장 %s: %s %.0f%%", market, view.stance, view.hedge_weight * 100)
    return {"ok": True, "path": str(path), "stance": view.stance, "hedge_weight": view.hedge_weight}


def market_view_today(conn, market: str, today: date) -> dict | None:
    """오늘 제출된 시장 뷰(없거나 어제 것이면 None). hedge.rebalance·trader_context 가 쓴다."""
    from trader.control import control

    if conn is None:
        return None
    v = control.get_flag(conn, f"{MARKET_VIEW_FLAG}{market}")
    if not v or v.get("date") != today.isoformat():
        return None
    return v


# --- submit-screen (Phase 6 scan) ---------------------------------------------


def submit_screen(
    ctx: ToolContext,
    *,
    market: Market,
    date: date,
    payload: dict,
    agent: str = "screener",
    call_meta: dict | None = None,
) -> dict:
    """Screened 검증 후 state/runs/<date>/<market>/_screen.json 저장."""
    if market not in ("KR", "US"):
        return {"ok": False, "error": f"market 은 KR 또는 US: {market!r}"}
    try:
        screened = schemas.Screened.model_validate(_unescape(payload))
    except ValidationError as e:
        msgs = "; ".join(
            f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()
        )
        return {"ok": False, "error": f"스키마 검증 실패(Screened): {msgs}"}
    d = market_dir(date, market, ctx.state, create=True)
    path = write_checkpoint(d, "_screen", _wrap(screened.model_dump(), _call(agent, call_meta)))
    logger.info("스크리닝 저장 %s: %s", market, [p.ticker for p in screened.picks])
    return {"ok": True, "path": str(path), "picks": [p.ticker for p in screened.picks]}


# --- 뉴스 발굴 (Phase 19 scout) --------------------------------------------------


def discovery_inputs(ctx: ToolContext, *, market: Market, date: date) -> dict:
    """scout 입력(`_discovery_inputs`, 파이썬이 세션 앞에서 만든다). 없으면 ok=false → 발굴 생략."""
    if market not in ("KR", "US"):
        return {"ok": False, "error": f"market 은 KR 또는 US: {market!r}"}
    d = market_dir(date, market, ctx.state)
    data = (read_checkpoint(d, "_discovery_inputs") or {}).get("data") or {}
    if not data:
        return {"ok": False, "error": "_discovery_inputs 없음 (발굴 단계 생략)"}
    return {"ok": True, "data": _shrink(data, BUNDLE_LIMIT_BYTES)}


def submit_discovery(
    ctx: ToolContext,
    *,
    market: Market,
    date: date,
    payload: dict,
    agent: str = "scout",
    call_meta: dict | None = None,
) -> dict:
    """Discovered 검증 → 파이썬이 코드·거래소·제외 확정(`discovery.resolve`) → `_discovery` 저장 →
    정량 후보와 병합한 screener 입력(`candidates`) 반환."""
    from trader import names
    from trader.config import Universe, load_universe
    from trader.scan import discovery

    if market not in ("KR", "US"):
        return {"ok": False, "error": f"market 은 KR 또는 US: {market!r}"}
    try:
        found = schemas.Discovered.model_validate(_unescape(payload))
    except ValidationError as e:
        msgs = "; ".join(
            f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()
        )
        return {"ok": False, "error": f"스키마 검증 실패(Discovered): {msgs}"}
    d = market_dir(date, market, ctx.state, create=True)
    inputs = (read_checkpoint(d, "_discovery_inputs") or {}).get("data") or {}
    try:
        universe = load_universe()
    except Exception:  # noqa: BLE001 - 설정이 없으면 유니버스 비움(전부 listed 로 본다)
        universe = Universe()
    cache_dir = (ctx.state or paths.state_dir()) / "cache"
    cache_names = names.load_cache_names(cache_dir)
    exclude = ctx.settings.excluded_tickers(market)
    exclude |= set(inputs.get("held") or [])
    resolved = discovery.resolve(
        found.model_dump(),
        market=market,
        universe=universe,
        cache_names=cache_names,
        market_client=ctx.market_client,
        exclude=exclude,
        event_watch=list(inputs.get("event_watch") or []),
        max_event=int(inputs.get("max_event_picks") or 0),
        max_per_event=int(inputs.get("max_picks_per_event") or 2),
    )
    path = write_checkpoint(d, "_discovery", _wrap(resolved, _call(agent, call_meta)))
    if inputs.get("intraday_round"):  # 장중 발굴(Phase 23)은 회차 사본도 남긴다
        write_checkpoint(
            d,
            f"_discovery_r{int(inputs['intraday_round'])}",
            _wrap(resolved, _call(agent, call_meta)),
        )
    merged = discovery.merge(
        list(inputs.get("quant_candidates") or []),
        resolved,
        max_event=int(inputs.get("max_event_picks") or 0),
    )
    logger.info("발굴 저장 %s: %s", market, discovery.line(resolved))
    return {
        "ok": True,
        "path": str(path),
        "candidates": merged,
        "discovered": [c["ticker"] for c in resolved["candidates"]],
        "unresolved": resolved["unresolved"],
    }


# --- submit-reflection (Phase 8 reflect) ----------------------------------------


def submit_reflection(
    ctx: ToolContext,
    *,
    market: Market,
    date: date,
    payload: list | dict,
    agent: str = "reflector",
    call_meta: dict | None = None,
) -> dict:
    """Reflection 배열을 검증해 메모리에 저장하고 runs/<date>/<market>/_reflect.json 에 덧붙인다."""
    from trader.memory import store

    if ctx.conn is None:
        return {"ok": False, "error": "원장 연결 없음"}
    if market not in ("KR", "US"):
        return {"ok": False, "error": f"market 은 KR 또는 US: {market!r}"}
    items = payload if isinstance(payload, list) else [payload]
    parsed: list[schemas.Reflection] = []
    for i, item in enumerate(items):
        try:
            parsed.append(schemas.Reflection.model_validate(_unescape(item)))
        except ValidationError as e:
            msgs = "; ".join(
                f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()
            )
            return {"ok": False, "error": f"스키마 검증 실패(Reflection[{i}]): {msgs}"}
    for r in parsed:
        if store.get_entry(ctx.conn, r.entry_id) is None:
            return {"ok": False, "error": f"메모리 엔트리 없음: {r.entry_id}"}
    for r in parsed:
        store.set_reflection(ctx.conn, r.entry_id, r.model_dump())
    d = market_dir(date, market, ctx.state, create=True)
    existing = read_checkpoint(d, "_reflect") or _wrap([], _call(agent, call_meta))
    data = list(existing.get("data") or []) + [r.model_dump() for r in parsed]
    write_checkpoint(d, "_reflect", _wrap(data, _call(agent, call_meta)))
    logger.info("반성 저장 %s: %d건", market, len(parsed))
    return {"ok": True, "saved": len(parsed), "entry_ids": [r.entry_id for r in parsed]}


# --- status -----------------------------------------------------------------


def sessions_today(
    conn: sqlite3.Connection | None, now: datetime, *, kind: str | None = None
) -> int:
    """오늘(KST) 시작한 세션 수(FAKE 제외, 실패 포함). `kind` 를 주면 그 `runs.kind`(예: `fo`,
    Phase 26 `fo.session_cap`)만 센다."""
    if conn is None:
        return 0
    start, _ = repo.day_bounds(now.astimezone(KST).date(), None)
    if kind is None:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM agent_calls WHERE agent='orchestrator' "
            "AND COALESCE(model, '') != 'fake' AND created_at >= ?",
            (start,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM agent_calls a JOIN runs r ON r.id = a.run_id "
            "WHERE a.agent='orchestrator' AND COALESCE(a.model, '') != 'fake' "
            "AND a.created_at >= ? AND r.kind = ?",
            (start, kind),
        ).fetchone()
    return int(row["n"])


def status(ctx: ToolContext, *, market: Market, date: date) -> dict:
    if market not in ("KR", "US"):
        return {"ok": False, "error": f"market 은 KR 또는 US: {market!r}"}
    mdir = market_dir(date, market, ctx.state)
    tickers: dict[str, list[str]] = {}
    if mdir.exists():
        for p in sorted(mdir.iterdir()):
            if p.is_dir():
                tickers[p.name] = list_checkpoints(p)
    return {
        "ok": True,
        "date": date.isoformat(),
        "market": market,
        "tickers": tickers,
        "sessions_today": sessions_today(ctx.conn, ctx.now()),
        "cap": ctx.settings.llm.daily_session_cap,
    }


# --- Phase 26 파생 세션 도구: fo bundle/write/summarize·submit-fo-decision ------------------
# 시장 디렉터리(`market_dir(date, "KR")`)의 `_fo_*` 단계. 쓰기 허용은 애널리스트 3역할·토론 턴뿐이고
# `_fo_summary` 는 summarize, `_fo_decision*` 는 submit 전용. 재평가는 파이썬이 세션 앞에서 남긴
# `_fo_reeval_r<n>_<계좌>` 요청(계좌·포지션·버전·request_id)과 봉투가 같아야 받는다.

FO_MARKET = "KR"
FO_INPUTS = "_fo_inputs"
FO_SUMMARY = "_fo_summary"
FO_MIN_ROLES = 2
_FO_ANALYST_RE = re.compile(r"^_fo_analyst_(technical|sentiment|news_macro)$")
_FO_DEBATE_RE = re.compile(r"^_fo_debate_r(\d+)_(bull|bear)$")
_FO_DECISION_RE = re.compile(r"^_fo_decision_r(\d+)_([A-Za-z0-9_-]+)$")
_FO_REEVAL_RE = re.compile(r"^_fo_reeval_r(\d+)_([A-Za-z0-9_-]+)$")
_FO_WRITABLE = ("_fo_analyst_<technical|sentiment|news_macro>", "_fo_debate_r<n>_<bull|bear>")
FO_PENDING_FLAG = "fo_pending_entry:"
FO_STALE_NOTE = "입력 노후 — 실행 안 함"


def _fo_dir(ctx: ToolContext, d: date, *, create: bool = False) -> Path:
    return market_dir(d, FO_MARKET, ctx.state, create=create)


def _fo_inputs(ctx: ToolContext, d: date) -> dict | None:
    cp = read_checkpoint(_fo_dir(ctx, d), FO_INPUTS)
    data = cp.get("data") if isinstance(cp, dict) else None
    return data if isinstance(data, dict) else None


def _fo_reports(d: Path) -> list[dict]:
    out = []
    for stage in list_checkpoints(d):
        if _FO_ANALYST_RE.match(stage):
            data = read_checkpoint(d, stage) or {}
            if isinstance(data.get("data"), dict):
                out.append(data["data"])
    return out


def _fo_turns(d: Path) -> dict[int, dict[str, schemas.DebateTurn]]:
    return _load_turns(d, _FO_DEBATE_RE)


def _fo_live_request(d: Path, account_id: str, position_id: int) -> tuple[int, dict] | None:
    """아직 결정이 들어오지 않은 이 포지션의 재평가 요청(라운드, 봉투) — 없으면 None."""
    decided = {r for r, _ in _fo_decision_stages(d, account_id)}
    for r, req in reversed(_fo_reeval_requests(d, account_id)):
        if req.get("position_id") == position_id and r not in decided:
            return r, req
    return None


def fo_pending_entry(conn: sqlite3.Connection, account_id: str, today: date) -> dict | None:
    """진입 창 밖에서 보류된 아침 결정(`control.fo_pending_entry:<계좌>`)의 소비자. 오늘 운영일
    것만 돌려주고 다른 날짜·dry_run 기록은 지운다(어제 보류분이 오늘 실행되지 않게, S3 P0)."""
    from trader.control import control

    key = f"{FO_PENDING_FLAG}{account_id}"
    flag = control.get_flag(conn, key)
    if not isinstance(flag, dict):
        return None
    if flag.get("date") != today.isoformat() or flag.get("dry_run"):
        control.set_flag(conn, key, None)
        return None
    return flag


def _fo_decision_stages(d: Path, account_id: str | None = None) -> list[tuple[int, str]]:
    """오늘의 결정 단계 [(round, stage)] — 아침(0) + 재평가(계좌 지정 시 그 계좌만)."""
    out: list[tuple[int, str]] = []
    for stage in list_checkpoints(d):
        if stage == "_fo_decision":
            out.append((0, stage))
            continue
        m = _FO_DECISION_RE.match(stage)
        if m and (account_id is None or m.group(2) == account_id):
            out.append((int(m.group(1)), stage))
    return sorted(out)


def _fo_reeval_requests(d: Path, account_id: str) -> list[tuple[int, dict]]:
    out: list[tuple[int, dict]] = []
    for stage in list_checkpoints(d):
        m = _FO_REEVAL_RE.match(stage)
        if m and m.group(2) == account_id:
            data = (read_checkpoint(d, stage) or {}).get("data") or {}
            out.append((int(m.group(1)), data))
    return sorted(out, key=lambda x: x[0])


def _fo_position_view(ctx: ToolContext, pos: Any) -> dict:
    """fo-trader 입력의 `context.position`(재평가): 현재 손절·배수·수명 예산 잔여."""
    from trader.fo import sizing as fo_sizing

    cfg = ctx.settings.fo
    snap = ctx.conn.execute(
        "SELECT avg FROM fo_positions_snapshot WHERE account_id = ? AND code = ? "
        "ORDER BY date DESC LIMIT 1",
        (pos.account_id, pos.code),
    ).fetchone() if ctx.conn is not None else None  # fmt: skip
    avg = Decimal(snap["avg"]) if snap and snap["avg"] else None
    budget = fo_sizing.position_budget(
        pos, ref_price=avg, multiplier=cfg.product_cfg().multiplier, r=cfg.risk_per_trade_pct
    )
    out = {
        k: getattr(pos, k)
        for k in (
            "id", "version", "account_id", "code", "kind", "contract_side", "direction",
            "vehicle", "status", "qty_held", "qty_target", "stop_pt", "stop_fut", "tp_pt",
            "leverage_used", "approved_mult", "premium_stop", "entry_premium", "horizon_until",
            "last_trade_date", "decision_date", "round", "opened_at",
        )
    }  # fmt: skip
    # remaining = 한도 − 실현·비용 − 보유분 계획 손실(추가로 쓸 수 있는 위험). 계획 손실을 모르면
    # None(확정값으로 주지 않는다, 단계 5 리뷰)
    out["budget"] = {
        "used": budget["used"], "planned": budget["planned"], "limit": budget["limit"],
        "remaining": budget["free"],
    }  # fmt: skip
    return json.loads(json.dumps(out, default=str, ensure_ascii=False))


def _fo_require_fo(ctx: ToolContext) -> dict | None:
    if ctx.fo is None:
        return {"ok": False, "error": "파생 실행 컨텍스트 없음(asset: fo 계좌가 없거나 도구 밖)"}
    return None


def fo_bundle(
    ctx: ToolContext,
    *,
    date: date,
    kind: str,
    role: str | None = None,
    round: int | None = None,
    side: str | None = None,
    position_id: int | None = None,
    account: str | None = None,
) -> dict:
    """파생 세션 서브에이전트 입력 묶음. kind=analyst(role) | researcher(round, side) |
    trader(재평가면 position_id·account → `_fo_reeval_r<n>_<계좌>` 요청의 트리거·request_id;
    `round` 를 주면 그 라운드의 요청만 읽고 없으면 오류 — stdin `position.round` 와 묶음이 어긋나지
    않게(S3 P1))."""
    from trader.fo import inputs as fo_inputs
    from trader.fo import store as fo_store

    d = _fo_dir(ctx, date)
    inputs = _fo_inputs(ctx, date)
    if inputs is None:
        return {"ok": False, "error": f"{FO_INPUTS} 없음 (파이썬이 세션 앞에서 만든다)"}
    if kind == "analyst":
        if role not in ("technical", "sentiment", "news_macro"):
            return {"ok": False, "error": "analyst 묶음은 --role technical|sentiment|news_macro"}
        return {"ok": True, "data": _shrink(fo_inputs.bundle(inputs, kind="analyst", role=role),
                                            BUNDLE_LIMIT_BYTES)}  # fmt: skip
    reports = _fo_reports(d)
    if not reports:
        return {"ok": False, "error": "_fo_analyst_* 없음 (애널리스트 먼저)"}
    if kind == "researcher":
        if round is None or side not in ("bull", "bear"):
            return {
                "ok": False,
                "error": "researcher 묶음은 --round 와 --side(bull|bear) 가 필요합니다",
            }
        own = opp = None
        if round > 0:
            prev = _fo_turns(d).get(round - 1, {})
            own = prev.get(side)
            opp = prev.get("bear" if side == "bull" else "bull")
            if own is None or opp is None:
                return {"ok": False, "error": f"라운드 {round - 1} 의 양측 턴이 모두 필요합니다"}
        data = fo_inputs.bundle(
            inputs, kind="researcher", round=round, side=side, reports=reports,
            previous=(own.model_dump() if own else None, opp.model_dump() if opp else None),
        )  # fmt: skip
        return {"ok": True, "data": _shrink(data, BUNDLE_LIMIT_BYTES)}
    if kind == "trader":
        summ = read_checkpoint(d, FO_SUMMARY)
        if summ is None:
            return {"ok": False, "error": f"{FO_SUMMARY} 없음 (fo summarize 먼저)"}
        position = trigger = None
        request_id = (inputs.get("meta") or {}).get("request_id")
        next_round = 0
        if position_id is not None or account is not None:
            if position_id is None or account is None or ctx.conn is None:
                return {"ok": False, "error": "재평가 묶음은 --position-id 와 --account 둘 다"}
            pos = fo_store.get_position(ctx.conn, position_id)
            if pos is None or pos.account_id != account:
                return {"ok": False, "error": f"포지션 {position_id} 이 계좌 {account} 에 없음"}
            position = _fo_position_view(ctx, pos)
            reqs = [r for r in _fo_reeval_requests(d, account) if r[1].get("position_id") == pos.id]
            if round is not None:
                reqs = [r for r in reqs if r[0] == round]
                if not reqs:
                    return {"ok": False, "error": f"재평가 요청 _fo_reeval_r{round}_{account} 없음"}
            if reqs:
                next_round, req = reqs[-1]
                trigger = req.get("trigger")
                request_id = req.get("request_id") or request_id
            else:
                next_round = 1 + max((r for r, _ in _fo_decision_stages(d, account)), default=0)
        decs = _fo_decision_stages(d, account)
        prev_stage = decs[-1][1] if decs else None
        prev = (read_checkpoint(d, prev_stage) or {}).get("data") if prev_stage else None
        data = fo_inputs.bundle(
            inputs, kind="trader", reports=reports, summary=_trader_summary(summ.get("data", summ)),
            position=position, trigger=trigger,
        )  # fmt: skip
        data["request_id"] = request_id
        data["previous_decision"] = prev
        data["previous_decision_stage"] = prev_stage
        data["next_round"] = next_round
        return {"ok": True, "data": _shrink(data, BUNDLE_LIMIT_BYTES, drop_evidence=True)}
    return {"ok": False, "error": f"알 수 없는 kind: {kind} (analyst|researcher|trader)"}


def fo_write(
    ctx: ToolContext,
    *,
    date: date,
    stage: str,
    payload: dict,
    agent: str | None = None,
    call_meta: dict | None = None,
) -> dict:
    """LLM 쓰기 가능 파생 단계: `_fo_analyst_<role>`(AnalystReport, role 일치),
    `_fo_debate_r<n>_<side>`(DebateTurn, round·side 일치, n ≤ debate_rounds). 토론 턴을 새로 쓰면
    `_fo_summary` 를 지운다(요약은 턴의 함수)."""
    ma = _FO_ANALYST_RE.match(stage)
    md = _FO_DEBATE_RE.match(stage)
    if ma:
        model: type = schemas.AnalystReport
    elif md:
        model = schemas.DebateTurn
        if int(md.group(1)) > ctx.settings.debate_rounds:
            return {
                "ok": False,
                "error": (
                    f"라운드 {md.group(1)} 는 설정(debate_rounds="
                    f"{ctx.settings.debate_rounds})을 넘습니다"
                ),
            }
    else:
        return {
            "ok": False,
            "error": f"쓸 수 없는 단계: {stage} (허용: {', '.join(_FO_WRITABLE)})",
        }
    v, err = _validate_payload(model, payload)
    if err:
        return err
    if ma and v.role != ma.group(1):
        return {
            "ok": False,
            "error": f"파일명 role({ma.group(1)}) 과 페이로드 role({v.role}) 불일치",
        }
    inputs = _fo_inputs(ctx, date)
    if inputs is None:
        return {"ok": False, "error": f"{FO_INPUTS} 없음 (파생 세션 밖에서는 쓸 수 없다)"}
    proxy = (inputs.get("meta") or {}).get("proxy")
    if ma and proxy and v.ticker != proxy:
        return {
            "ok": False,
            "error": f"리포트 ticker 는 입력의 proxy({proxy})여야 합니다: {v.ticker!r}",
        }
    if md and (v.round != int(md.group(1)) or v.side != md.group(2)):
        return {
            "ok": False,
            "error": (
                f"파일명 r{md.group(1)}_{md.group(2)} 와 페이로드 "
                f"round={v.round} side={v.side} 불일치"
            ),
        }
    d = _fo_dir(ctx, date, create=True)
    path = write_checkpoint(d, stage, _wrap(v.model_dump(), _call(agent, call_meta)))
    invalidated = False
    if md and FO_SUMMARY in list_checkpoints(d):
        (d / f"{FO_SUMMARY}.json").unlink(missing_ok=True)
        invalidated = True
    return {"ok": True, "path": str(path), "stage": stage, "summary_invalidated": invalidated}


def fo_summarize(ctx: ToolContext, *, date: date) -> dict:
    """`_fo_debate_*` 를 모아 `_fo_summary`(수렴 감시 포함)를 만든다."""
    return _summarize_turns(_fo_dir(ctx, date), FO_SUMMARY, _FO_DEBATE_RE)


def fo_reeval_request(
    ctx: ToolContext,
    *,
    date: date,
    account_id: str,
    position_id: int,
    trigger: dict | None,
) -> dict:
    """재평가 요청(파이썬·단계 4 트리거가 세션 앞에서 부른다): `_fo_reeval_r<n>_<계좌>` 에 봉투
    {account_id, position_id, expected_version, request_id, round, trigger} 를 남기고 돌려준다.
    세션 stdin `position` 이 이것이고, `submit-fo-decision --stage _fo_decision_r<n>_<계좌>` 는
    봉투가 이 요청과 같을 때만 받는다. 같은 포지션·같은 버전의 **미결 요청이 있으면 새 라운드를
    만들지 않고 그것을 돌려준다**(`reused: true`, 트리거는 먼저 든 것) — 두 트리거가 라운드
    번호를 다투지 않게. 버전이 바뀐 미결 요청은 어차피 제출이 거부되므로 새 라운드다(S3 P1)."""
    import uuid

    from trader.fo import store as fo_store

    if ctx.conn is None:
        return {"ok": False, "error": "원장 연결 없음"}
    pos = fo_store.get_position(ctx.conn, position_id)
    if pos is None or pos.account_id != account_id:
        return {"ok": False, "error": f"포지션 {position_id} 이 계좌 {account_id} 에 없음"}
    d = _fo_dir(ctx, date, create=True)
    live = _fo_live_request(d, account_id, pos.id)
    if live is not None and live[1].get("expected_version") == pos.version:
        n, req = live
        return {
            "ok": True, "path": str(d / f"_fo_reeval_r{n}_{account_id}.json"),
            "stage": f"_fo_reeval_r{n}_{account_id}", "round": n, "position": req, "reused": True,
        }  # fmt: skip
    used = [r for r, _ in _fo_decision_stages(d, account_id)] + [
        r for r, _ in _fo_reeval_requests(d, account_id)
    ]
    n = max(used, default=0) + 1
    stage = f"_fo_reeval_r{n}_{account_id}"
    position = {
        "account_id": account_id,
        "position_id": pos.id,
        "expected_version": pos.version,
        "request_id": uuid.uuid4().hex,
        "round": n,
        "trigger": trigger,
        "requested_at": ctx.now().isoformat(),
    }
    path = write_checkpoint(d, stage, _wrap(position, None))
    return {"ok": True, "path": str(path), "stage": stage, "round": n, "position": position}


def _fo_resolved(ctx: ToolContext, inputs: dict, env: Any, pos: Any, *, today: date) -> dict:
    """결정 저장 때 붙이는 계약 해석 {code, kind, strike, days_left, multiplier, index_at_decision,
    quote_asof, expiry_ym}. 실행기와 같은 규칙(`executor.resolve_contract`)."""
    from trader.fo import executor as fo_exec

    fo = ctx.fo
    cctx = (inputs.get("context") or {}).get("data") or {}
    idx = (cctx.get("index") or {}).get("price")
    underlying = Decimal(str(idx)) if idx not in (None, "") else None
    d = env.decision
    contract = None
    if pos is not None or d.direction != "FLAT":
        try:
            contract = fo_exec.resolve_contract(fo, env, pos, underlying=underlying, today=today)
        except Exception as e:  # noqa: BLE001 — 해석 실패는 기록(실행기가 다시 시도·거부)
            logger.warning("파생 계약 해석 실패: %s", e)
    code = pos.code if pos is not None else (contract.code if contract else None)
    kind = pos.kind if pos is not None else (contract.kind if contract else None)
    quote: dict | None = None
    tech = ((inputs.get("technical") or {}).get("data") or {}).get("quote") or {}
    if code is not None:
        if isinstance(tech.get("data"), dict) and tech["data"].get("code") == code:
            quote = tech["data"]
        else:
            quote = next(
                (q for q in cctx.get("option_chain") or [] if q.get("code") == code), None
            )
    days_left = quote.get("days_left") if quote else None
    if days_left is None:
        days_left = fo_exec.days_left_of(None, contract, today)
    return {
        "code": code,
        "kind": kind,
        "strike": str(contract.strike) if contract and contract.strike is not None else None,
        "expiry_ym": contract.expiry_ym if contract else None,
        "days_left": days_left,
        "multiplier": ctx.settings.fo.product_cfg().multiplier,
        "index_at_decision": str(underlying) if underlying is not None else None,
        "index_asof": (cctx.get("index") or {}).get("asof"),
        "quote_asof": quote.get("quote_asof") if quote else None,
        "quote_price": quote.get("price") if quote else None,
    }


def submit_fo_decision(
    ctx: ToolContext,
    *,
    date: date,
    payload: dict,
    stage: str | None = None,
    agent: str = "fo-trader",
    call_meta: dict | None = None,
) -> dict:
    """파생 결정 제출. 전제: 서로 다른 애널리스트 역할 ≥ 2 + r0 양측 턴 + `_fo_summary`.
    아침(`_fo_decision`)은 봉투의 계좌·포지션이 비어 있고 request_id 가 `_fo_inputs` 의 것과 같아야
    하며, 계좌마다 실행 창 안이면 `executor.run_leg`(실행 직전 전체 재계산), 밖이면
    `control.fo_pending_entry:<계좌>` 에 보류한다(과거 asof 입력은 기록만). 재평가
    (`_fo_decision_r<n>_<계좌>`)는 `_fo_reeval_r<n>_<계좌>` 요청과 봉투가 같고 포지션 버전이
    그대로일 때만 받아 저장·`fo_decisions` 행 + `ctx.fo.reeval_apply` 훅. 같은 단계 재제출은
    `already: true`."""
    from trader.fo import store as fo_store
    from trader.fo.schemas import FoDecisionEnvelope

    err = _fo_require_fo(ctx)
    if err:
        return err
    fo = ctx.fo
    if ctx.conn is None:
        return {"ok": False, "error": "원장 연결이 없어 결정을 기록할 수 없음"}
    inputs = _fo_inputs(ctx, date)
    if inputs is None:
        return {"ok": False, "error": f"{FO_INPUTS} 없음 (파이썬이 세션 앞에서 만든다)"}
    d = _fo_dir(ctx, date, create=True)
    stages = list_checkpoints(d)
    round_n, acct_id = 0, None
    if stage is not None and stage != "_fo_decision":
        m = _FO_DECISION_RE.match(stage)
        if not m or int(m.group(1)) < 1:
            return {
                "ok": False,
                "error": f"stage 는 _fo_decision 또는 _fo_decision_r<n>_<계좌>: {stage!r}",
            }
        round_n, acct_id = int(m.group(1)), m.group(2)
    stage = stage or "_fo_decision"
    if stage in stages:
        existing = (read_checkpoint(d, stage) or {}).get("data") or {}
        dec = existing.get("decision") or {}
        out = {
            "ok": True, "already": True, "stage": stage, "path": str(d / f"{stage}.json"),
            "intent": dec.get("intent"), "direction": dec.get("direction"),
            "vehicle": dec.get("vehicle"), "message": f"{stage} 이 이미 있어 기존 결정을 유지함",
        }  # fmt: skip
        if round_n == 0 and existing.get("decision"):
            # 저장 뒤 계좌 루프가 끊긴 경우: 판정(`_fo_risk_check_<계좌>`)도 보류 플래그도 없는
            # 계좌만 재개한다(접수된 계좌는 판정 파일이 있어 다시 내지 않는다, S3 P0)
            try:
                env_prev = FoDecisionEnvelope.model_validate(
                    {k: existing.get(k) for k in FoDecisionEnvelope.model_fields}
                )
            except ValidationError:
                return out
            todo = [
                a for a in fo.accounts
                if a.asset == "fo"
                and not _fo_account_done(ctx, d, stages, a.id, date, env_prev.request_id)
            ]  # fmt: skip
            if todo:
                res = _fo_run_accounts(
                    ctx, date=date, env=env_prev, stage=stage, inputs=inputs, accounts=todo,
                    record_only=existing.get("record_only") or _fo_submit_stale(ctx, inputs),
                )  # fmt: skip
                return out | res | {"resumed_execution": True}
        return out
    roles = {m.group(1) for s in stages if (m := _FO_ANALYST_RE.match(s))}
    missing: list[str] = []
    if len(roles) < FO_MIN_ROLES:
        missing.append(f"서로 다른 애널리스트 역할 {len(roles)}개 (최소 {FO_MIN_ROLES})")
    if not {"_fo_debate_r0_bull", "_fo_debate_r0_bear"} <= set(stages):
        missing.append("r0 양측 토론 턴 없음")
    if FO_SUMMARY not in stages:
        missing.append(f"{FO_SUMMARY} 없음")
    if missing:
        return {"ok": False, "error": "전제조건 미충족: " + ", ".join(missing)}
    try:
        env = FoDecisionEnvelope.model_validate(_unescape(payload))
    except ValidationError as e:
        msgs = "; ".join(
            f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()
        )
        return {"ok": False, "error": f"스키마 검증 실패(FoDecisionEnvelope): {msgs}"}
    meta = inputs.get("meta") or {}
    pos = None
    if round_n == 0:
        for k in ("account_id", "position_id", "expected_version"):
            if getattr(env, k) is not None:
                return {
                    "ok": False,
                    "error": f"아침 결정 봉투는 {k} 를 비워야 합니다(계좌별 판정은 파이썬)",
                }
        if env.request_id != meta.get("request_id"):
            return {
                "ok": False,
                "error": (
                    f"request_id 불일치: 입력 {meta.get('request_id')} ≠ 봉투 {env.request_id}"
                ),
            }
    else:
        req = (read_checkpoint(d, f"_fo_reeval_r{round_n}_{acct_id}") or {}).get("data") or {}
        if not req:
            return {"ok": False, "error": f"재평가 요청 _fo_reeval_r{round_n}_{acct_id} 없음"}
        for k in ("account_id", "position_id", "expected_version", "request_id"):
            if getattr(env, k) != req.get(k):
                return {
                    "ok": False,
                    "error": (
                        f"봉투 {k}={getattr(env, k)!r} 이 요청의 버전·값 {req.get(k)!r} 과 다름"
                    ),
                }
        pos = fo_store.get_position(ctx.conn, env.position_id)  # type: ignore[arg-type]
        if pos is None or pos.account_id != acct_id:
            return {"ok": False, "error": f"포지션 {env.position_id} 이 계좌 {acct_id} 에 없음"}
        if pos.version != env.expected_version:
            return {
                "ok": False,
                "error": (
                    f"포지션 버전 불일치 {pos.version} ≠ expected_version {env.expected_version}"
                ),
            }
    resolved = _fo_resolved(ctx, inputs, env, pos, today=date)
    if round_n > 0 and getattr(fo, "market_client", None) is not None:
        # 재평가의 기준 지수는 결정 시점의 지수(아침 `_fo_inputs` 의 값이 아님 — move 트리거 기준)
        try:
            idx, _ = fo.market_client.get_kospi200()
            if idx is not None:
                resolved["index_at_decision"] = str(idx)
        except Exception as e:  # noqa: BLE001 — 실패면 아침 입력 값 유지
            logger.warning("재평가 기준 지수 조회 실패(아침 값 유지): %s", e)
    historical = bool(meta.get("historical")) or bool(getattr(fo, "historical", False))
    # 기록만 하는 사유(실행 없음): 과거 asof 입력, 다른 운영일의 입력(S3 P0)
    record_only: str | None = None
    if historical:
        record_only = "historical"
    elif meta.get("date") and meta.get("date") != date.isoformat():
        record_only = "input_date"
    elif round_n == 0:
        record_only = _fo_submit_stale(ctx, inputs)
    data = {
        **env.model_dump(), "resolved": resolved, "stage": stage, "round": round_n,
        "historical": historical, "asof": meta.get("asof"), "record_only": record_only,
    }  # fmt: skip
    call = _call(agent, call_meta)
    path = write_checkpoint(d, stage, _wrap(data, call))
    dec = env.decision
    try:
        fo_store.record_decision(
            ctx.conn, date=date.isoformat(), account_id=env.account_id,
            position_id=env.position_id, round=round_n, request_id=env.request_id,
            intent=dec.intent, direction=dec.direction, vehicle=dec.vehicle,
            target_leverage=dec.target_leverage, confidence=dec.confidence,
            index_at_decision=resolved.get("index_at_decision"),
            quote_asof=resolved.get("quote_asof"),
            data=json.dumps(data, ensure_ascii=False, default=str),
        )  # fmt: skip
        ctx.conn.commit()
    except sqlite3.IntegrityError as e:
        ctx.conn.rollback()
        logger.warning("fo_decisions 중복(이미 기록됨) %s r%s: %s", stage, round_n, e)
    out: dict[str, Any] = {
        "ok": True, "path": str(path), "stage": stage, "round": round_n, "intent": dec.intent,
        "direction": dec.direction, "vehicle": dec.vehicle, "resolved": resolved,
    }  # fmt: skip
    if round_n > 0:
        # 재평가 적용은 `fo/reeval.apply`(늦은 재평가 CAS → run_leg). `reeval_apply` 훅은 테스트용
        from trader.fo import reeval as fo_reeval

        if fo.reeval_apply is not None:
            applied = fo.reeval_apply(env, round_n)
        else:
            applied = fo_reeval.apply(fo, env, round_n=round_n)
        executed = isinstance(applied, dict) and "discarded" not in applied
        return out | {"applied": applied, "executed": executed}
    return out | _fo_run_accounts(
        ctx, date=date, env=env, stage=stage, inputs=inputs,
        accounts=[a for a in fo.accounts if a.asset == "fo"], record_only=record_only,
    )  # fmt: skip


FO_INPUT_DATE_NOTE = "입력 날짜 불일치 — 실행 안 함"
FO_SUBMIT_STALE_NOTE = "제출·재개 시점 입력 노후 — 실행 안 함"


def _fo_submit_stale(ctx: ToolContext, inputs: dict) -> str | None:
    """제출·재개 시점 신선도(PR #61 리뷰): `_fo_inputs.meta.asof` 가 세션 한 번의 길이
    (`llm.timeout_s`) + `fo.max_input_age_s` 보다 오래됐으면 `"stale_submit"`. 진입 창 밖 보류는
    `run_leg` 가 실행 직전 다시 받아 재계산하므로 이 검사는 늦은 제출·몇 시간 뒤 재개만 막는다."""
    raw = (inputs.get("meta") or {}).get("asof")
    if not raw:
        return None
    try:
        at = datetime.fromisoformat(str(raw))
    except ValueError:
        return "stale_submit"
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    limit = ctx.settings.llm.timeout_s + ctx.settings.fo.max_input_age_s
    return "stale_submit" if (ctx.now() - at).total_seconds() > limit else None


def _fo_account_done(
    ctx: ToolContext, d: Path, stages: list[str], account_id: str, day: date, request_id: str
) -> bool:
    """아침 결정 재개 때 이 계좌를 건너뛸지(PR #61 리뷰·단계 4 리뷰). 판정 파일은 다리 예약
    **전에** 쓰이므로 그것만으로는 끝났다고 보지 않는다 — 보류 플래그·주문 체크포인트·**이 결정
    봉투(request_id)로 만든 원장 다리**(v34 `fo_order_legs.request_id`)가 있거나, 판정이 실행할
    것이 없는 경우(거부·기록만·오류·건너뜀·다리 없음)만 끝난 것이다."""
    if fo_pending_entry(ctx.conn, account_id, day) is not None:
        return True
    if f"_fo_order_{account_id}" in stages:
        return True
    if ctx.conn.execute(
        "SELECT 1 FROM fo_order_legs WHERE account_id = ? AND request_id = ? "
        "AND NOT (status = 'cancelled' AND order_no IS NULL AND sent_at IS NULL) LIMIT 1",
        (account_id, request_id),
    ).fetchone():
        return True
    rc_stage = f"_fo_risk_check_{account_id}"
    if rc_stage not in stages:
        return False
    rc = (read_checkpoint(d, rc_stage) or {}).get("data") or {}
    if rc.get("record_only") or rc.get("error") or rc.get("skipped"):
        return True
    if "intent" in rc and "verdict" not in rc:  # CLOSE·REPLACE 판정 — 축소 다리가 아직 없다
        return False
    verdict = rc.get("verdict")
    if verdict is None:  # 관망·거부 기록(`approved`·`legs` 를 직접 담은 형태)
        return not (rc.get("approved") and rc.get("legs"))
    return not (verdict.get("approved") and verdict.get("legs"))


def _fo_run_accounts(
    ctx: ToolContext,
    *,
    date: date,
    env: Any,
    stage: str,
    inputs: dict,
    accounts: list[AccountConfig],
    record_only: str | None,
) -> dict:
    """아침 결정의 계좌 루프(제출·재개 공용). 계좌마다: 기록만(과거 asof·입력 날짜) → 관망 →
    진입 창 안이면 `run_leg`, 밖이면 보류 플래그(dry_run 은 보류하지 않고 기록만). **계좌 하나의
    예외는 그 계좌의 `_fo_risk_check_<계좌>` 에 error 로 남기고 다음 계좌로**
    (`OrderBookkeepingError` 는 CRITICAL + 알림 — 접수번호는 사람이 대조). 낡은 보류 플래그는 먼저
    지운다(S3 P0)."""
    from trader.control import control
    from trader.fo import executor as fo_exec
    from trader.fo import rules as fo_rules
    from trader.fo import store as fo_store
    from trader.market_hours import is_fo_open
    from trader.orchestrator.executor import OrderBookkeepingError

    fo = ctx.fo
    dec = env.decision
    now = ctx.now()
    cfg = ctx.settings.fo
    executions: dict[str, list[dict]] = {}
    pending: list[str] = []
    notes: list[str] = []
    stale_note = {
        "historical": FO_STALE_NOTE, "input_date": FO_INPUT_DATE_NOTE,
        "stale_submit": FO_SUBMIT_STALE_NOTE,
    }.get(record_only)  # fmt: skip
    for a in accounts:
        rc_stage = f"_fo_risk_check_{a.id}"
        flag_key = f"{FO_PENDING_FLAG}{a.id}"
        if fo_pending_entry(ctx.conn, a.id, date) is None:
            control.set_flag(ctx.conn, flag_key, None)  # 낡은 보류 제거(소비자가 지웠을 수도)
        if stale_note:
            fo_exec._ckpt(
                fo, date, rc_stage,
                {"approved": False, "note": stale_note, "request_id": env.request_id,
                 "record_only": record_only, "reasons": [stale_note]},
            )  # fmt: skip
            continue
        try:
            has_pos = bool(fo_store.open_positions(ctx.conn, a.id))
            if dec.intent == "HOLD" and not has_pos:
                fo_exec._ckpt(
                    fo, date, rc_stage,
                    {"approved": True, "note": "관망(FLAT·HOLD, 보유 없음)",
                     "request_id": env.request_id, "reasons": [], "legs": []},
                )  # fmt: skip
                continue
            # 진입 창은 새 진입에만. 보유가 있는 RESIZE·REPLACE 는 축소·청산일 수 있어 장중이면
            # 바로 `run_leg` 로 보낸다(증액 다리는 게이트가 진입 창 밖이면 거부, PR #61 리뷰)
            entry = dec.intent == "OPEN" or (dec.intent in ("RESIZE", "REPLACE") and not has_pos)
            in_window = fo_rules._in_entry_window(now, cfg) if entry else is_fo_open(now)
            if in_window:
                executions[a.id] = fo_exec.run_leg(fo, a, env, round=0, source="decision")
            elif fo.dry_run:
                fo_exec._ckpt(
                    fo, date, rc_stage,
                    {"approved": False, "note": "진입 창 밖 — dry_run 이라 보류하지 않음",
                     "request_id": env.request_id, "record_only": "dry_run", "reasons": []},
                )  # fmt: skip
            else:
                control.set_flag(
                    ctx.conn, flag_key,
                    {"date": date.isoformat(), "stage": stage, "envelope": env.model_dump(),
                     "request_id": env.request_id, "requested_at": now.isoformat(),
                     "dry_run": False},
                )  # fmt: skip
                pending.append(a.id)
        except OrderBookkeepingError as e:
            logger.critical("파생 %s 주문 기록 실패(접수번호 %s): %s", a.id, e.order_no, e)
            fo_exec._notify(fo, f"[파생 주문 기록 실패] {a.id} 접수번호 {e.order_no or '-'}: {e}")
            executions[a.id] = [{"error": str(e), "order_no": e.order_no}]
            if rc_stage not in list_checkpoints(_fo_dir(ctx, date)):
                fo_exec._ckpt(
                    fo, date, rc_stage,
                    {"approved": False, "error": str(e), "order_no": e.order_no,
                     "request_id": env.request_id, "reasons": [str(e)]},
                )  # fmt: skip
        except Exception as e:  # noqa: BLE001 — 계좌 하나의 실패가 다른 계좌를 막지 않는다
            logger.exception("파생 %s 실행 실패: %s", a.id, e)
            err = f"{type(e).__name__}: {e}"
            fo_exec._ckpt(
                fo, date, rc_stage,
                {"approved": False, "error": err, "request_id": env.request_id, "reasons": [err]},
            )  # fmt: skip
            executions[a.id] = [{"error": err}]
    if stale_note:
        notes.append(stale_note)
    if pending:
        notes.append(f"진입 창 밖 — 보류 {', '.join(pending)}")
    executed = any("error" not in r for rs in executions.values() for r in rs)
    return {
        "executed": executed, "executions": executions, "pending": pending,
        "note": "; ".join(notes),
    }  # fmt: skip
