"""뉴스 발굴(scan/discovery): 상호 정규화·역색인·코드 확정·제외·US 거래소·병합 순서."""

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from trader.agents import schemas
from trader.config import Settings, Universe, UsSymbol
from trader.orchestrator import tools
from trader.orchestrator.run_store import market_dir, read_checkpoint
from trader.scan import discovery
from trader.sources import news_store

D = date(2026, 10, 9)
T0 = datetime(2026, 10, 9, 6, 0, tzinfo=UTC)
CACHE = {
    ("KR", "005930"): "삼성전자",
    ("KR", "000660"): "SK하이닉스",
    ("KR", "247540"): "에코프로비엠",
    ("US", "AAPL"): "Apple Inc.",
    ("US", "NVDA"): "NVIDIA Corp",
}
UNI_KR = Universe(kr=["005930", "000660"], exclude=["000020"])
UNI_US = Universe(us=[UsSymbol(symbol="AAPL", exchange="NAS")])


def test_normalize_and_reverse_index():
    assert (
        discovery.normalize("(주)에코프로비엠")
        == discovery.normalize("에코프로비엠 ")
        == "에코프로비엠"
    )
    assert discovery.normalize("Apple Inc.") == discovery.normalize("APPLE") == "apple"
    assert discovery.normalize("NVIDIA Corporation") == "nvidia"
    idx = discovery.reverse_index(CACHE, "KR")
    assert idx["에코프로비엠"] == "247540" and "apple" not in idx
    assert discovery.reverse_index(CACHE, "US")["apple"] == "AAPL"


def _disc(*cands, notes=()):
    return {"candidates": list(cands), "notes": list(notes)}


def _c(name, ticker=None, tier="universe", market="KR", conf=0.7, catalyst="촉매"):
    return {
        "name": name,
        "ticker": ticker,
        "market": market,
        "tier": tier,
        "catalyst": catalyst,
        "source_ids": ["news:1"],
        "confidence": conf,
    }


def test_resolve_universe_by_code_or_name_and_demotes_unknown_to_listed():
    out = discovery.resolve(
        _disc(
            _c("삼성전자", "005930"),  # 코드 일치
            _c("SK하이닉스", None),  # 이름으로 유니버스 코드
            _c(
                "에코프로비엠", None, tier="universe"
            ),  # 유니버스 아님 → listed 로 강등, 캐시 역색인
            _c("없는회사", None),  # 못 찾음
            _c("동화약품", "000020"),  # exclude
            _c("삼성전자", "005930"),  # 중복
        ),
        market="KR",
        universe=UNI_KR,
        cache_names=CACHE,
    )
    got = {c["ticker"]: c for c in out["candidates"]}
    assert set(got) == {"005930", "000660", "247540"}
    assert got["000660"]["tier"] == "universe" and got["247540"]["tier"] == "listed"
    assert got["000660"]["name"] == "SK하이닉스" and got["247540"]["exchange"] is None
    reasons = {u["name"]: u["reason"] for u in out["unresolved"]}
    assert "코드를 찾지 못함" in reasons["없는회사"] and "제외" in reasons["동화약품"]


def test_resolve_listed_cap_and_kr_quote_check():
    client = SimpleNamespace(
        get_price_kr=lambda t: SimpleNamespace(price=Decimal(1) if t != "999990" else None)
    )
    out = discovery.resolve(
        _disc(
            _c("에코프로비엠", None, conf=0.9),
            _c("A사", "999990", tier="listed", conf=0.8),  # 시세 없음
            _c("B사", "999991", tier="listed", conf=0.7),
            _c("C사", "999992", tier="listed", conf=0.6),  # 상한 2 초과
        ),
        market="KR",
        universe=UNI_KR,
        cache_names=CACHE,
        market_client=client,
    )
    assert [c["ticker"] for c in out["candidates"]] == ["247540", "999991"]
    reasons = [u["reason"] for u in out["unresolved"]]
    assert any("시세 없음" in r for r in reasons) and any("상한 2" in r for r in reasons)


def test_resolve_us_listed_needs_exchange_from_quotes():
    seen = []

    def price(sym, exch):
        seen.append((sym, exch))
        if (sym, exch) == ("NVDA", "NYS"):
            raise ValueError("no")
        if sym == "NVDA" and exch == "AMS":
            return SimpleNamespace(price=Decimal(1))
        if sym == "NVDA":
            return SimpleNamespace(price=None)
        raise ValueError("no")

    out = discovery.resolve(
        _disc(
            _c("NVIDIA Corp", None, market="US", tier="listed"),
            _c("Apple Inc.", None, market="US"),
            _c("Zzz Co", "ZZZ", market="US", tier="listed"),
        ),
        market="US",
        universe=UNI_US,
        cache_names=CACHE,
        market_client=SimpleNamespace(get_price_us=price),
    )
    got = {c["ticker"]: c for c in out["candidates"]}
    assert got["AAPL"]["tier"] == "universe" and got["AAPL"]["exchange"] == "NAS"
    assert got["NVDA"]["tier"] == "listed" and got["NVDA"]["exchange"] == "AMS"
    assert seen[:3] == [("NVDA", "NAS"), ("NVDA", "NYS"), ("NVDA", "AMS")]
    assert out["unresolved"][0]["ticker"] == "ZZZ" and "거래소" in out["unresolved"][0]["reason"]
    # 시세 클라이언트가 없으면 US 유니버스 밖은 확정할 수 없다
    out2 = discovery.resolve(
        _disc(_c("NVIDIA Corp", "NVDA", market="US", tier="listed")),
        market="US",
        universe=UNI_US,
        cache_names=CACHE,
    )
    assert out2["candidates"] == [] and out2["unresolved"]


def test_merge_orders_universe_hits_then_listed_then_rest():
    quant = [
        {"ticker": "000660", "exchange": None, "score": 9.0, "reasons": ["r"]},
        {"ticker": "005930", "exchange": None, "score": 8.0, "reasons": ["r"]},
        {"ticker": "000001", "exchange": None, "score": 7.0, "reasons": ["r"]},
        {"ticker": "000002", "exchange": None, "score": None, "reasons": []},
    ]
    resolved = {
        "candidates": [
            {
                "ticker": "005930",
                "tier": "universe",
                "catalyst": "수주",
                "confidence": 0.8,
                "exchange": None,
            },
            {
                "ticker": "247540",
                "tier": "listed",
                "catalyst": "증설",
                "confidence": 0.9,
                "exchange": None,
            },
            {
                "ticker": "999991",
                "tier": "listed",
                "catalyst": "임상",
                "confidence": 0.5,
                "exchange": None,
            },
            {
                "ticker": "999992",
                "tier": "listed",
                "catalyst": "x",
                "confidence": 0.4,
                "exchange": None,
            },
        ],
        "unresolved": [],
    }
    merged = discovery.merge(quant, resolved, max_listed=2)
    assert [c["ticker"] for c in merged] == [
        "005930",
        "247540",
        "999991",
        "000660",
        "000001",
        "000002",
    ]
    assert merged[0]["discovery"]["tier"] == "universe" and "뉴스 발굴" in merged[0]["reasons"][-1]
    assert merged[1]["score"] is None and merged[1]["discovery"]["catalyst"] == "증설"
    assert "discovery" not in merged[3]
    assert discovery.line(resolved).startswith("뉴스 발굴 4(유니버스 1·밖 3")
    assert discovery.summary(None) is None and discovery.line(None) == "뉴스 발굴 없음"


def test_build_inputs_writes_file_with_news_universe_and_recent_picks(project, conn):
    from datetime import timedelta

    from trader.orchestrator.run_store import write_checkpoint

    news_store.insert_items(
        conn,
        [
            {
                "title": "삼성전자 수주",
                "url": "https://n/1",
                "published_at": (T0 - timedelta(hours=2)).isoformat(),
                "received_at": (T0 - timedelta(hours=2)).isoformat(),
            }
        ],
        market="KR",
        source="google_rss",
        query="수주",
        lang="ko",
        names={"005930": "삼성전자"},
    )
    write_checkpoint(
        market_dir(D - timedelta(days=1), "KR", create=True),
        "_scan",
        {"picks": [{"ticker": "000660"}], "discovery": {"candidates": [{"ticker": "247540"}]}},
    )
    quant = [{"ticker": "005930", "exchange": None, "score": 1.0, "reasons": ["r"]}]
    data = discovery.build_inputs(
        conn,
        Settings(),
        UNI_KR,
        market="KR",
        today=D,
        asof=T0,
        quant_candidates=quant,
        held=["000660"],
        now=lambda: T0,
        cache_names=CACHE,
    )
    assert [u["ticker"] for u in data["universe"]] == ["005930", "000660"]
    assert data["universe"][0]["name"] == "삼성전자"
    assert data["news"][0]["title"] == "삼성전자 수주" and data["news"][0]["tickers"] == ["005930"]
    assert data["recent_picks"] == [
        {"date": (D - timedelta(days=1)).isoformat(), "picks": ["000660"], "discovered": ["247540"]}
    ]
    assert data["held"] == ["000660"] and data["quant_candidates"][0]["ticker"] == "005930"
    saved = read_checkpoint(market_dir(D, "KR"), "_discovery_inputs")
    assert saved["data"]["max_listed"] == discovery.MAX_LISTED


def _ctx(conn):
    from trader.config import AccountConfig

    return tools.ToolContext(
        settings=Settings(),
        accounts=[
            AccountConfig(id="paper-main", env="paper", cano="1", markets=["KR"], mode="auto")
        ],
        conn=conn,
        now=lambda: T0,
    )


def test_tool_discovery_inputs_and_submit_discovery_round_trip(project, conn, monkeypatch):
    import yaml

    (project / "config" / "universe.yaml").write_text(
        yaml.safe_dump({"kr": ["005930", "000660"], "us": [], "watch": [], "exclude": []}),
        encoding="utf-8",
    )
    ctx = _ctx(conn)
    assert tools.discovery_inputs(ctx, market="KR", date=D)["ok"] is False  # 입력 파일 없음
    quant = [
        {"ticker": "000660", "exchange": None, "score": 9.0, "reasons": ["r"]},
        {"ticker": "005930", "exchange": None, "score": 8.0, "reasons": ["r"]},
    ]
    discovery.build_inputs(
        conn,
        Settings(),
        Universe(kr=["005930", "000660"]),
        market="KR",
        today=D,
        asof=T0,
        quant_candidates=quant,
        held=["000660"],
        now=lambda: T0,
        cache_names=CACHE,
    )
    r = tools.discovery_inputs(ctx, market="KR", date=D)
    assert r["ok"] and r["data"]["market"] == "KR" and len(r["data"]["quant_candidates"]) == 2
    bad = tools.submit_discovery(ctx, market="KR", date=D, payload={"candidates": [{"name": "x"}]})
    assert bad["ok"] is False and "Discovered" in bad["error"]
    monkeypatch.setattr("trader.names.load_cache_names", lambda cache_dir: CACHE)
    payload = schemas.Discovered(
        candidates=[
            schemas.DiscoveredCandidate(
                name="삼성전자",
                ticker="005930",
                market="KR",
                tier="universe",
                catalyst="3분기 잠정실적",
                source_ids=["news:1"],
                confidence=0.8,
            ),
            schemas.DiscoveredCandidate(
                name="SK하이닉스",
                ticker="000660",
                market="KR",
                tier="universe",
                catalyst="보유 종목",
                confidence=0.5,
            ),  # held → 제외
            schemas.DiscoveredCandidate(
                name="에코프로비엠",
                ticker=None,
                market="KR",
                tier="listed",
                catalyst="증설",
                confidence=0.9,
            ),
        ],
        notes=["악재: 없음"],
    ).model_dump()
    r = tools.submit_discovery(
        ctx, market="KR", date=D, payload=payload, call_meta={"model": "fake"}
    )
    assert r["ok"] and r["discovered"] == ["005930", "247540"]
    assert [c["ticker"] for c in r["candidates"]] == ["005930", "247540", "000660"]
    assert r["unresolved"][0]["ticker"] == "000660"
    saved = read_checkpoint(market_dir(D, "KR"), "_discovery")
    assert saved["call"]["agent"] == "scout" and saved["data"]["notes"] == ["악재: 없음"]


def test_fake_screen_with_discovery_merges_and_summarizes(project, conn, monkeypatch):
    import yaml

    from trader.scan import screener

    monkeypatch.setenv("FAKE_CLAUDE", "1")
    (project / "config" / "universe.yaml").write_text(
        yaml.safe_dump({"kr": ["000003", "000000"], "us": [], "watch": [], "exclude": []}),
        encoding="utf-8",
    )
    cands = [
        {"ticker": f"0000{i:02d}", "exchange": None, "score": 10 - i, "reasons": ["r"]}
        for i in range(8)
    ]
    from trader.config import AccountConfig

    r = screener.run_screen(
        Settings(),
        conn,
        [AccountConfig(id="paper-main", env="paper", cano="1", markets=["KR"], mode="auto")],
        market="KR",
        date=D,
        candidates=cands,
        cap_hit=False,
        now=lambda: T0,
        asof=T0,
        universe=Universe(kr=["000003", "000000"]),
        discovery=True,
    )
    assert read_checkpoint(market_dir(D, "KR"), "_discovery_inputs") is not None
    # FAKE 는 정량 점수 순으로 첫 유니버스 종목(000000, score 10)을 발굴로 흉내 낸다
    assert r["discovery"]["candidates"][0]["ticker"] == "000000" and r["discovery"]["universe"] == 1
    assert r["picks"][0] == {"ticker": "000000", "reason": "FAKE: 뉴스 발굴"}
    assert len(r["picks"]) == 5


@pytest.mark.parametrize("tier", ["universe", "listed"])
def test_discovered_schema_limits(tier):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        schemas.Discovered(candidates=[_c(f"n{i}", tier=tier) for i in range(9)])
    ok = schemas.Discovered(candidates=[_c("n", tier=tier)])
    assert ok.candidates[0].tier == tier


def test_build_inputs_includes_naver_blog_posts_for_kr_only(project, conn):
    """Phase 21: `sources_cfg.channels.naver_blog` + http 가 있으면 블로그 최근 글을 발굴 입력에
    싣는다(KR 만, 실패는 빈 목록)."""
    import httpx
    import respx

    rss = (
        "<rss><channel><item><title>반도체 사이클</title><description>&lt;p&gt;메모리 업황"
        "&lt;/p&gt;</description><pubDate>Thu, 08 Oct 2026 10:00:00 +0900</pubDate>"
        "<link>https://blog.naver.com/x/1</link></item></channel></rss>"
    )
    cfg = SimpleNamespace(channels=SimpleNamespace(naver_blog=["ranto28"], naver_blog_posts=5))
    quant = [{"ticker": "005930", "exchange": None, "score": 1.0, "reasons": ["r"]}]
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://rss.blog.naver.com/ranto28.xml").respond(200, content=rss.encode())
        with httpx.Client() as http:
            data = discovery.build_inputs(
                conn,
                Settings(),
                UNI_KR,
                market="KR",
                today=D,
                asof=T0,
                quant_candidates=quant,
                now=lambda: T0,
                cache_names=CACHE,
                sources_cfg=cfg,
                http=http,
            )
            assert data["blog_posts"] == [
                {
                    "source": "naver_blog/ranto28",
                    "title": "반도체 사이클",
                    "excerpt": "메모리 업황",
                    "posted_at": "2026-10-08T01:00:00+00:00",
                }
            ]
            us = discovery.build_inputs(
                conn,
                Settings(),
                UNI_US,
                market="US",
                today=D,
                asof=T0,
                quant_candidates=[],
                now=lambda: T0,
                cache_names=CACHE,
                sources_cfg=cfg,
                http=http,
            )
            assert us["blog_posts"] == []
    # http 없음 → 빈 목록, 파일은 그대로 만들어진다
    data = discovery.build_inputs(
        conn,
        Settings(),
        UNI_KR,
        market="KR",
        today=D,
        asof=T0,
        quant_candidates=quant,
        now=lambda: T0,
        cache_names=CACHE,
        sources_cfg=cfg,
    )
    assert data["blog_posts"] == []


def test_news_burst_counts_novel_news_per_universe_or_held_ticker():
    novel = [
        {"id": 1, "title": "a", "tickers": ["005930"]},
        {"id": 2, "title": "b", "tickers": ["005930", "000660"]},
        {"id": 3, "title": "c", "tickers": []},
        {"id": 4, "title": "d", "tickers": ["999999"]},  # 유니버스·보유 밖
    ]
    out = discovery.news_burst(novel, universe_tickers={"005930"}, held={"000660"}, min_items=1)
    assert set(out["tickers"]) == {"005930", "000660"}
    assert len(out["tickers"]["005930"]) == 2 and out["untagged"] == 2 and out["total"] == 4
    out2 = discovery.news_burst(novel, universe_tickers={"005930"}, held=set(), min_items=2)
    assert set(out2["tickers"]) == {"005930"}


def test_build_inputs_intraday_uses_novel_news_only_and_writes_round_copy(project, conn):
    from datetime import timedelta

    news_store.insert_items(
        conn,
        [
            {
                "title": "저장소 48시간 뉴스(싣지 않음)",
                "url": "https://n/old",
                "published_at": (T0 - timedelta(hours=2)).isoformat(),
                "received_at": (T0 - timedelta(hours=2)).isoformat(),
            }
        ],
        market="KR",
        source="google_rss",
        query="q",
        lang="ko",
        names={},
    )
    novel = [{"id": 9, "title": "삼성전자 신규 수주", "tickers": ["005930"], "source": "t"}]
    kw = dict(
        market="KR",
        today=D,
        asof=T0,
        quant_candidates=[],
        held=[],
        now=lambda: T0,
        cache_names=CACHE,
        intraday_news=novel,
        analyzed_today=["000660"],
    )
    data = discovery.build_inputs(conn, Settings(), UNI_KR, **kw)
    assert [n["title"] for n in data["news"]] == ["삼성전자 신규 수주"]
    assert data["intraday_round"] == 1
    assert data["recent_picks"][0] == {"date": D.isoformat(), "picks": ["000660"], "discovered": []}
    d = market_dir(D, "KR")
    assert read_checkpoint(d, "_discovery_inputs_r1")["data"]["intraday_round"] == 1
    data2 = discovery.build_inputs(conn, Settings(), UNI_KR, **kw)
    assert data2["intraday_round"] == 2 and read_checkpoint(d, "_discovery_inputs_r2")
    # 평소(아침) 조립은 저장소 뉴스 그대로, 회차 없음
    kw.pop("intraday_news")
    kw.pop("analyzed_today")
    plain = discovery.build_inputs(conn, Settings(), UNI_KR, **kw)
    assert plain["intraday_round"] is None and plain["news"][0]["title"].startswith("저장소")
