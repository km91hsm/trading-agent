from datetime import UTC, datetime

from trader.sources import base

T0 = datetime(2026, 10, 9, tzinfo=UTC)


def test_run_section_ok():
    s = base.run_section(lambda: {"x": 1}, now=lambda: T0)
    assert s == {"data": {"x": 1}, "received_at": T0.isoformat(), "error": None}


def test_run_section_error_is_recorded_not_raised():
    def boom():
        raise ValueError("나쁜 값")

    s = base.run_section(boom, now=lambda: T0)
    assert s["data"] is None and s["error"] == "ValueError: 나쁜 값"


def test_run_section_disabled_skips_call():
    called = []
    s = base.run_section(lambda: called.append(1), enabled=False, now=lambda: T0)
    assert s["error"] == "disabled" and not called


def test_skipped_section_reason():
    s = base.skipped_section("phase6", now=lambda: T0)
    assert s["data"] is None and s["error"] == "phase6"
    assert {"disabled", "phase6", "not available for US"} <= set(base.SKIP_REASONS)
