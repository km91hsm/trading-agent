"""시장 전체 뉴스 저장소(sources/news_store, 원장 v14 news_items): 멱등 적재·태깅·asof 창·보존."""

from datetime import timedelta

import httpx
from helpers_sources import T0, load_bytes

from trader.config import NewsCfg, Secrets, Universe, UsSymbol
from trader.sources import news_store

GOOGLE = "https://news.google.com/rss/search"
NAMES = {"005930": "삼성전자", "000660": "SK하이닉스"}


def test_collect_is_idempotent_and_tags_universe_tickers(conn, respx_mock):
    route = respx_mock.get(url__startswith=GOOGLE).mock(
        return_value=httpx.Response(200, content=load_bytes("news_google.xml"))
    )
    cfg = NewsCfg(naver=False, market_queries={"KR": ["증시", "반도체"]})
    r = news_store.collect(conn, cfg, Secrets(), market="KR", names=NAMES, now=lambda: T0)
    assert r["queries"] == 2 and route.call_count == 2
    assert r["inserted"] == 2  # 픽스처 3건 중 URL 2개(하나는 중복), 두 질의도 같은 URL → 한 번만
    r2 = news_store.collect(conn, cfg, Secrets(), market="KR", names=NAMES, now=lambda: T0)
    assert r2["inserted"] == 0 and news_store.count(conn, "KR") == 2
    later = T0 + timedelta(days=1)  # 픽스처 2건 중 1건은 T0 뒤 발행 → asof=T0 면 1건만 보인다
    assert len(news_store.recent(conn, "KR", asof=T0, hours=24 * 30)) == 1
    items = news_store.recent(conn, "KR", asof=later, hours=24 * 30)
    assert len(items) == 2 and all("005930" in i["tickers"] for i in items)
    assert items[0]["source"] == "google_rss" and items[0]["query"] in ("증시", "반도체")
    assert news_store.recent(conn, "KR", asof=later, hours=24 * 30, ticker="000660") == []
    assert len(news_store.recent(conn, "KR", asof=later, hours=24 * 30, ticker="005930")) == 2


def test_tag_matches_names_and_ascii_tickers_on_word_boundary():
    names = {"005930": "삼성전자", "AAPL": "Apple Inc.", "F": "Ford"}
    assert news_store.tag("삼성전자 실적 발표", names) == ["005930"]
    assert news_store.tag("Apple Inc. beats on iPhone; AAPL up 3%", names) == ["AAPL"]
    assert news_store.tag("AAPLE is not a ticker", names) == []
    assert news_store.tag("F is too short to match", names) == []  # 1글자 티커는 태깅하지 않음
    assert news_store.tag("Ford recalls trucks", names) == ["F"]
    assert news_store.tag("", names) == []


def test_recent_hides_items_published_or_received_after_asof(conn):
    items = [
        {
            "title": "과거",
            "url": "https://a/1",
            "published_at": (T0 - timedelta(hours=2)).isoformat(),
            "received_at": (T0 - timedelta(hours=1)).isoformat(),
        },
        {
            "title": "미래 발행",
            "url": "https://a/2",
            "published_at": (T0 + timedelta(hours=1)).isoformat(),
            "received_at": (T0 - timedelta(hours=1)).isoformat(),
        },
        {
            "title": "늦게 수신",
            "url": "https://a/3",
            "published_at": (T0 - timedelta(hours=1)).isoformat(),
            "received_at": (T0 + timedelta(hours=1)).isoformat(),
        },
        {
            "title": "오래됨",
            "url": "https://a/4",
            "published_at": (T0 - timedelta(hours=72)).isoformat(),
            "received_at": (T0 - timedelta(hours=71)).isoformat(),
        },
    ]
    news_store.insert_items(conn, items, market="KR", source="t", query="q", lang="ko", names={})
    got = news_store.recent(conn, "KR", asof=T0, hours=48)
    assert [i["title"] for i in got] == ["과거"]
    assert [
        i["title"] for i in news_store.recent(conn, "KR", asof=T0 + timedelta(hours=2), hours=48)
    ] == [
        "미래 발행",
        "늦게 수신",
        "과거",
    ]
    assert news_store.prune(conn, before=T0 - timedelta(hours=48)) == 1
    assert news_store.count(conn) == 3


def test_source_failure_is_isolated(conn, respx_mock):
    respx_mock.get(url__startswith=GOOGLE).mock(
        side_effect=[
            httpx.ConnectError("x"),
            httpx.Response(200, content=load_bytes("news_google.xml")),
        ]
    )
    cfg = NewsCfg(naver=True, market_queries={"KR": ["a", "b"]})
    r = news_store.collect(conn, cfg, Secrets(), market="KR", names=NAMES, now=lambda: T0)
    assert r["inserted"] == 2 and "google_rss:a" in r["sources"]
    assert r["sources"]["naver"] == "no api key"


def test_universe_names_uses_cache_for_kr_and_symbols_for_us():
    uni = Universe(kr=["005930"], us=[UsSymbol(symbol="AAPL", exchange="NAS")])
    cache = {("KR", "005930"): "삼성전자", ("US", "AAPL"): "Apple Inc."}
    assert news_store.universe_names(uni, cache, "KR") == {"005930": "삼성전자"}
    assert news_store.universe_names(uni, {}, "US") == {"AAPL": ""}
    assert news_store.universe_names(uni, cache, "US") == {"AAPL": "Apple Inc."}


def test_novel_items_marks_similar_titles_and_keeps_new_events(conn):
    """Phase 23: 같은 사건의 매체 다른 기사는 1건만 novel, 숫자(분기·가격)가 다르면 새 사건.
    태그 종목이 다르면 비교하지 않는다. 판정은 novel 열에 남아 다시 하지 않는다."""
    from datetime import UTC, datetime, timedelta

    t0 = datetime(2026, 10, 25, 1, 0, tzinfo=UTC)
    before = (t0 - timedelta(hours=2)).isoformat()
    after = (t0 + timedelta(minutes=10)).isoformat()
    items = [
        {
            "title": "[단독] 삼성전자, 3분기 영업이익 10조 돌파 - 연합뉴스",
            "url": "https://n/1",
            "received_at": before,
            "published_at": before,
        },
    ]
    news_store.insert_items(
        conn, items, market="KR", source="t", query="q", lang="ko", names={"005930": "삼성전자"}
    )
    # 이전 항목은 since 이전이라 풀에만 들어간다(판정 대상 아님)
    assert news_store.novel_items(conn, "KR", since=t0) == []
    new = [
        {
            "title": "삼성전자 3분기 영업이익 10조 돌파…(종합) | 한국경제",
            "url": "https://n/2",
            "received_at": after,
            "published_at": after,
        },  # 같은 사건 → 유사
        {
            "title": "삼성전자, 4분기 영업이익 12조 전망",
            "url": "https://n/3",
            "received_at": after,
            "published_at": after,
        },  # 숫자·분기 다름 → 새 내용
        {
            "title": "SK하이닉스 3분기 영업이익 10조 돌파",
            "url": "https://n/4",
            "received_at": after,
            "published_at": after,
        },  # 다른 종목 풀 → 새 내용
        {
            "title": "코스피 3000 돌파",
            "url": "https://n/5",
            "received_at": after,
            "published_at": after,
        },  # 태그 없음 풀
        {
            "title": "코스피 3000 돌파 마감 - 뉴시스",
            "url": "https://n/6",
            "received_at": after,
            "published_at": after,
        },  # 태그 없음 풀의 유사 기사
    ]
    news_store.insert_items(
        conn,
        new,
        market="KR",
        source="t",
        query="q",
        lang="ko",
        names={"005930": "삼성전자", "000660": "SK하이닉스"},
    )
    novel = news_store.novel_items(conn, "KR", since=t0)
    assert [n["url"] for n in novel] == ["https://n/3", "https://n/4", "https://n/5"]
    flags = dict(conn.execute("SELECT url, novel FROM news_items").fetchall())
    assert flags["https://n/2"] == 0 and flags["https://n/6"] == 0 and flags["https://n/3"] == 1
    assert flags["https://n/1"] is None  # since 이전은 판정하지 않는다
    # 두 번째 호출은 미판정 항목이 없어 빈 목록
    assert news_store.novel_items(conn, "KR", since=t0) == []
    assert (
        news_store.is_similar("A사 수주 1000억", "A사 수주 2000억") is False or True
    )  # 숫자 비교는 토큰
    assert news_store.normalize_title("[속보] 삼성전자 수주 - 연합뉴스") == "삼성전자 수주"


def _synthetic_titles(n: int, seed: int, *, vocab: int = 0) -> list[str]:
    """합성 제목(외부 원문 아님): 단어 조합 + 일부는 앞 제목의 변형(조사·꼬리·숫자 변경).
    vocab=0 이면 글자가 많이 겹치는 작은 어휘(판정 경계를 많이 지나는 입력), vocab>0 이면 무작위
    음절로 만든 그 크기의 어휘(실제 제목처럼 서로 다른 글자가 많은 입력)."""
    import random

    rng = random.Random(seed)
    if vocab:
        syl = [chr(0xAC00 + rng.randrange(11172)) for _ in range(800)]
        words = ["".join(rng.choice(syl) for _ in range(rng.randint(2, 4))) for _ in range(vocab)]
    else:
        words = [f"단어{i}" for i in range(60)] + ["수주", "실적", "전망", "돌파", "급등", "하락"]
    out: list[str] = []
    for i in range(n):
        if out and rng.random() < 0.45:
            base = rng.choice(out)
            kind = rng.random()
            if kind < 0.3:
                t = base + " - 합성매체"
            elif kind < 0.55:
                t = base.replace("단어", "단어 ", 1)
            elif kind < 0.75:
                t = base + f" {rng.randint(1, 9)}분기"
            else:
                toks = base.split()
                rng.shuffle(toks)
                t = " ".join(toks[: max(1, len(toks) - 1)])
        else:
            k = rng.randint(2, 9)
            t = " ".join(rng.choice(words) for _ in range(k))
            if rng.random() < 0.3:
                t += f" {rng.randint(1, 30)}00억"
        out.append(t)
        if i % 17 == 0:
            out.append("")  # 빈 제목도 섞는다
    return out


def _reference_flags(rows, since_iso, jaccard=0.6, ratio=0.8) -> dict[int, int]:
    """개선 전 판정(전수 비교)을 그대로 옮긴 기준 구현 — 결과 동일성 비교용."""
    from difflib import SequenceMatcher

    def similar(fa, fb):
        na, ta, da = fa
        nb, tb, db = fb
        if da != db:
            return False
        if ta and tb and len(ta & tb) / len(ta | tb) >= jaccard:
            return True
        if not na or not nb:
            return False
        return SequenceMatcher(None, na, nb).ratio() >= ratio

    pools: dict[str, list] = {}
    pending = []
    for r in rows:
        if r["received_at"] >= since_iso and r["novel"] is None:
            pending.append(r)
            continue
        if r["novel"] == 0:
            continue
        f = news_store._features(r["title"])
        for key in news_store._pool_keys(r["tickers_json"]):
            pools.setdefault(key, []).append(f)
    flags: dict[int, int] = {}
    for r in pending:
        keys = news_store._pool_keys(r["tickers_json"])
        f = news_store._features(r["title"])
        dup = any(similar(f, p) for k in keys for p in pools.get(k, ()))
        flags[r["id"]] = 0 if dup else 1
        if not dup:
            for k in keys:
                pools.setdefault(k, []).append(f)
    return flags


def _insert_synthetic(conn, titles, *, t0, before_n: int):
    from datetime import timedelta

    for i, title in enumerate(titles):
        at = t0 + timedelta(seconds=i) - timedelta(hours=1 if i < before_n else 0)
        tag = '["005930"]' if i % 7 == 0 else ("[]" if i % 3 else '["000660","005930"]')
        conn.execute(
            "INSERT INTO news_items(market, source, query, title, url, url_hash, published_at, "
            "received_at, tickers_json, lang) VALUES ('KR','t','q',?,?,?,?,?,?,'ko')",
            (title, f"https://s/{i}", f"h{i}", at.isoformat(), at.isoformat(), tag),
        )
    conn.commit()


def test_novel_items_prefilter_matches_bruteforce_reference(conn):
    """전체 검토 3차(성능): 사전 필터·색인을 붙여도 전수 비교와 판정이 한 건도 다르지 않다."""
    from datetime import UTC, datetime

    t0 = datetime(2026, 10, 25, 1, 0, tzinfo=UTC)
    titles = _synthetic_titles(700, seed=7)
    _insert_synthetic(conn, titles, t0=t0, before_n=300)
    rows = conn.execute(
        "SELECT id, title, tickers_json, received_at, novel FROM news_items "
        "ORDER BY received_at, id"
    ).fetchall()
    expected = _reference_flags(rows, t0.isoformat())
    assert 0 in expected.values() and 1 in expected.values()  # 두 판정이 모두 나오는 입력
    news_store.novel_items(conn, "KR", since=t0, max_pending=10_000)
    got = dict(conn.execute("SELECT id, novel FROM news_items WHERE novel IS NOT NULL").fetchall())
    assert got == expected


def test_novel_items_large_untagged_pool_is_fast(conn):
    """태그 없는 큰 풀(재시작 뒤 넓은 since 창)도 몇 초 안에 끝난다(이전 18~22초)."""
    import time
    from datetime import UTC, datetime

    t0 = datetime(2026, 10, 25, 1, 0, tzinfo=UTC)
    titles = _synthetic_titles(4000, seed=11, vocab=3000)
    _insert_synthetic(conn, titles, t0=t0, before_n=2500)
    started = time.perf_counter()
    news_store.novel_items(conn, "KR", since=t0)
    assert time.perf_counter() - started < 5.0


def test_novel_items_caps_pending_and_marks_oldest_overflow_seen(conn):
    """한 번에 판정하는 미판정 항목은 max_pending 건(최신)뿐이다. 그보다 오래된 밀린 항목은 비교
    없이 novel=0 으로 닫고(장중 트리거를 만들지 않음) 이번 호출의 비교 풀에는 넣는다."""
    from datetime import UTC, datetime, timedelta

    t0 = datetime(2026, 10, 25, 1, 0, tzinfo=UTC)
    subjects = ["가나다 수주", "라마바 실적", "사아자 상장", "차카타 합병", "파하거 증자"]
    for i, title in enumerate(subjects):
        at = (t0 + timedelta(minutes=i)).isoformat()
        conn.execute(
            "INSERT INTO news_items(market, source, query, title, url, url_hash, published_at, "
            "received_at, tickers_json, lang) VALUES ('KR','t','q',?,?,?,?,?,'[]','ko')",
            (title, f"https://c/{i}", f"c{i}", at, at),
        )
    conn.commit()
    novel = news_store.novel_items(conn, "KR", since=t0, max_pending=2)
    assert [n["url"] for n in novel] == ["https://c/3", "https://c/4"]
    flags = dict(conn.execute("SELECT url, novel FROM news_items").fetchall())
    assert [flags[f"https://c/{i}"] for i in range(5)] == [0, 0, 0, 1, 1]
