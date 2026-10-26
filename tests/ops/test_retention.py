"""보존 정리(전체 검토 P12): 오래된 산출물·운영일 플래그·잡 기록만 지운다."""

from datetime import date

from trader.control import control
from trader.ops import retention

TODAY = date(2026, 10, 26)


def test_prune_removes_only_old_items(project, conn):
    runs = project / "state" / "runs"
    (runs / "2026-05-01" / "KR").mkdir(parents=True)
    (runs / "2026-10-20" / "KR").mkdir(parents=True)
    (runs / "not-a-date").mkdir(parents=True)
    control.set_flag(conn, "reconcile:paper-main:KR:2026-05-01", {"x": 1})
    control.set_flag(conn, "reconcile:paper-main:KR:2026-10-20", {"x": 1})
    control.set_flag(conn, "live_confirmed:real-main", {"x": 1})
    for status, at in (
        ("ok", "2026-05-01T00:00:00+00:00"),
        ("queued", "2026-05-01T00:00:00+00:00"),
        ("ok", "2026-10-20T00:00:00+00:00"),
    ):
        conn.execute(
            "INSERT INTO job_requests(kind, params, status, source, requested_at) "
            "VALUES ('fills_sync', '{}', ?, 'web', ?)",
            (status, at),
        )
    old_run = conn.execute(
        "INSERT INTO runs(kind, market, started_at, status) VALUES ('kr_monitor','KR',"
        "'2026-05-01T00:00:00+00:00','error')"
    ).lastrowid
    session_run = conn.execute(
        "INSERT INTO runs(kind, market, started_at, status) VALUES ('analyze','KR',"
        "'2026-05-01T00:00:00+00:00','ok')"
    ).lastrowid
    conn.execute(
        "INSERT INTO agent_calls(run_id, agent, model, status, created_at) "
        "VALUES (?, 'orchestrator', 'opus', 'ok', '2026-05-01T00:00:00+00:00')",
        (session_run,),
    )
    conn.execute(
        "INSERT INTO llm_rate_limits(observed_at, source, raw_json) "
        "VALUES ('2026-05-01T00:00:00+00:00', 'session', '{}')"
    )
    conn.commit()

    out = retention.prune(conn, project / "state", today=TODAY, keep_days=120)
    assert out == {"run_dirs": 1, "control": 1, "job_requests": 1, "runs": 1, "llm_rate_limits": 1}
    assert sorted(p.name for p in runs.iterdir()) == ["2026-10-20", "not-a-date"]
    assert control.get_flag(conn, "live_confirmed:real-main") is not None
    assert control.get_flag(conn, "reconcile:paper-main:KR:2026-10-20") is not None
    left = {r[0] for r in conn.execute("SELECT id FROM runs")}
    assert old_run not in left and session_run in left  # 세션 행이 있는 runs 는 운영일 집계용
    assert (
        conn.execute("SELECT COUNT(*) FROM job_requests WHERE status='queued'").fetchone()[0] == 1
    )
    # 멱등
    assert not any(retention.prune(conn, project / "state", today=TODAY, keep_days=120).values())


def test_prune_keeps_persistent_keys_with_date_like_account_ids(project, conn):
    """계좌 id 에 날짜 모양이 있어도 모드·실전 확인 같은 영구 키는 지우지 않는다(Codex 리뷰)."""
    control.set_flag(conn, "mode:paper-2026-01-01", "off")
    control.set_flag(conn, "live_confirmed:2026-01-01", True)
    control.set_flag(conn, "reconcile:paper-2026-01-01:KR:2026-01-02", {"x": 1})
    out = retention.prune(conn, project / "state", today=TODAY, keep_days=120)
    assert out["control"] == 1
    assert control.get_flag(conn, "mode:paper-2026-01-01") == "off"
    assert control.get_flag(conn, "live_confirmed:2026-01-01") is True


def test_prune_uses_last_date_segment_as_operational_day(project, conn):
    """계좌 id 가 날짜 모양이어도 운영일은 마지막 날짜 세그먼트다(PR #37 리뷰)."""
    keep = "sl_unrecorded:2026-01-01:KR:005930:2026-10-26"
    drop = "sl_unrecorded:2026-01-01:KR:005930:2026-01-02"
    control.set_flag(conn, keep, True)
    control.set_flag(conn, drop, True)
    control.set_flag(conn, "sl_recorded:2026-01-01:KR:005930:2026-10-25:live-auto", True)
    retention.prune(conn, project / "state", today=TODAY, keep_days=120)
    assert control.get_flag(conn, keep) is True
    assert control.get_flag(conn, drop) is None
    assert control.get_flag(conn, "sl_recorded:2026-01-01:KR:005930:2026-10-25:live-auto")
