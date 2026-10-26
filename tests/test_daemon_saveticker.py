"""데몬 ↔ 세이브티커 수집 서비스: 앱이 켜져 있는 동안 서비스를 띄우고(계정 파일이 있을 때만),
죽으면 다시, 종료 때 정리. 장애 감시는 텔레그램, 보관 기간 정리는 운영일 1회."""

from datetime import UTC, datetime

from helpers_web import Sent
from test_daemon import SCHED

from trader import config, daemon
from trader.config import Settings
from trader.saveticker import automation

NOW = datetime(2026, 10, 27, 1, 0, tzinfo=UTC)


class FakeProc:
    def __init__(self):
        self.pid = 4242
        self.code = None

    def poll(self):
        return self.code


def _deps(conn, project, *, collect=True):
    news = config.NewsCfg(saveticker_collect=collect)
    return daemon.DaemonDeps(
        settings=Settings(),
        schedule=SCHED,
        accounts=[],
        brokers={},
        market_client=None,
        conn_factory=lambda: conn,
        now=lambda: NOW,
        notifier=Sent(),
        sources_cfg=config.SourcesConfig.model_validate(
            {"news": news.model_dump(), "edgar": {"user_agent": "t t@t"}}
        ),
        state=project / "state",
    )


def _patch(monkeypatch):
    spawned, monitored = [], []

    def spawn(argv, *, log, env):
        spawned.append((argv, log, env))
        return FakeProc()

    monkeypatch.setattr(daemon.procs, "spawn_detached", spawn)
    monkeypatch.setattr(
        automation,
        "monitor",
        lambda home, *, send: monitored.append(send) or {"health": "healthy", "faults": []},
    )
    return spawned, monitored


def test_job_registered():
    jobs = daemon.build_jobs(SCHED, Settings(), markets=["KR"])
    assert any(j.kind == "saveticker" for j in jobs)


def test_no_credentials_no_service(conn, project, monkeypatch):
    spawned, monitored = _patch(monkeypatch)
    out = daemon._saveticker_body(_deps(conn, project))
    assert out["service"] == "계정 파일 없음" and not spawned and not monitored


def test_starts_once_and_restarts_when_dead(conn, project, monkeypatch):
    spawned, monitored = _patch(monkeypatch)
    (project / "secrets" / "saveticker.env").write_text("x", encoding="utf-8")
    deps = _deps(conn, project)
    out = daemon._saveticker_body(deps)
    argv, log, env = spawned[0]
    assert argv[-2:] == ["saveticker", "service"] and env["TRADER_HOME"] == str(project)
    assert log == project / "state" / "external" / "saveticker" / "runtime" / "daemon-service.log"
    assert out["service"].startswith("시작") and monitored
    assert daemon._saveticker_body(deps)["service"] == "실행 중" and len(spawned) == 1
    deps.saveticker_proc.code = 1  # 죽었다
    daemon._saveticker_body(deps)
    assert len(spawned) == 2
    # 텔레그램 알림은 messages 한 곳의 문구로
    monitored[-1]("수집 장애: news_all")
    assert deps.notifier.msgs[-1].startswith("[세이브티커 수집]")


def test_disabled_by_config(conn, project, monkeypatch):
    spawned, _ = _patch(monkeypatch)
    (project / "secrets" / "saveticker.env").write_text("x", encoding="utf-8")
    assert daemon._saveticker_body(_deps(conn, project, collect=False)) == {"skipped": "꺼짐"}
    assert not spawned


def test_prune_once_per_day(conn, project, monkeypatch):
    _patch(monkeypatch)
    calls = []
    monkeypatch.setattr(
        "trader.saveticker.store.Store.prune",
        lambda self, *, today, keep_days: calls.append((today, keep_days)) or {"revisions": 0},
    )
    (project / "state" / "external" / "saveticker" / "data").mkdir(parents=True)
    deps = _deps(conn, project)
    daemon._saveticker_body(deps)
    daemon._saveticker_body(deps)
    assert calls == [(NOW.date(), 30)]


def test_shutdown_stops_service(conn, project, monkeypatch):
    stopped = []
    monkeypatch.setattr(daemon.procs, "stop_tree", lambda proc, *, grace: stopped.append(grace))
    deps = _deps(conn, project)
    deps.saveticker_proc = FakeProc()
    daemon._stop_saveticker(deps)
    assert stopped == [automation.STOP_GRACE_S] and deps.saveticker_proc is None
    daemon._stop_saveticker(deps)  # 없으면 아무 일 없음
    assert len(stopped) == 1
