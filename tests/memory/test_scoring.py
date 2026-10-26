"""전체 검토 L2·L3: 재평가 라운드별 엔트리 + 방향 점수·회피 손실·체결가 손익 (원장 v23)."""

import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest
from helpers_agents import ok_decision
from helpers_ml import trading_days

from trader.config import MemorySettings
from trader.ledger import db, repo
from trader.memory import outcomes, store
from trader.ml import features as feat

D0 = date(2026, 9, 1)
DAYS = trading_days(30, start=D0)
NOW = datetime(2026, 10, 17, tzinfo=UTC)


def _store(prices: dict[str, list[float]]):
    rows = []
    for t, closes in prices.items():
        for d, c in zip(DAYS[: len(closes)], closes, strict=True):
            rows.append({"date": d, "ticker": t, "market": "KR", "close": c, "volume": 1})
    df = pd.DataFrame(rows)
    for c in feat.FEATURE_COLUMNS:
        if c not in df.columns:
            df[c] = np.nan
    feat.write_rows(df[feat.FEATURE_COLUMNS])


def _at(d: date, hour: int = 1) -> datetime:
    return datetime(d.year, d.month, d.day, hour, tzinfo=UTC)


def _decision_row(conn, ticker, action, d):
    return repo.ingest_checkpoint(
        conn, d, "KR", ticker, "30_decision", {"data": ok_decision(ticker=ticker, action=action)}
    )


def _record(conn, dec_id, ticker, action, d, *, round_no=0, hour=1, held=None):
    return store.record_decision(
        conn,
        decision_id=dec_id,
        ticker=ticker,
        market="KR",
        account_id="paper-main",
        decided_at=_at(d, hour).isoformat(),
        decision={"action": action, "confidence": 0.6},
        row=None,
        horizon_days=5,
        vector=[0.5] * 12,
        round_no=round_no,
        held_at_decision=held,
    )


def _snapshot(conn, d, ticker, qty):
    repo.snapshot_positions(
        conn,
        account_id="paper-main",
        d=d,
        nav=Decimal("10000000"),
        cash=Decimal("5000000"),
        positions=[
            {
                "ticker": ticker,
                "market": "KR",
                "qty": qty,
                "avg_price": Decimal("100"),
                "current_price": Decimal("100"),
                "eval_amount": Decimal(100 * qty),
            }
        ],
        market="KR",
    )


# --- L2: 재평가는 별도 엔트리 ---


def test_reevaluation_keeps_morning_entry_and_adds_round_entry(conn):
    dec_id = _decision_row(conn, "A", "BUY", DAYS[0])
    morning = _record(conn, dec_id, "A", "BUY", DAYS[0])
    reeval = _record(conn, dec_id, "A", "SELL", DAYS[0], round_no=1, hour=4)
    assert morning != reeval
    m, r = store.get_entry(conn, morning), store.get_entry(conn, reeval)
    assert m["decision"]["action"] == "BUY" and m["round"] == 0
    assert r["decision"]["action"] == "SELL" and r["round"] == 1
    # 같은 라운드의 재기록은 멱등 (세션 재시도)
    assert _record(conn, dec_id, "A", "SELL", DAYS[0], round_no=1, hour=5) == reeval
    assert conn.execute("SELECT COUNT(*) FROM memory_entries").fetchone()[0] == 2


def test_matured_count_and_retrieve_prefer_round0(conn):
    for i in range(3):
        dec_id = _decision_row(conn, f"T{i}", "BUY", DAYS[i])
        e0 = _record(conn, dec_id, f"T{i}", "BUY", DAYS[i])
        e1 = _record(conn, dec_id, f"T{i}", "TRIM", DAYS[i], round_no=1, hour=4)
        for e in (e0, e1):
            store.fill_outcome(conn, e, realized_return=0.01, alpha=0.0, outcome_at=NOW.isoformat())
    assert store.matured_count(conn) == 3  # 결정 수(아침 라운드)만 센다
    r = store.retrieve(
        conn,
        vector=[0.5] * 12,
        now=NOW,
        settings=MemorySettings(warmup=1, top_k=6),
        market="KR",
    )
    rounds = [e["round"] for e in r["entries"]]
    assert rounds[:3] == [0, 0, 0] and rounds[3:] == [1, 1, 1]
    by_round = {e["round"]: e["score"] for e in r["entries"]}
    assert by_round[1] < by_round[0]


# --- L3: 채점 ---


def test_hold_scored_by_holding_at_decision(conn, project):
    _store({"A": [100] * 5 + [90] * 25, "B": [100] * 5 + [110] * 25})
    a = _record(conn, _decision_row(conn, "A", "HOLD", DAYS[0]), "A", "HOLD", DAYS[0], held=True)
    b = _record(conn, _decision_row(conn, "B", "HOLD", DAYS[0]), "B", "HOLD", DAYS[0], held=False)
    c = _record(conn, _decision_row(conn, "A", "HOLD", DAYS[1]), "A", "HOLD", DAYS[1], held=False)
    outcomes.mature(conn, state=None, today=DAYS[10])
    ea, eb, ec = (store.get_entry(conn, x) for x in (a, b, c))
    # 보유 HOLD: 계속 들고 있었으니 가격수익 그대로
    assert ea["direction_score"] == pytest.approx(-0.10) and ea["avoided_loss"] is None
    # 미보유 HOLD: 점수 0, 오른 종목을 안 산 것은 손실이 아니다(회피 손실 0)
    assert eb["direction_score"] == 0.0 and eb["avoided_loss"] == 0.0
    # 미보유 HOLD 가 하락을 피했으면 회피 손실
    assert ec["direction_score"] == 0.0 and ec["avoided_loss"] == pytest.approx(0.10)
    # 하위 호환: realized_return·alpha 는 그대로 채운다
    assert eb["realized_return"] == 0.0 and eb["alpha_vs_benchmark"] is not None


def test_hold_held_falls_back_to_positions_snapshot(conn, project):
    _store({"A": [100] * 5 + [110] * 25})
    _snapshot(conn, DAYS[0], "A", 10)
    e = _record(conn, _decision_row(conn, "A", "HOLD", DAYS[0]), "A", "HOLD", DAYS[0])
    outcomes.mature(conn, state=None, today=DAYS[10])
    assert store.get_entry(conn, e)["direction_score"] == pytest.approx(0.10)


def test_sell_scores_avoided_loss_not_short_profit(conn, project):
    _store({"A": [100] * 5 + [90] * 25, "B": [100] * 5 + [110] * 25})
    s1 = _record(conn, _decision_row(conn, "A", "SELL", DAYS[0]), "A", "SELL", DAYS[0])
    s2 = _record(conn, _decision_row(conn, "B", "TRIM", DAYS[0]), "B", "TRIM", DAYS[0])
    outcomes.mature(conn, state=None, today=DAYS[10])
    e1, e2 = store.get_entry(conn, s1), store.get_entry(conn, s2)
    assert e1["direction_score"] == pytest.approx(0.10) and e1["avoided_loss"] == pytest.approx(
        0.10
    )
    assert e2["direction_score"] == pytest.approx(-0.10) and e2["avoided_loss"] == 0.0


def test_buy_has_no_avoided_loss(conn, project):
    _store({"A": [100] * 5 + [104] * 25})
    e = _record(conn, _decision_row(conn, "A", "BUY", DAYS[0]), "A", "BUY", DAYS[0])
    outcomes.mature(conn, state=None, today=DAYS[10])
    x = store.get_entry(conn, e)
    assert x["direction_score"] == pytest.approx(0.04) and x["avoided_loss"] is None
    assert x["realized_pnl_pct"] is None  # 체결이 없으면 NULL


def _order_with_fill(conn, dec_id, side, created, fill_price, qty=10):
    oid = repo.insert_order(
        conn,
        decision_id=dec_id,
        account_id="paper-main",
        ticker="A",
        market="KR",
        side=side,
        qty=qty,
        order_type="LIMIT",
        price=Decimal(str(fill_price)),
        status="filled",
        order_no="1",
        message="",
        mode="auto",
        created_at=created.isoformat(),
    )
    conn.execute(
        "INSERT INTO fills(order_id, account_id, qty, price, filled_at) VALUES (?, ?, ?, ?, ?)",
        (oid, "paper-main", qty, str(fill_price), created.isoformat()),
    )
    conn.commit()
    return oid


def test_realized_pnl_uses_actual_fill_price_per_round(conn, project):
    # 결정일 종가 100, 만기 종가 110. 아침 BUY 는 102 에 체결, 재평가 TRIM 은 108 에 체결
    _store({"A": [100] * 5 + [110] * 25})
    dec_id = _decision_row(conn, "A", "BUY", DAYS[0])
    morning = _record(conn, dec_id, "A", "BUY", DAYS[0], hour=1)
    _order_with_fill(conn, dec_id, "BUY", _at(DAYS[0], 1) + timedelta(seconds=5), 102)
    reeval = _record(conn, dec_id, "A", "TRIM", DAYS[0], round_no=1, hour=4)
    _order_with_fill(conn, dec_id, "SELL", _at(DAYS[0], 4) + timedelta(seconds=5), 108)
    outcomes.mature(conn, state=None, today=DAYS[10])
    m, r = store.get_entry(conn, morning), store.get_entry(conn, reeval)
    assert m["realized_pnl_pct"] == pytest.approx(110 / 102 - 1)
    # TRIM: 판 가격 108 → 만기 110 이면 판 뒤 더 올랐으니 음수
    assert r["realized_pnl_pct"] == pytest.approx(-(110 / 108 - 1))
    assert m["direction_score"] == pytest.approx(0.10) and r["direction_score"] == pytest.approx(
        -0.10
    )


# --- 마이그레이션 ---


# 메모리 재구성 마이그레이션 번호(머지 순서에 따라 바뀔 수 있어 문장으로 찾는다)
MEM_V = next(v for v, st in db.MIGRATIONS if any("memory_entries_new" in x for x in st))


def _v20_db(path) -> sqlite3.Connection:
    """메모리 재구성 직전 버전의 원장."""
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    prev = 0
    for v, stmts in db.MIGRATIONS:
        if v >= MEM_V:
            break
        for s in stmts:
            c.execute(s)
        prev = v
    c.execute("INSERT INTO schema_version(version) VALUES (?)", (prev,))
    c.commit()
    return c


def test_migration_memory_keeps_rows_and_backfills_scores(tmp_path):
    c = _v20_db(tmp_path / "t.db")
    dec = "2026-09-01"
    for i, (ticker, action, pr) in enumerate(
        [("A", "BUY", 0.05), ("B", "SELL", -0.04), ("C", "HOLD", 0.03), ("D", "HOLD", -0.02)],
        start=1,
    ):
        c.execute(
            "INSERT INTO decisions(id, date, market, ticker, action, json, created_at) "
            "VALUES (?, ?, 'KR', ?, ?, '{}', ?)",
            (i, dec, ticker, action, dec),
        )
        c.execute(
            "INSERT INTO memory_entries(id, ticker, market, account_id, decision_id, decided_at, "
            "situation_vector, decision, horizon_days, realized_return, alpha_vs_benchmark, "
            "outcome_at, tier, importance, created_at, price_return) "
            "VALUES (?, ?, 'KR', 'paper-main', ?, '2026-09-01T01:00:00+00:00', '[]', ?, 5, 0, 0, "
            "'2026-09-10', 'short', 0.5, '2026-09-01', ?)",
            (i, ticker, i, f'{{"action": "{action}"}}', pr),
        )
    c.execute(
        "INSERT INTO positions_snapshot(account_id, date, ticker, market, qty, created_at) "
        "VALUES ('paper-main', ?, 'C', 'KR', 5, ?)",
        (dec, dec),
    )
    c.commit()
    db.init_schema(c)
    rows = {r["ticker"]: dict(r) for r in c.execute("SELECT * FROM memory_entries")}
    assert len(rows) == 4 and {r["round"] for r in rows.values()} == {0}
    assert rows["A"]["direction_score"] == pytest.approx(0.05) and rows["A"]["avoided_loss"] is None
    assert rows["B"]["direction_score"] == pytest.approx(0.04)
    assert rows["B"]["avoided_loss"] == pytest.approx(0.04)
    assert rows["C"]["held_at_decision"] == 1 and rows["C"]["direction_score"] == pytest.approx(
        0.03
    )
    assert rows["D"]["held_at_decision"] == 0 and rows["D"]["direction_score"] == 0.0
    assert rows["D"]["avoided_loss"] == pytest.approx(0.02)
    # 같은 decision_id 의 재평가 라운드 행이 들어갈 수 있다 (UNIQUE(decision_id, round))
    c.execute(
        "INSERT INTO memory_entries(ticker, market, decision_id, round, decided_at, "
        "situation_vector, decision, horizon_days, tier, importance, created_at) "
        "VALUES ('A', 'KR', 1, 1, 'x', '[]', '{}', 5, 'short', 0.5, 'x')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(
            "INSERT INTO memory_entries(ticker, market, decision_id, round, decided_at, "
            "situation_vector, decision, horizon_days, tier, importance, created_at) "
            "VALUES ('A', 'KR', 1, 1, 'x', '[]', '{}', 5, 'short', 0.5, 'x')"
        )
    idx = {r["name"] for r in c.execute("PRAGMA index_list(memory_entries)")}
    assert "ix_memory_outcome" in idx


def test_migration_memory_holding_is_market_specific_and_uses_latest_prior_snapshot(tmp_path):
    """Codex: KR 스냅샷만 있는 날 US HOLD 를 '미보유' 로 굳히지 않는다, 전날 스냅샷도 본다."""
    c = _v20_db(tmp_path / "t.db")
    for i, (ticker, market, day) in enumerate(
        [("AAPL", "US", "2026-09-02"), ("005930", "KR", "2026-09-02")], start=1
    ):
        c.execute(
            "INSERT INTO decisions(id, date, market, ticker, action, json, created_at) "
            "VALUES (?, ?, ?, ?, 'HOLD', '{}', ?)",
            (i, day, market, ticker, day),
        )
        c.execute(
            "INSERT INTO memory_entries(id, ticker, market, account_id, decision_id, decided_at, "
            "situation_vector, decision, horizon_days, outcome_at, tier, importance, created_at, "
            "price_return) VALUES (?, ?, ?, 'paper-main', ?, '2026-09-02T01:00:00+00:00', '[]', "
            "'{\"action\": \"HOLD\"}', 5, '2026-09-10', 'short', 0.5, 'x', 0.03)",
            (i, ticker, market, i),
        )
    # KR: 전날(09-01) 스냅샷에만 보유 → 보유. US: KR 스냅샷만 있음 → 모름
    c.execute(
        "INSERT INTO positions_snapshot(account_id, date, ticker, market, qty, created_at) "
        "VALUES ('paper-main', '2026-09-01', '005930', 'KR', 3, 'x')"
    )
    c.commit()
    db.init_schema(c)
    rows = {r["ticker"]: dict(r) for r in c.execute("SELECT * FROM memory_entries")}
    assert rows["005930"]["held_at_decision"] == 1
    assert rows["005930"]["direction_score"] == pytest.approx(0.03)
    assert rows["AAPL"]["held_at_decision"] is None and rows["AAPL"]["direction_score"] == 0.0


def test_decision_quality_caps_reevaluation_weight_per_decision(conn):
    from trader.calibration import model as cal

    # 결정 1: 아침 +, 재평가 10건 전부 −. 결정 2: 아침 −. 재평가가 결정당 합 0.5 라
    # 적중률 = (1) / (1 + 0.5 + 1) = 0.4
    d1 = _decision_row(conn, "A", "BUY", DAYS[0])
    d2 = _decision_row(conn, "B", "BUY", DAYS[0])
    entries = [(_record(conn, d1, "A", "BUY", DAYS[0]), 0.02)]
    for n in range(1, 11):
        entries.append((_record(conn, d1, "A", "ADD", DAYS[0], round_no=n, hour=2), -0.01))
    entries.append((_record(conn, d2, "B", "BUY", DAYS[0]), -0.02))
    for e, ds in entries:
        store.fill_outcome(
            conn,
            e,
            realized_return=ds,
            alpha=0.0,
            outcome_at=NOW.isoformat(),
            price_return=ds,
            direction_score=ds,
        )
    q = cal.decision_quality(conn)
    assert q["n"] == 12 and q["hit_rate"] == pytest.approx(0.4)


def test_approved_order_belongs_to_round_that_requested_it(conn, project):
    """Codex: 승인 주문은 승인 시각에 생긴다 — 요청한 라운드(아침)로 돌려야 한다."""
    _store({"A": [100] * 5 + [110] * 25})
    dec_id = _decision_row(conn, "A", "BUY", DAYS[0])
    morning = _record(conn, dec_id, "A", "BUY", DAYS[0], hour=1)
    reeval = _record(conn, dec_id, "A", "ADD", DAYS[0], round_no=1, hour=2)
    requested = _at(DAYS[0], 1) + timedelta(seconds=5)
    approved = _at(DAYS[0], 3)  # 재평가 뒤 승인
    conn.execute(
        "INSERT INTO approvals(decision_id, account_id, order_json, status, requested_at, "
        "expires_at, decided_at) VALUES (?, 'paper-main', '{}', 'approved', ?, ?, ?)",
        (dec_id, requested.isoformat(), approved.isoformat(), approved.isoformat()),
    )
    oid = _order_with_fill(conn, dec_id, "BUY", approved + timedelta(seconds=1), 104)
    conn.execute("UPDATE orders SET mode='approve' WHERE id=?", (oid,))
    conn.commit()
    outcomes.mature(conn, state=None, today=DAYS[10])
    assert store.get_entry(conn, morning)["realized_pnl_pct"] == pytest.approx(110 / 104 - 1)
    assert store.get_entry(conn, reeval)["realized_pnl_pct"] is None


def test_us_reevaluation_after_kst_midnight_keeps_round_order():
    """Codex: US 운영일 재평가가 KST 자정을 넘겨도 decided_at 이 그날 15:30 KST 로 되돌지 않는다."""
    from trader.orchestrator import tools
    from trader.orchestrator.run_store import run_date_for

    morning = datetime(2026, 10, 26, 13, 30, tzinfo=UTC)  # 22:30 KST, NY 09:30
    late = datetime(2026, 10, 26, 16, 0, tzinfo=UTC)  # 다음날 01:00 KST, NY 12:00
    run = run_date_for("US", morning)
    assert run_date_for("US", late) == run
    assert tools._decided_at(morning, run, "US") == morning
    assert tools._decided_at(late, run, "US") == late


def test_us_consumers_use_decision_run_date_not_kst_date(conn):
    """Codex(GitHub): US 결정이 KST 자정 뒤 실제 시각으로 남아도 보정·복기·백테스트는
    운영일로 묶는다."""
    from helpers_agents import ok_report

    from trader import backtest
    from trader.calibration import model as cal
    from trader.orchestrator import reflect

    run = date(2026, 10, 26)
    dec_id = repo.ingest_checkpoint(
        conn, run, "US", "AAPL", "30_decision", {"data": ok_decision(ticker="AAPL")}
    )
    repo.ingest_checkpoint(
        conn,
        run,
        "US",
        "AAPL",
        "10_analyst_technical",
        {"data": ok_report(role="technical", ticker="AAPL", signal="BUY")},
    )
    late = datetime(2026, 10, 26, 16, 0, tzinfo=UTC)  # 10-27 01:00 KST
    e = store.record_decision(
        conn,
        decision_id=dec_id,
        ticker="AAPL",
        market="US",
        account_id="paper-main",
        decided_at=late.isoformat(),
        decision={"action": "BUY", "confidence": 0.6},
        row=None,
        horizon_days=5,
        vector=[0.5] * 12,
    )
    store.fill_outcome(
        conn, e, realized_return=0.02, alpha=0.0, outcome_at=NOW.isoformat(), price_return=0.02
    )
    hits = cal.signal_hits(conn)
    assert list(hits["role"]) == ["technical"] and list(hits["realized_sign"]) == [1]
    assert reflect.pending(conn, market="US")[0]["analyst_signals"] == {"technical": "BUY"}
    bt = backtest.simple_backtest(conn, None, since=run, market="US")
    assert bt["n"] == 1
