import json

from trader import names
from trader.ledger import repo


def _cache(tmp_path):
    d = tmp_path / "cache"
    d.mkdir()
    (d / "dart_corpcode.json").write_text(
        json.dumps(
            {
                "map": {
                    "005930": {"corp_name": "삼성전자"},
                    "009150": {"corp_name": "삼성전기"},
                    "066570": {"corp_name": "LG전자"},
                }
            }
        ),
        encoding="utf-8",
    )
    (d / "edgar_tickers.json").write_text(
        json.dumps({"map": {"AAPL": {"name": "Apple Inc."}}}), encoding="utf-8"
    )
    return d


def test_search_partial_name(tmp_path, conn):
    d = _cache(tmp_path)
    got = names.search(conn, d, "전자", market="KR")
    assert {r["ticker"] for r in got} == {"005930", "066570"}
    assert names.search(conn, d, "apple")[0]["ticker"] == "AAPL"
    assert names.search(conn, d, "0059")[0] == {
        "market": "KR",
        "ticker": "005930",
        "name": "삼성전자",
        "exchange": None,
        "source": "listed",
    }


def test_search_ranks_held_first(tmp_path, conn):
    d = _cache(tmp_path)
    repo.upsert_ticker_name(conn, "KR", "000660", "SK하이닉스", "inputs")
    got = names.search(
        conn, d, "삼성", market="KR", held={("KR", "009150")}, universe={("KR", "005930"): None}
    )
    assert [(r["ticker"], r["source"]) for r in got] == [("009150", "held"), ("005930", "universe")]
    assert names.search(conn, d, "하이닉스")[0]["source"] == "ledger"


def test_search_empty_query_returns_empty(tmp_path, conn):
    assert names.search(conn, _cache(tmp_path), "  ") == []
