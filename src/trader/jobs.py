"""작업 대기열 (Phase 17). 웹·데몬·CLI 가 요청을 원장 `job_requests` 에 넣고, 데몬의 `job_queue`
interval 잡이 한 번에 하나씩 꺼내 기존 잡 본체로 실행한다. 실행 주체는 데몬뿐이다.

- kind·params 는 여기의 pydantic 모델(`PARAMS`)이 유일한 검증이고, 같은 kind+params 가
  queued|running 이면 `DuplicateRequest`.
- claim 은 조건부 UPDATE(`status='queued'`)라 웹의 enqueue/cancel 과 경합해도 한 번만 실행된다.
- 우선순위(Phase 21): `priority DESC, id` 순으로 꺼낸다. 재평가(monitor 트리거)는 `PRIORITY[kind]`
  로 10, 나머지 0 — 백필·학습 뒤에 재평가가 수십 분 밀리지 않게.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Collection
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from trader.ledger.db import now_iso

Market = Literal["KR", "US"]
Status = Literal["queued", "running", "ok", "error", "skipped"]


class DuplicateRequest(ValueError):
    """같은 kind+params 요청이 이미 대기·실행 중."""


class JobCancelled(Exception):
    """실행 중 종료 신호로 중단됐다 (요청은 skipped)."""


class _P(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FeaturesBackfillParams(_P):
    market: Market | None = None
    years: int = Field(3, ge=1, le=4)


class FeaturesSnapshotParams(_P):
    market: Market | None = None


class EmptyParams(_P):
    pass


class UsageProbeParams(_P):
    force: bool = False  # True = 시스템 현황 '지금 확인'(최근 관측 무시), 페이지 접속 요청은 False


class MlTrainParams(_P):
    market: Market | None = None


class RlTrainParams(_P):
    market: Market | None = None
    seeds: int | None = Field(None, ge=1, le=10)


class ScanAnalyzeParams(_P):
    market: Market


class MarketViewParams(_P):
    market: Market


class ReevaluateParams(_P):
    """monitor 가 트리거를 기록한 뒤 재평가 세션을 큐로 넘긴다(손절 점검 틱을 막지 않도록)."""

    market: Market
    ticker: str = Field(min_length=1, max_length=12, pattern=r"^[A-Za-z0-9.\-]+$")
    exchange: Literal["NAS", "NYS", "AMS"] | None = None
    trigger_id: int = Field(ge=1)
    trigger_ids: list[int] = Field(
        default_factory=list
    )  # 같은 틱의 나머지 트리거(같은 세션이 처리)


class AnalyzeTickerParams(_P):
    market: Market
    ticker: str = Field(min_length=1, max_length=12, pattern=r"^[A-Za-z0-9.\-]+$")
    exchange: Literal["NAS", "NYS", "AMS"] | None = None
    dry_run: bool = True

    @field_validator("ticker")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()


class ReflectParams(_P):
    market: Market | None = None


class ReconcileParams(_P):
    market: Market | None = None


class DiscoverParams(_P):
    """장중 뉴스 발굴(Phase 23): scout 1단계 세션(kind=discover)."""

    market: Market


PARAMS: dict[str, type[BaseModel]] = {
    "features_backfill": FeaturesBackfillParams,
    "features_snapshot": FeaturesSnapshotParams,
    "features_derive": EmptyParams,
    "ml_train": MlTrainParams,
    "rl_train": RlTrainParams,
    "scan_analyze": ScanAnalyzeParams,
    "market_view": MarketViewParams,
    "reevaluate": ReevaluateParams,
    "analyze_ticker": AnalyzeTickerParams,
    "reflect": ReflectParams,
    "fills_sync": EmptyParams,
    "news_collect": EmptyParams,
    "usage_probe": UsageProbeParams,
    "reconcile": ReconcileParams,
    "discover": DiscoverParams,
}
KINDS: tuple[str, ...] = tuple(PARAMS)
# kind 기본 우선순위. 손절 뒤 재평가는 시간이 생명이라 앞으로, 나머지는 요청 순서.
PRIORITY: dict[str, int] = {"reevaluate": 10}
# 가벼운 kind(체결 동기화·뉴스 수집)만 heavy 락 밖. 나머지는 세션·KIS·CPU 를 오래 쓴다.
LIGHT_KINDS: frozenset[str] = frozenset({"fills_sync", "news_collect", "reconcile"})
# 정보 레인: 사용량 확인(claude 하위 프로세스, 최대 90초)은 데몬 `job_queue_info` 틱만 꺼낸다 —
# 빠른 레인에 두면 페이지 접속 확인이 체결통보 동기화·손절 재평가를 막는다(GitHub Codex #55)
INFO_KINDS: frozenset[str] = frozenset({"usage_probe"})
HEAVY_KINDS: frozenset[str] = frozenset(
    k for k in KINDS if k not in LIGHT_KINDS and k not in INFO_KINDS
)
# 빠른 레인(전체 검토 P2): heavy 락과 무관하게 데몬 `job_queue_fast` 틱이 꺼낸다 — 백필·scan_analyze
# 가 몇십 분 도는 동안 체결 동기화·뉴스 수집이 뒤로 밀리지 않게
FAST_KINDS: frozenset[str] = LIGHT_KINDS
# 재평가 레인(전체 검토 2차 2-P2): 손절 뒤 재평가는 분 단위가 생명이라 `job_queue_reeval` 틱만
# 꺼낸다 — 뉴스 수집(30분 주기, 수십 초)·대조가 빠른 레인을 잡고 있어도 밀리지 않는다
REEVAL_KINDS: frozenset[str] = frozenset({"reevaluate"})
# 기존 `job_queue` 가 꺼내는 종류 — 다른 레인 종류는 빼서 소비자를 하나로 둔다(두 레인이 같은 종목
# 재평가를 동시에 꺼내 `31_decision_r<n>` 를 함께 쓰는 경합 방지, Codex 리뷰)
SLOW_KINDS: frozenset[str] = frozenset(KINDS) - FAST_KINDS - INFO_KINDS - REEVAL_KINDS
_KIND_LABEL = {
    "features_backfill": "피처 백필",
    "features_snapshot": "피처 스냅샷",
    "features_derive": "파생 열 재계산",
    "ml_train": "퀀트 학습",
    "rl_train": "RL 학습",
    "scan_analyze": "스캔 → 분석",
    "market_view": "시장 세션",
    "reevaluate": "재평가",
    "analyze_ticker": "종목 분석",
    "reflect": "복기",
    "fills_sync": "체결 동기화",
    "news_collect": "뉴스 수집",
    "usage_probe": "Claude 사용량 확인",
    "reconcile": "KIS 대조",
    "discover": "장중 발굴",
}


def validate(kind: str, params: dict | None) -> dict:
    """kind·params 를 검증해 정규화된 dict 로 돌려준다. 잘못되면 한국어 ValueError."""
    model = PARAMS.get(kind)
    if model is None:
        raise ValueError(f"알 수 없는 작업: {kind}")
    try:
        return model.model_validate(params or {}).model_dump()
    except ValidationError as e:
        first = e.errors()[0]
        loc = ".".join(str(x) for x in first.get("loc", ())) or "params"
        raise ValueError(f"{kind} 파라미터 오류 ({loc}): {first.get('msg')}") from e


def canonical(params: dict) -> str:
    return json.dumps(params, sort_keys=True, ensure_ascii=False)


def is_heavy(kind: str) -> bool:
    return kind in HEAVY_KINDS


def job_id_for(kind: str, params: dict) -> str:
    """하트비트·runs 에 쓰는 이름. 스케줄 잡과 같은 작업이면 같은 id 로 적어 잡 표에 섞인다."""
    if kind == "scan_analyze":
        return f"{str(params.get('market', 'KR')).lower()}_scan_analyze"
    if kind == "reconcile" and params.get("market"):
        return f"{str(params['market']).lower()}_reconcile"
    return kind  # ml_train·reflect·fills_sync 는 스케줄 잡 id 와 같다


def label(kind: str, params: dict, name: str | None = None) -> str:
    base = _KIND_LABEL.get(kind, kind)
    market = params.get("market")
    if kind in ("analyze_ticker", "reevaluate"):
        from trader import names

        who = names.label(str(params.get("ticker", "")), name)
        mode = " (모의)" if kind == "analyze_ticker" and params.get("dry_run", True) else ""
        return f"{base} {who}{mode}"
    if kind == "features_backfill":
        return f"{base} {params.get('years', 3)}년" + (f" {market}" if market else "")
    if kind == "rl_train" and params.get("seeds"):
        return f"{market + ' ' if market else ''}{base} ({params['seeds']}시드)"
    return f"{market} {base}" if market else base


def _iso(now: datetime | None) -> str:
    return now.astimezone(UTC).isoformat() if now is not None else now_iso()


def enqueue(
    conn: sqlite3.Connection,
    kind: str,
    params: dict | None,
    *,
    source: str,
    now: datetime | None = None,
    priority: int | None = None,
) -> int:
    """검증 → 중복(queued|running) 검사 → 삽입. 반환은 요청 id. priority 가 None 이면
    `PRIORITY[kind]`(없으면 0)."""
    p = validate(kind, params)
    prio = PRIORITY.get(kind, 0) if priority is None else int(priority)
    key = canonical(p)
    if conn.in_transaction:
        conn.commit()
    # 중복 검사와 삽입을 한 쓰기 잠금 안에서 — 웹·텔레그램이 동시에 같은 요청을 넣어도 하나만
    # 들어간다(전체 검토 P5)
    conn.execute("BEGIN IMMEDIATE")
    try:
        dup = conn.execute(
            "SELECT id FROM job_requests WHERE kind=? AND params=? "
            "AND status IN ('queued','running') LIMIT 1",
            (kind, key),
        ).fetchone()
        if dup is not None:
            raise DuplicateRequest(f"같은 작업이 이미 대기·실행 중이에요 (#{dup['id']})")
        cur = conn.execute(
            "INSERT INTO job_requests(kind, params, status, source, requested_at, priority) "
            "VALUES (?, ?, 'queued', ?, ?, ?)",
            (kind, key, source, _iso(now), prio),
        )
    except BaseException:
        conn.rollback()
        raise
    conn.commit()
    return int(cur.lastrowid)


def claim_next(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
    kinds: Collection[str] | None = None,
) -> sqlite3.Row | None:
    """우선순위가 높은 것, 같으면 가장 오래된 queued 를 running 으로 바꿔 돌려준다. 다른 연결이
    먼저 가져갔으면 다음 건. `kinds` 가 있으면 그 종류만(빠른 레인)."""
    where, args = "status='queued'", []
    if kinds is not None:
        if not kinds:
            return None
        where += f" AND kind IN ({','.join('?' * len(kinds))})"
        args = sorted(kinds)
    while True:
        row = conn.execute(
            f"SELECT id FROM job_requests WHERE {where} ORDER BY priority DESC, id LIMIT 1", args
        ).fetchone()
        if row is None:
            return None
        cur = conn.execute(
            "UPDATE job_requests SET status='running', started_at=? WHERE id=? AND status='queued'",
            (_iso(now), row["id"]),
        )
        conn.commit()
        if cur.rowcount == 1:
            return conn.execute("SELECT * FROM job_requests WHERE id=?", (row["id"],)).fetchone()


def set_progress(conn: sqlite3.Connection, req_id: int, text: str) -> None:
    conn.execute("UPDATE job_requests SET progress=? WHERE id=?", (text[:500], req_id))
    conn.commit()


def finish(
    conn: sqlite3.Connection,
    req_id: int,
    status: Literal["ok", "error", "skipped"],
    *,
    result: dict | None = None,
    error: str | None = None,
    now: datetime | None = None,
) -> None:
    conn.execute(
        "UPDATE job_requests SET status=?, finished_at=?, error=?, result=? WHERE id=?",
        (
            status,
            _iso(now),
            error,
            json.dumps(result, ensure_ascii=False, default=str) if result is not None else None,
            req_id,
        ),
    )
    conn.commit()


def cancel(conn: sqlite3.Connection, req_id: int, *, now: datetime | None = None) -> bool:
    """대기 중(queued)만 취소 → skipped('취소'). 실행 중이면 False."""
    cur = conn.execute(
        "UPDATE job_requests SET status='skipped', finished_at=?, error='취소' "
        "WHERE id=? AND status='queued'",
        (_iso(now), req_id),
    )
    conn.commit()
    return cur.rowcount == 1


def mark_stale_running(
    conn: sqlite3.Connection, *, now: datetime | None = None, reason: str = "데몬 재시작으로 중단"
) -> int:
    """데몬 기동 때: 이전 프로세스가 남긴 running 을 error 로 닫는다(재실행하지 않는다)."""
    cur = conn.execute(
        "UPDATE job_requests SET status='error', finished_at=?, error=? WHERE status='running'",
        (_iso(now), reason),
    )
    conn.commit()
    return cur.rowcount


def expire_queued(conn: sqlite3.Connection, *, older_than: datetime) -> int:
    """오래 대기한 요청은 skipped('만료'). 데몬이 꺼진 동안 쌓인 요청이 한꺼번에 돌지 않도록."""
    cur = conn.execute(
        "UPDATE job_requests SET status='skipped', finished_at=?, error='만료' "
        "WHERE status='queued' AND requested_at < ?",
        (now_iso(), older_than.astimezone(UTC).isoformat()),
    )
    conn.commit()
    return cur.rowcount


def _hydrate(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["params"] = json.loads(d["params"]) if d.get("params") else {}
    d["result"] = json.loads(d["result"]) if d.get("result") else None
    return d


def list_recent(conn: sqlite3.Connection, *, limit: int = 30) -> list[dict]:
    """running, 그 다음 queued(실행될 순서 = priority DESC, id), 그 뒤 최근 종료분(최신 순)."""
    active = conn.execute(
        "SELECT * FROM job_requests WHERE status IN ('queued','running') "
        "ORDER BY status='running' DESC, priority DESC, id"
    ).fetchall()
    done = conn.execute(
        "SELECT * FROM job_requests WHERE status NOT IN ('queued','running') "
        "ORDER BY id DESC LIMIT ?",
        (max(0, limit - len(active)),),
    ).fetchall()
    return [_hydrate(r) for r in [*active, *done]]


def active_count(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM job_requests WHERE status IN ('queued','running')"
    ).fetchone()
    return int(row["n"])


def latest_by(conn: sqlite3.Connection, kind: str, *, source: str | None = None) -> dict | None:
    """kind(·source) 의 가장 최근 요청 (부트스트랩 재시도 제한용)."""
    sql = "SELECT * FROM job_requests WHERE kind=?"
    args: list = [kind]
    if source:
        sql += " AND source=?"
        args.append(source)
    row = conn.execute(sql + " ORDER BY id DESC LIMIT 1", args).fetchone()
    return _hydrate(row) if row else None
