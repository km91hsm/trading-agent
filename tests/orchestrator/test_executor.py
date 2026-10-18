from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from helpers_agents import ok_decision

from trader.agents.schemas import Decision
from trader.broker.fake import FakeBroker
from trader.broker.models import Cash
from trader.config import AccountConfig, Settings
from trader.control import control
from trader.ledger import repo
from trader.orchestrator import executor
from trader.orchestrator.run_store import read_checkpoint, run_dir

D = date(2026, 10, 9)
T0 = datetime(2026, 10, 9, 1, 0, tzinfo=UTC)
PRICE = Decimal("71500")


class FakeNotifier:
    def __init__(self):
        self.sent: list[str] = []

    def send(self, text: str, *, escape: bool = True) -> bool:
        self.sent.append(text)
        return True


def _accounts():
    return [
        AccountConfig(id="paper-main", env="paper", cano="1", markets=["KR", "US"], mode="auto"),
        AccountConfig(id="real-main", env="real", cano="2", markets=["KR"], mode="off"),
    ]


def _broker(a, **kw):
    return FakeBroker(
        a,
        cash={
            "KR": Cash(currency="KRW", available=Decimal("10000000"), total=Decimal("10000000")),
            "US": Cash(currency="USD", available=Decimal("10000"), total=Decimal("10000")),
        },
        **kw,
    )


@pytest.fixture
def ectx(project, conn):
    accounts = _accounts()
    brokers = {a.id: _broker(a) for a in accounts}
    return executor.ExecContext(
        settings=Settings(),
        accounts=accounts,
        conn=conn,
        brokers=brokers,
        notifier=FakeNotifier(),
        now=lambda: T0,
    )


def _run(ectx, decision=None, *, dry_run=False, market="KR", exchange=None, price=PRICE):
    dec = Decision(**(decision or ok_decision()))
    repo.ingest_checkpoint(
        ectx.conn, D, market, dec.ticker, "30_decision", {"data": dec.model_dump()}
    )
    return executor.execute(
        ectx, dec, date=D, market=market, price=price, exchange=exchange, dry_run=dry_run
    )


def test_paper_auto_places_order_and_writes_checkpoints(ectx):
    results = _run(ectx)
    by = {r["account_id"]: r for r in results}
    assert by["paper-main"]["approved"] and by["paper-main"]["qty"] == 14  # 100만/지정가 71000
    orders = ectx.brokers["paper-main"].orders
    assert len(orders) == 1 and orders[0].qty == 14 and orders[0].price == Decimal("71000")
    rc = read_checkpoint(run_dir(D, "KR", "005930"), "40_risk_check_paper-main")
    assert rc["data"]["approved"] is True and rc["data"]["mode"] == "auto"
    oc = read_checkpoint(run_dir(D, "KR", "005930"), "50_order_paper-main")
    assert oc["data"]["result"]["accepted"] and oc["data"]["request"]["qty"] == 14
    row = ectx.conn.execute("SELECT status, order_no, mode FROM orders").fetchone()
    assert (row["status"], row["order_no"], row["mode"]) == ("accepted", "FAKE-1", "auto")
    assert ectx.conn.execute("SELECT COUNT(*) FROM risk_checks").fetchone()[0] == 2
    assert any("주문" in m and "paper-main" in m for m in ectx.notifier.sent)
    assert ectx.conn.execute("SELECT COUNT(*) FROM positions_snapshot").fetchone()[0] >= 1


def test_real_off_records_only(ectx):
    results = _run(ectx)
    real = next(r for r in results if r["account_id"] == "real-main")
    assert real["mode"] == "off" and real["order"] is None
    rc = read_checkpoint(run_dir(D, "KR", "005930"), "40_risk_check_real-main")
    assert rc["data"]["mode"] == "off" and isinstance(rc["data"]["approved"], bool)
    assert read_checkpoint(run_dir(D, "KR", "005930"), "50_order_real-main") is None
    assert ectx.brokers["real-main"].orders == []
    rows = ectx.conn.execute("SELECT account_id FROM orders").fetchall()
    assert all(r["account_id"] != "real-main" for r in rows)


def test_real_auto_requires_control_confirmation_record(ectx):
    real = ectx.accounts[1]
    control.set_flag(ectx.conn, "mode:real-main", "auto")  # 확인 문자열 없이 직접 쓴 값
    results = _run(ectx)
    r = next(x for x in results if x["account_id"] == "real-main")
    assert r["mode"] == "off" and "확인" in r["note"] and ectx.brokers["real-main"].orders == []
    control.set_account_mode(ectx.conn, real, "auto", confirm="LIVE")
    assert control.get_flag(ectx.conn, "live_confirmed:real-main")
    results = _run(ectx, ok_decision(ticker="000660"))
    r = next(x for x in results if x["account_id"] == "real-main")
    assert r["mode"] == "auto" and len(ectx.brokers["real-main"].orders) == 1
    control.set_account_mode(ectx.conn, real, "off")  # auto 해제 시 확인 기록도 지운다
    assert not control.get_flag(ectx.conn, "live_confirmed:real-main")


def test_approve_mode_creates_approval_and_notifies(ectx):
    control.set_account_mode(ectx.conn, ectx.accounts[0], "approve")
    results = _run(ectx)
    r = next(x for x in results if x["account_id"] == "paper-main")
    assert r["mode"] == "approve" and r["approval_id"] is not None and r["order"] is None
    pending = repo.pending_approvals(ectx.conn)
    assert len(pending) == 1 and pending[0]["order"]["qty"] == 14
    assert pending[0]["expires_at"] == "2026-10-09T01:30:00+00:00"
    assert ectx.brokers["paper-main"].orders == []
    assert any("/approve" in m for m in ectx.notifier.sent)


def test_dry_run_no_orders(ectx):
    results = _run(ectx, dry_run=True)
    r = next(x for x in results if x["account_id"] == "paper-main")
    assert r["approved"] and r["dry_run"] and r["order"] is None
    assert ectx.brokers["paper-main"].orders == [] and repo.pending_approvals(ectx.conn) == []
    rc = read_checkpoint(run_dir(D, "KR", "005930"), "40_risk_check_paper-main")
    assert rc["data"]["dry_run"] is True
    assert read_checkpoint(run_dir(D, "KR", "005930"), "50_order_paper-main") is None


def test_broker_failure_recorded_and_continues(ectx):
    second = AccountConfig(id="paper-2", env="paper", cano="3", markets=["KR"], mode="auto")
    ectx.accounts.append(second)
    ectx.brokers["paper-main"] = _broker(ectx.accounts[0], fail={"place_order"})
    ectx.brokers["paper-2"] = _broker(second)
    results = _run(ectx)
    by = {r["account_id"]: r for r in results}
    assert (
        by["paper-main"]["order"]["accepted"] is False
        and "가짜 실패" in by["paper-main"]["order"]["message"]
    )
    assert by["paper-2"]["order"]["accepted"] is True
    oc = read_checkpoint(run_dir(D, "KR", "005930"), "50_order_paper-main")
    assert oc["data"]["result"]["accepted"] is False
    rows = {r["account_id"]: r["status"] for r in ectx.conn.execute("SELECT * FROM orders")}
    assert rows == {"paper-main": "failed", "paper-2": "accepted"}


def test_balance_failure_rejects_account(ectx):
    ectx.brokers["paper-main"] = _broker(ectx.accounts[0], fail={"get_cash"})
    results = _run(ectx)
    r = next(x for x in results if x["account_id"] == "paper-main")
    assert r["approved"] is False and any("잔고" in s for s in r["reasons"])
    assert ectx.brokers["paper-main"].orders == []


def test_missing_broker_in_auto_is_rejected(ectx):
    """브로커가 없으면 잔고를 알 수 없으므로 판정 자체가 거부로 기록된다 (주문 없음)."""
    del ectx.brokers["paper-main"]
    results = _run(ectx)
    r = next(x for x in results if x["account_id"] == "paper-main")
    assert r["approved"] is False and r["order"] is None
    assert any("브로커" in s for s in r["reasons"])


def test_today_pnl_from_previous_snapshot_blocks_buy(ectx):
    repo.snapshot_positions(
        ectx.conn,
        account_id="paper-main",
        d=date(2026, 10, 8),
        market="KR",
        nav=Decimal("10800000"),
        cash=Decimal("10800000"),
        positions=[],
    )
    assert repo.latest_nav_before(ectx.conn, "paper-main", D, market="KR") == Decimal("10800000")
    results = _run(ectx)
    r = next(x for x in results if x["account_id"] == "paper-main")
    assert r["approved"] is False and any("일일 손실" in s for s in r["reasons"])


def test_us_order_carries_exchange(ectx):
    results = _run(
        ectx,
        ok_decision(ticker="AAPL", limit_price=200.0, take_profit=230.0, stop_loss=180.0),
        market="US",
        exchange="NAS",
        price=Decimal("200"),
    )
    r = next(x for x in results if x["account_id"] == "paper-main")
    assert r["approved"]
    o = ectx.brokers["paper-main"].orders[0]
    assert o.market == "US" and o.exchange == "NAS" and o.order_type == "LIMIT"


def _pending(ectx) -> int:
    control.set_account_mode(ectx.conn, ectx.accounts[0], "approve")
    results = _run(ectx)
    return next(x for x in results if x["account_id"] == "paper-main")["approval_id"]


def test_approve_executes_order_once(ectx):
    aid = _pending(ectx)
    r = executor.approve(ectx, aid)
    assert r["ok"] and r["order"]["accepted"] and r["order"]["order_no"] == "FAKE-1"
    assert len(ectx.brokers["paper-main"].orders) == 1
    assert repo.get_approval(ectx.conn, aid)["status"] == "approved"
    oc = read_checkpoint(run_dir(D, "KR", "005930"), "50_order_paper-main")
    assert oc["data"]["mode"] == "approve" and oc["data"]["result"]["accepted"]
    row = ectx.conn.execute("SELECT mode, status FROM orders").fetchone()
    assert (row["mode"], row["status"]) == ("approve", "accepted")
    r2 = executor.approve(ectx, aid)  # 두 번째 승인은 거부, 주문 1건 유지
    assert r2["ok"] is False and len(ectx.brokers["paper-main"].orders) == 1
    assert executor.approve(ectx, 999)["ok"] is False


def test_expired_approval_not_executed(ectx):
    aid = _pending(ectx)
    ectx.now = lambda: T0 + timedelta(minutes=31)
    r = executor.approve(ectx, aid)
    assert r["ok"] is False and "만료" in r["error"]
    assert ectx.brokers["paper-main"].orders == []
    assert repo.get_approval(ectx.conn, aid)["status"] == "expired"


def test_reject_and_expire_pass(ectx):
    aid = _pending(ectx)
    assert executor.reject(ectx, aid)["ok"]
    assert repo.get_approval(ectx.conn, aid)["status"] == "rejected"
    assert executor.reject(ectx, aid)["ok"] is False
    aid2 = _pending(ectx)
    ectx.now = lambda: T0 + timedelta(hours=1)
    assert executor.expire_approvals(ectx) == [aid2]
    assert repo.pending_approvals(ectx.conn) == []


# --- 리뷰 반영 ---------------------------------------------------------------------


def test_resubmit_does_not_duplicate_order(ectx):
    """같은 결정을 두 번 실행해도(세션 재시도·재실행) 계좌당 주문은 1건."""
    from trader.broker.models import Position

    pos = Position(
        ticker="005930",
        market="KR",
        qty=10,
        avg_price=Decimal("70000"),
        current_price=Decimal("71500"),
        eval_amount=Decimal("715000"),
        pnl_pct=0.02,
    )
    ectx.brokers["paper-main"].positions = [pos]
    dec = ok_decision(action="SELL", target_weight=0.0)
    _run(ectx, dec)
    r = _run(ectx, dec)
    assert len(ectx.brokers["paper-main"].orders) == 1
    rr = next(x for x in r if x["account_id"] == "paper-main")
    assert rr["order"] is None and any("이미" in s for s in rr["reasons"])


def test_resubmit_does_not_duplicate_approval(ectx):
    control.set_account_mode(ectx.conn, ectx.accounts[0], "approve")
    _run(ectx)
    _run(ectx)
    assert len(repo.pending_approvals(ectx.conn)) == 1


def test_place_value_error_recorded_and_continues(ectx):
    class BadBroker:
        def __init__(self, inner):
            self.inner = inner

        def get_positions(self, m):
            return self.inner.get_positions(m)

        def get_cash(self, m):
            return self.inner.get_cash(m)

        def place_order(self, req):
            raise ValueError("US 주문에는 exchange 가 필요합니다")

    second = AccountConfig(id="paper-2", env="paper", cano="3", markets=["KR"], mode="auto")
    ectx.accounts.append(second)
    ectx.brokers["paper-2"] = _broker(second)
    ectx.brokers["paper-main"] = BadBroker(_broker(ectx.accounts[0]))
    results = _run(ectx)
    by = {r["account_id"]: r for r in results}
    assert by["paper-main"]["order"]["accepted"] is False
    assert by["paper-2"]["order"]["accepted"] is True
    assert read_checkpoint(run_dir(D, "KR", "005930"), "50_order_paper-main") is not None


def test_approve_failure_marks_status_failed_not_lost(ectx):
    aid = _pending(ectx)
    ectx.brokers["paper-main"] = _broker(ectx.accounts[0], fail={"place_order"})
    r = executor.approve(ectx, aid)
    assert r["ok"] is False
    assert repo.get_approval(ectx.conn, aid)["status"] == "failed"
    assert (
        read_checkpoint(run_dir(D, "KR", "005930"), "50_order_paper-main")["data"]["status"]
        == "failed"
    )


def test_approve_conditional_update_guards_race(ectx):
    aid = _pending(ectx)
    # 다른 프로세스가 먼저 처리한 상황을 흉내: pending 이 아니면 UPDATE 가 0행
    assert repo.decide_approval(ectx.conn, aid, "approved") is True
    assert repo.decide_approval(ectx.conn, aid, "approved") is False
    assert executor.approve(ectx, aid)["ok"] is False
    assert ectx.brokers["paper-main"].orders == []


def test_dual_market_aggregates_are_per_market(ectx):
    """KR 1천만원 스냅샷·주문이 US(1만달러) 판정에 섞이면 안 된다."""
    _run(ectx)  # KR BUY 14주 @71000 = 994,000 KRW 주문 + KR 스냅샷
    assert repo.today_order_amount(ectx.conn, "paper-main", D, market="US") == Decimal(0)
    assert repo.today_order_amount(ectx.conn, "paper-main", D, market="KR") == Decimal("994000")
    r = _run(
        ectx,
        ok_decision(ticker="AAPL", limit_price=200.0, take_profit=230.0, stop_loss=180.0),
        market="US",
        exchange="NAS",
        price=Decimal("200"),
    )
    us = next(x for x in r if x["account_id"] == "paper-main")
    assert us["approved"] and us["qty"] == 5  # 0.1 × 10,000 / 200
    # 다음 날 US 일일 손익은 US NAV 기준 (KR nav 1천만원이 아니라 1만달러)
    assert repo.latest_nav_before(
        ectx.conn, "paper-main", D + timedelta(days=1), market="US"
    ) == Decimal("10000")
    assert repo.latest_nav_before(
        ectx.conn, "paper-main", D + timedelta(days=1), market="KR"
    ) == Decimal("10000000")


def test_risk_check_checkpoint_round_trips_as_verdict(ectx):
    from trader.risk.rules import RiskVerdict

    _run(ectx)
    cp = read_checkpoint(run_dir(D, "KR", "005930"), "40_risk_check_paper-main")
    v = RiskVerdict.model_validate(cp["data"])
    assert v.mode == "auto" and v.dry_run is False and v.stance == "normal"


# --- 일시정지 (Phase 5 리뷰 반영): paused 면 주문·승인 요청·승인 실행을 내지 않는다 ---


def test_paused_blocks_orders_and_approval_requests(ectx):
    control.set_flag(ectx.conn, "paused", True)
    r = _run(ectx)
    p = next(x for x in r if x["account_id"] == "paper-main")
    assert p["order"] is None and p["approval_id"] is None
    assert p["approved"] is True and "일시정지" in (p["note"] or "")
    assert ectx.brokers["paper-main"].orders == [] and repo.pending_approvals(ectx.conn) == []


def test_paused_blocks_approve(ectx):
    aid = _pending(ectx)
    control.set_flag(ectx.conn, "paused", True)
    r = executor.approve(ectx, aid)
    assert r["ok"] is False and "일시정지" in r["error"]
    assert repo.get_approval(ectx.conn, aid)["status"] == "pending"  # 요청은 살아 있다
    control.set_flag(ectx.conn, "paused", False)
    assert executor.approve(ectx, aid)["ok"]


def test_executor_passes_holiday_fn_to_rules(ectx):
    """보유 1거래일(10/8 매수) 인데 10/9 가 휴장이면 is_holiday 가 거래일 계산에 반영된다."""
    from trader.broker.models import Position

    pos = Position(
        ticker="005930",
        market="KR",
        qty=10,
        avg_price=Decimal("70000"),
        current_price=Decimal("71500"),
        eval_amount=Decimal("715000"),
        pnl_pct=0.02,
    )
    ectx.brokers["paper-main"].positions = [pos]
    dec_id = repo.ingest_checkpoint(
        ectx.conn, D, "KR", "005930", "30_decision", {"data": ok_decision()}
    )
    repo.insert_order(
        ectx.conn,
        decision_id=dec_id,
        account_id="paper-main",
        ticker="005930",
        market="KR",
        side="BUY",
        qty=10,
        order_type="LIMIT",
        price=Decimal("70000"),
        status="accepted",
        order_no="0",
        message="",
        mode="auto",
        created_at="2026-10-05T01:00:00+00:00",
    )
    # 10/5(월) 매수 → 10/9: 평일 4일 → 통과. 10/6·10/7·10/8 휴장이면 1일 → 거부
    ectx.holiday_fn = lambda d: d in {date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 8)}
    r = _run(ectx, ok_decision(action="SELL", target_weight=0.0))
    p = next(x for x in r if x["account_id"] == "paper-main")
    assert p["approved"] is False and any("보유" in s for s in p["reasons"])


def test_executor_updates_circuit_breaker_from_nav(ectx):
    """모의 계좌 판정 때 최근 30일 NAV 로 서킷브레이커를 갱신한다 (−10% 드로다운이면 켜짐)."""
    for i in range(10):
        repo.snapshot_positions(
            ectx.conn,
            account_id="paper-main",
            d=D - timedelta(days=12 - i),
            market="KR",
            nav=Decimal("10000000") if i < 5 else Decimal("8800000"),
            cash=Decimal(1),
            positions=[],
        )
    # 브로커 현금 1천만 → 오늘 NAV 1천만: 고점 1천만 대비 0% → 해제 상태여야 함
    _run(ectx)
    assert control.get_flag(ectx.conn, "memory_breaker:KR")["active"] is False
    cash = {"KR": Cash(currency="KRW", available=Decimal("8900000"), total=Decimal("8900000"))}
    ectx.brokers["paper-main"] = FakeBroker(ectx.accounts[0], cash=cash)
    _run(ectx, ok_decision(ticker="000660"))
    b = control.get_flag(ectx.conn, "memory_breaker:KR")
    assert b["active"] is True and "드로다운" in b["reason"]
    assert repo.nav_history(ectx.conn, "paper-main", "KR", days=30)[-1][1] == Decimal("8900000")


def test_build_portfolio_breaker_uses_settings_and_market(ectx):
    from trader.config import MemorySettings
    from trader.memory import store

    acct = ectx.accounts[0]
    repo.snapshot_positions(
        ectx.conn,
        account_id=acct.id,
        d=D - timedelta(days=1),
        market="KR",
        nav=Decimal(100),
        cash=Decimal(100),
        positions=[],
    )
    broker = FakeBroker(
        acct, cash={"KR": Cash(currency="KRW", available=Decimal(97), total=Decimal(97))}
    )
    executor.build_portfolio(
        ectx.conn, broker, acct, "KR", D, memory_settings=MemorySettings(drawdown_stop_pct=-0.02)
    )
    assert store.breaker_state(ectx.conn, "KR") and store.breaker_state(ectx.conn, "US") is None
    executor.build_portfolio(ectx.conn, broker, acct, "KR", D, memory_settings=MemorySettings())
    assert store.breaker_state(ectx.conn, "KR") is None  # 기본 −10% 기준이면 해제


# --- Phase 9: RL 사이저 섀도·적용 (모의 계좌 한정) ---------------------------------------


def _sizer(mode, weight=0.05):
    from trader.config import RlSizerCfg
    from trader.ml.rl import shadow
    from trader.ml.rl.policy import FixedPolicy

    return shadow.Sizer(
        policy=FixedPolicy(weight), version="v1", cfg=RlSizerCfg(enabled=True, mode=mode)
    )


def _shadow_rows(ectx, account_id=None):
    sql = "SELECT * FROM rl_shadow"
    args = ()
    if account_id:
        sql += " WHERE account_id=?"
        args = (account_id,)
    return ectx.conn.execute(sql, args).fetchall()


def test_shadow_mode_records_but_does_not_change_qty(ectx):
    ectx.rl_sizer = _sizer("shadow")
    by = {r["account_id"]: r for r in _run(ectx)}
    assert by["paper-main"]["qty"] == 14  # 변경 없음
    rows = _shadow_rows(ectx)
    assert len(rows) == 1 and rows[0]["account_id"] == "paper-main"
    assert rows[0]["proposed_weight"] == 0.05 and rows[0]["applied"] == 0


def test_apply_mode_changes_paper_qty_and_marks_applied(ectx):
    ectx.rl_sizer = _sizer("apply")
    by = {r["account_id"]: r for r in _run(ectx)}
    assert by["paper-main"]["qty"] == 7  # 50만/지정가 71000
    assert any(r.startswith("RL 사이저 적용") for r in by["paper-main"]["reasons"])
    assert _shadow_rows(ectx)[0]["applied"] == 1


def test_real_account_untouched_even_in_apply_mode(ectx):
    for a in ectx.accounts:
        if a.env == "real":
            control.set_account_mode(ectx.conn, a, "approve")
    ectx.rl_sizer = _sizer("apply")
    by = {r["account_id"]: r for r in _run(ectx)}
    assert not any(r.startswith("RL") for r in by["real-main"]["reasons"])
    assert _shadow_rows(ectx, "real-main") == []


def test_apply_mode_still_capped_by_risk_gate(ectx):
    ectx.rl_sizer = _sizer("apply", weight=0.90)
    by = {r["account_id"]: r for r in _run(ectx)}
    assert by["paper-main"]["qty"] == 35  # 25% 상한 250만/지정가 71000


def test_disabled_or_no_model_is_noop(ectx):
    ectx.rl_sizer = None
    results = _run(ectx)
    assert _shadow_rows(ectx) == [] and results[0]["qty"] == 14


def test_hold_and_sell_skip_rl(ectx):
    ectx.rl_sizer = _sizer("apply")
    _run(ectx, ok_decision(action="HOLD"))
    assert _shadow_rows(ectx) == []


def test_dry_run_does_not_record_shadow(ectx):
    ectx.rl_sizer = _sizer("shadow")
    _run(ectx, dry_run=True)
    assert _shadow_rows(ectx) == []


def test_apply_mode_rejected_by_gate_not_marked_applied(ectx):
    ectx.rl_sizer = _sizer("apply", weight=0.05)
    # 기대수익 < 왕복비용×2 로 게이트가 거부 → 제안은 기록되지만 applied=0
    by = {r["account_id"]: r for r in _run(ectx, ok_decision(take_profit=71100.0))}
    assert by["paper-main"]["approved"] is False
    rows = _shadow_rows(ectx, "paper-main")
    assert len(rows) == 1 and rows[0]["applied"] == 0


def test_stop_loss_not_blocked_by_pending_sell_approval(ectx):
    """approve 계좌에 같은 결정의 SELL 승인 요청이 pending 이어도 손절은 즉시 나간다."""
    from trader.broker.models import Position

    pos = Position(
        ticker="005930",
        market="KR",
        qty=10,
        avg_price=Decimal("70000"),
        current_price=Decimal("71500"),
        eval_amount=Decimal("715000"),
        pnl_pct=0.02,
    )
    ectx.brokers["paper-main"].positions = [pos]
    control.set_account_mode(ectx.conn, ectx.accounts[0], "approve")
    trim = ok_decision(action="TRIM", target_weight=0.0)
    r = _run(ectx, trim)
    assert next(x for x in r if x["account_id"] == "paper-main")["approval_id"] is not None
    sl = Decision(**ok_decision(action="SELL", target_weight=0.0, order_type="MARKET"))
    dec_id = repo.decision_id(ectx.conn, D, "KR", "005930")
    r = executor.execute(
        ectx,
        sl,
        date=D,
        market="KR",
        price=Decimal("60000"),
        dry_run=False,
        is_stop_loss=True,
        decision_id=dec_id,
    )
    mine = next(x for x in r if x["account_id"] == "paper-main")
    assert mine["approved"] and mine["order"]["accepted"]
    assert len(ectx.brokers["paper-main"].orders) == 1


def test_approved_market_order_records_amount_for_cash_deduction(ectx):
    """승인 뒤 낸 KR 시장가 주문도 amount(요청 시 현재가 × 수량)를 남겨 가능금액 차감에 들어간다."""
    control.set_account_mode(ectx.conn, ectx.accounts[0], "approve")
    r = _run(ectx, ok_decision(order_type="MARKET", limit_price=None))
    aid = next(x for x in r if x["account_id"] == "paper-main")["approval_id"]
    assert executor.approve(ectx, aid)["ok"]
    row = ectx.conn.execute("SELECT amount, price FROM orders").fetchone()
    assert row["price"] is None and row["amount"] == str(Decimal(13) * PRICE)
    assert repo.today_order_amount(ectx.conn, "paper-main", D, market="KR") == Decimal(13) * PRICE
