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
    # 25% 상한(250만/71000 = 35주)보다 1회 손실 한도가 더 좁다: 1천만 × 2% ÷ (71000 − 65000) = 33주
    assert by["paper-main"]["qty"] == 33


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


class RaisingNotifier(FakeNotifier):
    def send(self, text: str, *, escape: bool = True) -> bool:
        super().send(text)
        raise RuntimeError("텔레그램 다운")


def test_place_keeps_ledger_row_when_notify_fails(ectx):
    """주문이 접수된 뒤 알림이 실패해도 orders 행·50_order 는 남고 결과가 돌아와야 한다
    (행이 없으면 재실행 때 has_open_order_or_approval 이 비어 같은 주문을 다시 낸다)."""
    ectx.notifier = RaisingNotifier()
    results = _run(ectx)
    r = next(x for x in results if x["account_id"] == "paper-main")
    assert r["order"]["accepted"] and len(ectx.brokers["paper-main"].orders) == 1
    n = ectx.conn.execute("SELECT COUNT(*) AS n FROM orders WHERE status='accepted'").fetchone()
    assert n["n"] == 1
    assert read_checkpoint(run_dir(D, "KR", "005930"), "50_order_paper-main") is not None


def test_place_insert_failure_is_critical_and_notified(ectx, monkeypatch):
    """접수 뒤 insert_order 가 실패하면 접수번호를 알림에 남기고 예외를 올린다."""

    def boom(*a, **kw):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(repo, "insert_order", boom)
    with pytest.raises(RuntimeError):
        _run(ectx)
    assert any("FAKE-1" in s and "기록 실패" in s for s in ectx.notifier.sent)


def test_approve_bookkeeping_failure_marks_failed_not_stuck(ectx, monkeypatch):
    """승인 뒤 주문 기록이 예외로 끊기면 승인이 approved 로 굳지 않고 failed 가 돼야 한다."""
    aid = _pending(ectx)

    def boom(*a, **kw):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(repo, "insert_order", boom)
    r = executor.approve(ectx, aid)
    assert r["ok"] is False and "FAKE-1" in r["error"]
    assert repo.get_approval(ectx.conn, aid)["status"] == "failed"


def test_unknown_network_result_is_recorded_as_unknown_and_counts_as_live(ectx):
    """응답을 못 받은 주문(NETWORK_UNKNOWN)은 failed 가 아니라 unknown 으로 남겨
    중복 방지 조건이 살아 있는 주문으로 보게 한다 (KIS 에 접수됐을 수 있다)."""
    from trader.broker.errors import KisApiError

    class TimeoutBroker(FakeBroker):
        def place_order(self, request):
            raise KisApiError("NETWORK_UNKNOWN", "요청 실패: ReadTimeout", "TTTC0802U")

    cash = _broker(ectx.accounts[0]).cash
    ectx.brokers["paper-main"] = TimeoutBroker(ectx.accounts[0], cash=cash)
    results = _run(ectx)
    r = next(x for x in results if x["account_id"] == "paper-main")
    assert r["order"]["status"] == "unknown" and r["order"]["accepted"] is False
    row = ectx.conn.execute("SELECT status, order_no FROM orders").fetchone()
    assert (row["status"], row["order_no"]) == ("unknown", None)
    dec_id = repo.decision_id(ectx.conn, D, "KR", "005930")
    assert repo.has_open_order_or_approval(ectx.conn, dec_id, "paper-main", "BUY")
    assert repo.has_open_order_today(ectx.conn, "paper-main", "005930", "BUY", D, market="KR")
    assert "005930" in repo.today_buy_tickers(ectx.conn, "paper-main", D, market="KR")
    assert any("미확인" in m for m in ectx.notifier.sent)
    # "살아 있을 수 있다" 고 알린 주문을 뒤이어 "[주문 실패]" 로 또 알리면 운영자가 재주문한다
    assert not any("주문 실패" in m for m in ectx.notifier.sent)


def test_approve_rejected_when_account_mode_is_no_longer_approve(ectx):
    """요청 뒤 계좌를 off 로 내렸으면 승인해도 주문이 나가지 않는다 (요청은 pending 유지)."""
    aid = _pending(ectx)
    control.set_account_mode(ectx.conn, ectx.accounts[0], "off")
    r = executor.approve(ectx, aid)
    assert r["ok"] is False and "모드" in r["error"]
    assert ectx.brokers["paper-main"].orders == []
    assert repo.get_approval(ectx.conn, aid)["status"] == "pending"


def test_rejected_order_bookkeeping_failure_is_not_escalated(ectx, monkeypatch):
    """브로커가 거부한 주문(접수번호 없음)의 기록 실패는 긴급 알림·중단 대상이 아니다."""
    second = AccountConfig(id="paper-2", env="paper", cano="3", markets=["KR"], mode="auto")
    ectx.accounts.append(second)
    ectx.brokers["paper-main"] = _broker(ectx.accounts[0], fail={"place_order"})
    ectx.brokers["paper-2"] = _broker(second)
    real_insert = repo.insert_order
    calls = {"n": 0}

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database is locked")
        return real_insert(*a, **kw)

    monkeypatch.setattr(repo, "insert_order", flaky)
    results = _run(ectx)
    by = {r["account_id"]: r for r in results}
    assert by["paper-main"]["order"]["status"] == "failed"
    assert by["paper-2"]["order"]["accepted"] is True  # 다음 계좌는 계속 실행된다
    assert not any("긴급" in m for m in ectx.notifier.sent)


# --- 리뷰 F8·F9·F17 -------------------------------------------------------------------


def test_account_exception_does_not_block_other_accounts(ectx, monkeypatch):
    """한 계좌의 예외(원장 잠금 등)는 그 계좌의 error 항목으로 남고 다음 계좌는 계속 실행된다."""
    ectx.accounts[1] = AccountConfig(
        id="real-main", env="real", cano="2", markets=["KR"], mode="off"
    )
    orig = executor.build_portfolio

    def boom(conn, broker, account, *a, **kw):
        if account.id == "paper-main":
            raise OSError("disk")
        return orig(conn, broker, account, *a, **kw)

    monkeypatch.setattr(executor, "build_portfolio", boom)
    results = _run(ectx)
    by = {r["account_id"]: r for r in results}
    assert "OSError" in by["paper-main"]["error"]
    assert by["real-main"]["mode"] == "off" and "error" not in by["real-main"]
    assert read_checkpoint(run_dir(D, "KR", "005930"), "40_risk_check_real-main") is not None
    assert any("[실행 실패]" in m for m in ectx.notifier.sent)


def test_skip_accounts_only_runs_remaining(ectx):
    results = executor.execute(
        ectx,
        Decision(**ok_decision()),
        date=D,
        market="KR",
        price=PRICE,
        dry_run=True,
        skip_accounts=frozenset({"paper-main"}),
    )
    assert [r["account_id"] for r in results] == ["real-main"]


def test_pending_approval_reserves_cash(ectx):
    """approve 계좌의 pending 요청 금액은 당일 주문금액에 들어가 다음 매수의 가능금액을 줄인다."""
    aid = _pending(ectx)
    a = repo.get_approval(ectx.conn, aid)
    qty, price = int(a["order"]["qty"]), Decimal(str(a["order"]["price"] or PRICE))
    assert repo.today_order_amount(ectx.conn, "paper-main", D, market="KR") == qty * price
    executor.reject(ectx, aid)
    assert repo.today_order_amount(ectx.conn, "paper-main", D, market="KR") == 0


def test_approval_keeps_order_stage_and_unknown_is_not_failed(ectx, monkeypatch):
    """재평가 승인은 51_order_r<n> 에 쓰고(오늘 분석의 50_order 를 덮지 않음), 응답 없는 주문은
    failed 가 아니라 approved 로 두어 체결 동기화가 확정하게 한다."""
    from trader.broker.errors import KisApiError

    control.set_account_mode(ectx.conn, ectx.accounts[0], "approve")
    dec = Decision(**ok_decision())
    repo.ingest_checkpoint(
        ectx.conn, D, "KR", dec.ticker, "30_decision", {"data": dec.model_dump()}
    )
    results = executor.execute(
        ectx, dec, date=D, market="KR", price=PRICE, dry_run=False, resume_round=1
    )
    aid = next(x for x in results if x["account_id"] == "paper-main")["approval_id"]
    assert repo.get_approval(ectx.conn, aid)["order"][executor.STAGE_KEY] == "51_order_r1"

    class TimeoutBroker(FakeBroker):
        def place_order(self, request):
            raise KisApiError("NETWORK_UNKNOWN", "요청 실패: ReadTimeout", "TTTC0802U")

    ectx.brokers["paper-main"] = TimeoutBroker(
        ectx.accounts[0], cash=_broker(ectx.accounts[0]).cash
    )
    r = executor.approve(ectx, aid)
    assert r["ok"] is False and r["order"]["status"] == "unknown"
    assert repo.get_approval(ectx.conn, aid)["status"] == "approved"
    d = run_dir(D, "KR", "005930")
    assert read_checkpoint(d, "51_order_r1_paper-main") is not None
    assert read_checkpoint(d, "50_order_paper-main") is None


def test_reject_race_is_reported(ectx):
    aid = _pending(ectx)
    repo.decide_approval(ectx.conn, aid, "rejected")  # 다른 프로세스가 먼저 처리
    r = executor.reject(ectx, aid)
    assert r["ok"] is False and "이미" in r["error"]


def test_breaker_sums_paper_accounts_nav(ectx):
    """Phase 21: 모의 계좌가 둘이면 서킷브레이커는 날짜별 NAV 합으로 판정한다 — 한 계좌만 보면
    다른 계좌의 판정이 바로 되돌리거나(OR 아님) 작은 계좌의 드로다운이 과대평가된다."""
    from trader.config import AccountConfig
    from trader.ledger import db

    second = AccountConfig(id="paper-2", env="paper", cano="9", markets=["KR"], mode="auto")
    db.sync_accounts(ectx.conn, [*ectx.accounts, second])
    for i in range(6):
        d = D - timedelta(days=6 - i)
        repo.snapshot_positions(
            ectx.conn,
            account_id="paper-main",
            d=d,
            market="KR",
            nav=Decimal("1000000"),
            cash=Decimal(1),
            positions=[],
        )
        repo.snapshot_positions(
            ectx.conn,
            account_id="paper-2",
            d=d,
            market="KR",
            nav=Decimal("9000000"),
            cash=Decimal(1),
            positions=[],
        )
    # paper-main 만 보면 −15%(브레이커 켜짐), 합산이면 10,000,000 → 9,850,000 = −1.5% (꺼짐)
    series = executor._paper_nav_series(ectx.conn, ectx.accounts[0], "KR", D, Decimal("850000"))
    assert series[-1] == (D.isoformat(), Decimal("9850000"))
    assert series[0][1] == Decimal("10000000")
    executor._update_breaker(ectx.conn, ectx.accounts[0], "KR", D, Decimal("850000"))
    assert control.get_flag(ectx.conn, "memory_breaker:KR")["active"] is False


# --- 검토 O8: 실행 키 선점 --------------------------------------------------------------


def test_concurrent_execute_places_one_order(ectx, monkeypatch):
    """두 프로세스(스레드 + 각자 연결)가 같은 결정을 동시에 실행해도 주문은 1건."""
    import threading

    from trader.ledger import db

    paper = ectx.accounts[0]
    barrier = threading.Barrier(2, timeout=10)

    real_check = repo.has_open_order_or_approval

    def check_then_wait(*a, **kw):
        out = real_check(*a, **kw)
        barrier.wait()  # 둘 다 중복 확인을 "없음" 으로 지난 뒤에 주문으로 간다
        return out

    monkeypatch.setattr(repo, "has_open_order_or_approval", check_then_wait)
    broker = _broker(paper)
    dec = Decision(**ok_decision())
    repo.ingest_checkpoint(
        ectx.conn, D, "KR", dec.ticker, "30_decision", {"data": dec.model_dump()}
    )
    results: list = []

    def worker():
        c = db.connect()
        try:
            ctx = executor.ExecContext(
                settings=Settings(),
                accounts=[paper],
                conn=c,
                brokers={paper.id: broker},
                notifier=FakeNotifier(),
                now=lambda: T0,
            )
            results.extend(
                executor.execute(ctx, dec, date=D, market="KR", price=PRICE, dry_run=False)
            )
        finally:
            c.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(broker.orders) == 1
    assert sorted(r["approved"] for r in results) == [False, True]
    lost = next(r for r in results if not r["approved"])
    assert any("다른 프로세스" in s for s in lost["reasons"])
    row = ectx.conn.execute("SELECT key, order_id FROM exec_keys").fetchone()
    assert row["order_id"] is not None and row["key"].endswith("|paper-main|BUY|50_order")


def test_rejected_order_releases_exec_key(ectx):
    from trader.broker.errors import KisApiError

    calls = []

    def reject_once(request):
        calls.append(request)
        if len(calls) == 1:
            raise KisApiError("APBK0013", "주문 거부")
        return FakeBroker.place_order(ectx.brokers["paper-main"], request)

    ectx.brokers["paper-main"].place_order = reject_once
    _run(ectx)
    assert ectx.conn.execute("SELECT COUNT(*) FROM exec_keys").fetchone()[0] == 0
    r = next(x for x in _run(ectx) if x["account_id"] == "paper-main")
    assert r["order"]["accepted"] and len(calls) == 2
    assert ectx.conn.execute("SELECT COUNT(*) FROM exec_keys").fetchone()[0] == 1


def test_unknown_order_keeps_exec_key(ectx):
    from trader.broker.errors import UNKNOWN_RESPONSE_CODE, KisApiError

    def boom(request):
        raise KisApiError(UNKNOWN_RESPONSE_CODE, "응답 없음")

    ectx.brokers["paper-main"].place_order = boom
    _run(ectx)
    assert ectx.conn.execute("SELECT COUNT(*) FROM exec_keys").fetchone()[0] == 1


def test_stop_loss_exec_key_is_per_ticker_day(ectx):
    from trader.broker.models import Position

    pos = Position(
        ticker="005930",
        market="KR",
        qty=10,
        avg_price=Decimal("80000"),
        current_price=PRICE,
        eval_amount=PRICE * 10,
        pnl_pct=-0.1,
    )
    ectx.brokers["paper-main"].positions = [pos]
    dec = Decision(
        **ok_decision(action="SELL", target_weight=0.0, order_type="MARKET", limit_price=None)
    )
    for _ in range(2):
        executor.execute(
            ectx, dec, date=D, market="KR", price=PRICE, dry_run=False, is_stop_loss=True
        )
    assert len(ectx.brokers["paper-main"].orders) == 1
    key = ectx.conn.execute("SELECT key FROM exec_keys").fetchone()["key"]
    assert key == f"stop|paper-main|005930|{D.isoformat()}|q10"


# --- 검토 O12: 승인 직전 최소 재판정 ----------------------------------------------------


def test_approve_buy_expires_when_regime_turned_bear(ectx):
    aid = _pending(ectx)
    control.set_flag(ectx.conn, "regime:KR", {"state": "bear", "count": 3})
    r = executor.approve(ectx, aid)
    assert r["ok"] is False and "bear" in r["error"]
    assert ectx.brokers["paper-main"].orders == []
    assert repo.get_approval(ectx.conn, aid)["status"] == "expired"


def test_approve_buy_passes_bear_with_strategist_risk_on(ectx, monkeypatch):
    aid = _pending(ectx)
    control.set_flag(ectx.conn, "regime:KR", {"state": "bear", "count": 3})
    monkeypatch.setattr(executor, "_agent_tilt", lambda ctx, market, today: 1)
    assert executor.approve(ectx, aid)["ok"]


def test_approve_buy_expires_when_daily_loss_limit_hit(ectx):
    aid = _pending(ectx)
    repo.snapshot_positions(
        ectx.conn,
        account_id="paper-main",
        d=date(2026, 10, 8),
        market="KR",
        nav=Decimal("10800000"),  # 오늘 1000만 → −7.4%
        cash=Decimal("10800000"),
        positions=[],
    )
    r = executor.approve(ectx, aid)
    assert r["ok"] is False and "일일 손실" in r["error"]
    assert ectx.brokers["paper-main"].orders == []
    assert repo.get_approval(ectx.conn, aid)["status"] == "expired"


def test_approve_sell_not_rechecked_in_bear(ectx):
    from trader.broker.models import Position

    ectx.brokers["paper-main"].positions = [
        Position(
            ticker="005930",
            market="KR",
            qty=10,
            avg_price=PRICE,
            current_price=PRICE,
            eval_amount=PRICE * 10,
            pnl_pct=0.0,
        )
    ]
    control.set_account_mode(ectx.conn, ectx.accounts[0], "approve")
    res = _run(ectx, ok_decision(action="SELL", target_weight=0.0))
    aid = next(x for x in res if x["account_id"] == "paper-main")["approval_id"]
    control.set_flag(ectx.conn, "regime:KR", {"state": "bear", "count": 3})
    assert executor.approve(ectx, aid)["ok"]


def _keys(ectx):
    return [dict(r) for r in ectx.conn.execute("SELECT * FROM exec_keys ORDER BY key")]


def test_approval_request_claims_key_and_approve_binds_order(ectx):
    """승인 모드도 요청 때 키를 잡는다(두 프로세스가 같은 결정의 승인 요청을 둘 만들지 않게)."""
    aid = _pending(ectx)
    (k,) = _keys(ectx)
    assert k["approval_id"] == aid and k["sent_at"] is None
    assert executor.approve(ectx, aid)["ok"]
    (k,) = _keys(ectx)
    assert k["order_id"] is not None and k["sent_at"] is not None


def test_closed_approval_key_is_reclaimed(ectx):
    aid = _pending(ectx)
    executor.reject(ectx, aid)
    r = next(x for x in _run(ectx) if x["account_id"] == "paper-main")
    assert r["approval_id"] and r["approval_id"] != aid  # 거절 뒤 재제출은 새 요청


def test_pending_approval_key_blocks_other_process(ectx):
    """다른 프로세스가 잡은 살아 있는 승인 키가 있으면 승인 요청을 또 만들지 않는다."""
    aid = _pending(ectx)
    ectx.conn.execute("UPDATE approvals SET decision_id=0 WHERE id=?", (aid,))  # 중복 확인 우회
    ectx.conn.commit()
    r = next(x for x in _run(ectx) if x["account_id"] == "paper-main")
    assert r["approval_id"] is None and any("실행 키" in s for s in r["reasons"])


def test_stale_unsent_key_is_reclaimed_but_sent_key_is_not(ectx):
    did = None
    dec = Decision(**ok_decision())
    repo.ingest_checkpoint(
        ectx.conn, D, "KR", dec.ticker, "30_decision", {"data": dec.model_dump()}
    )
    did = repo.decision_id(ectx.conn, D, "KR", dec.ticker)
    key = f"{did}|paper-main|BUY|50_order"
    old = (T0 - timedelta(minutes=11)).isoformat()
    ectx.conn.execute(
        "INSERT INTO exec_keys(key, created_at, sent_at) VALUES (?, ?, ?)", (key, old, old)
    )
    ectx.conn.commit()
    executor.execute(ectx, dec, date=D, market="KR", price=PRICE, dry_run=False)
    assert ectx.brokers["paper-main"].orders == []  # 보냈는데 행이 없는 키: 수동 대조
    ectx.conn.execute("UPDATE exec_keys SET sent_at=NULL WHERE key=?", (key,))
    ectx.conn.commit()
    executor.execute(ectx, dec, date=D, market="KR", price=PRICE, dry_run=False)
    assert len(ectx.brokers["paper-main"].orders) == 1  # 선점 뒤 죽은 프로세스의 키는 넘겨받음


def test_stop_loss_after_cancelled_stop_is_placed_again(ectx):
    """US 손절 지정가가 취소된 뒤(monitor 가 현재가 위 SELL 을 취소) 같은 날 재손절은 나간다."""
    from trader.broker.models import Position

    ectx.brokers["paper-main"].positions = [
        Position(
            ticker="005930",
            market="KR",
            qty=10,
            avg_price=Decimal("80000"),
            current_price=PRICE,
            eval_amount=PRICE * 10,
            pnl_pct=-0.1,
        )
    ]
    dec = Decision(**ok_decision(action="SELL", target_weight=0.0))
    kw = dict(date=D, market="KR", price=PRICE, dry_run=False, is_stop_loss=True)
    executor.execute(ectx, dec, **kw)
    oid = ectx.conn.execute("SELECT id FROM orders").fetchone()["id"]
    repo.set_order_status(ectx.conn, oid, "cancelled")
    executor.execute(ectx, dec, **kw)
    assert len(ectx.brokers["paper-main"].orders) == 2


# --- 전체 검토 2026-10-27 -------------------------------------------------------------


def test_stop_loss_remainder_after_partial_fill_can_be_sold_same_day(ectx):
    """첫 손절이 일부만 체결(filled)되고 잔량이 남으면 같은 날 잔량 손절이 다시 나간다."""
    from trader.broker.models import Position

    def pos(q):
        return Position(
            ticker="005930",
            market="KR",
            qty=q,
            avg_price=Decimal("80000"),
            current_price=PRICE,
            eval_amount=PRICE * q,
            pnl_pct=-0.1,
        )

    dec = Decision(
        **ok_decision(action="SELL", target_weight=0.0, order_type="MARKET", limit_price=None)
    )
    b = ectx.brokers["paper-main"]
    b.positions = [pos(10)]
    executor.execute(ectx, dec, date=D, market="KR", price=PRICE, dry_run=False, is_stop_loss=True)
    ectx.conn.execute("UPDATE orders SET status='filled'")
    ectx.conn.commit()
    b.positions = [pos(4)]  # 6주만 체결됐다
    executor.execute(ectx, dec, date=D, market="KR", price=PRICE, dry_run=False, is_stop_loss=True)
    assert [o.qty for o in b.orders] == [10, 4]


def test_approve_buy_expires_when_killswitch_on(ectx):
    from trader.risk import killswitch

    aid = _pending(ectx)
    killswitch.engage(ectx.conn, reason="테스트 /kill", source="telegram")
    r = executor.approve(ectx, aid)
    assert r["ok"] is False and "킬스위치" in r["error"]
    assert ectx.brokers["paper-main"].orders == []
    assert repo.get_approval(ectx.conn, aid)["status"] == "expired"


def test_decision_turned_away_from_buy_cancels_open_buy(ectx):
    _run(ectx)  # 아침 BUY 지정가 접수
    b = ectx.brokers["paper-main"]
    assert len(b.orders) == 1
    hold = ok_decision(action="HOLD", target_weight=0.0)
    _run(ectx, hold)
    assert b.cancelled == ["FAKE-1"]
    st = ectx.conn.execute("SELECT status FROM orders WHERE account_id='paper-main'").fetchone()
    assert st["status"] == "cancelled"
    assert any("미체결 매수 취소" in m for m in ectx.notifier.sent)


def test_dry_run_hold_does_not_cancel(ectx):
    _run(ectx)
    _run(ectx, ok_decision(action="HOLD", target_weight=0.0), dry_run=True)
    assert ectx.brokers["paper-main"].cancelled == []


def test_killswitch_trip_cancels_open_buys(ectx):
    _run(ectx)
    b = ectx.brokers["paper-main"]
    n, left = executor.on_killswitch(ectx.conn, b, "paper-main", "KR", D)
    assert (n, left) == (1, []) and b.cancelled == ["FAKE-1"]
