"""종목명: 원장 ticker_names(관측 시 upsert) → 캐시 JSON(fetched_at 무시) → 코드만. 네트워크 없음"""

import json
from decimal import Decimal

from trader import names
from trader.ledger import repo


def _write_caches(cache_dir, *, broken=False):
    cache_dir.mkdir(parents=True, exist_ok=True)
    if broken:
        (cache_dir / "dart_corpcode.json").write_text("{not json", encoding="utf-8")
        return
    (cache_dir / "dart_corpcode.json").write_text(
        json.dumps(
            {
                "fetched_at": "2020-01-01T00:00:00+00:00",  # 만료된 캐시라도 이름은 읽는다
                "map": {"005930": {"corp_code": "00126380", "corp_name": "삼성전자"}},
            }
        ),
        encoding="utf-8",
    )
    (cache_dir / "edgar_tickers.json").write_text(
        json.dumps(
            {
                "fetched_at": "2020-01-01T00:00:00+00:00",
                "map": {"AAPL": {"cik": "1", "name": "Apple Inc."}},
            }
        ),
        encoding="utf-8",
    )


def test_upsert_ignores_empty_and_ticker_equal_names(conn):
    assert repo.upsert_ticker_name(conn, "KR", "005930", "", "balance") is False
    assert repo.upsert_ticker_name(conn, "KR", "005930", "005930", "inputs") is False
    assert repo.upsert_ticker_name(conn, "KR", "005930", " 삼성전자 ", "balance") is True
    assert repo.ticker_names(conn) == {("KR", "005930"): "삼성전자"}
    # 같은 키는 덮어쓴다 (최근 관측 우선)
    repo.upsert_ticker_name(conn, "KR", "005930", "삼성전자(우)", "inputs")
    assert repo.ticker_names(conn, market="KR") == {("KR", "005930"): "삼성전자(우)"}
    assert repo.ticker_names(conn, market="US") == {}


def test_load_cache_names_reads_expired_cache_and_tolerates_missing(project):
    cache = project / "state" / "cache"
    assert names.load_cache_names(cache) == {}  # 디렉터리 없음
    _write_caches(cache)
    got = names.load_cache_names(cache)
    assert got[("KR", "005930")] == "삼성전자" and got[("US", "AAPL")] == "Apple Inc."
    _write_caches(cache, broken=True)
    assert ("KR", "005930") not in names.load_cache_names(cache)


def test_resolve_prefers_ledger_then_cache_and_records_cache_hit(conn, project):
    cache = project / "state" / "cache"
    _write_caches(cache)
    assert names.resolve(conn, cache, "KR", "005930") == "삼성전자"
    row = conn.execute(
        "SELECT source FROM ticker_names WHERE market='KR' AND ticker='005930'"
    ).fetchone()
    assert row["source"] == "cache"
    repo.upsert_ticker_name(conn, "US", "AAPL", "애플", "balance")
    assert names.resolve(conn, cache, "US", "AAPL") == "애플"  # 원장(잔고 관측)이 캐시보다 우선
    assert names.resolve(conn, cache, "KR", "999999") is None


def test_label_and_display(conn, project):
    assert names.label("005930", "삼성전자") == "삼성전자(005930)"
    assert names.label("005930", None) == "005930"
    assert names.label("005930", "") == "005930"
    assert names.display(conn, project / "state" / "cache", "KR", "005930") == "005930"
    repo.upsert_ticker_name(conn, "KR", "005930", "삼성전자", "inputs")
    assert names.display(conn, project / "state" / "cache", "KR", "005930") == "삼성전자(005930)"
    assert names.display(None, project / "state" / "cache", "KR", "005930") == "005930"  # 원장 없음


def test_position_name_is_recorded_by_build_portfolio(conn, project):
    from datetime import date

    from conftest import make_account

    from trader.broker.fake import FakeBroker
    from trader.broker.models import Cash, Position
    from trader.orchestrator import executor

    acct = make_account("paper-main", "paper", "auto")
    broker = FakeBroker(
        acct,
        positions=[
            Position(
                ticker="005930",
                market="KR",
                qty=1,
                avg_price=Decimal("70000"),
                current_price=Decimal("71000"),
                eval_amount=Decimal("71000"),
                pnl_pct=0.01,
                name="삼성전자",
            )
        ],
        cash={"KR": Cash(currency="KRW", available=Decimal("1000"), total=Decimal("1000"))},
    )
    executor.build_portfolio(conn, broker, acct, "KR", date(2026, 10, 23))
    assert repo.ticker_names(conn) == {("KR", "005930"): "삼성전자"}


def test_approval_request_and_status_use_names(conn, project):
    """승인 요청 알림·approvals.order·STATUS·텔레그램 한 줄이 모두 '이름 코드' 표기를 쓴다."""
    from datetime import UTC, date, datetime

    from conftest import make_account
    from helpers_agents import ok_decision

    from trader import status
    from trader.agents.schemas import Decision
    from trader.broker.fake import FakeBroker
    from trader.broker.models import Cash
    from trader.config import Settings
    from trader.notify import commands
    from trader.orchestrator import executor

    class Sent:
        def __init__(self):
            self.sent = []

        def send(self, text, **kw):
            self.sent.append(text)
            return True

    repo.upsert_ticker_name(conn, "KR", "005930", "삼성전자", "inputs")
    acct = make_account("paper-main", "paper", "approve")
    broker = FakeBroker(
        acct,
        cash={"KR": Cash(currency="KRW", available=Decimal("10000000"), total=Decimal("10000000"))},
    )
    t0 = datetime(2026, 10, 23, 1, 0, tzinfo=UTC)
    ectx = executor.ExecContext(
        settings=Settings(),
        accounts=[acct],
        conn=conn,
        brokers={acct.id: broker},
        notifier=Sent(),
        now=lambda: t0,
    )
    dec = Decision(**ok_decision())
    d = date(2026, 10, 23)
    repo.ingest_checkpoint(conn, d, "KR", "005930", "30_decision", {"data": dec.model_dump()})
    executor.execute(
        ectx, dec, date=d, market="KR", price=Decimal("71000"), exchange=None, dry_run=False
    )
    assert any("삼성전자(005930)" in m for m in ectx.notifier.sent)
    pending = repo.pending_approvals(conn)
    assert pending and pending[0]["order"].get("name") == "삼성전자"
    assert "삼성전자(005930)" in commands.approval_line(pending[0])
    (project / "docs" / "TODO.md").write_text("", encoding="utf-8")
    text = status.build_status(conn, Settings(), [acct], project / "docs" / "TODO.md", now=t0)
    assert "매수 삼성전자(005930)" in text
    # 승인 실행은 order 의 name 을 벗겨 OrderRequest 를 만든다
    r = executor.approve(ectx, pending[0]["id"])
    assert r["ok"] is True and broker.orders[0].ticker == "005930"


def test_configured_instrument_names_fill_etfs(conn, project):
    """헤지·현금·지수 대용 ETF 는 DART 기업코드 캐시에 없다 — 설정 `instrument_names` 가 마지막
    대체 경로이고(원장·캐시가 먼저), 적중하면 원장에 source='config' 로 남긴다."""
    cache = project / "state" / "cache"
    assert names.resolve(conn, cache, "KR", "252670") == "KODEX 200선물인버스2X"
    assert repo.ticker_names(conn)[("KR", "252670")] == "KODEX 200선물인버스2X"
    got = names.resolve_many(conn, cache, [("KR", "114800"), ("US", "SH"), ("KR", "999999")])
    assert got == {("KR", "114800"): "KODEX 인버스", ("US", "SH"): "ProShares Short S&P500"}
    repo.upsert_ticker_name(conn, "KR", "069500", "관측 이름", "balance")
    assert names.resolve(conn, cache, "KR", "069500") == "관측 이름"  # 관측값 우선
    assert names.display(conn, cache, "KR", "252670") == "KODEX 200선물인버스2X(252670)"


def test_fill_missing_names_from_broker(conn, project):
    """데몬 names_fill: 원장에 나온 종목 중 이름이 없는 것만 조회해 source='kis' 로 남긴다.
    못 찾은 종목은 그날 다시 묻지 않는다."""
    from datetime import date

    from trader.ledger import repo as r

    for t in ("252670", "005930", "777777"):
        r.snapshot_positions(conn, account_id="paper-main", d=date(2026, 10, 29), market="KR",
                             nav=Decimal(1), cash=Decimal(1),
                             positions=[{"ticker": t, "market": "KR", "qty": 1}])  # fmt: skip
    r.upsert_ticker_name(conn, "KR", "005930", "삼성전자", "balance")
    asked: list[tuple[str, str]] = []

    def lookup(market, ticker, exchange=None):
        asked.append((market, ticker))
        return {"777777": "합성 종목"}.get(ticker)

    cache = project / "state" / "cache"
    out = names.fill_missing(conn, lookup, cache_dir=cache, today=date(2026, 10, 29))
    # 252670 은 설정 이름으로 채워져 조회하지 않고, 005930 은 이미 있다
    assert asked == [("KR", "777777")] and out["filled"] == 1
    assert r.ticker_names(conn)[("KR", "777777")] == "합성 종목"
    asked.clear()
    r.snapshot_positions(conn, account_id="paper-main", d=date(2026, 10, 29), market="KR",
                         nav=Decimal(1), cash=Decimal(1),
                         positions=[{"ticker": "888888", "market": "KR", "qty": 1}])  # fmt: skip
    names.fill_missing(conn, lookup, cache_dir=cache, today=date(2026, 10, 29))
    names.fill_missing(conn, lookup, cache_dir=cache, today=date(2026, 10, 29))
    assert asked == [("KR", "888888")]  # 못 찾은 종목은 그날 한 번만
