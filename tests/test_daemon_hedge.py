"""데몬 ↔ 국면 판정·헤지: scan_analyze 앞 regime 갱신, monitor 첫 틱 헤지 1회, paused 보류."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from conftest import make_account
from test_daemon import _deps

from trader import daemon
from trader.broker.fake import FakeBroker, FakeMarket
from trader.broker.models import Candle, Cash
from trader.config import HedgeCfg, Settings
from trader.control import control
from trader.orchestrator import hedge
from trader.risk import regime

NOW = datetime(2026, 10, 24, 1, 0, tzinfo=UTC)  # 10:00 KST (KR monitor 창 안)
CFG = HedgeCfg(ma_days=20, momentum_days=5, exit_streak_days=1)


def _candles(closes):
    d0 = date(2026, 9, 1)
    return [
        Candle(date=d0 + timedelta(days=i), open=c, high=c, low=c, close=Decimal(str(c)), volume=1)
        for i, c in enumerate(closes)
    ]


class Notes:
    def __init__(self):
        self.msgs = []

    def send(self, text, *, escape=True):
        self.msgs.append(text)
        return True


def _hedge_deps(conn, *, closes, prices=None):
    market = FakeMarket(prices=prices or {}, candles={"069500": _candles(closes)}, now=lambda: NOW)
    deps = _deps(conn, market, now=lambda: NOW)
    deps.settings = Settings(hedge=CFG)
    deps.notifier = Notes()
    acct = make_account("paper-main", "paper", "auto", ["KR", "US"])
    deps.brokers = {
        "paper-main": FakeBroker(
            acct,
            cash={
                "KR": Cash(currency="KRW", available=Decimal("10000000"), total=Decimal("10000000"))
            },
        )
    }
    deps.accounts = [acct]
    return deps


def test_regime_body_updates_flag_and_notifies_transition(conn):
    deps = _hedge_deps(conn, closes=[100] * 30 + [95, 90, 85, 80, 75, 70])
    out = daemon._regime_body(deps, "KR")
    assert out["state"] == "bear" and out["count"] == 2
    assert regime.load(conn, "KR")["state"] == "bear"
    assert any("[국면 전환] KR — → 하락" in m for m in deps.notifier.msgs)
    # 데이터가 없으면 error 로 보고하고 직전 상태 유지
    deps.market_client.candles = {}
    assert "error" in daemon._regime_body(deps, "KR") and regime.load(conn, "KR")["state"] == "bear"
    deps.settings = Settings(hedge=HedgeCfg(enabled=False))
    assert daemon._regime_body(deps, "KR") == {"skipped": "hedge.enabled=false"}


def test_monitor_body_runs_hedge_once_per_day(conn, monkeypatch):
    deps = _hedge_deps(
        conn, closes=[100] * 30 + [95, 90, 85, 80, 75, 70], prices={"114800": Decimal("5000")}
    )
    daemon._regime_body(deps, "KR")
    from trader.orchestrator import monitor as monitor_mod

    monkeypatch.setattr(monitor_mod, "monitor_once", lambda ctx, **kw: {"checked": 0})
    out = daemon._monitor_body(deps, "KR")
    assert out["hedge"]["action"] == "enter" and out["hedge"]["ticker"] == "114800"
    orders = deps.brokers["paper-main"].orders
    assert len(orders) == 1 and orders[0].ticker == "114800" and orders[0].qty == 400
    assert control.get_flag(conn, hedge.done_key("KR", date(2026, 10, 24)))
    out2 = daemon._monitor_body(deps, "KR")
    assert "hedge" not in out2 and len(deps.brokers["paper-main"].orders) == 1


def test_monitor_body_hedge_paused_notifies_once(conn, monkeypatch):
    deps = _hedge_deps(
        conn, closes=[100] * 30 + [95, 90, 85, 80, 75, 70], prices={"114800": Decimal(1)}
    )
    daemon._regime_body(deps, "KR")
    control.set_flag(conn, "paused", True)
    from trader.orchestrator import monitor as monitor_mod

    monkeypatch.setattr(monitor_mod, "monitor_once", lambda ctx, **kw: {"checked": 0})
    daemon._monitor_body(deps, "KR")
    daemon._monitor_body(deps, "KR")
    assert sum("[헤지 보류: 일시정지]" in m for m in deps.notifier.msgs) == 1
    assert deps.brokers["paper-main"].orders == []


def test_hedge_failure_does_not_block_monitor(conn, monkeypatch):
    deps = _hedge_deps(conn, closes=[100] * 30)
    from trader.orchestrator import monitor as monitor_mod

    monkeypatch.setattr(monitor_mod, "monitor_once", lambda ctx, **kw: {"checked": 7})

    def boom(d, m):
        raise RuntimeError("x")

    monkeypatch.setattr(daemon, "_hedge_body", boom)
    out = daemon._monitor_body(deps, "KR")
    assert out["checked"] == 7 and out["hedge"]["error"].startswith("RuntimeError")


# --- Phase 18: 시장 세션 ------------------------------------------------------------


def test_market_view_body_runs_fake_session_once_and_notifies(conn, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE", "1")
    deps = _hedge_deps(conn, closes=[100] * 30 + [95, 90, 85, 80, 75, 70])
    daemon._regime_body(deps, "KR")
    out = daemon._market_view_body(deps, "KR")
    assert out["view"]["stance"] == "RISK_OFF" and out["view"]["hedge_weight"] == 0.1
    assert any("[시장 뷰] KR 위험 회피" in m for m in deps.notifier.msgs)
    assert conn.execute("SELECT COUNT(*) FROM runs WHERE kind='market'").fetchone()[0] == 1
    # 같은 날 다시 부르면 세션을 띄우지 않는다
    assert daemon._market_view_body(deps, "KR")["skipped"] == "오늘 이미 제안 있음"
    # 그 뒤 헤지는 제안 비중(10%)으로 진입
    deps.market_client.prices = {"114800": Decimal("5000")}
    hedge_out = daemon._hedge_body(deps, "KR")
    assert hedge_out["action"] == "enter" and hedge_out["weight"] == 0.1
    assert regime.summary(conn, ["KR"], today=NOW.date())[0]["view"]["stance"] == "RISK_OFF"
    assert "제안 10%" in regime.line(conn, ["KR"], today=NOW.date())


def test_market_view_body_skips_when_disabled_or_cap_hit(conn, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE", "1")
    deps = _hedge_deps(conn, closes=[100] * 36)
    deps.settings = Settings(hedge=HedgeCfg(market_session=False))
    assert "market_session" in daemon._market_view_body(deps, "KR")["skipped"]
    deps.settings = Settings(hedge=CFG)
    monkeypatch.setattr("trader.orchestrator.tools.sessions_today", lambda conn, now: 99)
    out = daemon._market_view_body(deps, "KR")
    assert out["skipped"] == "세션 상한" and not deps.notifier.msgs


def test_scan_analyze_body_calls_market_view_after_regime(conn, monkeypatch):
    deps = _hedge_deps(conn, closes=[100] * 36)
    order = []
    monkeypatch.setattr(daemon, "_regime_body", lambda d, m: order.append("regime"))
    monkeypatch.setattr(daemon, "_market_view_body", lambda d, m: order.append("view"))
    monkeypatch.setattr(daemon, "_scan_body", lambda d, m: order.append("scan"))
    monkeypatch.setattr(daemon, "_analyze_body", lambda d, m, scan_error=None: {"ok": True})
    daemon._scan_analyze_body(deps, "KR")
    assert order == ["regime", "view", "scan"]


def test_market_view_queue_kind(conn):
    from trader import jobs as jobq

    assert jobq.validate("market_view", {"market": "US"}) == {"market": "US"}
    assert jobq.is_heavy("market_view") and jobq.label("market_view", {"market": "US"}) == (
        "US 시장 세션"
    )


def test_market_view_body_respects_market_session_cap(conn, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE", "1")
    deps = _hedge_deps(conn, closes=[100] * 36)
    deps.settings = Settings(hedge=CFG, llm={"market_session_cap": 0})
    out = daemon._market_view_body(deps, "KR")
    assert out["skipped"] == "세션 상한"


def test_reflect_body_matures_market_views(conn, monkeypatch):
    from trader.orchestrator import market_view as mv

    deps = _hedge_deps(conn, closes=[100 - i for i in range(10)])
    monkeypatch.setattr(deps, "snapshot_fn", None)
    monkeypatch.setattr("trader.memory.outcomes.mature", lambda conn, **kw: [])
    monkeypatch.setattr("trader.orchestrator.reflect.pending", lambda conn, **kw: False)
    monkeypatch.setattr("trader.memory.store.promote_and_prune", lambda conn, **kw: {"deleted": 0})
    first_date = deps.market_client.candles["069500"][0].date
    mv.record_view(
        conn,
        today=first_date,
        market="KR",
        view={"stance": "RISK_OFF", "hedge_weight": 0.1, "horizon_days": 2},
    )
    out = daemon._reflect_body(deps)
    assert out["views_matured"] == 1
    assert conn.execute("SELECT hit FROM market_views").fetchone()["hit"] == 1


def test_us_early_closes_rules():
    ec = daemon.us_early_closes(2026)
    assert date(2026, 11, 27) in ec  # 추수감사절(11/26) 다음 날
    assert date(2026, 12, 24) in ec  # 목요일
    assert date(2026, 7, 3) not in ec  # 금요일: 7/4 토요일 대체 휴장
    ec27 = daemon.us_early_closes(2027)
    assert date(2027, 11, 26) in ec27 and date(2027, 12, 24) not in ec27  # 금요일
    assert len(daemon.us_early_closes(2025)) == 3  # 7/3 목, 11/28, 12/24 수


def test_proxy_candles_drop_pre_open_stub(conn):
    """개장 전 거래량 0 스텁 봉은 국면 proxy 일봉에서 버린다 (리뷰 F15)."""
    from decimal import Decimal as _D

    from trader.broker.models import Candle

    deps = _hedge_deps(conn, closes=[100] * 30)
    good = Candle(
        date=date(2026, 10, 23), open=_D(1), high=_D(1), low=_D(1), close=_D(100), volume=10
    )
    stub = Candle(
        date=date(2026, 10, 24), open=_D(1), high=_D(1), low=_D(1), close=_D(50), volume=0
    )
    deps.market_client = SimpleNamespace(get_daily_kr=lambda *a, **k: [good, stub])
    assert daemon._proxy_candles(deps, "KR") == [good]


def test_regime_apply_view_turns_strategist_risk_off_into_fourth_signal(conn, monkeypatch):
    """Phase 19: 시장 세션 제안이 RISK_OFF(확신 ≥ 기준)면 전략가 신호로 재판정해 1/3 → bear 전환,
    같은 날 재호출 안전, RISK_ON 이면 신호를 끈다."""
    from trader.orchestrator import market_view as mv

    deps = _hedge_deps(conn, closes=[100] * 30 + [99.5, 99, 98.5, 98, 97.5, 97])  # 20일선 아래 1개
    # 전체 검토 L8 뒤 agent_signal 기본 끔 — Phase 19 동작은 켜서 확인
    deps.settings = Settings(hedge=CFG.model_copy(update={"agent_signal": True}))
    out = daemon._regime_body(deps, "KR")
    assert out["state"] == "neutral" and out["count"] == 1
    monkeypatch.setattr(
        mv,
        "load_view",
        lambda conn_, m, d: {"stance": "RISK_OFF", "confidence": 0.8, "hedge_weight": 0.1},
    )
    r = daemon._regime_apply_view(deps, "KR")
    assert r["state"] == "bear" and r["agent_risk_off"] is True
    assert any("[국면 전환] KR 중립 → 하락 (1/3+전략가)" in m for m in deps.notifier.msgs)
    n = len(deps.notifier.msgs)
    assert daemon._regime_apply_view(deps, "KR")["state"] == "bear" and len(deps.notifier.msgs) == n
    assert regime.load(conn, "KR")["streak"] == 0
    # 확신이 낮으면 지표로 쓰지 않는다 → 다시 neutral
    monkeypatch.setattr(
        mv,
        "load_view",
        lambda conn_, m, d: {"stance": "RISK_OFF", "confidence": 0.5, "hedge_weight": 0.1},
    )
    assert daemon._regime_apply_view(deps, "KR")["state"] == "neutral"
    deps.settings = Settings(hedge=HedgeCfg(agent_signal=False))
    assert "agent_signal" in daemon._regime_apply_view(deps, "KR")["skipped"]


def test_scan_analyze_body_applies_view_after_market_session(conn, monkeypatch):
    deps = _hedge_deps(conn, closes=[100] * 36)
    order = []
    monkeypatch.setattr(daemon, "_regime_body", lambda d, m: order.append("regime"))
    monkeypatch.setattr(daemon, "_market_view_body", lambda d, m: order.append("view"))
    monkeypatch.setattr(daemon, "_regime_apply_view", lambda d, m: order.append("apply"))
    monkeypatch.setattr(daemon, "_scan_body", lambda d, m: order.append("scan"))
    monkeypatch.setattr(daemon, "_analyze_body", lambda d, m, scan_error=None: {"ok": True})
    daemon._scan_analyze_body(deps, "KR")
    assert order == ["regime", "view", "apply", "scan"]


def test_regime_body_uses_foreign_flow_only_when_enabled(conn):
    """Phase 21: hedge.flow_signal=true ∧ KR 이면 지수 proxy 외인 수급을 보조 지표로 읽는다.
    꺼져 있으면 KIS 수급 조회를 하지 않는다."""
    from types import SimpleNamespace as NS

    deps = _hedge_deps(conn, closes=[100] * 30 + [95, 90, 85, 80, 75, 70])
    deps.market_client.flows = {"069500": [NS(foreign_net=-5)] * 20}
    out = daemon._regime_body(deps, "KR")
    assert (
        out["signals"]["flow_weak"] is False and "get_investor_kr" not in deps.market_client.calls
    )
    deps.settings = Settings(hedge=CFG.model_copy(update={"flow_signal": True}))
    out = daemon._regime_body(deps, "KR")
    assert out["signals"]["flow_weak"] is True and out["count"] == 3
    assert "get_investor_kr" in deps.market_client.calls
    # 조회 실패는 False(지표 없음), 판정은 계속
    deps.market_client.fail = {"get_investor_kr"}
    out = daemon._regime_body(deps, "KR")
    assert out["signals"]["flow_weak"] is False


def test_monitor_body_runs_reprice_after_open_once(conn, monkeypatch):
    """Phase 23 B: monitor 틱이 개장 after_min 뒤 재호가를 1회 부른다(그 전엔 대기)."""
    from trader.orchestrator import reprice as reprice_mod

    early = datetime(2026, 10, 23, 0, 10, tzinfo=UTC)  # 09:10 KST
    deps = _hedge_deps(conn, closes=[100] * 40)
    deps.settings = Settings()
    deps.now = lambda: early
    assert daemon._reprice_body(deps, "KR") == {"skipped": "개장 뒤 대기"}
    deps.now = lambda: datetime(2026, 10, 23, 0, 40, tzinfo=UTC)  # 09:40 KST
    out = daemon._reprice_body(deps, "KR")
    assert out == {"repriced": [], "skipped": []}
    assert control.get_flag(conn, reprice_mod.done_key("KR", date(2026, 10, 23))) is True
    assert daemon._reprice_body(deps, "KR") == {"skipped": "오늘 이미 실행"}
    deps.dry_run = True
    control.set_flag(conn, reprice_mod.done_key("KR", date(2026, 10, 23)), None)
    assert daemon._reprice_body(deps, "KR") == {"skipped": "비활성"}


def test_reprice_body_quote_fn_returns_full_quote(conn, monkeypatch):
    """2-O5: 데몬의 재호가 quote_fn 은 가격만이 아니라 시세 객체(등락률·종목상태)를 넘긴다."""
    from trader.orchestrator import reprice as reprice_mod

    deps = _hedge_deps(conn, closes=[100] * 40, prices={"005930": Decimal("70300")})
    deps.settings = Settings()
    deps.now = lambda: datetime(2026, 10, 23, 0, 40, tzinfo=UTC)  # 09:40 KST
    seen = {}

    def fake_reprice(ctx, *, quote_fn, **kw):
        seen["q"] = quote_fn("005930", None)
        return {"repriced": [], "skipped": []}

    monkeypatch.setattr(reprice_mod, "reprice", fake_reprice)
    assert daemon._reprice_body(deps, "KR") == {"repriced": [], "skipped": []}
    q = seen["q"]
    assert Decimal(str(q.price)) == Decimal("70300") and hasattr(q, "change_pct")


def test_proxy_candles_drop_today_bar_while_session_open(conn):
    """2-P4(L7): 장중의 오늘 봉은 거래량이 있어도 미완성 — 국면 지표는 D−1 완성 봉까지만."""
    from decimal import Decimal as _D

    deps = _hedge_deps(conn, closes=[100] * 30)
    y = Candle(date=date(2026, 10, 22), open=_D(1), high=_D(1), low=_D(1), close=_D(100), volume=9)
    t = Candle(date=date(2026, 10, 23), open=_D(1), high=_D(1), low=_D(1), close=_D(90), volume=9)
    deps.market_client = SimpleNamespace(get_daily_kr=lambda *a, **k: [y, t])
    deps.now = lambda: datetime(2026, 10, 23, 1, 0, tzinfo=UTC)  # 목 10:00 KST 장중
    assert daemon._proxy_candles(deps, "KR") == [y]
    deps.now = lambda: datetime(2026, 10, 23, 7, 0, tzinfo=UTC)  # 16:00 KST 마감 뒤
    assert daemon._proxy_candles(deps, "KR") == [y, t]


def test_ksd_body_keeps_last_success_and_notifies_when_stale(conn):
    """2-O9: 종류 하나의 수집이 실패하면 그 종류의 지난 일정을 유지하고, 3일 넘게 실패가 이어지면
    [권리 일정 수집 실패] 알림 — 조용히 빈 일정으로 블랙아웃이 꺼지지 않게."""
    from trader.risk import blackout

    class M:
        def __init__(self, fail=()):
            self.fail = set(fail)

        def ksd_schedule(self, kind, start, end):
            if kind in self.fail:
                raise RuntimeError("x")
            return [{"ticker": "005930", "kind": kind, "date": "2026-10-30", "name": "삼성전자"}]

    deps = _hedge_deps(conn, closes=[100] * 30)
    deps.settings = Settings(event_blackout={"enabled": True, "kinds": ["paidin", "rev_split"]})
    deps.market_client = M()
    assert daemon._ksd_body(deps)["events"] == 2
    assert deps.notifier.msgs == []
    deps.market_client = M(fail={"rev_split"})
    deps.now = lambda: NOW + timedelta(days=4)
    out = daemon._ksd_body(deps)
    assert out["events"] == 2 and out["stale"] == ["rev_split"]
    data = control.get_flag(conn, blackout.FLAG)
    assert sorted(e["kind"] for e in data["events"]) == ["paidin", "rev_split"]
    assert any("[권리 일정 수집 실패]" in m and "액면교체" in m for m in deps.notifier.msgs)
