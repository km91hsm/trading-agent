"""Claude 사용량: 상태줄 관측 파일(rate_limits) 읽기 + agent_calls 5시간·7일 창 집계."""

import json
from datetime import UTC, datetime, timedelta

from trader import usage

NOW = datetime(2026, 10, 23, 3, 0, tzinfo=UTC)


def test_read_rate_limits_missing_or_broken_is_none(tmp_path):
    assert usage.read_rate_limits(tmp_path / "none.json", NOW) is None
    p = tmp_path / "rl.json"
    p.write_text("{nope", encoding="utf-8")
    assert usage.read_rate_limits(p, NOW) is None
    p.write_text(json.dumps({"at": 1, "rate_limits": None}), encoding="utf-8")
    assert usage.read_rate_limits(p, NOW) is None


def test_read_rate_limits_parses_windows_and_age(tmp_path):
    p = tmp_path / "rl.json"
    at = int((NOW - timedelta(minutes=3)).timestamp())
    resets = int((NOW + timedelta(hours=2)).timestamp())
    p.write_text(
        json.dumps(
            {
                "at": at,
                "rate_limits": {
                    "five_hour": {"used_percentage": 12.4, "resets_at": resets},
                    "seven_day": {"used_percentage": 40},
                    "spend_limit": None,
                },
            }
        ),
        encoding="utf-8",
    )
    got = usage.read_rate_limits(p, NOW)
    assert got["age_s"] == 180 and got["observed_at"].startswith("2026-10-23T02:57")
    reset_iso = datetime.fromtimestamp(resets, UTC).isoformat()
    assert got["five_hour"] == {"used_pct": 12.4, "resets_at": reset_iso}
    assert got["seven_day"] == {"used_pct": 40.0, "resets_at": None}
    assert got["spend_limit"] is None


def test_ledger_windows_counts_sessions_cost_and_excludes_fake(conn):
    def row(agent, model, status, cost, dur, when):
        conn.execute(
            "INSERT INTO agent_calls(agent, model, status, cost_usd, duration_ms, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (agent, model, status, cost, dur, when.isoformat()),
        )

    row("orchestrator", "sonnet", "ok", 1.5, 100_000, NOW - timedelta(hours=1))
    row("orchestrator", "sonnet", "error", 0.2, 20_000, NOW - timedelta(hours=4))
    row("orchestrator", "sonnet", "ok", 2.0, 200_000, NOW - timedelta(days=2))
    row("orchestrator", "fake", "ok", 0.0, 10, NOW - timedelta(minutes=5))  # 집계 제외
    row("orchestrator", "sonnet", "ok", 9.0, 1, NOW - timedelta(days=8))  # 7일 밖
    conn.commit()
    w = usage.ledger_windows(conn, NOW, cap=12)
    assert w["h5"] == {"sessions": 2, "cost_usd": 1.7, "avg_duration_s": 60.0, "errors": 1}
    assert w["d7"] == {"sessions": 3, "cost_usd": 3.7, "avg_duration_s": 106.7, "errors": 1}
    assert w["today"] == {"sessions": 2, "cap": 12}  # KST 오늘(10-23) 안의 비-fake 세션
