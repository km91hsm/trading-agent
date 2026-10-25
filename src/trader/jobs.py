"""작업 대기열 (Phase 17). 웹·데몬·CLI 가 요청을 원장 `job_requests` 에 넣고, 데몬의 `job_queue`
interval 잡이 한 번에 하나씩 꺼내 기존 잡 본체로 실행한다. 실행 주체는 데몬뿐이다.

- kind·params 는 여기의 pydantic 모델(`PARAMS`)이 유일한 검증이고, 같은 kind+params 가
  queued|running 이면 `DuplicateRequest`.
- claim 은 조건부 UPDATE(`status='queued'`)라 웹의 enqueue/cancel 과 경합해도 한 번만 실행된다.
"""

from __future__ import annotations

import json
import sqlite3
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
}
KINDS: tuple[str, ...] = tuple(PARAMS)
# 가벼운 kind(체결 동기화)만 heavy 락 밖. 나머지는 세션·KIS·CPU 를 오래 쓴다.
HEAVY_KINDS: frozenset[str] = frozenset(k for k in KINDS if k != "fills_sync")
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
) -> int:
    """검증 → 중복(queued|running) 검사 → 삽입. 반환은 요청 id."""
    p = validate(kind, params)
    key = canonical(p)
    dup = conn.execute(
        "SELECT id FROM job_requests WHERE kind=? AND params=? AND status IN ('queued','running') "
        "LIMIT 1",
        (kind, key),
    ).fetchone()
    if dup is not None:
        raise DuplicateRequest(f"같은 작업이 이미 대기·실행 중이에요 (#{dup['id']})")
    cur = conn.execute(
        "INSERT INTO job_requests(kind, params, status, source, requested_at) "
        "VALUES (?, ?, 'queued', ?, ?)",
        (kind, key, source, _iso(now)),
    )
    conn.commit()
    return int(cur.lastrowid)


def claim_next(conn: sqlite3.Connection, *, now: datetime | None = None) -> sqlite3.Row | None:
    """가장 오래된 queued 를 running 으로 바꿔 돌려준다. 다른 연결이 먼저 가져갔으면 다음 건."""
    while True:
        row = conn.execute(
            "SELECT id FROM job_requests WHERE status='queued' ORDER BY id LIMIT 1"
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
    """queued·running(오래된 순) 먼저, 그 뒤 최근 종료분(최신 순)."""
    active = conn.execute(
        "SELECT * FROM job_requests WHERE status IN ('queued','running') ORDER BY id"
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
