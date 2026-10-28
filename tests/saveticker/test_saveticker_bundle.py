"""수집기 번들·데몬 연결: 실행 명령(번들은 `trader`), 저장 위치(= 리더 설정), Chromium 첫 실행 설치,
재기동 판단, 저장 공간(조회수 변경 무시·보관 기간 정리)."""

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from trader.platform import bundle
from trader.saveticker import automation, chromium
from trader.saveticker import home as st_home


def test_cli_argv_dev_and_frozen(tmp_path, monkeypatch):
    assert bundle.cli_argv() == [sys.executable, "-m", "trader"]
    exe_dir = tmp_path / "dist" / "trader"
    (exe_dir / "_internal").mkdir(parents=True)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "trader"))
    assert bundle.cli_argv() == [str(exe_dir / "trader")]
    assert bundle.daemon_argv() == [str(exe_dir / "trader"), "daemon"]


def test_home_follows_reader_config(project):
    """수집기가 쓰는 곳 = 리더가 읽는 곳(`news.saveticker_dir`). 설정이 없으면 기본 폴더."""
    assert st_home() == project / "state" / "external" / "saveticker"
    other = project / "elsewhere"
    src = Path(__file__).resolve().parents[2] / "templates" / "config" / "sources.yaml"
    text = src.read_text(encoding="utf-8").replace(
        "saveticker_dir: state/external/saveticker", f"saveticker_dir: {other}"
    )
    (project / "config" / "sources.yaml").write_text(text, encoding="utf-8")
    assert st_home() == other


class FakeInstall:
    def __init__(self, code, *, hang=False):
        self.code, self.hang, self.terminated = code, hang, False

    def wait(self, timeout=None):
        if self.hang and not self.terminated:
            raise chromium.subprocess.TimeoutExpired("install", timeout)
        return self.code

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.terminated = True


def test_chromium_ensure_installs_only_when_missing(monkeypatch):
    calls = []
    monkeypatch.setattr(chromium, "installed", lambda: bool(calls))

    def popen(argv, **kw):
        calls.append((argv, kw))
        return FakeInstall(0)

    monkeypatch.setattr(chromium.subprocess, "Popen", popen)
    assert chromium.ensure() is True
    argv, kw = calls[0]
    assert argv[-2:] == ["install", "chromium"] and kw["shell"] is False
    assert kw["env"]["PW_LANG_NAME"] == "python"
    assert chromium.ensure() is True and len(calls) == 1  # 이미 있으면 설치하지 않는다


def test_install_and_workers_share_browser_dir(monkeypatch, tmp_path):
    """번들 Playwright 는 PLAYWRIGHT_BROWSERS_PATH 가 없으면 패키지 안을 본다 → 설치 전에 OS 캐시로
    명시해 설치·확인·수집 작업(env 상속)이 같은 곳을 본다. 사용자가 정한 값은 존중."""
    from trader.platform import paths

    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    seen = []
    monkeypatch.setattr(chromium, "installed", lambda: False)

    def popen(argv, **kw):
        seen.append(kw["env"].get("PLAYWRIGHT_BROWSERS_PATH"))
        return FakeInstall(0)

    monkeypatch.setattr(chromium.subprocess, "Popen", popen)
    chromium.ensure()
    assert seen == [str(paths.playwright_browsers_dir())]
    assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == seen[0]
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    chromium.prepare_env()
    assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == str(tmp_path)


def test_chromium_ensure_reports_failure(monkeypatch):
    monkeypatch.setattr(chromium, "installed", lambda: False)
    monkeypatch.setattr(chromium.subprocess, "Popen", lambda argv, **kw: FakeInstall(1))
    assert chromium.ensure() is False


def test_chromium_install_stops_with_service(monkeypatch):
    """설치 중 서비스가 종료되면 설치 프로세스도 끝낸다(고아 방지, Codex 리뷰)."""
    import threading

    proc = FakeInstall(0, hang=True)
    stop = threading.Event()
    stop.set()
    monkeypatch.setattr(chromium, "installed", lambda: False)
    monkeypatch.setattr(chromium.subprocess, "Popen", lambda argv, **kw: proc)
    assert chromium.ensure(stop) is False and proc.terminated


def test_kill_leftovers_targets_live_collector_lanes(tmp_path, monkeypatch):
    """서비스가 강제 종료되면 각자 세션인 수집 작업·브라우저가 남는다 → 러너 pid 그룹을 끝낸다."""
    from trader.platform import procs

    (tmp_path / "runtime").mkdir()
    rows = {
        "runner-news": {"pid": 501, "proc_start": "s501"},
        "runner-market": {"pid": 502, "proc_start": "s502"},  # 수집기 아님
        "runner": {"pid": 503, "proc_start": "old"},  # 재부팅 뒤 다른 프로그램이 같은 pid
        "runner-community": {"pid": 504},  # 시작 시각 기록 없음 → 확인 불가, 건드리지 않음
        "runner-background": {"pid": 505, "proc_start": "s505"},
    }
    for name, row in rows.items():
        (tmp_path / "runtime" / f"{name}.json").write_text(json.dumps(row), "utf-8")
    monkeypatch.setattr(automation, "collector_alive", lambda pid: pid != 502)
    monkeypatch.setattr(procs, "process_start", lambda pid: f"s{pid}")
    killed = []
    monkeypatch.setattr(procs, "kill_group", killed.append)
    assert automation.kill_leftovers(tmp_path) == 2 and sorted(killed) == [501, 505]


def test_calendar_range_rolls_and_views_follow():
    from trader.config import KST
    from trader.saveticker.cli import rolling_range, select_views

    assert rolling_range(datetime(2026, 10, 27, tzinfo=KST)) == ("2026-09", "2027-01")
    assert rolling_range(datetime(2027, 1, 31, tzinfo=KST)) == ("2026-12", "2027-04")
    views = select_views("2026-12", "2027-04", [], only=None, lane="background")
    assert "calendar_2027-04" in {v.stream for v in views}


def _service_state(home, value):
    (home / "runtime").mkdir(parents=True, exist_ok=True)
    (home / "runtime" / "service.json").write_text(json.dumps(value), encoding="utf-8")


@pytest.mark.skipif(os.name == "nt", reason="Windows 는 프로세스 목록을 보지 않는다(항상 False)")
def test_service_elsewhere_reads_process_list(monkeypatch):
    rows = f"  1 /sbin/launchd\n{os.getpid()} python -m trader saveticker service\n"
    monkeypatch.setattr(
        automation.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=rows, returncode=0)
    )
    assert automation.service_elsewhere() is False  # 자기 자신은 빼고
    rows += "  77 /opt/trader/trader saveticker service\n"
    assert automation.service_elsewhere() is True


def test_should_start_rules(tmp_path, monkeypatch):
    monkeypatch.setattr(automation, "service_elsewhere", lambda: False)
    now = datetime(2026, 10, 27, 1, 0, tzinfo=UTC)
    home = tmp_path / "st"
    creds = tmp_path / "saveticker.env"
    assert automation.should_start(home, creds, now=now) == (False, "계정 파일 없음")
    creds.write_text("x", encoding="utf-8")
    assert automation.should_start(home, creds, now=now) == (True, "")
    # 인증 실패로 멈췄으면 계정 파일이 바뀔 때까지 다시 띄우지 않는다(계정 잠김 방지)
    _service_state(
        home,
        {"state": "blocked", "reason": "authentication_or_stopped", "at": now.isoformat()},
    )
    os.utime(creds, (now.timestamp() - 60, now.timestamp() - 60))
    assert automation.should_start(home, creds, now=now)[0] is False
    os.utime(creds, (now.timestamp() + 60, now.timestamp() + 60))
    assert automation.should_start(home, creds, now=now)[0] is True
    # 재기동 한도는 1시간 뒤 다시 시도
    _service_state(home, {"state": "blocked", "reason": "restart_limit", "at": now.isoformat()})
    os.utime(creds, (now.timestamp() - 60, now.timestamp() - 60))
    assert automation.should_start(home, creds, now=now + timedelta(minutes=30))[0] is False
    assert automation.should_start(home, creds, now=now + timedelta(minutes=61))[0] is True
    # 다른 프로세스(예: launchd 서비스)가 서비스 잠금을 쥐고 있으면 띄우지 않는다
    lock = home / "runtime" / "service.lock"
    lock.write_text(json.dumps({"pid": os.getpid(), "proc_start": None}), encoding="utf-8")
    assert automation.should_start(home, creds, now=now + timedelta(hours=2)) == (
        False,
        "이미 실행 중",
    )
    lock.unlink()
    # 다른 폴더의 서비스(예: 저장소 쪽 launchd)가 같은 PC 에서 돌면 같은 계정 이중 로그인을 피한다
    monkeypatch.setattr(automation, "service_elsewhere", lambda: True)
    assert automation.should_start(home, creds, now=now + timedelta(hours=2))[0] is False


def _capture(items, when, stream="news_all"):
    from trader.saveticker.models import Capture

    return Capture(
        stream=stream,
        page_url="https://www.saveticker.com/news",
        observed_at=when,
        items=items,
    )


def test_news_views_change_is_not_a_new_revision(tmp_path):
    """뉴스는 조회수만 바뀐 행을 새 버전으로 쓰지 않는다(하루 1만 줄 중 대부분이 조회수 변경이었다).
    커뮤니티·순위는 그대로 새 버전."""
    from trader.saveticker.store import Store

    store = Store(tmp_path)
    item = {"id": "n1", "title": "제목", "views": "10"}
    assert store.save(_capture([item], "2026-10-25T00:00:00+00:00"))["changed"] == 1
    assert (
        store.save(_capture([item | {"views": "99"}], "2026-10-25T00:01:00+00:00"))["changed"] == 0
    )
    assert (
        store.save(_capture([item | {"title": "정정"}], "2026-10-25T00:02:00+00:00"))["changed"]
        == 1
    )
    row = {"id": "c1", "title": "QQQ", "views": "1"}
    assert (
        store.save(_capture([row], "2026-10-25T00:00:00+00:00", "community_top5"))["changed"] == 1
    )
    assert (
        store.save(_capture([row | {"views": "2"}], "2026-10-25T00:01:00+00:00", "community_top5"))[
            "changed"
        ]
        == 1
    )


def test_prune_keeps_latest_and_recent(tmp_path):
    from trader.saveticker.store import Store

    store = Store(tmp_path)
    old, new = "2026-09-01T00:00:00+00:00", "2026-10-25T00:00:00+00:00"
    store.save(_capture([{"id": "a", "title": "옛"}, {"id": "b", "title": "b1"}], old))
    store.save(_capture([{"id": "b", "title": "b2"}], new))
    (tmp_path / "attachments").mkdir(exist_ok=True)
    pdf, txt = tmp_path / "attachments" / "x.pdf", tmp_path / "attachments" / "x.txt"
    pdf.write_bytes(b"%PDF")
    txt.write_text("발췌", encoding="utf-8")
    stamp = datetime(2026, 9, 1, tzinfo=UTC).timestamp()
    os.utime(pdf, (stamp, stamp))
    out = store.prune(today=datetime(2026, 10, 27, tzinfo=UTC).date(), keep_days=30)
    assert out["revisions"] == 2  # 09-01 의 a·b1 (같은 기사가 다시 보이면 새 버전 한 줄로 돌아온다)
    assert out["captures"] == 1 and out["day_dirs"] == 1 and out["pdfs"] == 1
    assert not (tmp_path / "2026-09-01").exists() and (tmp_path / "2026-10-25").exists()
    assert txt.exists() and not pdf.exists()  # 추출 텍스트는 작아서 남긴다
    assert json.loads((tmp_path / "latest" / "news_all.json").read_text(encoding="utf-8"))["items"]
    assert store.prune(today=datetime(2026, 10, 27, tzinfo=UTC).date(), keep_days=30) == {
        "revisions": 0,
        "captures": 0,
        "day_dirs": 0,
        "pdfs": 0,
    }


def test_service_blocks_when_chromium_install_fails(tmp_path, monkeypatch):
    """Chromium 을 받지 못하면(오프라인 등) 수집 프로세스를 띄우지 않고 1시간 뒤 다시 시도한다."""
    monkeypatch.setattr(chromium, "ensure", lambda stop=None: False)
    automation.service(tmp_path)
    state = automation.read(tmp_path / "runtime" / "service.json")
    assert state["state"] == "blocked" and state["reason"] == "chromium_install_failed"
    assert not (tmp_path / "runtime" / "service.lock").exists()  # 잠금은 풀어 둔다


def test_prune_keeps_last_good_capture_and_latest_files(tmp_path):
    """마지막 캡처가 실패여도 그 전의 마지막 성공 화면은 남긴다 — latest/<stream>.json 의 원본."""
    from trader.saveticker.models import Capture
    from trader.saveticker.store import Store

    store = Store(tmp_path)
    store.save(_capture([{"id": "a", "title": "성공"}], "2026-09-01T00:00:00+00:00"))
    store.save(_capture([{"id": "b", "title": "더 옛 성공"}], "2026-09-01T00:00:01+00:00"))
    failed = Capture(
        stream="news_all",
        page_url="https://www.saveticker.com/news",
        observed_at="2026-09-02T00:00:00+00:00",
        items=[],
        status="error",
        reason="TimeoutError",
    )
    store.save(failed)
    out = store.prune(today=datetime(2026, 10, 27, tzinfo=UTC).date(), keep_days=30)
    assert out["captures"] == 1  # 첫 성공만 지운다(마지막 성공·마지막 캡처는 남김)
    store.export(incremental=True)
    latest = json.loads((tmp_path / "latest" / "news_all.json").read_text(encoding="utf-8"))
    assert latest["items"][0]["title"] == "더 옛 성공"
    assert store.status()["streams"]["news_all"]["status"] == "error"
