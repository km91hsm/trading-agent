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


# --- Phase 24 단계 3: 세션 rate_limit_event 기록·최신 관측·감속 ---------------------------------
from trader.config import Settings  # noqa: E402


def _info(h5: float, d7: float):
    return {
        "status": "allowed",
        "unifiedWindows": {
            "five_hour": {"utilization": h5, "resetsAt": 1792990000},
            "seven_day": {"utilization": d7, "resetsAt": 1793300000},
        },
    }


def _settings(**throttle):
    return Settings(llm={"usage_file": None, "usage_throttle": throttle})


def test_record_and_latest_observation_prefers_newer_source(conn, tmp_path):
    usage.record_rate_limit(conn, _info(0.3, 0.5), now=NOW - timedelta(minutes=10))
    p = tmp_path / "rl.json"
    p.write_text(
        json.dumps(
            {
                "at": int((NOW - timedelta(hours=1)).timestamp()),
                "rate_limits": {"five_hour": {"used_percentage": 99}},
            }
        ),
        encoding="utf-8",
    )
    got = usage.latest_observation(conn, NOW, p)
    assert (
        got["source"] == "session" and got["five_hour"]["used_pct"] == 30.0 and got["age_s"] == 600
    )
    p.write_text(
        json.dumps(
            {
                "at": int((NOW - timedelta(minutes=1)).timestamp()),
                "rate_limits": {"five_hour": {"used_percentage": 99}},
            }
        ),
        encoding="utf-8",
    )
    assert usage.latest_observation(conn, NOW, p)["source"] == "statusline"


def test_record_rate_limit_single_window_type(conn):
    usage.record_rate_limit(conn, {"rateLimitType": "seven_day", "utilization": 0.4}, now=NOW)
    usage.record_rate_limit(conn, {"status": "allowed"}, now=NOW)  # 창 없음 → 기록 안 함
    rows = conn.execute("SELECT five_hour_util, seven_day_util FROM llm_rate_limits").fetchall()
    assert [tuple(r) for r in rows] == [(None, 0.4)]


def test_allowance_soft_blocks_analyze_allows_resume(conn):
    usage.record_rate_limit(conn, _info(0.85, 0.5), now=NOW)
    s = _settings()
    ok, why = usage.session_allowance(conn, s, kind="analyze", now=NOW)
    assert not ok and "5시간" in why and "85%" in why
    assert usage.session_allowance(conn, s, kind="resume", now=NOW) == (True, None)


def test_allowance_hard_blocks_all(conn):
    usage.record_rate_limit(conn, _info(0.2, 0.96), now=NOW)
    ok, why = usage.session_allowance(conn, _settings(), kind="resume", now=NOW)
    assert not ok and "7일" in why


def test_allowance_stale_or_missing_or_disabled_allows(conn):
    s = _settings()
    assert usage.session_allowance(conn, s, kind="analyze", now=NOW) == (True, None)
    usage.record_rate_limit(conn, _info(0.99, 0.99), now=NOW - timedelta(hours=7))
    assert usage.session_allowance(conn, s, kind="analyze", now=NOW) == (True, None)
    usage.record_rate_limit(conn, _info(0.99, 0.99), now=NOW)
    assert usage.session_allowance(conn, _settings(enabled=False), kind="analyze", now=NOW) == (
        True,
        None,
    )


def test_single_window_event_keeps_other_window(conn):
    """7일 90% 뒤 5시간 20% 만 담긴 이벤트가 와도 7일 감속은 유지된다."""
    usage.record_rate_limit(
        conn, {"rateLimitType": "seven_day", "utilization": 0.9}, now=NOW - timedelta(minutes=5)
    )
    usage.record_rate_limit(conn, {"rateLimitType": "five_hour", "utilization": 0.2}, now=NOW)
    obs = usage.latest_observation(conn, NOW, None)
    assert obs["seven_day"]["used_pct"] == 90.0 and obs["five_hour"]["used_pct"] == 20.0
    ok, why = usage.session_allowance(conn, _settings(), kind="analyze", now=NOW)
    assert not ok and "7일" in why


def test_reset_window_no_longer_throttles(conn):
    soon = int((NOW + timedelta(minutes=1)).timestamp())
    usage.record_rate_limit(
        conn,
        {"unifiedWindows": {"five_hour": {"utilization": 0.96, "resetsAt": soon}}},
        now=NOW - timedelta(minutes=30),
    )
    assert usage.session_allowance(conn, _settings(), kind="resume", now=NOW)[0] is False
    later = NOW + timedelta(minutes=2)
    assert usage.session_allowance(conn, _settings(), kind="resume", now=later) == (True, None)


def test_throttle_runtime_flag_overrides_yaml(conn):
    """설정 화면 켜기·끄기(control `usage_throttle`)가 yaml `enabled` 보다 우선, 지우면 yaml 로."""
    from trader.control import control

    usage.record_rate_limit(conn, _info(0.2, 0.96), now=NOW)
    s = _settings()
    assert usage.throttle_enabled(conn, s) == (True, "yaml")
    control.set_flag(conn, usage.THROTTLE_FLAG, False)
    assert usage.throttle_enabled(conn, s) == (False, "control")
    assert usage.session_allowance(conn, s, kind="analyze", now=NOW) == (True, None)
    control.set_flag(conn, usage.THROTTLE_FLAG, None)
    assert not usage.session_allowance(conn, s, kind="analyze", now=NOW)[0]
