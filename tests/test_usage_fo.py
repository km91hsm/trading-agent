"""Phase 26: `fo_resume` 는 보유 재평가라 soft 감속 예외, `fo` 는 새 세션이라 거부."""

from datetime import UTC, datetime

from trader import usage
from trader.config import Settings

NOW = datetime(2026, 10, 27, 1, 0, tzinfo=UTC)


def _info(five: float, seven: float) -> dict:
    return {
        "status": "allowed",
        "unifiedWindows": {
            "five_hour": {"utilization": five, "resetsAt": int(NOW.timestamp()) + 3600},
            "seven_day": {"utilization": seven, "resetsAt": int(NOW.timestamp()) + 86400},
        },
    }


def test_fo_resume_allowed_under_soft_throttle(conn):
    assert usage.RESUME_KINDS == frozenset({"resume", "fo_resume"})
    usage.record_rate_limit(conn, _info(0.85, 0.5), now=NOW)
    s = Settings()
    ok, why = usage.session_allowance(conn, s, kind="fo", now=NOW)
    assert not ok and "fo 생략" in why
    assert usage.session_allowance(conn, s, kind="fo_resume", now=NOW) == (True, None)
    usage.record_rate_limit(conn, _info(0.96, 0.5), now=NOW)
    assert not usage.session_allowance(conn, s, kind="fo_resume", now=NOW)[0]
