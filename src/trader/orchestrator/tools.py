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
        "calibration": calibration,
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
            "lesson": (e.get("reflection") or {}).get("lesson") if e.get("reflection") else None,
            "score": e["score"],
        }
        for e in r["entries"]
    ]
    return {"memory": entries, "memory_status": {"warmup": r["warmup"], "breaker": r["breaker"]}}


def ensure_calibration(ctx: ToolContext, *, ticker: str, market: Market, date: date) -> dict | None:
    """결과 엔트리가 min_decisions(60) 이상일 때만 35_calibration 을 쓴다(있으면 그대로)."""
    from trader.calibration import model as cal
    from trader.config import load_ml
    from trader.memory import vector as vec
    from trader.ml import quant_report

    if ctx.conn is None:
        return None
    d = _dir(ctx, ticker, market, date)
    existing = read_checkpoint(d, "35_calibration")
    if existing is not None:
        return existing.get("data")
    try:
        cfg = load_ml().models.calibration
        if not cfg.enabled:
            return None
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


def _load_turns(d: Path) -> dict[int, dict[str, schemas.DebateTurn]]:
    turns: dict[int, dict[str, schemas.DebateTurn]] = {}
    for stage in list_checkpoints(d):
        m = _DEBATE_RE.match(stage)
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
    try:
        summ = summary_mod.research_summary(turns[0], turns[final_round], rounds=final_round)
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
    for n in (300, 200, 140, 100):
        cut = {
            **_cut(obj, n),
            "note": f"묶음이 {limit}바이트를 넘어 긴 문자열을 {n}자에서 잘랐습니다",
        }
        if _size(cut) <= limit:
            return cut
    return cut


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
            change_pct = (
                None  # 트리거 가격의 등락률은 모른다 — 아침 시세 값으로 추격 판정하지 않는다
            )
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
    if ctx.conn is not None:  # 수집이 알아낸 종목명을 원장에 남긴다 (웹·알림 표기용)
        repo.upsert_ticker_name(
            ctx.conn, market, ticker, (inputs.get("meta") or {}).get("name"), "inputs"
        )
    if resume_stage:  # 재평가 결정이 최신 결정이 되도록 마지막에 적재
        repo.ingest_checkpoint(
            ctx.conn, date, market, ticker, resume_stage, read_checkpoint(d, resume_stage) or {}
        )
    m_round = _RESUME_RE.match(resume_stage) if resume_stage else None
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
) -> tuple[list[dict], int | None]:
    """리스크 게이트·계좌별 실행(주문은 executor 안에서만) + 메모리 엔트리.

    결정은 1회, 판정·주문은 계좌마다. 재평가 라운드는 41_/51_ 체크포인트 이름에 들어간다."""
    from trader.orchestrator import executor

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
        ctx, decision, market, date, round_no=resume_round or 0, decided_at=decided_at
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
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("메모리 기록 실패 %s %s: %s", market, decision.ticker, e)
        return None


REQUOTE_AFTER_S = 600  # 입력 수집 뒤 이만큼 지났으면 제출 때 현재가를 다시 받는다(Phase 21)


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
    turns: dict[int, dict[str, schemas.DebateTurn]] = {}
    for stage in list_checkpoints(d):
        m = _MARKET_DEBATE_RE.match(stage)
        if not m:
            continue
        data = read_checkpoint(d, stage) or {}
        turns.setdefault(int(m.group(1)), {})[m.group(2)] = schemas.DebateTurn.model_validate(
            data["data"]
        )
    return turns


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
    try:
        v = model.model_validate(_unescape(payload))
    except ValidationError as e:
        msgs = "; ".join(
            f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()
        )
        return {"ok": False, "error": f"스키마 검증 실패({model.__name__}): {msgs}"}
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
    turns = _market_turns(d)
    if 0 not in turns or set(turns[0]) != {"bull", "bear"}:
        return {"ok": False, "error": "라운드 0 의 bull·bear 턴이 모두 필요합니다"}
    final_round = max(r for r, sides in turns.items() if set(sides) == {"bull", "bear"})
    try:
        summ = summary_mod.research_summary(turns[0], turns[final_round], rounds=final_round)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    path = write_checkpoint(d, "_market_summary", _wrap(summ, None))
    return {
        "ok": True,
        "path": str(path),
        "sycophantic": summ["sycophantic"],
        "preferred": summ["preferred"],
    }


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
    turns: dict[int, dict[str, schemas.DebateTurn]] = {}
    for stage in list_checkpoints(d):
        m = _FO_DEBATE_RE.match(stage)
        if not m:
            continue
        data = read_checkpoint(d, stage) or {}
        turns.setdefault(int(m.group(1)), {})[m.group(2)] = schemas.DebateTurn.model_validate(
            data["data"]
        )
    return turns


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
    cfg = ctx.settings.fo
    used = (pos.realized_loss or Decimal(0)) + (pos.fees or Decimal(0))
    limit = (
        pos.nav_at_open * Decimal(str(cfg.risk_per_trade_pct))
        if pos.nav_at_open is not None
        else None
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
    out["budget"] = {
        "used": used,
        "limit": limit,
        "remaining": (limit - used) if limit is not None else None,
    }
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
    trader(재평가면 position_id·account → `_fo_reeval_r<n>_<계좌>` 요청의 트리거·request_id)."""
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
    try:
        v = model.model_validate(_unescape(payload))
    except ValidationError as e:
        msgs = "; ".join(
            f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()
        )
        return {"ok": False, "error": f"스키마 검증 실패({model.__name__}): {msgs}"}
    if ma and v.role != ma.group(1):
        return {
            "ok": False,
            "error": f"파일명 role({ma.group(1)}) 과 페이로드 role({v.role}) 불일치",
        }
    if md and (v.round != int(md.group(1)) or v.side != md.group(2)):
        return {
            "ok": False,
            "error": (
                f"파일명 r{md.group(1)}_{md.group(2)} 와 페이로드 "
                f"round={v.round} side={v.side} 불일치"
            ),
        }
    if _fo_inputs(ctx, date) is None:
        return {"ok": False, "error": f"{FO_INPUTS} 없음 (파생 세션 밖에서는 쓸 수 없다)"}
    d = _fo_dir(ctx, date, create=True)
    path = write_checkpoint(d, stage, _wrap(v.model_dump(), _call(agent, call_meta)))
    invalidated = False
    if md and FO_SUMMARY in list_checkpoints(d):
        (d / f"{FO_SUMMARY}.json").unlink(missing_ok=True)
        invalidated = True
    return {"ok": True, "path": str(path), "stage": stage, "summary_invalidated": invalidated}


def fo_summarize(ctx: ToolContext, *, date: date) -> dict:
    """`_fo_debate_*` 를 모아 `_fo_summary`(수렴 감시 포함)를 만든다."""
    d = _fo_dir(ctx, date)
    turns = _fo_turns(d)
    if 0 not in turns or set(turns[0]) != {"bull", "bear"}:
        return {"ok": False, "error": "라운드 0 의 bull·bear 턴이 모두 필요합니다"}
    final_round = max(r for r, sides in turns.items() if set(sides) == {"bull", "bear"})
    try:
        summ = summary_mod.research_summary(turns[0], turns[final_round], rounds=final_round)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    path = write_checkpoint(d, FO_SUMMARY, _wrap(summ, None))
    return {
        "ok": True, "path": str(path), "rounds": final_round,
        "sycophantic": summ["sycophantic"], "preferred": summ["preferred"],
    }  # fmt: skip


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
    봉투가 이 요청과 같을 때만 받는다."""
    import uuid

    from trader.fo import store as fo_store

    if ctx.conn is None:
        return {"ok": False, "error": "원장 연결 없음"}
    pos = fo_store.get_position(ctx.conn, position_id)
    if pos is None or pos.account_id != account_id:
        return {"ok": False, "error": f"포지션 {position_id} 이 계좌 {account_id} 에 없음"}
    d = _fo_dir(ctx, date, create=True)
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
    from trader.control import control
    from trader.fo import executor as fo_exec
    from trader.fo import rules as fo_rules
    from trader.fo import store as fo_store
    from trader.fo.schemas import FoDecisionEnvelope
    from trader.market_hours import is_fo_open

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
        return {
            "ok": True, "already": True, "stage": stage, "path": str(d / f"{stage}.json"),
            "intent": dec.get("intent"), "direction": dec.get("direction"),
            "vehicle": dec.get("vehicle"), "message": f"{stage} 이 이미 있어 기존 결정을 유지함",
        }  # fmt: skip
    roles = {m.group(1) for s in stages if (m := _FO_ANALYST_RE.match(s))}
    missing: list[str] = []
    if len(roles) < FO_MIN_ROLES:
        missing.append(f"서로 다른 애널리스트 역할 {len(roles)}개 (최소 {FO_MIN_ROLES})")
    turns = _fo_turns(d)
    if 0 not in turns or set(turns[0]) != {"bull", "bear"}:
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
    historical = bool(meta.get("historical")) or bool(getattr(fo, "historical", False))
    data = {
        **env.model_dump(), "resolved": resolved, "stage": stage, "round": round_n,
        "historical": historical, "asof": meta.get("asof"),
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
        applied = fo.reeval_apply(env, round_n) if fo.reeval_apply is not None else None
        return out | {"applied": applied, "executed": applied is not None}

    # 아침: 계좌별 실행·보류·기록
    now = ctx.now()
    cfg = ctx.settings.fo
    executions: dict[str, list[dict]] = {}
    pending: list[str] = []
    notes: list[str] = []
    accounts = [a for a in fo.accounts if a.asset == "fo"]
    for a in accounts:
        rc_stage = f"_fo_risk_check_{a.id}"
        if historical:
            fo_exec._ckpt(
                fo, date, rc_stage,
                {"approved": False, "note": FO_STALE_NOTE, "request_id": env.request_id,
                 "record_only": "historical", "reasons": [FO_STALE_NOTE]},
            )  # fmt: skip
            continue
        has_pos = bool(fo_store.open_positions(ctx.conn, a.id))
        if dec.intent == "HOLD" and not has_pos:
            fo_exec._ckpt(
                fo, date, rc_stage,
                {"approved": True, "note": "관망(FLAT·HOLD, 보유 없음)",
                 "request_id": env.request_id, "reasons": [], "legs": []},
            )  # fmt: skip
            continue
        entry = dec.intent in ("OPEN", "RESIZE", "REPLACE")
        in_window = fo_rules._in_entry_window(now, cfg) if entry else is_fo_open(now)
        if in_window:
            executions[a.id] = fo_exec.run_leg(fo, a, env, round=0, source="decision")
        else:
            control.set_flag(
                ctx.conn, f"{FO_PENDING_FLAG}{a.id}",
                {"date": date.isoformat(), "stage": stage, "envelope": env.model_dump(),
                 "requested_at": now.isoformat()},
            )  # fmt: skip
            pending.append(a.id)
    if historical:
        notes.append(FO_STALE_NOTE)
    if pending:
        notes.append(f"진입 창 밖 — 보류 {', '.join(pending)}")
    return out | {
        "executed": bool(executions), "executions": executions, "pending": pending,
        "note": "; ".join(notes),
    }  # fmt: skip
