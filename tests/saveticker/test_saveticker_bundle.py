"""수집기 번들·데몬 연결: 실행 명령(번들은 `trader`), 저장 위치(= 리더 설정), Chromium 첫 실행 설치,
재기동 판단, 저장 공간(조회수 변경 무시·보관 기간 정리)."""

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

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


def test_chromium_ensure_installs_only_when_missing(monkeypatch):
    calls = []
    monkeypatch.setattr(chromium, "installed", lambda: bool(calls))
    monkeypatch.setattr(
        chromium.subprocess,
        "run",
        lambda argv, **kw: calls.append((argv, kw)) or SimpleNamespace(returncode=0),
    )
    assert chromium.ensure() is True
    argv, kw = calls[0]
    assert argv[-2:] == ["install", "chromium"] and kw["shell"] is False
    assert kw["env"]["PW_LANG_NAME"] == "python"
    assert chromium.ensure() is True and len(calls) == 1  # 이미 있으면 설치하지 않는다


def test_chromium_ensure_reports_failure(monkeypatch):
    monkeypatch.setattr(chromium, "installed", lambda: False)
    monkeypatch.setattr(
        chromium.subprocess, "run", lambda argv, **kw: SimpleNamespace(returncode=1)
    )
    assert chromium.ensure() is False


def _service_state(home, value):
    (home / "runtime").mkdir(parents=True, exist_ok=True)
    (home / "runtime" / "service.json").write_text(json.dumps(value), encoding="utf-8")


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
    assert json.loads((tmp_path / "latest" / "news_all.json").read_text())["items"]
    assert store.prune(today=datetime(2026, 10, 27, tzinfo=UTC).date(), keep_days=30) == {
        "revisions": 0,
        "captures": 0,
        "day_dirs": 0,
        "pdfs": 0,
    }


def test_service_blocks_when_chromium_install_fails(tmp_path, monkeypatch):
    """Chromium 을 받지 못하면(오프라인 등) 수집 프로세스를 띄우지 않고 1시간 뒤 다시 시도한다."""
    monkeypatch.setattr(chromium, "ensure", lambda: False)
    automation.service(tmp_path)
    state = automation.read(tmp_path / "runtime" / "service.json")
    assert state["state"] == "blocked" and state["reason"] == "chromium_install_failed"
    assert not (tmp_path / "runtime" / "service.lock").exists()  # 잠금은 풀어 둔다
