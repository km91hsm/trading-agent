"""Phase 25 단계 6: 공시 중요도 (합성 데이터만 — 실제 공시 원문 없음)."""

from datetime import UTC, datetime

import pytest

from trader.sources import disclosures as D

NOW = datetime(2026, 10, 26, 3, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "name,sev",
    [
        ("주식등의대량보유상황보고서", None),
        ("감자결정", "critical"),
        ("주권매매거래정지", "critical"),
        ("상장폐지 사유 발생", "critical"),
        ("회생절차개시신청", "critical"),
        ("감사보고서제출(의견거절)", "critical"),
        ("유상증자결정", "high"),
        ("전환사채권발행결정", "high"),
        ("최대주주변경", "high"),
    ],
)
def test_classify_dart(name, sev):
    assert D.classify_dart(name) == sev


def test_classify_8k_picks_most_severe():
    assert D.classify_8k(["2.02", "9.01"]) == "info"
    assert D.classify_8k(["2.02", "1.03"]) == "critical"
    assert D.classify_8k(["1.05"]) == "high"
    assert D.classify_8k(["8.01"]) is None


class FakeDart:
    def market_disclosures(self, day, pages=3):
        return [
            {
                "stock_code": "111111",
                "corp_name": "가나",
                "report_nm": "감자결정",
                "rcept_no": "1",
                "rcept_dt": "20261026",
            },
            {
                "stock_code": "999999",
                "corp_name": "밖",
                "report_nm": "상장폐지",
                "rcept_no": "2",
                "rcept_dt": "20261026",
            },
            {
                "stock_code": "111111",
                "corp_name": "가나",
                "report_nm": "기업설명회",
                "rcept_no": "3",
                "rcept_dt": "20261026",
            },
        ]


def test_collect_kr_filters_tickers_and_is_idempotent(conn):
    out = D.collect(conn, market="KR", tickers={"111111"}, now=NOW, dart=FakeDart())
    assert out == {"inserted": 2, "critical": 1}
    assert (
        D.collect(conn, market="KR", tickers={"111111"}, now=NOW, dart=FakeDart())["inserted"] == 0
    )
    rows = conn.execute(
        "SELECT source, severity, tickers_json FROM news_items ORDER BY id"
    ).fetchall()
    assert [(r["source"], r["severity"]) for r in rows] == [("dart", "critical"), ("dart", None)]
    (c,) = D.critical_recent(
        conn, "KR", since=datetime(2026, 10, 1, tzinfo=UTC), tickers={"111111"}
    )
    assert c["tickers"] == ["111111"]


class FakeEdgar:
    def cik(self, s):
        return "0000000001" if s == "ABC" else None

    def submissions(self, cik):
        return {
            "filings": {
                "recent": {
                    "form": ["8-K", "10-Q", "8-K", "8-K"],
                    "filingDate": ["2026-10-25", "2026-10-25", "2026-10-01", "2026-10-24"],
                    "accessionNumber": ["0001-26-1", "0001-26-2", "0001-26-3", "0001-26-4"],
                    "primaryDocument": ["a.htm", "b.htm", "c.htm", "d.htm"],
                    "items": ["1.03,9.01", "", "2.02", "2.02"],
                }
            }
        }


def test_collect_us_only_recent_8k(conn):
    out = D.collect(conn, market="US", tickers={"ABC", "ZZZ"}, now=NOW, edgar=FakeEdgar())
    assert out == {"inserted": 2, "critical": 1}
    rows = conn.execute("SELECT title, severity, url FROM news_items ORDER BY id").fetchall()
    assert rows[0]["severity"] == "critical" and rows[0]["url"].endswith(
        "/000126 1/a.htm".replace(" ", "")
    )
    assert rows[1]["severity"] == "info"


def test_collect_without_client_skips(conn):
    assert D.collect(conn, market="KR", tickers={"1"}, now=NOW)["skipped"]


class TwoClassEdgar(FakeEdgar):
    def cik(self, s):
        return "0000000001" if s in ("GOOG", "GOOGL") else None


def test_shared_cik_filing_keeps_all_ticker_tags(conn):
    out = D.collect(conn, market="US", tickers={"GOOG", "GOOGL"}, now=NOW, edgar=TwoClassEdgar())
    assert out["critical"] == 1
    for t in ("GOOG", "GOOGL"):
        assert D.critical_recent(conn, "US", since=datetime(2026, 10, 1, tzinfo=UTC), tickers={t})


def test_later_target_merges_tags_into_existing_row(conn):
    D.collect(conn, market="US", tickers={"GOOG"}, now=NOW, edgar=TwoClassEdgar())
    D.collect(conn, market="US", tickers={"GOOGL"}, now=NOW, edgar=TwoClassEdgar())
    (c,) = D.critical_recent(conn, "US", since=datetime(2026, 10, 1, tzinfo=UTC), tickers={"GOOGL"})
    assert sorted(c["tickers"]) == ["GOOG", "GOOGL"]


def test_dart_published_at_is_utc(conn):
    D.collect(conn, market="KR", tickers={"111111"}, now=NOW, dart=FakeDart())
    pub = conn.execute("SELECT published_at FROM news_items LIMIT 1").fetchone()[0]
    assert pub == "2026-10-25T15:00:00+00:00"  # 2026-10-26 00:00 KST
