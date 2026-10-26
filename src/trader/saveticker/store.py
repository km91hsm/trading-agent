"""SQLite 변경 이력과 복구 가능한 JSONL·최신 파일을 저장한다."""

from __future__ import annotations

import contextlib
import hashlib
import json
import shutil
import sqlite3
import tempfile
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from trader.saveticker.models import Capture

# 뉴스의 참여 수치(조회수 등)는 20초마다 바뀌어 같은 기사가 하루 수백 번 새 버전이 됐다 — 뉴스
# 스트림에서는 디지스트에서 뺀다(커뮤니티·순위 화면은 그 숫자가 정보라 그대로 둔다).
_VOLATILE = ("displayed_time",)
_NEWS_VOLATILE = ("views", "likes", "comment_count")


def _stable(stream: str, item: dict) -> dict:
    drop = _VOLATILE + (_NEWS_VOLATILE if stream.startswith("news_") else ())
    return {k: v for k, v in item.items() if k not in drop}


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def atomic_json(path: Path, value) -> None:
    atomic_text(path, _json(value) + "\n")


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
    ) as f:
        tmp = Path(f.name)
        try:
            f.write(text)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    try:
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


class Store:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            if db.execute("PRAGMA user_version").fetchone()[0] > 1:
                raise ValueError("더 새로운 수집 DB 형식입니다")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS revisions (
                    seq INTEGER PRIMARY KEY, stream TEXT NOT NULL, item_id TEXT NOT NULL,
                    received_at TEXT NOT NULL, digest TEXT NOT NULL, payload TEXT NOT NULL,
                    view TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS revisions_lookup
                    ON revisions(stream, item_id, seq);
                CREATE TABLE IF NOT EXISTS exports (
                    path TEXT PRIMARY KEY, last_seq INTEGER NOT NULL, byte_count INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS captures (
                    seq INTEGER PRIMARY KEY, stream TEXT NOT NULL,
                    observed_at TEXT NOT NULL, payload TEXT NOT NULL
                );
            """)
            db.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in db.execute("PRAGMA table_info(revisions)")}
            if "view" not in columns:
                db.execute("ALTER TABLE revisions ADD COLUMN view TEXT NOT NULL DEFAULT '{}' ")
            db.execute("PRAGMA user_version=1")

    @contextlib.contextmanager
    def _connect(self):
        db = sqlite3.connect(self.root / "feed.sqlite3", timeout=30)
        db.execute("PRAGMA journal_mode=WAL")
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def save(self, capture: Capture) -> dict:
        data = capture.model_dump(mode="json")
        data["observed_at"] = capture.observed_at.isoformat()
        for item in data["items"]:
            for key in ("published_at", "scheduled_at"):
                if item.get(key):
                    item[key] = datetime.fromisoformat(item[key]).astimezone(UTC).isoformat()
        stamp = data["observed_at"]
        changed = 0
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # 같은 스트림의 관측 시각 역행은 최신 파일과 PIT 이력을 오염시키므로 거부한다.
            last = db.execute(
                "SELECT observed_at FROM captures WHERE stream=? ORDER BY seq DESC LIMIT 1",
                (capture.stream,),
            ).fetchone()
            if last and stamp < last[0]:
                raise ValueError("수집 시각이 이전 관측보다 과거입니다")
            if capture.status in ("ok", "partial"):
                for item in data["items"]:
                    stable = _stable(capture.stream, item)
                    digest = hashlib.sha256(_json(stable).encode()).hexdigest()
                    prior = db.execute(
                        "SELECT digest FROM revisions WHERE stream=? AND item_id=? "
                        "ORDER BY seq DESC LIMIT 1",
                        (capture.stream, item["id"]),
                    ).fetchone()
                    if prior and prior[0] == digest:
                        continue
                    db.execute(
                        "INSERT INTO revisions(stream,item_id,received_at,digest,payload,view) "
                        "VALUES (?,?,?,?,?,?)",
                        (
                            capture.stream,
                            item["id"],
                            stamp,
                            digest,
                            _json(item),
                            _json(data["view"]),
                        ),
                    )
                    changed += 1
            db.execute(
                "INSERT INTO captures(stream,observed_at,payload) VALUES (?,?,?)",
                (capture.stream, stamp, _json(data)),
            )
        self.export(incremental=True)
        return {
            "stream": capture.stream,
            "changed": changed,
            "count": len(capture.items),
            "status": capture.status,
        }

    @staticmethod
    def _row(row) -> dict:
        return {
            "revision": row["seq"],
            "stream": row["stream"],
            "received_at": row["received_at"],
            "item": json.loads(row["payload"]),
            "view": json.loads(row["view"]),
        }

    def export(self, *, incremental: bool = False) -> None:
        """새 버전만 추가. 쓰기 중단으로 파일 크기가 다르면 DB에서 해당 파일만 복구한다."""
        with self._connect() as db:
            # 다른 import 프로세스도 같은 DB 잠금을 거쳐 파일·커서를 동시에 갱신한다.
            db.execute("BEGIN IMMEDIATE")
            states = {row["path"]: row for row in db.execute("SELECT * FROM exports")}
            floor = max((row["last_seq"] for row in states.values()), default=0)
            pending = db.execute(
                "SELECT * FROM revisions WHERE seq>? ORDER BY seq", (floor if incremental else 0,)
            ).fetchall()
            groups: dict[str, list] = {}
            for row in pending:
                name = row["received_at"][:10] + "/" + row["stream"].split("_")[0] + ".jsonl"
                groups.setdefault(name, []).append(row)
            for name, state in states.items():
                path = self.root / name
                if not path.exists() or path.stat().st_size != state["byte_count"]:
                    groups.setdefault(name, [])
            for name, pending_rows in groups.items():
                path = self.root / name
                state = states.get(name)
                healthy = (
                    incremental
                    and state
                    and path.exists()
                    and path.stat().st_size == state["byte_count"]
                )
                if healthy:
                    with path.open("a", encoding="utf-8", newline="") as f:
                        for row in pending_rows:
                            f.write(_json(self._row(row)) + "\n")
                    last_seq = max((row["seq"] for row in pending_rows), default=state["last_seq"])
                else:
                    day, filename = name.split("/")
                    kind = filename.removesuffix(".jsonl")
                    rows = db.execute(
                        "SELECT * FROM revisions WHERE substr(received_at,1,10)=? "
                        "AND (stream=? OR stream LIKE ?) ORDER BY seq",
                        (day, kind, kind + "_%"),
                    ).fetchall()
                    atomic_text(path, "".join(_json(self._row(row)) + "\n" for row in rows))
                    last_seq = max((row["seq"] for row in rows), default=0)
                db.execute(
                    "INSERT OR REPLACE INTO exports VALUES (?,?,?)",
                    (name, last_seq, path.stat().st_size),
                )
            latest = db.execute(
                "SELECT payload FROM captures WHERE seq IN "
                "(SELECT MAX(seq) FROM captures GROUP BY stream)"
            ).fetchall()
            good = db.execute(
                "SELECT payload FROM captures WHERE seq IN "
                "(SELECT MAX(seq) FROM captures WHERE "
                "json_extract(payload,'$.status') IN ('ok','partial') "
                "AND json_array_length(payload,'$.items') > 0 GROUP BY stream)"
            ).fetchall()
            successes = {json.loads(row[0])["stream"]: json.loads(row[0]) for row in good}
            for stream, payload in successes.items():
                atomic_json(self.root / "latest" / f"{stream}.json", payload)
            streams = {}
            for row in latest:
                payload = json.loads(row[0])
                stream = payload["stream"]
                streams[stream] = {
                    "view": payload.get("view", {}),
                    "status": payload["status"],
                    "reason": payload["reason"],
                    "last_attempt_at": payload["observed_at"],
                    "last_success_at": successes.get(stream, {}).get("observed_at"),
                    "count": len(payload["items"]),
                    "elapsed_s": payload["elapsed_s"],
                    "target_interval_s": payload["target_interval_s"],
                    "overrun_s": max(0, payload["elapsed_s"] - payload["target_interval_s"]),
                }
            atomic_json(self.root / "status.json", {"streams": streams})

    def prune(self, *, today: date, keep_days: int) -> dict[str, int]:
        """보관 기간(`keep_days`)보다 오래된 이력을 지운다(멱등): 변경 버전·화면 캡처(스트림별
        마지막 것은 남김 — latest·status 의 원본), 날짜 폴더 JSONL, PDF 원본(추출 텍스트·manifest 는
        작아서 남김). 지운 SQLite 공간은 파일을 줄이지 않고 다음 쓰기가 재사용한다."""
        cutoff = today - timedelta(days=keep_days)
        stamp = datetime.combine(cutoff, time.min, UTC).isoformat()
        out = {"revisions": 0, "captures": 0, "day_dirs": 0, "pdfs": 0}
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            out["revisions"] = db.execute(
                "DELETE FROM revisions WHERE received_at < ?", (stamp,)
            ).rowcount
            out["captures"] = db.execute(
                "DELETE FROM captures WHERE observed_at < ? AND seq NOT IN "
                "(SELECT MAX(seq) FROM captures GROUP BY stream)",
                (stamp,),
            ).rowcount
            # 지운 날짜의 내보내기 커서도 지운다(남기면 export 가 파일이 없다고 보고 다시 만든다)
            db.execute("DELETE FROM exports WHERE substr(path, 1, 10) < ?", (cutoff.isoformat(),))
        for d in sorted(self.root.iterdir()):
            try:
                day = date.fromisoformat(d.name)
            except ValueError:
                continue
            if d.is_dir() and day < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                out["day_dirs"] += 1
        attachments = self.root / "attachments"
        if attachments.is_dir():
            limit = datetime.combine(cutoff, time.min, UTC).timestamp()
            for pdf in attachments.glob("*.pdf"):
                try:
                    if pdf.stat().st_mtime < limit:
                        pdf.unlink()
                        out["pdfs"] += 1
                except OSError:
                    continue
        return out

    def status(self) -> dict:
        p = self.root / "status.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"streams": {}}

    def query(self, *, asof: datetime, ticker: str | None = None, limit: int = 100) -> list[dict]:
        if asof.tzinfo is None:
            raise ValueError("asof에 시간대가 필요합니다")
        stamp = asof.astimezone(UTC).isoformat()
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM revisions WHERE seq IN "
                "(SELECT MAX(seq) FROM revisions WHERE received_at<=? "
                "GROUP BY stream,item_id UNION SELECT MAX(seq) FROM revisions "
                "WHERE received_at<=? AND json_extract(payload,'$.kind')='news' "
                "AND json_extract(payload,'$.detail_status')='ok' GROUP BY item_id) "
                "ORDER BY received_at DESC,seq DESC",
                (stamp, stamp),
            ).fetchall()
            current = db.execute(
                "SELECT stream,payload FROM captures WHERE seq IN "
                "(SELECT MAX(seq) FROM captures WHERE observed_at<=? "
                "AND json_extract(payload,'$.status') IN ('ok','partial') "
                "AND json_array_length(payload,'$.items')>0 GROUP BY stream)",
                (stamp,),
            ).fetchall()
        membership = {
            r["stream"]: {x["id"] for x in json.loads(r["payload"])["items"]} for r in current
        }
        result = []
        seen = {}
        for row in rows:
            item = json.loads(row["payload"])
            if item.get("kind") in ("quote", "event", "trend") and item["id"] not in membership.get(
                row["stream"], set()
            ):
                continue
            if ticker and ticker.upper() not in item.get("tickers", []):
                continue
            if item.get("published_at") and item["published_at"] > stamp:
                continue
            dedup = item["id"] if item.get("kind") in ("news", "post", "comment") else None
            record = {
                "stream": row["stream"],
                "received_at": row["received_at"],
                "item": item,
                "view": json.loads(row["view"]),
            }
            if dedup and dedup in seen:
                index = seen[dedup]
                prior = result[index]["item"]
                if (
                    item.get("kind") == "news"
                    and item.get("detail_status") == "ok"
                    and prior.get("detail_status") != "ok"
                ):
                    result[index] = record
                continue
            if dedup:
                seen[dedup] = len(result)
            result.append(record)
        return sorted(result, key=lambda r: r["received_at"], reverse=True)[:limit]
