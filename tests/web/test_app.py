from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from helpers_web import AUTH, make_deps
from typer.testing import CliRunner

from trader.cli import app
from trader.web.app import create_app


def test_health_and_settings(client):
    r = client.get("/api/system/health")
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["version"]
    s = client.get("/api/system/settings").json()
    assert s["web"]["port"] == 8787 and "KIS_" not in str(s)
    # 2-W7: 토큰 없는 조회 API 는 tailnet 사용자 이메일을 돌려주지 않는다(수만)
    assert "tailscale_users" not in s["web"] and s["web"]["tailscale_users_count"] == 0
    assert s["web"]["realtime_ws"] is True and "tailscale_hosts" not in s["web"]
    assert s["risk"]["max_positions"] == 5 and s["llm"]["budget_usd"] == 5.0


def test_status_markdown_empty_when_missing(client):
    assert client.get("/api/system/status").json()["markdown"] == ""


def test_control_requires_token(client):
    assert client.post("/api/control/pause").status_code == 401
    bad = {"Authorization": "Bearer nope"}
    assert client.post("/api/control/pause", headers=bad).status_code == 401
    r = client.post("/api/control/pause", headers=AUTH)
    assert r.status_code == 200 and r.json()["paused"] is True


def test_control_unavailable_without_configured_token(project, conn):
    c = TestClient(create_app(make_deps(project, token=None)))
    assert c.post("/api/control/pause", headers=AUTH).status_code == 401
    assert c.post("/api/control/pause", headers={"Authorization": "Bearer "}).status_code == 401


def test_root_without_dist_shows_notice(client):
    r = client.get("/")
    assert r.status_code == 200 and "npm run build" in r.text


def test_root_serves_dist_index(project, conn, tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>SPA</html>", encoding="utf-8")
    (dist / "assets" / "a.js").write_text("console.log(1)", encoding="utf-8")
    c = TestClient(create_app(make_deps(project, dist=dist)))
    assert c.get("/").text == "<html>SPA</html>"
    assert c.get("/orders").text == "<html>SPA</html>"
    assert c.get("/assets/a.js").text == "console.log(1)"
    assert c.get("/api/nope").status_code == 404


def test_cli_web_command_registered():
    r = CliRunner().invoke(app, ["web", "--help"])
    assert r.exit_code == 0 and "port" in r.stdout


def test_non_ascii_token_is_compared_not_500(project, conn):
    # HTTP 헤더는 latin-1 이라 비ASCII 토큰은 보낼 수 없다. 설정만 비ASCII 여도 500 이 아니라 401.
    c = TestClient(create_app(make_deps(project, token="토큰")))
    assert c.post("/api/control/pause", headers={"Authorization": "Bearer abc"}).status_code == 401
    assert c.post("/api/control/pause").status_code == 401


def test_dist_root_file_and_traversal(project, conn, tmp_path):
    real = tmp_path / "real_dist"
    (real / "assets").mkdir(parents=True)
    (real / "index.html").write_text("<html>SPA</html>", encoding="utf-8")
    (real / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    link = tmp_path / "dist_link"
    link.symlink_to(real, target_is_directory=True)
    c = TestClient(create_app(make_deps(project, dist=link)))
    assert c.get("/favicon.svg").text == "<svg/>"  # 심볼릭 링크 dist 에서도 루트 파일 제공
    assert c.get("/../pyproject.toml").text == "<html>SPA</html>"
    assert c.get("/%2e%2e/pyproject.toml").text == "<html>SPA</html>"


def test_foreign_host_rejected(client):
    r = client.get("/api/system/health", headers={"Host": "evil.example"})
    assert r.status_code == 400
    assert client.get("/api/system/health", headers={"Host": "localhost:8787"}).status_code == 200


def test_hosts_for_bind_and_wildcard_allows_lan_host(project, conn):
    """--host 0.0.0.0 이면 클라이언트는 LAN IP 를 Host 로 보내므로 바인드 주소를 허용 목록에 넣는
    것으론 전부 400 이 난다. 비루프백 바인드는 모든 Host 를 허용한다(경고는 CLI 가 남긴다)."""
    from trader.cli import _hosts_for_bind

    assert _hosts_for_bind("127.0.0.1") == [] and _hosts_for_bind("localhost") == []
    assert _hosts_for_bind("0.0.0.0") == ["*"] and _hosts_for_bind("192.168.0.10") == ["*"]
    deps = make_deps(project)
    deps.extra_hosts.extend(_hosts_for_bind("0.0.0.0"))
    c = TestClient(create_app(deps))
    assert c.get("/api/system/health", headers={"Host": "192.168.0.10:8787"}).status_code == 200


def test_usage_endpoint_reports_windows_and_observed_limits(client, deps, conn, tmp_path):
    import json

    deps.settings.llm.usage_file = None  # 개발 머신의 ~/.claude/rate_limits.json 에 기대지 않는다
    r = client.get("/api/system/usage").json()
    assert r["rate_limits"] is None and r["windows"]["today"] == {"sessions": 0, "cap": 80}
    assert r["throttle"]["state"] == "ok"
    assert "usage_file" not in r and r["usage_file_present"] is False  # 2-W7: 홈 경로 노출 안 함
    p = tmp_path / "rl.json"
    p.write_text(
        json.dumps({"at": 0, "rate_limits": {"five_hour": {"used_percentage": 3}}}),
        encoding="utf-8",
    )
    deps.settings.llm.usage_file = str(p)
    r = client.get("/api/system/usage").json()
    assert r["rate_limits"]["five_hour"]["used_pct"] == 3.0 and "/usage" in r["note"]
    assert r["usage_file_present"] is True
    assert r["rate_limits"]["source"] == "statusline"
    # Phase 24: 세션 rate_limit_event 가 더 최신이면 그것, 사용률이 높으면 감속 상태
    from trader import usage

    usage.record_rate_limit(
        conn, {"unifiedWindows": {"five_hour": {"utilization": 0.85}}}, now=deps.now()
    )
    r = client.get("/api/system/usage").json()
    assert (
        r["rate_limits"]["source"] == "session"
        and r["rate_limits"]["five_hour"]["used_pct"] == 85.0
    )
    assert r["throttle"]["state"] == "soft" and "보유 재평가만" in r["throttle"]["reason"]


def test_daemon_endpoint_without_daemon_lists_schedule(client, deps):
    from trader.config import ScheduleConfig

    r = client.get("/api/system/daemon").json()
    assert r["alive"] is False and r["jobs"] == []  # schedule 없음 → 예정표 비움
    deps.schedule = ScheduleConfig.model_validate(
        {
            "kr": {
                "scan": "07:30",
                "monitor": {"start": "09:00", "end": "15:30", "interval_s": 60},
            },
            "us": {
                "scan": "21:30",
                "monitor": {"start": "23:30", "end": "06:00", "interval_s": 60},
            },
            "reflect": "16:30",
            "ml_train": {"weekday": "sat", "time": "10:00"},
        }
    )
    r = client.get("/api/system/daemon").json()
    assert any(j["id"] == "kr_scan_analyze" for j in r["jobs"]) and r["pid"] is None


def test_today_includes_scan_summary_with_discovery_reason(client, deps):
    """Phase 21: /api/today.scan 은 picks 사유 + 발굴 촉매, 발굴 한 줄."""
    from trader.orchestrator import run_store

    r = client.get("/api/today", params={"market": "KR"})
    assert r.status_code == 200 and r.json()["scan"] is None
    d = run_store.run_date_for("KR", deps.now())
    run_store.write_checkpoint(
        run_store.market_dir(d, "KR", deps.state, create=True),
        "_scan",
        {
            "picks": [
                {"ticker": "247540", "reason": "screener"},
                {"ticker": "005930", "reason": "x"},
            ],
            "analyze_targets": [{"ticker": "247540"}, {"ticker": "005930"}],
            "cap_hit": False,
            "discovery": {
                "candidates": [
                    {
                        "ticker": "247540",
                        "tier": "listed",
                        "catalyst": "대규모 수주",
                        "name": "에코프로비엠",
                    }
                ],
                "universe": 0,
                "listed": 1,
                "unresolved": 0,
            },
        },
    )
    scan = client.get("/api/today", params={"market": "KR"}).json()["scan"]
    assert scan["targets"] == 2 and scan["cap_hit"] is False
    assert scan["picks"][0] == {
        "ticker": "247540",
        "reason": "screener",
        "discovery": {"tier": "listed", "catalyst": "대규모 수주"},
    }
    assert scan["picks"][1]["discovery"] is None
    assert scan["discovery_line"] == "뉴스 발굴 1(유니버스 0·밖 1·미해결 0)"


def test_today_lists_market_files_and_market_checkpoint_endpoint(client, deps):
    """Phase 21: /api/today.market_files + /api/market-checkpoint 로 시장 단계 파일을 읽는다."""
    from trader.orchestrator import run_store

    d = run_store.run_date_for("KR", deps.now())
    mdir = run_store.market_dir(d, "KR", deps.state, create=True)
    run_store.write_checkpoint(
        mdir, "_market_view", {"schema_version": 1, "data": {"stance": "RISK_OFF"}}
    )
    run_store.write_checkpoint(mdir, "_scan", {"picks": []})
    r = client.get("/api/today", params={"market": "KR"}).json()
    assert r["market_files"] == ["_market_view", "_scan"]
    cp = client.get("/api/market-checkpoint", params={"market": "KR", "stage": "_market_view"})
    assert cp.status_code == 200 and cp.json()["data"] == {"stance": "RISK_OFF"}
    raw = client.get("/api/market-checkpoint", params={"market": "KR", "stage": "_scan"}).json()
    assert raw["data"] == {"picks": []} and raw["schema_version"] is None
    assert (
        client.get("/api/market-checkpoint", params={"market": "KR", "stage": "_nope"}).status_code
        == 404
    )
    assert (
        client.get("/api/market-checkpoint", params={"market": "KR", "stage": "../x"}).status_code
        == 400
    )


def test_today_lists_news_jobs_for_market_and_day(client, deps, conn):
    """Phase 23: /api/today.news_jobs 는 그 운영일·시장의 source='news' 요청만."""
    from trader import jobs as jobq

    now = deps.now()
    jobq.enqueue(conn, "discover", {"market": "KR"}, source="news", now=now)
    jobq.enqueue(
        conn, "analyze_ticker", {"market": "KR", "ticker": "005930"}, source="news", now=now
    )
    jobq.enqueue(conn, "discover", {"market": "US"}, source="news", now=now)
    jobq.enqueue(conn, "news_collect", {}, source="web", now=now)
    rows = client.get("/api/today", params={"market": "KR"}).json()["news_jobs"]
    assert [(r["kind"], r["ticker"], r["status"]) for r in rows] == [
        ("analyze_ticker", "005930", "queued"),
        ("discover", None, "queued"),
    ]
    assert client.get("/api/today", params={"market": "US"}).json()["news_jobs"][0]["kind"] == (
        "discover"
    )


def test_portfolio_positions_carry_horizon_profile(client, deps, conn):
    """Phase 23 T: 보유 행에 최근 결정의 보유기간 프로필."""
    conn.execute(
        "INSERT INTO positions_snapshot(account_id, market, date, ticker, qty, avg_price, "
        "current_price, eval_amount, nav, cash, created_at) VALUES "
        "('paper-main','KR','2026-10-24','005930',1,'70000','71000','71000','1000000','900000',"
        "'2026-10-24T07:00:00+00:00'),"
        "('paper-main','KR','2026-10-24','000660',1,'100000','100000','100000','1000000',"
        "'900000','2026-10-24T07:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO decisions(date, market, ticker, action, json, created_at) "
        "VALUES ('2026-10-24','KR','005930','BUY','{\"horizon_days\": 5}',"
        "'2026-10-24T00:00:00+00:00')"
    )
    conn.commit()
    r = client.get("/api/portfolio", params={"account_id": "paper-main", "market": "KR"}).json()
    by = {p["ticker"]: p for p in r["snapshot"]["positions"]}
    assert by["005930"]["horizon"] == "short" and by["005930"]["horizon_days"] == 5
    assert by["000660"]["horizon"] is None


def test_system_ops_endpoint_live_and_cached(client, conn):
    """Phase 23 A: /api/system/ops 는 데몬 결과가 없으면 지금 판정, 있으면 그것을 돌려준다."""
    from trader.control import control
    from trader.ops import health

    r = client.get("/api/system/ops").json()
    assert r["source"] == "live" and r["level"] in health.LEVELS and "headline" in r
    control.set_flag(
        conn,
        health.RESULT_FLAG,
        {
            "level": "ok",
            "label": "정상",
            "at": "2026-10-24T01:00:00+00:00",
            "items": [],
            "actions": [],
            "areas": {},
        },
    )
    conn.commit()
    r2 = client.get("/api/system/ops").json()
    assert r2["source"] == "daemon" and r2["headline"].startswith("점검 정상")
    assert client.get("/api/system/ops", params={"live": "true"}).json()["source"] == "live"


def test_decision_trace_endpoint(client, conn):
    """Phase 23 E: /api/decisions/{id}/trace, 없으면 404."""
    from datetime import date

    from helpers_agents import ok_decision

    from trader.ledger import repo

    did = repo.ingest_checkpoint(
        conn, date(2026, 10, 9), "KR", "005930", "30_decision", {"data": ok_decision()}
    )
    conn.commit()
    r = client.get(f"/api/decisions/{did}/trace")
    assert r.status_code == 200 and r.json()["decision"]["id"] == did
    assert r.json()["analysts"] == [] and "stages" in r.json()
    assert client.get("/api/decisions/999/trace").status_code == 404


def test_csv_format_for_orders_decisions_positions(client, conn):
    """Phase 23 G: ?format=csv 는 같은 표를 CSV(BOM) 로."""
    from datetime import date

    from helpers_agents import ok_decision

    from trader.ledger import repo

    repo.ingest_checkpoint(
        conn, date(2026, 10, 9), "KR", "005930", "30_decision", {"data": ok_decision()}
    )
    conn.commit()
    r = client.get("/api/orders", params={"account_id": "paper-main", "format": "csv"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert r.text.startswith("﻿") and "attachment" in r.headers["content-disposition"]
    r2 = client.get("/api/decisions", params={"format": "csv"})
    lines = r2.text.lstrip("﻿").splitlines()
    assert lines[0].startswith("id,date,market,ticker,action") and "005930" in lines[1]
    r3 = client.get(
        "/api/portfolio", params={"account_id": "paper-main", "market": "KR", "format": "csv"}
    )
    assert r3.status_code == 200 and r3.headers["content-type"].startswith("text/csv")
    # 기본은 그대로 JSON
    assert isinstance(client.get("/api/decisions").json(), list)


def _snap(conn, acct, market, ticker, qty, avg, cur, nav, cash, d="2026-10-24"):
    conn.execute(
        "INSERT INTO positions_snapshot(account_id, market, date, ticker, qty, avg_price, "
        "current_price, eval_amount, nav, cash, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            acct,
            market,
            d,
            ticker,
            qty,
            avg,
            cur,
            str(qty * float(cur)) if cur else None,
            nav,
            cash,
            f"{d}T07:00:00+00:00",
        ),
    )


def test_portfolio_position_plan_distances(client, conn):
    """Phase 24: 보유 행에 최근 결정의 계획(익절·손절·지정가)과 현재가 대비 거리."""
    _snap(conn, "paper-main", "KR", "005930", 2, "90", "100", "1000", "800")
    _snap(conn, "paper-main", "KR", "000660", 1, "100", "100", "1000", "800")
    conn.execute(
        "INSERT INTO decisions(date, market, ticker, action, target_weight, json, created_at) "
        "VALUES ('2026-10-24','KR','005930','BUY',0.2,"
        '\'{"horizon_days": 5, "take_profit": 110, "stop_loss": 95, "limit_price": 99,'
        " \"target_weight\": 0.2}','2026-10-24T00:00:00+00:00')"
    )
    conn.commit()
    r = client.get("/api/portfolio", params={"account_id": "paper-main", "market": "KR"}).json()
    by = {p["ticker"]: p for p in r["snapshot"]["positions"]}
    plan = by["005930"]["plan"]
    assert plan["take_profit"] == 110 and plan["stop_loss"] == 95 and plan["horizon"] == "short"
    assert plan["dist_take_profit_pct"] == pytest.approx(0.1)
    assert plan["dist_stop_pct"] == pytest.approx(-0.05)
    assert plan["dist_limit_pct"] == pytest.approx(-0.01)
    assert plan["target_weight"] == 0.2
    assert by["005930"]["weight"] == pytest.approx(0.2)
    assert by["000660"]["plan"] is None


def test_portfolio_overview_per_market(client, conn):
    """Phase 24: 전체 계좌 합산은 시장(통화) × 모의/실전별. 보유·비중·섹터의 분모도 환경별."""
    _snap(conn, "paper-main", "KR", "005930", 2, "90", "100", "1000", "800")
    _snap(conn, "real-main", "KR", "005930", 1, "120", "100", "500", "400")
    _snap(conn, "paper-main", "US", "AAPL", 1, "10", "12", "100", "88")
    conn.execute(
        "INSERT INTO decisions(date, market, ticker, action, json, created_at) "
        "VALUES ('2026-10-24','KR','005930','BUY','{\"horizon_days\": 5}',"
        "'2026-10-24T00:00:00+00:00')"
    )
    conn.commit()
    r = client.get("/api/portfolio/overview").json()
    kr, us = r["markets"]["KR"]["envs"], r["markets"]["US"]["envs"]
    assert Decimal(kr["paper"]["nav"]) == Decimal("1000")
    assert Decimal(kr["real"]["nav"]) == Decimal("500")
    assert Decimal(us["paper"]["nav"]) == Decimal("100") and "real" not in us
    (pp,) = kr["paper"]["positions"]
    (rp,) = kr["real"]["positions"]
    assert pp["qty"] == 2 and rp["qty"] == 1 and rp["accounts"] == ["real-main"]
    assert pp["weight"] == pytest.approx(0.2) and rp["weight"] == pytest.approx(0.2)
    assert pp["horizon"] == "short" and pp["horizon_days"] == 5  # 보유기간 열이 읽는 최상위 필드
    assert Decimal(rp["pnl"]) == Decimal("-20")
    assert r["accounts_missing_snapshot"] == {"KR": [], "US": []}  # real-main 은 KR 전용
    assert r["configured"] == 2


def test_portfolio_overview_same_ticker_merged_within_env(client, conn):
    """같은 환경의 같은 종목은 합치고 평단은 수량 가중."""
    from trader.ledger import views

    class A:
        def __init__(self, id, env):
            self.id, self.env, self.markets = id, env, ["KR"]

    _snap(conn, "p1", "KR", "005930", 2, "90", "100", "1000", "800")
    _snap(conn, "p2", "KR", "005930", 1, "120", "100", "500", "400")
    conn.commit()
    r = views.account_overview(
        conn, [A("p1", "paper"), A("p2", "paper")], sector_of=None, hedge_tickers=lambda m: set()
    )
    (pos,) = r["markets"]["KR"]["envs"]["paper"]["positions"]
    assert pos["qty"] == 3 and Decimal(pos["avg_price"]) == Decimal("100")


def test_portfolio_overview_no_snapshots_lists_missing(client, conn):
    r = client.get("/api/portfolio/overview").json()
    assert r["markets"]["KR"]["envs"] == {}
    assert sorted(r["accounts_missing_snapshot"]["KR"]) == ["paper-main", "real-main"]
    assert r["configured"] == 2


def test_names_search_endpoint(client, conn):
    """Phase 24 단계 2: 이름 부분 일치 검색, 보유 우선."""
    from trader.ledger import repo

    repo.upsert_ticker_name(conn, "KR", "005930", "삼성전자", "inputs")
    repo.upsert_ticker_name(conn, "KR", "009150", "삼성전기", "inputs")
    _snap(conn, "paper-main", "KR", "009150", 1, "100", "100", "1000", "900")
    conn.commit()
    rows = client.get("/api/names/search", params={"q": "삼성", "market": "KR"}).json()
    assert [r["ticker"] for r in rows][:2] == ["009150", "005930"]
    assert rows[0]["source"] == "held"
    assert client.get("/api/names/search", params={"q": ""}).json() == []
    assert client.get("/api/names/search", params={"q": "a", "market": "XX"}).status_code == 400


def test_portfolio_position_drift(client, conn):
    """Phase 25: 보유 행 드리프트(%p) = 현재 비중 − 최근 결정 목표 비중. HOLD 결정은 None."""
    _snap(conn, "paper-main", "KR", "005930", 3, "90", "100", "1000", "700")  # 30%
    _snap(conn, "paper-main", "KR", "000660", 1, "100", "100", "1000", "700")  # 10%
    conn.execute(
        "INSERT INTO decisions(date, market, ticker, action, target_weight, json, created_at) "
        "VALUES ('2026-10-24','KR','005930','BUY',0.2,'{\"target_weight\": 0.2}',"
        "'2026-10-24T00:00:00+00:00'), ('2026-10-24','KR','000660','HOLD',0.0,"
        "'{\"target_weight\": 0.0}','2026-10-24T00:00:00+00:00')"
    )
    conn.commit()
    r = client.get("/api/portfolio", params={"account_id": "paper-main", "market": "KR"}).json()
    by = {p["ticker"]: p for p in r["snapshot"]["positions"]}
    assert by["005930"]["drift_pp"] == pytest.approx(10.0)
    assert by["000660"]["drift_pp"] is None
    assert r["rebalance_band_pct"] == 0.05


def _req(headers: dict):
    from starlette.requests import Request

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/x",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        }
    )


def test_require_same_origin_checks_origin_and_referer_without_fetch_metadata():
    """2-W9: Sec-Fetch-Site 가 없으면 Origin → Referer 로 같은 출처를 본다. 둘 다 없으면 비브라우저
    호출(테스트·CLI)이라 통과."""
    import pytest
    from fastapi import HTTPException

    from trader.web.routers._common import require_same_origin

    host = {"host": "127.0.0.1:8787"}
    require_same_origin(_req(host))  # 헤더 없음 — 통과
    require_same_origin(_req({**host, "origin": "http://127.0.0.1:8787"}))
    require_same_origin(_req({**host, "referer": "http://127.0.0.1:8787/orders"}))
    require_same_origin(_req({**host, "sec-fetch-site": "same-origin", "origin": "http://x.test"}))
    for bad in (
        {"origin": "http://evil.test"},
        {"origin": "null"},
        {"origin": "http://127.0.0.1:9999"},
        {"referer": "http://evil.test/page"},
        {"sec-fetch-site": "cross-site"},
    ):
        with pytest.raises(HTTPException) as e:
            require_same_origin(_req({**host, **bad}))
        assert e.value.status_code == 403, bad


def test_usage_throttle_toggle_api(client, conn):
    """설정 화면: 사용량 감속 켜기·끄기(토큰), /api/system/usage 가 실효 값과 출처를 보인다."""
    from helpers_web import AUTH

    assert client.post("/api/control/usage-throttle", json={"enabled": False}).status_code == 401
    r = client.post("/api/control/usage-throttle", json={"enabled": False}, headers=AUTH)
    assert r.status_code == 200 and r.json() == {"enabled": False, "source": "control"}
    t = client.get("/api/system/usage").json()["throttle"]
    assert t["enabled"] is False and t["source"] == "control"
    r = client.post("/api/control/usage-throttle", json={"enabled": None}, headers=AUTH)
    assert r.json() == {"enabled": True, "source": "yaml"}
