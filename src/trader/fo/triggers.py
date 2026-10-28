"""파생 재평가 트리거(Phase 26 단계 4, 스펙 §5). `candidates` 는 순수 판정(기초지수·호가·뉴스 건수
→ 후보 목록), `check` 는 원장 `triggers`(kind `fo:<종류>`, ticker = 계약 코드) 기록으로 빈도
제한을 적용해 **한 틱에 하나**만 돌려준다. 기록(`record`)과 재평가 요청은 엔진이 한다.

빈도 규칙: 미처리(`handled_at` NULL) 트리거가 오늘 있으면 새로 내지 않는다 · 코드당
`reeval.interval_min` 간격 · 하루 `reeval.max_per_day` · `KINDS_ONCE` 는 운영일 1회 ·
`periodic` 은 시간 버킷당 1회 + `periodic_max_per_day` · `expiry` 는 강제 청산일 직전 운영일
09:05 이후 1회. 세는 것은 **성공한 재평가만**(`session_id` 가 `실패:` 로 시작하지 않는 처리 행) —
기동 실패는 다음 간격에 다시 시도한다(현물 `repo.reevaluated_between` 과 같은 규칙)."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from trader.config import KST, FoCfg, HorizonProfile
from trader.fo.types import FoPositionRow
from trader.ledger import repo

MARKET = "KR"
KINDS_ONCE = ("invalidation", "move", "take_profit", "news", "iv_spike")
PRIORITY = ("invalidation", "move", "take_profit", "news", "iv_spike", "periodic", "expiry")
EXPIRY_OPINION_AT = time(9, 5)
SESSION_OPEN = time(8, 45)  # 파생 정규장 시작 — periodic 버킷의 기준
# 실제로 재평가가 끝난 처리만 센다 — 기동 실패('실패:')·세션 없이 버린 것('폐기:')은 빼서 다음
# 간격에 다시 시도한다(단계 4 리뷰)
_OK = "(session_id IS NULL OR (session_id NOT LIKE '실패:%' AND session_id NOT LIKE '폐기:%'))"


@dataclass(frozen=True)
class Trigger:
    kind: str
    detail: dict[str, Any] = field(default_factory=dict)
    price: Decimal | None = None


def ledger_kind(kind: str) -> str:
    return f"fo:{kind}"


def _business_days_before(d: date, n: int, holiday_fn: Callable[[date], bool] | None) -> date:
    """d 에서 거꾸로 n 운영일(주말·휴장 제외) 앞의 날."""
    out = d
    while n > 0:
        out -= timedelta(days=1)
        if out.weekday() < 5 and not (holiday_fn and holiday_fn(out)):
            n -= 1
    return out


def force_close_day_of(
    position: FoPositionRow,
    cfg: FoCfg,
    *,
    last_trade_date: date,
    holiday_fn: Callable[[date], bool] | None = None,
) -> date:
    """강제 청산일 = 최종거래일 D 에서 `roll_days`(옵션 매도 +1) 운영일 앞."""
    n = cfg.roll_days + (1 if position.vehicle == "OPTION_SELL" else 0)
    return _business_days_before(last_trade_date, n, holiday_fn)


def opinion_day_of(force_close_day: date, holiday_fn: Callable[[date], bool] | None = None) -> date:
    """만기 의견 트리거 날 = 강제 청산일 직전 운영일(R16)."""
    return _business_days_before(force_close_day, 1, holiday_fn)


def candidates(
    *,
    position: FoPositionRow,
    underlying: Decimal | None,
    iv: float | None,
    cfg: FoCfg,
    profile: HorizonProfile,
    now: datetime,
    today: date,
    novel_news: int,
    opinion_day: date | None,
    ref_index: Decimal | None,
    ref_iv: float | None,
    take_profit_pt: Decimal | None,
    invalidation_conditions: list[str] | None,
) -> list[Trigger]:
    """순수 판정. 빈도 제한 없이 지금 성립하는 트리거를 `PRIORITY` 순으로."""
    from trader.orchestrator.monitor import parse_price_conditions

    out: list[Trigger] = []
    idx = underlying
    if idx is not None:
        for op, px in parse_price_conditions(invalidation_conditions or []):
            if (op == "<" and idx < px) or (op == ">=" and idx >= px):
                out.append(Trigger("invalidation", {"condition": f"{op} {px}"}, idx))
                break
        if ref_index is not None and ref_index > 0:
            move = idx / ref_index - 1
            if abs(move) >= Decimal(str(profile.move_pct)):
                out.append(Trigger("move", {"move": f"{move * 100:.2f}%",
                                            "threshold": f"{profile.move_pct:.2%}",
                                            "ref_index": str(ref_index)}, idx))  # fmt: skip
        if take_profit_pt is not None and take_profit_pt > 0:
            near = Decimal(str(cfg.reeval.tp_near_pct))
            hit = (
                idx >= take_profit_pt * (1 - near)
                if position.direction == "LONG"
                else idx <= take_profit_pt * (1 + near)
            )
            if hit:
                out.append(Trigger("take_profit", {"take_profit_pt": str(take_profit_pt)}, idx))
    if novel_news >= profile.news_min_novel:
        out.append(Trigger("news", {"novel": novel_news, "min": profile.news_min_novel}, idx))
    if position.kind != "FUTURES" and iv is not None and ref_iv and ref_iv > 0:
        if iv >= ref_iv * (1 + cfg.reeval.iv_spike_pct):
            out.append(Trigger("iv_spike", {"iv": iv, "ref_iv": ref_iv}, idx))
    local = now.astimezone(KST)
    if cfg.reeval.periodic_max_per_day > 0 and local.time() >= SESSION_OPEN:
        out.append(Trigger("periodic", {"bucket": _bucket(local, cfg)}, idx))
    if opinion_day is not None and today == opinion_day and local.time() >= EXPIRY_OPINION_AT:
        out.append(Trigger("expiry", {"opinion_day": opinion_day.isoformat()}, idx))
    return sorted(out, key=lambda t: PRIORITY.index(t.kind))


def _bucket(local: datetime, cfg: FoCfg) -> int:
    start = datetime.combine(local.date(), SESSION_OPEN, tzinfo=local.tzinfo)
    return int((local - start).total_seconds() // 60) // cfg.reeval.periodic_min


def _bucket_bounds(now: datetime, cfg: FoCfg) -> tuple[str, str]:
    local = now.astimezone(KST)
    start = datetime.combine(local.date(), SESSION_OPEN, tzinfo=KST)
    lo = start + timedelta(minutes=_bucket(local, cfg) * cfg.reeval.periodic_min)
    hi = lo + timedelta(minutes=cfg.reeval.periodic_min)
    return lo.astimezone(now.tzinfo).isoformat(), hi.astimezone(now.tzinfo).isoformat()


def _fo_rows(
    conn: sqlite3.Connection, account_id: str, code: str, lo: str, hi: str, extra: str = "", *args
):
    """이 계좌·계약의 `fo:*` 트리거(같은 계약을 가진 다른 계좌의 빈도와 섞지 않는다)."""
    return conn.execute(
        "SELECT kind, handled_at, session_id FROM triggers WHERE market = ? AND account_id = ? "
        f"AND ticker = ? AND kind LIKE 'fo:%' AND created_at >= ? AND created_at < ? {extra}",
        (MARKET, account_id, code, lo, hi, *args),
    ).fetchall()


def check(
    conn: sqlite3.Connection,
    *,
    position: FoPositionRow,
    underlying: Decimal | None,
    iv: float | None,
    cfg: FoCfg,
    profile: HorizonProfile,
    now: datetime,
    today: date,
    novel_news: int = 0,
    opinion_day: date | None = None,
    ref_index: Decimal | None = None,
    ref_iv: float | None = None,
    take_profit_pt: Decimal | None = None,
    invalidation_conditions: list[str] | None = None,
) -> Trigger | None:
    """빈도 제한을 통과한 가장 우선인 트리거 하나(나머지는 `detail.bundled` 로 묶음).
    없으면 None."""
    code, acct = position.code, position.account_id
    lo, hi = repo.day_bounds(today, MARKET)
    if _fo_rows(conn, acct, code, lo, hi, "AND handled_at IS NULL"):
        return None  # 대기 중인 재평가가 있다
    done = _fo_rows(conn, acct, code, lo, hi, f"AND handled_at IS NOT NULL AND {_OK}")
    if len(done) >= cfg.reeval.max_per_day:
        return None
    last = max((r["handled_at"] for r in done), default=None)
    if last is not None:
        gap = now - datetime.fromisoformat(last)
        if gap < timedelta(minutes=cfg.reeval.interval_min):
            return None
    done_kinds = [r["kind"] for r in done]
    cands = candidates(
        position=position, underlying=underlying, iv=iv, cfg=cfg, profile=profile, now=now,
        today=today, novel_news=novel_news, opinion_day=opinion_day, ref_index=ref_index,
        ref_iv=ref_iv, take_profit_pt=take_profit_pt,
        invalidation_conditions=invalidation_conditions,
    )  # fmt: skip
    allowed: list[Trigger] = []
    for t in cands:
        lk = ledger_kind(t.kind)
        if t.kind in KINDS_ONCE or t.kind == "expiry":
            if lk in done_kinds:
                continue
        elif t.kind == "periodic":
            if done_kinds.count(lk) >= cfg.reeval.periodic_max_per_day:
                continue
            b_lo, b_hi = _bucket_bounds(now, cfg)
            if _fo_rows(conn, acct, code, b_lo, b_hi, "AND kind = ? AND handled_at IS NOT NULL "
                        f"AND {_OK}", lk):  # fmt: skip
                continue
        allowed.append(t)
    if not allowed:
        return None
    first = allowed[0]
    bundled = [t.kind for t in allowed[1:]]
    return Trigger(first.kind, {**first.detail, "bundled": bundled} if bundled else first.detail,
                   first.price)  # fmt: skip


def record(
    conn: sqlite3.Connection, position: FoPositionRow, trigger: Trigger, *, now: datetime
) -> int:
    """`triggers` 에 `fo:<종류>` 행을 남기고 id 를 돌려준다(커밋은 `repo.insert_trigger`)."""
    return repo.insert_trigger(
        conn, market=MARKET, ticker=position.code, kind=ledger_kind(trigger.kind),
        detail={**trigger.detail, "position_id": position.id}, price=trigger.price,
        decision_id=None, account_id=position.account_id, created_at=now.isoformat(),
    )  # fmt: skip
