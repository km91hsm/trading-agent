"""FAKE_CLAUDE=1 모드.

claude 를 띄우지 않고 오케스트레이터가 할 도구 호출 순서를 파이썬으로 흉내 낸다.

실제 세션과 같은 도구 함수(tools.*)를 쓰므로 전제조건·스키마 검증·체크포인트 재개가 그대로 적용된다.
응답은 고정 JSON (애널리스트 NEUTRAL, bull BULLISH 0.7 / bear BEARISH 0.6, 트레이더 HOLD).
"""

from __future__ import annotations

from datetime import date

from trader.orchestrator import tools
from trader.orchestrator.run_store import list_checkpoints, read_checkpoint, run_dir

ANALYST_ROLES = ("technical", "fundamental", "news_macro", "sentiment")
FAKE_MODEL = "fake"


def fake_report(ticker: str, asof: str, role: str) -> dict:
    return {
        "ticker": ticker,
        "asof": asof,
        "role": role,
        "summary": f"FAKE {role} 리포트",
        "bullets": ["FAKE 근거"],
        "signal": "NEUTRAL",
        "score": 0.0,
        "confidence": 0.5,
        "horizon": "short",
        "key_risks": [],
        "counter_evidence": [],
        "evidence": [{"claim": "FAKE", "source_id": f"{role}"}],
        "data_gaps": ["FAKE 모드"],
    }


def fake_turn(side: str, round_no: int) -> dict:
    bull = side == "bull"
    return {
        "side": side,
        "round": round_no,
        "stance": "BULLISH" if bull else "BEARISH",
        "valuation_view": "UNDERVALUED" if bull else "OVERVALUED",
        "arguments": [f"FAKE {side} 논거"],
        "rebuttals": [f"FAKE 반박 r{round_no}"] if round_no > 0 else [],
        "timing": {"action": "WAIT", "condition": None, "price_zone": None},
        "conviction": 0.7 if bull else 0.6,
        "what_would_change_my_mind": ["FAKE"],
    }


def fake_decision(ticker: str) -> dict:
    return {
        "ticker": ticker,
        "action": "HOLD",
        "target_weight": 0.0,
        "order_type": "LIMIT",
        "limit_price": None,
        "stop_loss": None,
        "take_profit": None,
        "horizon_days": 5,
        "confidence": 0.5,
        "rationale": "FAKE 모드: 기본값 HOLD",
        "invalidation_conditions": [],
        "memory_ids_used": [],
    }


def _call(agent: str) -> dict:
    return {
        "agent": agent,
        "model": FAKE_MODEL,
        "session_id": "fake",
        "cost_usd": 0.0,
        "duration_ms": 0,
    }


def run_fake_reflect(
    ctx: tools.ToolContext, *, market: str, date: date, entries: list[dict]
) -> list[str]:
    """reflect 모드: 모든 엔트리에 NEUTRAL 반성(lesson 없음)."""
    if not entries:
        return []
    payload = [
        {
            "entry_id": e["entry_id"],
            "signal_review": {
                role: {"verdict": "UNKNOWN", "note": "FAKE"}
                for role in (e.get("analyst_signals") or {})
            },
            "decision_review": {"verdict": "NEUTRAL", "note": "FAKE 모드: 판단 보류"},
            "lesson": None,
            "confidence": 0.5,
        }
        for e in entries
    ]
    r = tools.submit_reflection(
        ctx, market=market, date=date, payload=payload, call_meta=_call("reflector")
    )
    if not r["ok"]:
        raise tools.ToolError(r["error"])
    return ["_reflect"]


def run_fake_screen(
    ctx: tools.ToolContext, *, market: str, date: date, candidates: list[dict]
) -> list[str]:
    """screen 모드: `_discovery_inputs` 가 있으면 유니버스 첫 종목 1개를 발굴(scout 흉내)해 병합한
    뒤, 점수 있는 후보(발굴 후보 포함) 상위 5개를 고른다."""
    from trader.orchestrator.run_store import market_dir

    di = (read_checkpoint(market_dir(date, market, ctx.state), "_discovery_inputs") or {}).get(
        "data"
    ) or {}
    uni_by = {u.get("ticker"): u for u in di.get("universe") or []}
    # 정량 점수 순으로 첫 유니버스 종목 하나만 발굴한다(거른 종목을 되살리지 않아 순서가 유지된다)
    first = next(
        (
            uni_by[c["ticker"]]
            for c in candidates
            if c.get("score") is not None and c["ticker"] in uni_by
        ),
        None,
    )
    if first is not None:
        r = tools.submit_discovery(
            ctx,
            market=market,
            date=date,
            payload={
                "candidates": [
                    {
                        "name": first.get("name") or first["ticker"],
                        "ticker": first["ticker"],
                        "market": market,
                        "tier": "universe",
                        "catalyst": "FAKE 발굴: 뉴스 촉매",
                        "source_ids": ["market_notes"],
                        "confidence": 0.6,
                    }
                ],
                "notes": ["FAKE 모드"],
            },
            agent="scout",
            call_meta=_call("scout"),
        )
        if r["ok"]:
            candidates = r["candidates"]
    scored = [
        c for c in candidates if c.get("score") is not None or c.get("discovery")
    ] or candidates
    picks = [
        {
            "ticker": c["ticker"],
            "reason": "FAKE: 뉴스 발굴" if c.get("discovery") else "FAKE: 정량 상위",
        }
        for c in scored[:5]
    ]
    r = tools.submit_screen(
        ctx,
        market=market,
        date=date,
        payload={"picks": picks, "rejected": [c["ticker"] for c in scored[5:]]},
        agent="screener",
        call_meta=_call("screener"),
    )
    if not r["ok"]:
        raise tools.ToolError(r["error"])
    return ["_screen"]


def run_fake_ticker(
    ctx: tools.ToolContext,
    *,
    ticker: str,
    market: str,
    date: date,
    debate_rounds: int,
    trigger: dict | None = None,
) -> list[str]:
    """한 종목에 대해 남은 단계만 실행하고 새로 쓴 단계 이름을 돌려준다."""
    d = run_dir(date, market, ticker, ctx.state)
    inputs = read_checkpoint(d, "00_inputs")
    if inputs is None:
        raise tools.ToolError(f"00_inputs 없음: {market} {ticker} (먼저 collect)")
    asof = str((inputs.get("meta") or {}).get("asof", ""))
    written: list[str] = []

    def write(stage: str, payload: dict, agent: str) -> None:
        if stage in list_checkpoints(d):
            return
        r = tools.checkpoint_write(
            ctx,
            ticker=ticker,
            market=market,
            date=date,
            stage=stage,
            payload=payload,
            agent=agent,
            call_meta=_call(agent),
        )
        if not r["ok"]:
            raise tools.ToolError(r["error"])
        written.append(stage)

    for role in ANALYST_ROLES:
        write(f"10_analyst_{role}", fake_report(ticker, asof, role), f"analyst-{role}")
    before = len(written)
    for n in range(0, debate_rounds + 1):
        for side in ("bull", "bear"):
            write(f"20_debate_r{n}_{side}", fake_turn(side, n), f"researcher-{side}")
    debate_changed = len(written) > before
    # 토론 턴을 새로 썼으면 요약도 다시 만든다 (오케스트레이터 프롬프트와 같은 규칙)
    if debate_changed or "25_research_summary" not in list_checkpoints(d):
        r = tools.checkpoint_summarize(ctx, ticker=ticker, market=market, date=date)
        if not r["ok"]:
            raise tools.ToolError(r["error"])
        written.append("25_research_summary")
    if trigger is not None:
        n = 1 + sum(1 for s in list_checkpoints(d) if s.startswith("31_decision_r"))
        stage = f"31_decision_r{n}"
        r = tools.submit_decision(
            ctx,
            ticker=ticker,
            market=market,
            date=date,
            payload=fake_decision(ticker),
            agent="trader",
            call_meta=_call("trader"),
            stage=stage,
        )
        if not r["ok"]:
            raise tools.ToolError(r["error"])
        return written + [stage]
    if "30_decision" not in list_checkpoints(d):
        r = tools.submit_decision(
            ctx,
            ticker=ticker,
            market=market,
            date=date,
            payload=fake_decision(ticker),
            agent="trader",
            call_meta=_call("trader"),
        )
        if not r["ok"]:
            raise tools.ToolError(r["error"])
        written.append("30_decision")
    return written


def run_fake_market(
    ctx: tools.ToolContext, *, market: str, date: date, debate_rounds: int = 1
) -> list[str]:
    """market 모드: 거시 리포트(NEUTRAL) → 토론 r0(+반박) → 요약 → HedgeProposal.
    국면이 bear 면 RISK_OFF 0.10, 아니면 RISK_ON 0.0
    (파이썬 규칙이 어차피 bear 아니면 진입 안 함)."""
    from trader.orchestrator.run_store import market_dir, read_checkpoint

    d = market_dir(date, market, ctx.state)
    inputs = (read_checkpoint(d, "_market_inputs") or {}).get("data") or {}
    proxy = inputs.get("proxy") or tools.MARKET_PROXY_LABEL.get(market, market)
    written: list[str] = []
    r = tools.market_write(
        ctx,
        market=market,
        date=date,
        stage="_market_analyst",
        payload=fake_report(proxy, date.isoformat(), "news_macro"),
        agent="analyst-market",
        call_meta=_call("analyst-market"),
    )
    if not r["ok"]:
        raise tools.ToolError(r["error"])
    written.append("_market_analyst")
    for rnd in range(0, min(debate_rounds, 1) + 1):
        for side in ("bull", "bear"):
            r = tools.market_write(
                ctx,
                market=market,
                date=date,
                stage=f"_market_debate_r{rnd}_{side}",
                payload=fake_turn(side, rnd),
                agent=f"researcher-{side}",
                call_meta=_call(f"researcher-{side}"),
            )
            if not r["ok"]:
                raise tools.ToolError(r["error"])
            written.append(f"_market_debate_r{rnd}_{side}")
    r = tools.market_summarize(ctx, market=market, date=date)
    if not r["ok"]:
        raise tools.ToolError(r["error"])
    written.append("_market_summary")
    bear = (inputs.get("regime") or {}).get("state") == "bear"
    r = tools.submit_market_view(
        ctx,
        market=market,
        date=date,
        payload={
            "market": market,
            "stance": "RISK_OFF" if bear else "RISK_ON",
            "hedge_weight": 0.10 if bear else 0.0,
            "leveraged": False,
            "confidence": 0.5,
            "horizon_days": 20,
            "rationale": "FAKE 모드: 국면 지표를 그대로 따름",
            "invalidation_conditions": [],
        },
        agent="strategist",
        call_meta=_call("strategist"),
    )
    if not r["ok"]:
        raise tools.ToolError(r["error"])
    written.append("_market_view")
    return written
