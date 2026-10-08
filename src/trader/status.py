"""STATUS.md 생성기: 새 claude 세션이 읽는 시스템 상태 요약 (≤ 8,000자)."""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from trader.config import AccountConfig, Settings
from trader.control.control import get_account_mode, is_paused
from trader.platform import paths

MAX_CHARS = 8000
_KST = ZoneInfo("Asia/Seoul")
_OPEN = re.compile(r"^\s*- \[ \]\s*(.*)$")
_DONE = re.compile(r"^\s*- \[[xX]\]")


def todo_progress(todo_path: Path) -> dict[str, tuple[int, int]]:
    """`## ...` 절 제목 → (완료, 전체) 체크박스 수."""
    if not todo_path.exists():
        return {}
    result: dict[str, tuple[int, int]] = {}
    current: str | None = None
    for line in todo_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            result[current] = (0, 0)
        elif current is not None:
            done, total = result[current]
            if _DONE.match(line):
                result[current] = (done + 1, total + 1)
            elif _OPEN.match(line):
                result[current] = (done, total + 1)
    return result


def _next_todos(todo_path: Path, limit: int = 5) -> list[str]:
    if not todo_path.exists():
        return []
    items: list[str] = []
    for line in todo_path.read_text(encoding="utf-8").splitlines():
        m = _OPEN.match(line)
        if m:
            items.append(m.group(1).strip())
            if len(items) >= limit:
                break
    return items


def _today_calls(conn: sqlite3.Connection, now: datetime) -> tuple[int, float]:
    start_kst = now.astimezone(_KST).replace(hour=0, minute=0, second=0, microsecond=0)
    start = start_kst.astimezone(UTC).isoformat()
    row = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(cost_usd), 0) AS c FROM agent_calls "
        "WHERE created_at >= ?",
        (start,),
    ).fetchone()
    return int(row["n"]), float(row["c"])


def _recent_decisions(runs_dir: Path, limit: int = 3) -> list[str]:
    """state/runs 에서 최근 30_decision.json 을 수정시각 순으로 읽어 한 줄씩 만든다."""
    import json

    if not runs_dir.exists():
        return []
    files = sorted(
        runs_dir.glob("*/*/*/30_decision.json"), key=lambda p: p.stat().st_mtime, reverse=True
    )[:limit]
    out: list[str] = []
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8")).get("data", {})
        except (OSError, ValueError):
            continue
        date_s, market, ticker = f.parts[-4], f.parts[-3], f.parts[-2]
        out.append(
            f"{date_s} {market} {ticker}: {data.get('action')} "
            f"w={data.get('target_weight')} conf={data.get('confidence')}"
        )
    return out


def build_status(
    conn: sqlite3.Connection,
    settings: Settings,
    accounts: list[AccountConfig],
    todo_path: Path,
    now: datetime | None = None,
) -> str:
    now = now or datetime.now(UTC)
    stamp = now.astimezone(_KST).strftime("%Y-%m-%d %H:%M KST")
    lines = [
        f"# 시스템 상태 (생성: {stamp})",
        "",
        "## 실행 제어",
        f"- 일시정지: {'예' if is_paused(conn) else '아니오'}",
        f"- 토론 라운드: {settings.debate_rounds}",
        f"- 하루 세션 상한: {settings.llm.daily_session_cap}",
        "",
        "## 계좌",
        "| id | env | 모드 | 시장 |",
        "|---|---|---|---|",
    ]
    for a in accounts:
        lines.append(f"| {a.id} | {a.env} | {get_account_mode(conn, a)} | {','.join(a.markets)} |")
    lines += ["", "## 마지막 실행"]
    rows = conn.execute(
        "SELECT kind, market, started_at, status FROM runs ORDER BY id DESC LIMIT 5"
    ).fetchall()
    if rows:
        lines += [
            f"- {r['kind']} {r['market'] or ''} {r['started_at']} → {r['status']}" for r in rows
        ]
    else:
        lines.append("아직 실행 기록 없음")
    n, cost = _today_calls(conn, now)
    lines += ["", "## 오늘 LLM 호출", f"- {n}건, ${cost:.2f}"]
    decisions = _recent_decisions(paths.state_dir() / "runs", limit=3)
    if decisions:
        lines += ["", "## 최근 결정"]
        lines += [f"- {d}" for d in decisions]
    lines += ["", "## 구현 진행도"]
    lines += [f"- {t}: {d}/{n}" for t, (d, n) in todo_progress(todo_path).items()]
    lines += ["", "## 다음 할 일"]
    lines += [f"- {t}" for t in _next_todos(todo_path)]
    text = "\n".join(lines)
    if len(text) >= MAX_CHARS:
        text = text[: MAX_CHARS - 1]
    return text + "\n" if not text.endswith("\n") else text


def write_status(text: str, path: Path | None = None) -> Path:
    path = path or paths.state_dir() / "STATUS.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path
