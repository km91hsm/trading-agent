"""원장 백업(Stock D-075): SQLite 백업 API 로 일관된 사본, 일별 14·주별(월요일) 8 보존."""

import sqlite3
from datetime import UTC, date, datetime, timedelta

from trader.ledger import backup


def test_backup_copies_consistent_db(conn, project):
    conn.execute("INSERT INTO control(key, value, updated_at) VALUES ('x', '1', 'now')")
    conn.commit()
    out = backup.backup(state=project / "state", now=datetime(2026, 10, 27, 8, 10, tzinfo=UTC))
    assert out.name == "trader-20261027_081000.db" and out.parent.name == "backups"
    copy = sqlite3.connect(out)
    assert copy.execute("SELECT value FROM control WHERE key='x'").fetchone()[0] == "1"
    assert copy.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert not list(out.parent.glob("*.tmp"))


def test_prune_keeps_recent_daily_and_monday_weekly(tmp_path):
    d0 = date(2026, 10, 27)
    for i in range(80):
        day = d0 - timedelta(days=i)
        (tmp_path / f"trader-{day:%Y%m%d}_081000.db").write_bytes(b"x")
    (tmp_path / "unrelated.txt").write_text("keep")
    removed = backup.prune(tmp_path, today=d0, keep_daily=14, keep_weekly=8)
    kept = sorted(p.name for p in tmp_path.glob("trader-*.db"))
    days = {datetime.strptime(n[7:15], "%Y%m%d").date() for n in kept}
    recent = {d0 - timedelta(days=i) for i in range(14)}
    mondays = {d for d in days if d.weekday() == 0 and d not in recent}
    assert recent <= days and len(mondays) == 8 and days == recent | mondays
    assert removed == 80 - len(kept) and (tmp_path / "unrelated.txt").exists()
