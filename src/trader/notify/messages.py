"""텔레그램 알림 문구 (Phase 24). 모든 알림 문장은 여기서 만든다 — 호출부는 값만 넘긴다.

형식(테스트로 고정):

    [태그] 제목 한 줄 (계좌 · 종목명(코드))
    항목 줄들 (방향·수량·가격·상태 …)

    최종 의견: 매수 · 목표 비중 15% · 확신 0.62      ← 주문·승인 요청·재평가 주문만
    근거: 첫 두 문장
    목표가 80,000원 · 손절가 65,000원

    행동 안내 (/approve 12 …)

종목은 항상 `names.label` 의 '종목명(코드)'. 금액은 KR '71,500원', US '$227.55'. HTML 이스케이프는
`TelegramNotifier.send` 가 한다(여기서는 평문).

어투(전체 검토 T3): 안내·결과 문장은 해요체('…했어요'), 제목·항목 줄은 명사구. 내부 코드값
(`auto`·`bear`·`RISK_OFF`·`take_profit`·dry-run)은 아래 표로 한글로 바꿔 보이고 그대로
내보내지 않는다."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

SIDE = {"BUY": "매수", "SELL": "매도"}
# HOLD 는 '관망' — 보유 수량의 '보유' 와 겹치지 않게 (웹 glossary.ts 와 같은 말, 전체 검토 W12)
ACTION = {"BUY": "매수", "SELL": "매도", "HOLD": "관망", "ADD": "추가 매수", "TRIM": "일부 매도"}
MODE = {"auto": "자동", "approve": "승인", "off": "끔"}
REGIME = {"bull": "상승", "neutral": "중립", "bear": "하락"}
STANCE = {"RISK_ON": "위험 선호", "RISK_OFF": "위험 회피", "NEUTRAL": "중립"}
DRY_RUN = "주문 없이 판정"  # dry_run 표기 (웹 glossary `dry_run` 과 같은 말)
SCALP_EXIT = {
    "take_profit": "익절",
    "stop": "손절",
    "close": "마감 청산",
    "max_hold": "보유 시간 초과",
    "judge_exit": "판정자 조기 청산",
}


def money(v: Any, market: str | None) -> str:
    """금액·가격 표기. 없으면 '—'. KR 정수 원, US $ 소수 2자리."""
    if v is None or v == "":
        return "—"
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError):
        return str(v)
    if market == "US":
        return f"${d:,.2f}"
    return f"{int(d.to_integral_value()):,}원"


def pct(v: Any, digits: int = 0) -> str:
    try:
        return f"{float(v) * 100:.{digits}f}%"
    except (TypeError, ValueError):
        return "—"


def first_sentences(text: str | None, n: int = 2, limit: int = 160) -> str:
    """근거 앞 n 문장(없으면 앞 limit 자)."""
    t = " ".join((text or "").split())
    if not t:
        return ""
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", t) if p.strip()]
    out = " ".join(parts[:n]) if parts else t
    return out if len(out) <= limit else out[: limit - 1] + "…"


@dataclass(frozen=True)
class DecisionBrief:
    """알림에 붙이는 최종 의견 요약."""

    action: str
    target_weight: float | None = None
    confidence: float | None = None
    rationale: str | None = None
    take_profit: Any = None
    stop_loss: Any = None
    horizon_days: int | None = None

    @classmethod
    def of(cls, decision: Any) -> DecisionBrief | None:
        """Decision 모델·dict(결정 행 data) 어느 쪽이든. 없으면 None."""
        if decision is None:
            return None
        get = (
            decision.get
            if isinstance(decision, dict)
            else lambda k, d=None: getattr(decision, k, d)
        )
        action = get("action")
        if not action:
            return None
        return cls(
            action=str(action),
            target_weight=get("target_weight"),
            confidence=get("confidence"),
            rationale=get("rationale"),
            take_profit=get("take_profit"),
            stop_loss=get("stop_loss"),
            horizon_days=get("horizon_days"),
        )


def opinion_lines(o: DecisionBrief, market: str | None) -> list[str]:
    head = f"최종 의견: {ACTION.get(o.action, o.action)}"
    if o.target_weight is not None:
        head += f" · 목표 비중 {pct(o.target_weight)}"
    if o.confidence is not None:
        head += f" · 확신 {float(o.confidence):.2f}"
    out = [head]
    why = first_sentences(o.rationale)
    if why:
        out.append(f"근거: {why}")
    prices = []
    if o.take_profit is not None:
        prices.append(f"목표가 {money(o.take_profit, market)}")
    if o.stop_loss is not None:
        prices.append(f"손절가 {money(o.stop_loss, market)}")
    if o.horizon_days:
        prices.append(f"보유 {o.horizon_days}일")
    if prices:
        out.append(" · ".join(prices))
    return out


def render(
    tag: str,
    title: str = "",
    lines: list[str] | tuple[str, ...] = (),
    *,
    opinion: DecisionBrief | None = None,
    market: str | None = None,
    action: str | None = None,
) -> str:
    """공통 틀. 빈 줄은 버리고, 의견·행동 안내 앞에는 빈 줄 하나."""
    parts = [f"[{tag}] {title}".rstrip()]
    parts += [ln for ln in lines if ln]
    if opinion is not None:
        parts += ["", *opinion_lines(opinion, market)]
    if action:
        parts += ["", action]
    return "\n".join(parts)


def event(tag: str, text: str = "", *lines: str) -> str:
    """종목·주문이 없는 운영 알림(스캔·국면·학습·잡 …)."""
    return render(tag, text, lines)


def briefing(lines: list[str], uncertain: list[str]) -> str:
    """아침 브리핑(사람이 읽는 요약 — 결정·주문과 무관)."""
    return event(
        "아침 브리핑",
        "",
        *[f"· {ln}" for ln in lines],
        *[f"(불확실) {u}" for u in uncertain],
    )


def adjusted_prices(market: str, tickers: list[str]) -> str:
    """수정주가 소급 변경 감지 → 해당 종목 재백필(피처·라벨 절벽 방지)."""
    return event(
        "수정주가 변경",
        f"{market} {len(tickers)}종목 과거 일봉을 다시 받았어요",
        ", ".join(tickers[:10]) + (" …" if len(tickers) > 10 else ""),
    )


def killswitch_on(reason: str) -> str:
    """킬스위치 켜짐: 신규 매수 중단(매도·손절·헤지는 계속), 해제는 사람만."""
    return event(
        "킬스위치",
        "신규 매수를 멈췄어요",
        f"사유: {reason}",
        '매도·손절·헤지는 계속돼요. 해제: trader control killswitch off --note "원인"',
    )


def saveticker_health(text: str) -> str:
    """세이브티커 수집 장애·복구(수집기 감시가 만든 고정 문구 한 줄)."""
    return event("세이브티커 수집", text)


def failure(tag: str, scope: str, err: Any) -> str:
    """실패 알림: '[태그] 범위' + 오류 한 줄. 예외면 '형식: 메시지'."""
    msg = f"{type(err).__name__}: {err}" if isinstance(err, BaseException) else str(err)
    return render(tag, scope, [f"오류: {msg}"] if msg else [])


def _order_line(side: str, qty: Any, price: Any, market: str | None) -> str:
    where = f"@{money(price, market)}" if price is not None else "시장가"
    return f"{SIDE.get(str(side), str(side))} {qty}주 {where}"


# --- 주문·승인 -----------------------------------------------------------------------------


def order(
    *,
    account: str,
    market: str,
    label: str,
    side: str,
    qty: Any,
    price: Any,
    mode: str,
    accepted: bool,
    order_no: str | None,
    message: str | None,
    opinion: DecisionBrief | None = None,
    kind: str = "",
) -> str:
    """주문 접수/실패. kind 는 제목 꼬리('재평가'·'헤지'·'재호가' 등)."""
    tag = ("주문" if accepted else "주문 실패") + (f" · {kind}" if kind else "")
    lines = [
        f"{_order_line(side, qty, price, market)} · {MODE.get(mode, mode)}",
        f"접수번호 {order_no or '—'}" + (f" · {message}" if message else ""),
    ]
    return render(
        tag, f"{account} · {label}", lines, opinion=opinion if accepted else None, market=market
    )


def order_unknown(*, account: str, market: str, label: str, side: str, qty: Any, err: str) -> str:
    return render(
        "주문 상태 미확인",
        f"{account} · {label}",
        [
            f"{SIDE.get(side, side)} {qty}주 — 응답을 받지 못했어요",
            f"사유: {err}",
            "KIS 앱에서 확인하세요. 체결 동기화(fills sync)가 확정해요.",
        ],
    )


def bookkeeping_failure(
    *, account: str, label: str, side: str, qty: Any, order_no: str | None, status: str, err: Any
) -> str:
    return render(
        "긴급: 주문 기록 실패",
        f"{account} · {label}",
        [
            f"{SIDE.get(side, side)} {qty}주 · 접수번호 {order_no or '—'} ({status})",
            f"원장에 없으니 수동 확인이 필요해요: {err}",
        ],
    )


def execution_failure(*, account: str, market: str, label: str, err: Any) -> str:
    return failure("실행 실패", f"{account} · {market} {label}", err)


ENV = {"paper": "모의", "real": "실전"}
REAL_CONFIRM = "실전"  # 텔레그램에서 실전 승인에 붙여야 하는 확인어 (/approve 12 실전)


def kst_hhmm(iso: str | None) -> str:
    """UTC ISO → 'HH:MM'(KST). 해석 못 하면 원문."""
    if not iso:
        return "—"
    try:
        from datetime import datetime

        from trader.config import KST

        return datetime.fromisoformat(iso).astimezone(KST).strftime("%H:%M")
    except (TypeError, ValueError):
        return str(iso)


def _amount(qty: Any, price: Any, market: str | None) -> str:
    try:
        return money(Decimal(str(qty)) * Decimal(str(price)), market)
    except (InvalidOperation, ValueError, TypeError):
        return ""


def approve_hint(approval_id: int, env: str | None) -> str:
    """승인·거부 명령 안내. 실전은 확인어를 붙여야 실행된다(전체 검토 T2)."""
    ok = f"/approve {approval_id}" + (f" {REAL_CONFIRM}" if env == "real" else "")
    return f"{ok} · /reject {approval_id}"


def approval_request(
    *,
    approval_id: int,
    account: str,
    market: str,
    label: str,
    side: str,
    qty: Any,
    price: Any,
    reasons: list[str],
    opinion: DecisionBrief | None = None,
    kind: str = "",
    env: str | None = None,
    quote_price: Any = None,
    expires_at: str | None = None,
) -> str:
    """승인 요청: 모의/실전·금액·리스크 판정·의견·만료 시각(KST) — 폰에서 판단에 필요한 것만."""
    tag = f"승인 요청 #{approval_id}" + (f" · {ENV[env]}" if env in ENV else "")
    tag += f" · {kind}" if kind else ""
    line = _order_line(side, qty, price, market)
    amt = _amount(qty, price if price is not None else quote_price, market)
    if amt:
        line += f" = {amt}" + (" (현재가 기준)" if price is None else "")
    judged = "; ".join(reasons)
    until = f" — {kst_hhmm(expires_at)} KST 까지" if expires_at else " (30분 안)"
    return render(
        tag,
        f"{account} · {label}",
        [line, f"리스크 판정: {judged}" if judged else "리스크 한도 통과"],
        opinion=opinion,
        market=market,
        action=approve_hint(approval_id, env) + until,
    )


def approval_rejected(*, approval_id: int, account: str, label: str) -> str:
    return render("거부", f"승인 요청 #{approval_id} · {account} · {label}")


def approval_tampered(*, approval_id: int, account: str, label: str) -> str:
    return render(
        "긴급: 승인 지문 불일치",
        f"승인 요청 #{approval_id} · {account} · {label}",
        ["요청 뒤 주문 내용이 바뀌었어요 — 주문하지 않고 실패로 닫았어요. 원장을 확인하세요."],
    )


# --- 손절·재평가·재호가 --------------------------------------------------------------------


def event_detected(*, market: str, event: dict) -> str:
    """사건 수혜(2026-10-27): 오늘 처음 감지된 사건 — 알림만, 매수는 다음 scan 의 분석·게이트."""
    kind = "단기 사건" if event.get("kind") == "shock" else "구조적 흐름"
    tickers = ", ".join(
        f"{t.get('name')}({t.get('role')})" for t in (event.get("tickers") or [])[:6]
    )
    return render(
        "사건 감지",
        f"{market} · {event.get('label')} ({kind}, 뉴스 {event.get('hits')}건)",
        [
            *[f"· {n.get('title')}" for n in (event.get("news") or [])[:3]],
            f"수혜 후보: {tickers}" if tickers else "",
            "다음 scan 에서 scout 가 이 묶음 안에서 고르고, 분석·리스크 게이트를 거쳐요",
        ],
    )


def open_buys_cancelled(
    *, account: str, label: str, reason: str, cancelled: int, left: list[str]
) -> str:
    """결정이 매수에서 돌아서 당일 미체결 BUY 를 거둔 결과(전체 검토 2026-10-27)."""
    return render(
        "미체결 매수 취소",
        f"{account} · {label}",
        [
            f"{reason} — 당일 미체결 매수 {cancelled}건 취소",
            f"취소하지 못함: {', '.join(left)} — 증권사 앱에서 직접 취소하세요" if left else "",
        ],
    )


def stop_loss(
    *, account: str, market: str, label: str, qty: Any, quote: Any, level: Any, order_no: Any
) -> str:
    return render(
        "손절",
        f"{account} · {label}",
        [
            f"{qty}주 즉시 매도 · 접수번호 {order_no or '—'}",
            f"현재가 {money(quote, market)} ≤ 손절선 {money(level, market)}",
        ],
    )


def stop_deferred(*, account: str, market: str, label: str) -> str:
    return render(
        "손절 보류",
        f"{account} · {label}",
        ["당일 미체결 매도를 취소하지 못해 손절을 내지 않았어요. 다음 점검에서 다시 해요."],
    )


def stop_paused(*, market: str, label: str, quote: Any, level: Any) -> str:
    return render(
        "손절 보류: 일시정지",
        f"{market} {label}",
        [
            f"현재가 {money(quote, market)} ≤ 손절선 {money(level, market)}",
            "재개하면 다음 점검에서 매도해요.",
        ],
    )


def reevaluation_failed(*, market: str, label: str, err: Any) -> str:
    return failure("재평가 실패", f"{market} {label}", err)


def reprice(
    *, account: str, market: str, label: str, side: str, qty: Any, old: Any, new: Any, tail: str
) -> str:
    return render(
        "재호가",
        f"{account} · {label}",
        [
            f"{SIDE.get(side, side)} {qty}주 {money(old, market)} → {money(new, market)}",
            f"원 주문 취소 · {tail}",
        ],
    )


def reprice_deferred(
    *, account: str, market: str, label: str, side: str, qty: Any, limit: Any, why: str
) -> str:
    return render(
        "재호가 보류",
        f"{account} · {label}",
        [
            f"{SIDE.get(side, side)} {qty}주 @{money(limit, market)}",
            f"{why} — 원 주문은 그대로 둬요",
        ],
    )


# --- 헤지·단타·대조·체결 --------------------------------------------------------------------


def hedge(
    *, entering: bool, account: str, market: str, label: str, qty: Any, detail: str = ""
) -> str:
    return render(
        "헤지 진입" if entering else "헤지 해제",
        f"{account} · {label}",
        [f"{'매수' if entering else '매도'} {qty}주", detail],
    )


def scalp(
    *, leg: str, market: str, label: str, price: Any = None, reason: str = "", ret: Any = None
) -> str:
    """leg: entry|exit|halt. 진입·청산은 틱마다 보내지 않고 `scalp_digest` 로 묶는다."""
    if leg == "halt":
        return render("단타 중단", market, [reason, "오늘은 새로 진입하지 않아요."])
    lines = [f"@{money(price, market)}" + (f" · 수익 {float(ret):+.2%}" if ret is not None else "")]
    lines.append(SCALP_EXIT.get(reason, reason))
    return render("단타 진입" if leg == "entry" else "단타 청산", f"{market} {label}", lines)


def scalp_digest(*, market: str, items: list[dict], dry_run: bool = False) -> str:
    """단타 진입·청산 묶음 요약(전체 검토 T3 — 틱마다 오던 알림 피로). items 는 엔진 outbox 의
    {leg, label, price, reason, ret?} 순서 그대로. 수익 합계는 이 묶음 청산 수익률의 단순 합."""
    entries = [i for i in items if i.get("leg") == "entry"]
    exits = [i for i in items if i.get("leg") == "exit"]
    head = f"{market} 진입 {len(entries)} · 청산 {len(exits)}"
    if exits:
        head += f" · 수익 합계 {sum(float(i.get('ret') or 0) for i in exits):+.2%}"
    if dry_run:
        head += f" ({DRY_RUN})"
    lines = []
    for i in items:
        where = f"{i.get('label')} @{money(i.get('price'), market)}"
        if i.get("leg") == "entry":
            lines.append(f"진입 {where} · {i.get('reason') or '—'}")
        else:
            why = SCALP_EXIT.get(str(i.get("reason")), str(i.get("reason") or "—"))
            ret = f" · {float(i['ret']):+.2%}" if i.get("ret") is not None else ""
            lines.append(f"청산 {where} · {why}{ret}")
    return render("단타", head, lines)


# --- 운영 이벤트(국면·시장 뷰·모드·학습) — 코드값을 한글 표로 -------------------------------


def regime_change(*, market: str, before: str | None, after: str, count: str) -> str:
    def name(s: str | None) -> str:
        return REGIME.get(s, s) if s else "—"

    return render("국면 전환", f"{market} {name(before)} → {name(after)} ({count})")


def market_view(
    *,
    market: str,
    stance: str | None,
    hedge_weight: Any,
    leveraged: bool,
    rationale: str | None = None,
) -> str:
    stance_text = STANCE.get(str(stance), stance or "—")
    head = f"{market} {stance_text} · 헤지 제안 {float(hedge_weight or 0):.0%}"
    why = first_sentences(rationale)
    return render(
        "시장 뷰", head + ("" if leveraged else " (레버리지 거부)"), [f"근거: {why}" if why else ""]
    )


def mode_changed(*, account: str, mode: str) -> str:
    return render("모드", f"{account} → {MODE.get(mode, mode)}")


def ml_trained(
    *,
    kind: str,
    version: str | None,
    trained: bool,
    detail: str | None,
    promoted: bool | None,
    note: str = "",
) -> str:
    """학습 결과. promoted None(학습 안 됨·수동 승격 모델)이면 승격 줄 없음."""
    head = f"{kind} {version or '—'}: {'학습 완료' if trained else '데이터 부족'}"
    if detail:
        head += f" · {detail}"
    lines = []
    if promoted is not None:
        lines.append("승격했어요" if promoted else "승격 안 함(기준 미달)")
    return render("ML 학습", head, [*lines, note])


def reconcile_mismatch(*, account: str, market: str, day: str, parts: list[str]) -> str:
    return render(
        "대조 불일치",
        f"{account} · {market} {day}",
        [*parts, "원장 스냅샷을 브로커 값으로 고쳤어요. 주문·체결 행은 그대로예요."],
    )


def fill_ambiguous(*, account: str, market: str, label: str, side: str, detail: str) -> str:
    return render(
        "체결 매칭 모호", f"{account} · {market} {label}", [f"{SIDE.get(side, side)} — {detail}"]
    )


def disclosure(*, market: str, label: str, title: str, url: str, action: str) -> str:
    """중요 공시(critical, Phase 25) — 보유·유니버스 종목. action 은 '재분석 요청 #n' 등."""
    return render("중요 공시", f"{market} {label}", [title, url, action])


def theme_heat(*, market: str, themes: list[dict]) -> str:
    """테마 열기 상위 테마가 바뀌었을 때 (Phase 25)."""
    lines = []
    for th in themes:
        names = ", ".join(
            (f"{t['name']}({t['ticker']})" if t.get("name") else t["ticker"])
            for t in (th.get("tickers") or [])[:3]
        )
        cat = "촉매 있음" if th.get("catalyst_ok") else "촉매 없음(커뮤니티뿐)"
        lines.append(f"{th['rank']}. {theme_name(th['theme'])} · 점수 {th['score']:.2f} · {cat}")
        if names:
            lines.append(f"   {names}")
    return render("테마 열기", market, lines)


def theme_name(theme: str) -> str:
    """'섹터:반도체' → '반도체(섹터)', 'kw:로봇' → '로봇(뉴스 키워드)'."""
    kind, _, name = theme.partition(":")
    return f"{name}({'섹터' if kind == '섹터' else '뉴스 키워드'})" if name else theme


def ksd_stale(items: list[tuple[str, int | None]]) -> str:
    """권리 일정(예탁원) 종류별 수집이 STALE_DAYS 넘게 실패(2-O9) — 지난 일정으로 판정 중."""
    lines = [
        f"{label}: {days}일째 실패" if days else f"{label}: 수집 기록 없음" for label, days in items
    ]
    tail = "지난 일정으로 블랙아웃을 판정하고 있어요"
    return render("권리 일정 수집 실패", "KR", [*lines, tail])
