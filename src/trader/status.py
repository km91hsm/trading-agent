"""STATUS.md 생성기: 새 claude 세션이 읽는 시스템 상태 요약 (≤ 8,000자)."""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from trader import names
from trader.config import KST, AccountConfig, Settings
from trader.control.control import is_paused
from trader.ledger import metrics, repo
from trader.notify.commands import approval_line
from trader.orchestrator.executor import effective_mode
from trader.platform import paths
from trader.risk import regime as regime_mod

MAX_CHARS = 8000
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
    start, _ = repo.day_bounds(now.astimezone(KST).date(), None)
    row = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(cost_usd), 0) AS c FROM agent_calls "
        "WHERE created_at >= ? AND COALESCE(model, '') != 'fake'",
        (start,),
    ).fetchone()
    return int(row["n"]), float(row["c"])


def _recent_decisions(
    runs_dir: Path, limit: int = 3, conn: sqlite3.Connection | None = None
) -> list[str]:
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
        shown = names.display(conn, names.default_cache_dir(), market, ticker)
        out.append(
            f"{date_s} {market} {shown}: {data.get('action')} "
            f"w={data.get('target_weight')} conf={data.get('confidence')}"
        )
    return out


def _perf_lines(conn: sqlite3.Connection, accounts: list[AccountConfig]) -> list[str]:
    from trader.ledger import performance

    out = []
    for a in accounts:
        for m in a.markets:
            met = performance.metrics_of(
                performance.nav_series(conn, a.id, m, days=30, asset=getattr(a, "asset", "stock"))
            )
            if met["days"] == 0:
                continue
            ret = met["return"]
            out.append(
                f"- {a.id} {m}: {'' if ret is None else f'{ret:+.1%}'} "
                f"MDD {'' if met['mdd'] is None else f'{met["mdd"]:.1%}'} ({met['days']}일)"
            )
    return out or ["- 스냅샷 없음"]


def _ops_line(conn: sqlite3.Connection) -> str:
    """운영 점검(Phase 23 A) 머리 한 줄 + 항목 요약."""
    from trader.control import control
    from trader.ops import health

    r = control.get_flag(conn, health.RESULT_FLAG)
    head = health.headline(r)
    if not r or not r.get("items"):
        return head
    return head + " — " + "; ".join(i["text"] for i in r["items"][:3])


def _scalp_lines(settings: Settings, now: datetime) -> list[str]:
    """분 단위 단타(Phase 23 S) 한 줄: 켬/끔 + 오늘 진입·청산 수."""
    from trader.orchestrator.run_store import run_date_for
    from trader.scalp import engine as scalp_engine

    if not settings.scalp.enabled:
        return ["- 끔 (scalp.enabled=false)"]
    out = []
    for m in settings.markets:
        st = scalp_engine.load_state(None, run_date_for(m, now), m)
        halted = f", 중단: {st['halted']}" if st.get("halted") else ""
        out.append(
            f"- {m}: 보유 {len(st.get('open') or [])}·청산 {len(st.get('closed') or [])}{halted}"
        )
    return out


def _regime_lines(conn: sqlite3.Connection, settings: Settings) -> list[str]:
    """시장별 국면(bull|neutral|bear, n/3)과 헤지 설정 한 줄. 헤지가 꺼져 있으면 그 사실만."""
    h = settings.hedge
    if not h.enabled:
        return ["- 헤지 비활성 (hedge.enabled=false)"]
    out = [f"- {regime_mod.line(conn, list(settings.markets))}"]
    inst = ", ".join(
        f"{m} {d.get('inverse').symbol if d.get('inverse') else '-'}"
        + (f"/{d['inverse2x'].symbol}" if h.leverage and d.get("inverse2x") else "")
        for m, d in h.instruments.items()
    )
    out.append(
        f"- 헤지 최대 {h.max_weight_pct:.0%} · 상품 {inst} · bear 기준 {h.bear_min_signals}/3"
    )
    if h.market_session:
        out.append(
            f"- 시장 세션: 매일 scan 앞 1회(거시 애널리스트·강세/약세 토론·전략가), "
            f"제안 비중 {h.min_weight_pct:.0%}~{h.max_weight_pct:.0%} — 진입은 bear 규칙만"
        )
        parts = []
        for r in regime_mod.summary(conn, list(settings.markets)):
            sc = r.get("scorecard")
            if not sc:
                parts.append(f"{r['market']} -")
            elif sc["matured"]:
                parts.append(
                    f"{r['market']} {sc['hits']}/{sc['matured']}({sc['hit_rate']:.0%}, "
                    f"제안 {sc['n']}건)"
                )
            else:
                parts.append(f"{r['market']} 만기 전 {sc['n']}건")
        out.append(f"- 제안 적중률(지수 proxy, 만기 기준): {' · '.join(parts)}")
    return out


def scan_lines(settings: Settings, now: datetime, state: Path | None = None) -> list[str]:
    """시장별 오늘 `_scan.json` 한 줄(picks·분석 대상·뉴스 발굴). 텔레그램 /status 와 공용."""
    from trader.orchestrator import run_store
    from trader.scan import discovery as discovery_mod

    out = []
    for m in settings.markets:
        d = run_store.run_date_for(m, now)
        scan = run_store.read_checkpoint(run_store.market_dir(d, m, state), "_scan")
        if not scan:
            out.append(f"- {m} {d}: 아직 없음")
            continue
        picks = ", ".join(str(p.get("ticker")) for p in scan.get("picks") or []) or "-"
        out.append(
            f"- {m} {d}: picks {picks} · 분석 대상 {len(scan.get('analyze_targets') or [])} · "
            f"{discovery_mod.line(scan.get('discovery'))}"
        )
    return out


def _ml_lines() -> list[str]:
    try:
        from trader.ml import registry

        metas = registry.list_models("quant")
    except Exception as e:  # noqa: BLE001 - 레지스트리 문제가 STATUS 생성을 막지 않는다
        return [f"- 조회 실패: {e}"]
    out = []
    if not metas:
        out.append("- 없음 (`uv run trader ml train` 으로 학습)")
    for m in metas[:3]:
        met = m.get("metrics") or {}
        out.append(
            f"- quant {m.get('version')}: 정확도 {met.get('accuracy', '-')} / 기준선 "
            f"{met.get('baseline', '-')} / 알파 {met.get('alpha', '-')} · "
            f"{'승격' if m.get('promoted') else '미승격'}"
        )
    return out + _rl_lines()


def _rl_lines() -> list[str]:
    try:
        from trader.ml.rl import report

        meta = report.current_model(None)
    except Exception as e:  # noqa: BLE001
        return [f"- rl_sizer 조회 실패: {e}"]
    if meta is None:
        return ["- rl_sizer 없음 (`uv run trader rl train`, 승격은 `rl promote` 수동)"]
    s = meta.get("summary") or {}
    rl, fx, llm = s.get("rl") or {}, s.get("fixed") or {}, s.get("llm") or {}
    return [
        f"- rl_sizer {meta.get('version')}: RL {rl.get('return_mean', '-')} / 고정 "
        f"{fx.get('return_mean', '-')} / LLM {llm.get('return_mean', '-')} · "
        f"{'승격' if meta.get('promoted') else '미승격'}"
    ]


def build_status(
    conn: sqlite3.Connection,
    settings: Settings,
    accounts: list[AccountConfig],
    todo_path: Path,
    now: datetime | None = None,
) -> str:
    now = now or datetime.now(UTC)
    stamp = now.astimezone(KST).strftime("%Y-%m-%d %H:%M KST")
    lines = [
        f"# 시스템 상태 (생성: {stamp})",
        "",
        "## 실행 제어",
        f"- 운영 점검: {_ops_line(conn)}",
        f"- 일시정지: {'예' if is_paused(conn) else '아니오'}",
        f"- 토론 라운드: {settings.debate_rounds}",
        f"- 하루 세션 상한: {settings.llm.daily_session_cap}",
        "",
        "## 계좌",
        "| id | env | 모드 | 시장 | 성향 |",
        "|---|---|---|---|---|",
    ]
    today = now.astimezone(KST).date()
    for a in accounts:
        by_market = metrics.stance_by_market(conn, a.id, today, a.markets)
        st = ", ".join(f"{m} {v}" for m, v in by_market.items())
        mode, note = effective_mode(conn, a)
        shown = f"{mode} (강등: 확인 기록 없음)" if note else mode
        lines.append(f"| {a.id} | {a.env} | {shown} | {','.join(a.markets)} | {st} |")
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
    decisions = _recent_decisions(paths.state_dir() / "runs", limit=3, conn=conn)
    if decisions:
        lines += ["", "## 최근 결정"]
        lines += [f"- {d}" for d in decisions]
    lines += ["", "## 오늘 스캔", *scan_lines(settings, now)]
    lines += ["", "## 국면·헤지", *_regime_lines(conn, settings)]
    lines += ["", "## 단타(모의 전용)", *_scalp_lines(settings, now)]
    from trader.fo import status as fo_status

    lines += ["", "## 파생(선물옵션)", *fo_status.lines(conn, now)]
    lines += ["", "## 성과(30일)", *_perf_lines(conn, accounts)]
    lines += ["", "## ML 모델", *_ml_lines()]
    dist = metrics.decision_distribution(conn, days=30, now=now)
    lines += ["", "## 결정 분포(30일)"]
    if dist:
        lines.append("- " + ", ".join(f"{k} {v}" for k, v in dist.items()))
        conv = metrics.convergence_rate(conn, days=30, now=now)
        if conv is not None:
            lines.append(f"- 토론 수렴율: {conv:.0%}")
    else:
        lines.append("- 결정 없음")
    pending = metrics.pending_approvals(conn)
    lines += ["", "## 미승인 주문"]
    if pending:
        lines += [f"- {approval_line(p)}" for p in pending]
    else:
        lines.append("- 없음")
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
