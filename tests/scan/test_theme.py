"""Phase 25 단계 4: 테마 열기 점수 (합성 데이터)."""

from datetime import UTC, date, datetime

import pytest

from trader.broker.models import RankRow
from trader.config import Settings
from trader.scan import theme

W = Settings().theme.weights.model_dump()
SECT = {"111111": "반도체", "222222": "반도체", "333333": "2차전지"}.get


def _news(i, title, tickers, source="naver"):
    return {"id": i, "title": title, "tickers": tickers, "source": source}


def _inp(**over):
    base = dict(market="KR", news_today=[], news_prev=[], sector_of=SECT)
    base.update(over)
    return theme.ThemeInputs(**base)


def test_news_delta_raises_score():
    inp = _inp(
        news_today=[_news(1, "HBM 수주", ["111111"]), _news(2, "HBM 증설", ["222222"])],
        news_prev=[],
    )
    got = theme.score_themes(inp, theme.cluster_themes(inp), W)
    assert got[0]["theme"] == "섹터:반도체" and got[0]["components"]["news"] == 1.0
    assert got[0]["rank"] == 1 and got[0]["members"] == ["111111", "222222"]


def test_catalyst_requires_non_community_source():
    inp = _inp(news_today=[_news(1, "a", ["333333"], source="saveticker:로이터")])
    (t,) = theme.score_themes(inp, theme.cluster_themes(inp), W)
    assert t["catalyst_ok"] is False
    inp = _inp(news_today=[_news(1, "a", ["333333"], source="google_rss")])
    (t,) = theme.score_themes(inp, theme.cluster_themes(inp), W)
    assert t["catalyst_ok"] is True and t["catalyst_ids"] == [1]


def test_keyword_cluster_threshold():
    titles = ["로봇 수주 확대", "로봇 부품 공급", "로봇 투자"]
    inp = _inp(news_today=[_news(i, t, ["999999"]) for i, t in enumerate(titles)], sector_of=None)
    assert "kw:로봇" in theme.cluster_themes(inp, min_titles=3)
    assert "kw:로봇" not in theme.cluster_themes(inp, min_titles=4)


def test_compute_without_rank_api_still_scores(project, conn):
    """순위 API 가 없거나(US·실패) 빈 목록이어도 뉴스·세이브티커로 점수."""
    (project / "config" / "sectors.yaml").write_text(
        "sectors: [반도체, 기타]\nkr: {'005930': 반도체}\nus: {}\n", encoding="utf-8"
    )
    conn.execute(
        "INSERT INTO news_items(market, source, title, url, url_hash, published_at, received_at,"
        " tickers_json, novel) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "KR",
            "naver",
            "HBM 수주",
            "u",
            "h1",
            "2026-10-26T01:00:00+00:00",
            "2026-10-26T01:00:00+00:00",
            '["005930"]',
            1,
        ),
    )
    conn.commit()

    class Broken:
        def volume_rank_kr(self, limit=30):
            raise RuntimeError("필드 미확인")

        fluctuation_rank_kr = foreign_institution_kr = volume_rank_kr

    heat = theme.compute(
        conn,
        Settings(),
        market="KR",
        today=date(2026, 10, 26),
        now=datetime(2026, 10, 26, 2, tzinfo=UTC),
        market_client=Broken(),
    )
    assert heat["signals"]["ranks"] == {"volume": 0, "fluct": 0, "flow": 0}
    assert heat["themes"] and heat["themes"][0]["catalyst_ok"] is True


def test_rank_and_flow_components():
    inp = _inp(
        ranks={
            "volume": [RankRow(ticker="333333", rank=1)],
            "flow": [RankRow(ticker="333333", rank=1, net_buy=10)],
        }
    )
    (t,) = theme.score_themes(inp, theme.cluster_themes(inp), W)
    assert t["theme"] == "섹터:2차전지"
    assert t["components"]["rank"] == 1.0 and t["components"]["flow"] == 1.0
    assert "외인·기관 순매수" in t["tickers"][0]["why"]


def test_saveticker_ranks_reads_daily_files(tmp_path):
    import json

    d = tmp_path / "data" / "2026-10-26"
    d.mkdir(parents=True)
    rows = [
        {"item": {"rank": 3, "tickers": ["nvda"]}},
        {"item": {"rank": 1, "tickers": ["NVDA"]}},
        {"item": {"rank": "x"}},
    ]
    (d / "market.jsonl").write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    assert theme.saveticker_ranks(tmp_path, date(2026, 10, 26)) == {"NVDA": 1}
    assert theme.saveticker_ranks(tmp_path, date(2026, 10, 25)) == {}


def test_theme_of_map_today_only(conn):
    from trader.control import control

    control.set_flag(
        conn,
        "theme_heat:KR",
        {
            "date": "2026-10-26",
            "themes": [
                {"theme": "섹터:반도체", "members": ["111111", "222222"], "tickers": []},
                {"theme": "kw:로봇", "members": ["999999"], "tickers": []},
            ],
        },
    )
    assert theme.theme_of_map(conn, "KR", date(2026, 10, 26), top_n=1) == {
        "111111": "섹터:반도체",
        "222222": "섹터:반도체",
    }
    assert theme.theme_of_map(conn, "KR", date(2026, 10, 27), top_n=3) == {}


def test_mature_fills_forward_return(conn, monkeypatch):
    import pandas as pd

    from trader.memory import outcomes

    conn.execute(
        "INSERT INTO theme_heat(market, date, observed_at, theme, rank, score, catalyst_ok, "
        "tickers_json, components_json) VALUES ('KR','2026-10-01','x','섹터:반도체',1,0.9,1,"
        "?, '{}')",
        ('["111111"]',),
    )
    days = pd.date_range("2026-10-01", periods=8, freq="B").date
    table = pd.DataFrame({"111111": [100.0] * 5 + [110.0] * 3, "222222": [100.0] * 8}, index=days)
    monkeypatch.setattr(outcomes, "_close_table", lambda *a, **k: table)
    assert theme.mature(conn, "KR", state=None, today=date(2026, 10, 26)) == 1
    v = conn.execute("SELECT fwd_ret_5d FROM theme_heat").fetchone()[0]
    assert v == pytest.approx(0.1 - 0.05)  # 종목 +10%, 1/N(두 종목 평균) +5%
    assert theme.mature(conn, "KR", state=None, today=date(2026, 10, 26)) == 0
