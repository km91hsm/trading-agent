"""사건 수혜(2026-10-27): 뉴스 키워드로 사건을 감지하고(파이썬), scout 는 표의 수혜 묶음 안에서만
고르며(파이썬이 검증), shock 사건 종목은 테마 안전장치(묶음 비중·단기 보유·익절)를 받는다."""

from datetime import UTC, date, datetime, timedelta

from trader import config
from trader.config import EventsConfig
from trader.scan import discovery, events
from trader.sources import news_store

NOW = datetime.now(UTC)
TODAY = date(2026, 10, 27)

CFG = EventsConfig.model_validate(
    {
        "enabled": True,
        "events": [
            {
                "id": "war_geopolitics",
                "label": "전쟁·지정학 위기",
                "kind": "shock",
                "min_news": 2,
                "keywords": ["공습", "미사일 발사", "airstrike"],
                "beneficiaries": {
                    "KR": [
                        {"ticker": "012450", "name": "한화에어로스페이스", "role": "방산"},
                        {"ticker": "132030", "name": "KODEX 골드선물(H)", "role": "금"},
                    ],
                    "US": [
                        {"ticker": "LMT", "exchange": "NYS", "name": "Lockheed", "role": "방산"}
                    ],
                },
            },
            {
                "id": "ai_memory_shortage",
                "label": "AI 메모리 부족",
                "kind": "structural",
                "min_news": 2,
                "keywords": ["HBM"],
                "beneficiaries": {
                    "KR": [{"ticker": "000660", "name": "SK하이닉스", "role": "메모리"}]
                },
            },
        ],
    }
)


def _news(conn, titles, market="KR"):
    news_store.insert_items(
        conn,
        [{"url": f"https://ex.test/{market}/{i}", "title": t} for i, t in enumerate(titles)],
        market=market,
        source="test",
        query="q",
        lang="ko",
        names={},
    )


def test_detect_counts_keywords_and_needs_min_news(conn):
    _news(conn, ["중동 공습 확대", "북부 미사일 발사 이어져", "HBM 증설 소식"])
    found = events.detect(conn, CFG, market="KR", asof=datetime.now(UTC) + timedelta(seconds=5))
    assert [e["id"] for e in found] == ["war_geopolitics"]  # HBM 은 1건뿐 — 기준 2건 미만
    war = found[0]
    assert war["hits"] == 2 and war["kind"] == "shock"
    assert {t["ticker"] for t in war["tickers"]} == {"012450", "132030"}
    assert (
        events.detect(conn, CFG.model_copy(update={"enabled": False}), market="KR", asof=NOW) == []
    )


def test_record_theme_map_only_shock_and_alert_once(conn):
    found = [
        {"id": "war_geopolitics", "label": "전쟁·지정학 위기", "kind": "shock", "hits": 3,
         "news": [], "tickers": [{"ticker": "012450", "name": "한화에어로", "role": "방산"}]},
        {"id": "ai_memory_shortage", "label": "AI 메모리 부족", "kind": "structural", "hits": 3,
         "news": [], "tickers": [{"ticker": "000660", "name": "SK하이닉스", "role": "메모리"}]},
    ]  # fmt: skip
    events.record(conn, "KR", TODAY, found)
    m = events.theme_of_map(conn, "KR", TODAY)
    assert m == {"012450": ["사건:전쟁·지정학 위기"]}  # 구조적 흐름은 단기 안전장치 없음
    assert events.theme_of_map(conn, "KR", TODAY + timedelta(days=1)) == {}
    assert len(events.new_for_alert(conn, "KR", TODAY, found)) == 2
    assert events.new_for_alert(conn, "KR", TODAY, found) == []


def test_resolve_event_tier_only_from_table_with_caps():
    watch = [
        {"id": "war_geopolitics", "label": "전쟁", "kind": "shock", "hits": 3, "news": [],
         "tickers": [{"ticker": "012450", "name": "한화에어로스페이스", "role": "방산"},
                     {"ticker": "132030", "name": "KODEX 골드선물(H)", "role": "금"},
                     {"ticker": "064350", "name": "현대로템", "role": "방산"}]},
    ]  # fmt: skip
    disc = {
        "candidates": [
            {"name": "한화에어로스페이스", "ticker": "012450", "market": "KR", "tier": "event",
             "catalyst": "공습 확대", "confidence": 0.7, "source_ids": ["event:war_geopolitics"]},
            {"name": "KODEX 골드선물(H)", "ticker": "132030", "market": "KR", "tier": "event",
             "catalyst": "안전자산", "confidence": 0.6, "source_ids": []},
            {"name": "현대로템", "ticker": "064350", "market": "KR", "tier": "event",
             "catalyst": "x", "confidence": 0.5, "source_ids": []},  # 사건당 상한 2 초과
            {"name": "지어낸 종목", "ticker": "999999", "market": "KR", "tier": "event",
             "catalyst": "x", "confidence": 0.9, "source_ids": []},  # 표에 없음
        ]
    }  # fmt: skip
    r = discovery.resolve(
        disc,
        market="KR",
        universe=config.Universe(kr=["005930"]),
        cache_names={},
        event_watch=watch,
        max_event=4,
        max_per_event=2,
    )
    assert [c["ticker"] for c in r["candidates"]] == ["012450", "132030"]
    assert all(c["tier"] == "event" and c["event_id"] == "war_geopolitics" for c in r["candidates"])
    reasons = {u["ticker"]: u["reason"] for u in r["unresolved"]}
    assert "사건당 상한" in reasons["064350"] and "표" in reasons["999999"]
    merged = discovery.merge([], r, max_event=1)
    assert [m["ticker"] for m in merged] == ["012450"]  # 확신순 + 사건 후보 상한
    assert merged[0]["discovery"]["event_id"] == "war_geopolitics"


def test_event_watch_in_discovery_inputs(conn, project):
    (project / "config" / "events.yaml").write_text(
        "enabled: true\nevents:\n"
        "  - {id: war_geopolitics, label: 전쟁, kind: shock, min_news: 1, keywords: [공습],\n"
        "     beneficiaries: {KR: [{ticker: '012450', name: 한화에어로스페이스, role: 방산}]}}\n",
        encoding="utf-8",
    )
    _news(conn, ["공습 개시"])
    data = discovery.build_inputs(
        conn,
        config.Settings(),
        config.Universe(kr=["005930"]),
        market="KR",
        today=TODAY,
        asof=datetime.now(UTC) + timedelta(seconds=5),
        quant_candidates=[],
        state=project / "state",
    )
    assert data["event_watch"][0]["id"] == "war_geopolitics" and data["max_event_picks"] == 4
    assert events.today_events(conn, "KR", TODAY)[0]["id"] == "war_geopolitics"


def test_repo_events_yaml_is_valid_and_templates_match():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    cfg = config.load_events(root / "config" / "events.yaml")
    assert cfg.enabled and len(cfg.events) >= 20
    assert (root / "templates" / "config" / "events.yaml").read_text(encoding="utf-8") == (
        root / "config" / "events.yaml"
    ).read_text(encoding="utf-8")


def test_shock_event_ticker_gets_theme_safeguard_in_gate(conn, project):
    """오늘 감지된 shock 사건의 수혜 종목은 게이트에서 테마 규칙(보유 ≤10일·익절 필수)을 받는다."""
    from trader.orchestrator import executor

    events.record(
        conn,
        "KR",
        TODAY,
        [{"id": "war_geopolitics", "label": "전쟁", "kind": "shock", "hits": 3, "news": [],
          "tickers": [{"ticker": "012450", "name": "한화에어로스페이스", "role": "방산"}]}],
    )  # fmt: skip
    (project / "config" / "events.yaml").write_text("enabled: true\n", encoding="utf-8")
    ctx = executor.ExecContext(settings=config.Settings(), accounts=[], conn=conn)
    theme_of = executor._theme_of(ctx, "KR", TODAY)
    assert theme_of is not None and theme_of("012450") == ["사건:전쟁"]
    assert theme_of("005930") is None
    # 끄면 그날 기록된 사건도 게이트에 적용하지 않는다
    (project / "config" / "events.yaml").write_text("enabled: false\n", encoding="utf-8")
    off = executor._theme_of(ctx, "KR", TODAY)
    assert off is None or off("012450") is None


def test_daemon_events_check_alerts_once(conn, project, monkeypatch):
    from test_daemon import _deps

    from trader import daemon
    from trader.broker.fake import FakeMarket

    (project / "config" / "events.yaml").write_text(
        "enabled: true\nevents:\n"
        "  - {id: war_geopolitics, label: 전쟁, kind: shock, min_news: 1, keywords: [공습],\n"
        "     beneficiaries: {KR: [{ticker: '012450', name: 한화에어로스페이스, role: 방산}]}}\n",
        encoding="utf-8",
    )
    _news(conn, ["공습 개시"])
    deps = _deps(conn, FakeMarket(), now=lambda: datetime.now(UTC) + timedelta(seconds=5))

    class Sent:
        msgs: list[str] = []

        def send(self, text, *, escape=True):
            self.msgs.append(text)
            return True

    deps.notifier = Sent()
    r = daemon._events_check(deps, conn, "KR")
    assert r["alerted"] == ["war_geopolitics"] and "[사건 감지]" in Sent.msgs[0]
    assert daemon._events_check(deps, conn, "KR")["alerted"] == []


def test_english_keywords_need_word_boundary():
    assert events._matches("Software award season", ["war"]) is False
    assert events._matches("War escalates in region", ["war"]) is True
    assert events._matches("중동 공습 확대", ["공습"]) is True


def test_event_cap_counts_universe_members_too():
    watch = [
        {"id": "a", "label": "a", "kind": "shock", "hits": 3, "news": [],
         "tickers": [{"ticker": "005930", "name": "삼성전자", "role": "x"}]},
        {"id": "b", "label": "b", "kind": "shock", "hits": 3, "news": [],
         "tickers": [{"ticker": "000660", "name": "SK하이닉스", "role": "x"}]},
    ]  # fmt: skip
    disc = {
        "candidates": [
            {"name": "삼성전자", "ticker": "005930", "market": "KR", "tier": "event",
             "catalyst": "x", "confidence": 0.5, "source_ids": []},
            {"name": "SK하이닉스", "ticker": "000660", "market": "KR", "tier": "event",
             "catalyst": "x", "confidence": 0.5, "source_ids": []},
        ]
    }  # fmt: skip
    r = discovery.resolve(
        disc,
        market="KR",
        universe=config.Universe(kr=["005930", "000660"]),
        cache_names={},
        event_watch=watch,
        max_event=1,
    )
    assert [c["ticker"] for c in r["candidates"]] == ["005930"]


def test_resolve_event_ticker_is_case_insensitive():
    """2-L22: scout 가 US 심볼을 소문자로 내도 `watch_index`(대문자 키)에서 찾는다."""
    watch = [
        {"id": "oil_shock", "label": "유가", "kind": "shock", "hits": 2, "news": [],
         "tickers": [{"ticker": "XOM", "name": "Exxon", "role": "정유", "exchange": "NYS"}]},
    ]  # fmt: skip
    disc = {"candidates": [{"name": "Exxon", "ticker": "xom", "market": "US", "tier": "event",
                            "catalyst": "x", "confidence": 0.6, "source_ids": []}]}  # fmt: skip
    r = discovery.resolve(
        disc, market="US", universe=config.Universe(kr=["005930"]), cache_names={},
        event_watch=watch, max_event=4, max_per_event=2,
    )  # fmt: skip
    assert [c["ticker"] for c in r["candidates"]] == ["XOM"] and r["unresolved"] == []


def test_detect_dedupes_same_story_and_honors_exclude(conn):
    """2-L10: 같은 통신 기사가 매체마다 따로 적재돼도(세이브티커 22스트림·네이버·구글) 정규화 제목
    기준 1건으로 세고, `exclude` 패턴(무역전쟁)이 든 뉴스는 세지 않는다 — 한글 키워드가 부분
    문자열이라 `전쟁` 이 무역전쟁·반도체전쟁에 걸리던 것."""
    cfg = EventsConfig.model_validate(
        {
            "enabled": True,
            "events": [
                {
                    "id": "war_geopolitics",
                    "label": "전쟁",
                    "min_news": 2,
                    "keywords": ["공습", "전쟁"],
                    "exclude": ["무역전쟁", "반도체 전쟁"],
                    "beneficiaries": {"KR": [{"ticker": "012450", "name": "한화에어로"}]},
                }
            ],
        }
    )
    _news(
        conn,
        ["중동 공습 확대 - 연합뉴스", "[속보] 중동 공습 확대 - 뉴시스", "미중 무역전쟁 격화"],
    )
    asof = datetime.now(UTC) + timedelta(seconds=5)
    assert events.detect(conn, cfg, market="KR", asof=asof) == []  # 중복 1건 + 제외 → 기준 2 미만
    news_store.insert_items(  # URL 이 겹치면 적재되지 않으므로(멱등) 새 URL 로
        conn,
        [{"url": "https://ex.test/KR/war-1", "title": "접경 지역 전쟁 위기 고조"}],
        market="KR",
        source="test",
        query="q",
        lang="ko",
        names={},
    )
    found = events.detect(conn, cfg, market="KR", asof=asof)
    assert [e["id"] for e in found] == ["war_geopolitics"] and found[0]["hits"] == 2
    titles = [n["title"] for n in found[0]["news"]]
    assert titles[0] == "접경 지역 전쟁 위기 고조" and len(titles) == 2 and "공습" in titles[1]


def test_membership_excludes_records_observed_after_asof(conn):
    """19번(i): 테마·사건 소속 조회는 관측 시각이 기준 시각 뒤면 넣지 않고(after_asof), 관측 시각이
    없는 옛 기록은 시각을 몰라 넣지 않는다(unknown_time). 기준 시각이 없으면(실행 게이트) 최신."""
    from datetime import UTC, date, datetime

    from trader.control import control
    from trader.scan import events, membership

    d = date(2026, 10, 27)
    ev = [{"id": "e1", "label": "유가 급등", "kind": "shock", "tickers": [{"ticker": "010950"}]}]
    events.record(conn, "KR", d, ev, now=datetime(2026, 10, 27, 1, 0, tzinfo=UTC))
    kw = {"theme_top_n": 3, "theme_enabled": True, "events_enabled": True}
    live = membership.of(conn, "KR", d, **kw)
    assert live["map"]["010950"] == ["사건:유가 급등"] and live["status"]["events"] == "ok"
    early = membership.of(conn, "KR", d, asof=datetime(2026, 10, 27, 0, 30, tzinfo=UTC), **kw)
    assert early["map"] == {} and early["status"]["events"] == "after_asof"
    control.set_flag(conn, "events:KR", {"date": d.isoformat(), "events": ev})  # 옛 형식
    old = membership.of(conn, "KR", d, asof=datetime(2026, 10, 27, 2, 0, tzinfo=UTC), **kw)
    assert old["status"]["events"] == "unknown_time" and old["map"] == {}
