"""단타 청산(전체 검토 O6·O7): 허용 판정보다 먼저, 계좌별 체결 수량만, 실패·미체결은 재시도."""

from datetime import timedelta
from decimal import Decimal

from conftest import make_account

from scalp.test_engine import NOW, D, _bars, _ctx, _scan
from trader.broker.models import Fill, Position
from trader.config import HedgeCfg
from trader.control import control
from trader.orchestrator.run_store import read_checkpoint, run_dir
from trader.scalp import engine

T = "005930"


def _pos(qty: int, price: str = "10050") -> Position:
    p = Decimal(price)
    return Position(
        ticker=T,
        market="KR",
        qty=qty,
        avg_price=p,
        current_price=p,
        eval_amount=p * qty,
        pnl_pct=0.0,
    )


def _fill(order_no: str, side: str, qty: int, price: str, minutes: int = 1) -> Fill:
    return Fill(
        order_no=order_no,
        ticker=T,
        market="KR",
        side=side,  # type: ignore[arg-type]
        qty=qty,
        price=Decimal(price),
        filled_at=NOW + timedelta(minutes=minutes),
    )


def _advance(ctx, minutes: int) -> None:
    t = NOW + timedelta(minutes=minutes)
    ctx.now = lambda: t


def _enter(ctx, conn):
    _scan([T])
    out = engine.tick(ctx, market="KR", today=D)
    assert [e["ticker"] for e in out["entries"]] == [T]
    return engine.load_state(None, D, "KR")["open"][0]


def _sells(conn, account="paper-main"):
    return conn.execute(
        "SELECT qty, status FROM orders WHERE side='SELL' AND account_id=? ORDER BY id", (account,)
    ).fetchall()


def test_entry_accepted_is_pending_until_filled(conn, project):
    ctx = _ctx(conn, minutes={T: _bars(True)}, prices={T: Decimal("10050")})
    pos = _enter(ctx, conn)
    assert pos["status"] == "pending_entry"
    leg = pos["legs"]["paper-main"]
    assert leg["req"] == 49 and len(leg["buy"]) == 1
    # 체결 동기화 뒤 보유로
    ctx.brokers["paper-main"].fills[("KR", D)] = [_fill("FAKE-1", "BUY", 49, "10040")]
    _advance(ctx, 1)
    engine.tick(ctx, market="KR", today=D)
    pos = engine.load_state(None, D, "KR")["open"][0]
    assert pos["status"] == "held" and pos["filled"] == {"paper-main": 49}


def test_stop_exit_runs_after_bear_switch_and_ret_uses_fill_prices(conn, project):
    ctx = _ctx(
        conn, minutes={T: _bars(True)}, prices={T: Decimal("10050")}, hedge=HedgeCfg(enabled=True)
    )
    _enter(ctx, conn)
    b = ctx.brokers["paper-main"]
    b.fills[("KR", D)] = [_fill("FAKE-1", "BUY", 49, "10000")]
    b.positions = [_pos(49)]
    control.set_flag(conn, "regime:KR", {"state": "bear", "count": 3})
    ctx.market_client.prices[T] = Decimal("9800")  # 손절 −1% 아래
    _advance(ctx, 3)
    out = engine.tick(ctx, market="KR", today=D)
    assert "bear" in out["skipped"]  # 신규 진입은 막히지만
    assert [r["qty"] for r in _sells(conn)] == [49]  # 청산은 나간다
    st = engine.load_state(None, D, "KR")
    assert st["open"][0]["status"] == "closing" and st["closed"] == []
    # 청산 체결 → 체결가 기준 실현손익
    b.fills[("KR", D)].append(_fill("FAKE-2", "SELL", 49, "9850", minutes=4))
    _advance(ctx, 4)
    out = engine.tick(ctx, market="KR", today=D)
    assert out["exits"][0]["reason"] == "stop" and abs(out["exits"][0]["ret"] + 0.015) < 1e-6
    st = engine.load_state(None, D, "KR")
    assert st["open"] == [] and st["closed"][0]["exit_price"] == "9850"


def test_exit_skips_accounts_that_did_not_enter(conn, project):
    accounts = [
        make_account("paper-main", "paper", "auto", ["KR"]),
        make_account("paper-two", "paper", "auto", ["KR"]),
    ]
    ctx = _ctx(conn, minutes={T: _bars(True)}, prices={T: Decimal("10050")}, accounts=accounts)
    ctx.brokers["paper-two"].fail = {"place_order"}
    pos = _enter(ctx, conn)
    assert set(pos["legs"]) == {"paper-main"}
    ctx.brokers["paper-two"].fail = set()
    ctx.brokers["paper-main"].fills[("KR", D)] = [_fill("FAKE-1", "BUY", 49, "10050")]
    ctx.brokers["paper-main"].positions = [_pos(49)]
    ctx.brokers["paper-two"].positions = [_pos(30)]  # 일반 보유분: 단타와 무관
    ctx.market_client.prices[T] = Decimal("9900")
    _advance(ctx, 3)
    engine.tick(ctx, market="KR", today=D)
    assert [r["qty"] for r in _sells(conn)] == [49]
    assert _sells(conn, "paper-two") == []
    assert read_checkpoint(run_dir(D, "KR", T), "47_scalp_exit_paper-two") is None


def test_exit_failure_keeps_closing_and_retries(conn, project):
    ctx = _ctx(conn, minutes={T: _bars(True)}, prices={T: Decimal("10050")})
    _enter(ctx, conn)
    b = ctx.brokers["paper-main"]
    b.fills[("KR", D)] = [_fill("FAKE-1", "BUY", 49, "10050")]
    b.positions = [_pos(49)]
    ctx.market_client.prices[T] = Decimal("9900")
    b.fail = {"place_order"}
    _advance(ctx, 3)
    out = engine.tick(ctx, market="KR", today=D)
    assert out["exits"] == []
    st = engine.load_state(None, D, "KR")
    assert st["open"][0]["status"] == "closing" and st["closed"] == []
    assert [r["status"] for r in _sells(conn)] == ["failed"]
    # 다음 틱: 가격이 손절선 위로 돌아와도 청산을 이어 간다
    b.fail = set()
    ctx.market_client.prices[T] = Decimal("10060")
    _advance(ctx, 4)
    engine.tick(ctx, market="KR", today=D)
    assert [r["status"] for r in _sells(conn)] == ["failed", "accepted"]
    assert engine.load_state(None, D, "KR")["open"][0]["status"] == "closing"


def test_exit_cancels_unfilled_buy_then_sells_only_filled_part(conn, project):
    ctx = _ctx(conn, minutes={T: _bars(True)}, prices={T: Decimal("10050")})
    _enter(ctx, conn)
    b = ctx.brokers["paper-main"]
    b.fills[("KR", D)] = [_fill("FAKE-1", "BUY", 20, "10050")]  # 49주 중 20주만 체결
    b.positions = [_pos(120)]  # 일반 보유 100 + 단타 20
    ctx.market_client.prices[T] = Decimal("9900")
    _advance(ctx, 3)
    engine.tick(ctx, market="KR", today=D)
    assert b.cancelled == ["FAKE-1"]
    buy = conn.execute("SELECT status FROM orders WHERE side='BUY'").fetchone()
    assert buy["status"] == "cancelled"
    assert [r["qty"] for r in _sells(conn)] == [20]  # 일반 보유 100주는 건드리지 않는다


def test_exit_without_any_fill_cancels_buy_and_voids_position(conn, project):
    ctx = _ctx(conn, minutes={T: _bars(True)}, prices={T: Decimal("10050")})
    _enter(ctx, conn)
    b = ctx.brokers["paper-main"]
    ctx.market_client.prices[T] = Decimal("9900")
    _advance(ctx, 3)
    engine.tick(ctx, market="KR", today=D)
    assert b.cancelled == ["FAKE-1"] and _sells(conn) == []
    # 취소 직전 체결이 있었을 수 있어 한 번 더 체결을 확인한 뒤에야 소멸시킨다
    assert engine.load_state(None, D, "KR")["open"][0]["legs"]["paper-main"]["recheck"] == ["buy"]
    _advance(ctx, 4)
    out = engine.tick(ctx, market="KR", today=D)
    st = engine.load_state(None, D, "KR")
    assert st["open"] == [] and st["closed"] == [] and out["exits"] == []


def test_fill_right_before_cancel_is_sold_not_voided(conn, project):
    """취소 직전 체결(원장 반영 전)을 놓치지 않는다: 다음 틱 동기화가 찾은 체결분을 판다."""
    ctx = _ctx(conn, minutes={T: _bars(True)}, prices={T: Decimal("10050")})
    _enter(ctx, conn)
    b = ctx.brokers["paper-main"]
    ctx.market_client.prices[T] = Decimal("9900")
    _advance(ctx, 3)
    engine.tick(ctx, market="KR", today=D)  # 체결 0 으로 보이는 채 BUY 취소
    b.fills[("KR", D)] = [_fill("FAKE-1", "BUY", 10, "10050", minutes=3)]
    b.positions = [_pos(10)]
    _advance(ctx, 4)
    engine.tick(ctx, market="KR", today=D)
    assert [r["qty"] for r in _sells(conn)] == [10]


def test_sell_cancelled_with_unseen_fill_is_not_resold(conn, project):
    """지정가 청산을 취소했는데 취소 직전 체결이 있었다면 그 수량을 다시 팔지 않는다."""
    ctx = _ctx(conn, minutes={T: _bars(True)}, prices={T: Decimal("10050")})
    _enter(ctx, conn)
    b = ctx.brokers["paper-main"]
    b.fills[("KR", D)] = [_fill("FAKE-1", "BUY", 49, "10050")]
    b.positions = [_pos(149)]  # 일반 보유 100
    ctx.market_client.prices[T] = Decimal("9900")
    _advance(ctx, 3)
    engine.tick(ctx, market="KR", today=D)
    sell = conn.execute("SELECT id FROM orders WHERE side='SELL'").fetchone()["id"]
    conn.execute("UPDATE orders SET order_type='LIMIT' WHERE id=?", (sell,))  # US 지정가처럼
    conn.commit()
    _advance(ctx, 3 + engine.EXIT_REQUOTE_S // 60 + 1)
    engine.tick(ctx, market="KR", today=D)  # 오래된 지정가 취소(체결은 아직 모름)
    assert b.cancelled == ["FAKE-2"] and len(_sells(conn)) == 1
    b.fills[("KR", D)].append(_fill("FAKE-2", "SELL", 49, "9900", minutes=5))
    _advance(ctx, 10)
    out = engine.tick(ctx, market="KR", today=D)
    assert len(_sells(conn)) == 1  # 다시 팔지 않고 끝낸다
    assert out["exits"] and out["exits"][0]["reason"] == "stop"


def test_unrecorded_exit_order_is_adopted_not_resubmitted(conn, project):
    """청산 접수 뒤 상태 저장 전에 끊긴 경우: 원장의 결정 없는 SELL 을 흡수해 다시 내지 않는다."""
    ctx = _ctx(conn, minutes={T: _bars(True)}, prices={T: Decimal("10050")})
    _enter(ctx, conn)
    b = ctx.brokers["paper-main"]
    b.fills[("KR", D)] = [_fill("FAKE-1", "BUY", 49, "10050")]
    b.positions = [_pos(149)]
    ctx.market_client.prices[T] = Decimal("9900")
    _advance(ctx, 3)
    engine.tick(ctx, market="KR", today=D)
    st = engine.load_state(None, D, "KR")
    st["open"][0]["legs"]["paper-main"]["sell"] = []  # 저장 전에 끊긴 것처럼
    engine.save_state(None, D, "KR", st)
    _advance(ctx, 4)
    engine.tick(ctx, market="KR", today=D)
    assert len(_sells(conn)) == 1


def test_unfinished_position_carries_over_to_next_day(conn, project):
    """마감까지 못 판 단타는 다음 운영일 첫 틱에 승계돼 청산을 이어 간다."""
    from datetime import date as _date

    ctx = _ctx(conn, minutes={T: _bars(True)}, prices={T: Decimal("10050")})
    _enter(ctx, conn)
    b = ctx.brokers["paper-main"]
    b.fills[("KR", D)] = [_fill("FAKE-1", "BUY", 49, "10050")]
    b.positions = [_pos(49)]
    _advance(ctx, 1)
    engine.tick(ctx, market="KR", today=D)  # held, 청산 신호 없음
    nxt = _date(2026, 10, 26)
    _advance(ctx, 2 * 24 * 60)
    out = engine.tick(ctx, market="KR", today=nxt)
    assert [r["qty"] for r in _sells(conn)] == [49]
    assert engine.load_state(None, D, "KR")["open"] == []
    pos = engine.load_state(None, nxt, "KR")["open"][0]
    assert pos["exit_reason"] == "carry_over" and pos["carried_from"] == D.isoformat()
    assert out["entries"] == []


def test_paused_does_not_drop_open_position(conn, project):
    ctx = _ctx(conn, minutes={T: _bars(True)}, prices={T: Decimal("10050")})
    _enter(ctx, conn)
    b = ctx.brokers["paper-main"]
    b.fills[("KR", D)] = [_fill("FAKE-1", "BUY", 49, "10050")]
    b.positions = [_pos(49)]
    control.set_flag(conn, "paused", True)
    ctx.market_client.prices[T] = Decimal("9900")
    _advance(ctx, 3)
    out = engine.tick(ctx, market="KR", today=D)
    assert out["skipped"] == "일시정지" and _sells(conn) == []  # 일시정지: 주문 없음
    assert engine.load_state(None, D, "KR")["open"][0]["status"] == "closing"
    control.set_flag(conn, "paused", False)
    _advance(ctx, 4)
    engine.tick(ctx, market="KR", today=D)
    assert [r["qty"] for r in _sells(conn)] == [49]


def test_legacy_state_without_legs_sells_once_with_cap(conn, project):
    """옛 상태 파일(legs 없음)은 요청 수량을 체결로 보고 청산 주문을 한 번만 낸다."""
    ctx = _ctx(conn, minutes={T: _bars(False)}, prices={T: Decimal("9900")})
    st = {
        "open": [
            {
                "ticker": T,
                "exchange": None,
                "weight": 0.05,
                "entry_price": "10050",
                "entry_time": "090500",
                "entry_at": NOW.isoformat(),
                "qty": {"paper-main": 10},
                "dry_run": False,
            }
        ],
        "closed": [],
        "done": [T],
        "halted": None,
    }
    engine.save_state(None, D, "KR", st)
    ctx.brokers["paper-main"].positions = [_pos(110)]
    engine.tick(ctx, market="KR", today=D)
    engine.tick(ctx, market="KR", today=D)
    assert [r["qty"] for r in _sells(conn)] == [10]


def test_entering_position_adopts_ledger_buy_after_crash(conn, project):
    """진입 접수 뒤 상태 저장 전에 끊긴 경우: 임시 포지션이 원장의 BUY 를 흡수해 추적한다."""
    ctx = _ctx(conn, minutes={T: _bars(True)}, prices={T: Decimal("10050")})
    _enter(ctx, conn)
    st = engine.load_state(None, D, "KR")
    st["open"][0] |= {"status": "entering", "legs": {}, "qty": {}}  # 저장 전에 끊긴 것처럼
    engine.save_state(None, D, "KR", st)
    ctx.brokers["paper-main"].fills[("KR", D)] = [_fill("FAKE-1", "BUY", 49, "10050")]
    _advance(ctx, 1)
    engine.tick(ctx, market="KR", today=D)
    pos = engine.load_state(None, D, "KR")["open"][0]
    assert pos["status"] == "held" and pos["filled"] == {"paper-main": 49}
    assert len(ctx.brokers["paper-main"].orders) == 1  # 다시 사지 않았다


def test_failed_fill_query_keeps_recheck(conn, project):
    """취소 뒤 체결 조회가 실패하면 재확인을 지우지 않는다(실패를 '체결 없음' 으로 보지 않음)."""
    ctx = _ctx(conn, minutes={T: _bars(True)}, prices={T: Decimal("10050")})
    _enter(ctx, conn)
    b = ctx.brokers["paper-main"]
    ctx.market_client.prices[T] = Decimal("9900")
    _advance(ctx, 3)
    engine.tick(ctx, market="KR", today=D)  # BUY 취소 → recheck
    b.fail = {"get_fills"}
    _advance(ctx, 4)
    engine.tick(ctx, market="KR", today=D)
    pos = engine.load_state(None, D, "KR")["open"][0]
    assert pos["legs"]["paper-main"]["recheck"] == ["buy"]
    b.fail = set()
    _advance(ctx, 5)
    engine.tick(ctx, market="KR", today=D)
    assert engine.load_state(None, D, "KR")["open"] == []


def test_carry_over_is_idempotent_and_syncs_original_day(conn, project):
    """이월이 끊겨 옛 파일에 남아 있어도 중복 승계하지 않고, 전일 주문일로 체결을 다시 읽는다."""
    from datetime import date as _date

    ctx = _ctx(conn, minutes={T: _bars(True)}, prices={T: Decimal("10050")})
    _enter(ctx, conn)
    old = engine.load_state(None, D, "KR")
    b = ctx.brokers["paper-main"]
    b.fills[("KR", D)] = [_fill("FAKE-1", "BUY", 49, "10050")]  # 전일 체결(동기화 누락분)
    b.positions = [_pos(49)]
    nxt = _date(2026, 10, 26)
    _advance(ctx, 2 * 24 * 60)
    engine.tick(ctx, market="KR", today=nxt)  # 전일 주문일로 체결을 읽어 49주 청산
    assert [r["qty"] for r in _sells(conn)] == [49]
    b.fills[("KR", nxt)] = [_fill("FAKE-2", "SELL", 49, "9900", minutes=2 * 24 * 60)]
    _advance(ctx, 2 * 24 * 60 + 1)
    engine.tick(ctx, market="KR", today=nxt)
    assert engine.load_state(None, nxt, "KR")["open"] == []
    engine.save_state(None, D, "KR", old)  # 옛 파일을 비우기 전에 끊긴 것처럼 되살림
    _advance(ctx, 2 * 24 * 60 + 2)
    engine.tick(ctx, market="KR", today=nxt)
    st = engine.load_state(None, nxt, "KR")
    assert st["open"] == [] and len(st["closed"]) == 1  # 다시 승계·이중 집계하지 않는다
    assert len(_sells(conn)) == 1


def test_recheck_is_saved_before_cancel_call(conn, project):
    """취소 요청 시점에 재확인 표시가 이미 파일에 있다(취소 뒤 끊겨도 체결을 다시 읽음)."""
    ctx = _ctx(conn, minutes={T: _bars(True)}, prices={T: Decimal("10050")})
    _enter(ctx, conn)
    b = ctx.brokers["paper-main"]
    seen = []
    orig = b.cancel_order

    def cancel(order_no, **kw):
        seen.append(
            engine.load_state(None, D, "KR")["open"][0]["legs"]["paper-main"].get("recheck")
        )
        return orig(order_no, **kw)

    b.cancel_order = cancel
    ctx.market_client.prices[T] = Decimal("9900")
    _advance(ctx, 3)
    engine.tick(ctx, market="KR", today=D)
    assert seen == [["buy"]]


def test_entering_position_carried_over_adopts_entry(conn, project):
    """진입 접수 뒤 저장 전에 끊긴 채(entering) 다음 날로 넘어와도 진입 주문을 흡수해 청산한다."""
    from datetime import date as _date

    ctx = _ctx(conn, minutes={T: _bars(True)}, prices={T: Decimal("10050")})
    _enter(ctx, conn)
    st = engine.load_state(None, D, "KR")
    st["open"][0] |= {"status": "entering", "legs": {}, "qty": {}}
    engine.save_state(None, D, "KR", st)
    b = ctx.brokers["paper-main"]
    b.fills[("KR", D)] = [_fill("FAKE-1", "BUY", 49, "10050")]
    b.positions = [_pos(49)]
    nxt = _date(2026, 10, 26)
    _advance(ctx, 2 * 24 * 60)
    engine.tick(ctx, market="KR", today=nxt)
    assert [r["qty"] for r in _sells(conn)] == [49]
