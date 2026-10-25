from datetime import UTC, datetime


def test_calendar_keeps_future_schedule_and_release_values_separate():
    from trader.sources.saveticker.dom import extract

    html = """<div><h3>1월 2일 금요일</h3><div><div data-stretched-link-card>
      <span>21:30</span><h4><button>신규실업수당청구건수</button></h4>
      <p><span>194K</span><span>(예상: 200K 이전: 199K)</span></p></div></div></div>"""
    items = extract(html, "calendar_2027-01", month="2027-01")
    assert len(items) == 1
    assert items[0].scheduled_at == datetime(2027, 1, 2, 12, 30, tzinfo=UTC)
    assert (items[0].actual, items[0].forecast, items[0].previous) == ("194K", "200K", "199K")
    assert items[0].published_at is None


def test_virtual_news_id_does_not_depend_on_scroll_index_or_views():
    from trader.sources.saveticker.dom import extract

    html = """<div id="news-tabpanel"><div data-index="6"><div role="button"
      data-read="false" aria-label="엔비디아 실적"><div><div><div>로이터</div></div>
      <span>3분 전</span></div><p>엔비디아 실적</p><div><span>$NVDA</span><div>143</div>
      </div></div></div></div>"""
    first = extract(html, "news_all")[0]
    second = extract(html.replace("143", "145").replace('index="6"', 'index="20"'), "news_all")[0]
    assert first.id == second.id
    assert first.title == "엔비디아 실적" and first.tickers == ["NVDA"]
    assert first.published_at is None and second.views == "145"


def test_rankings_parse_exact_tickers_and_preserve_basis():
    from trader.sources.saveticker.dom import extract

    html = """<p>오늘 거래대금 순 · 09:00 기준</p><div data-stretched-link-card>
      <span>1</span><h3><button>마이크로소프트</button></h3><span>MSFT</span>
      <span>$524.49</span><span>+0.03%</span><span>거래대금 $6.9B</span></div>"""
    item = extract(html, "market_turnover_live")[0]
    assert item.tickers == ["MSFT"]
    assert item.price == "$524.49" and item.change_pct == "+0.03%"
    assert item.turnover == "$6.9B" and item.rank == 1
    assert "09:00 기준" in item.basis


def test_community_posts_ignore_menu_and_like_button_as_body():
    from trader.sources.saveticker.dom import extract

    html = """<div class="post"><div><div><span class="text-small-bold">투자자</span>
      <span>4시간 전</span></div><p class="whitespace-pre-wrap">$AAPL 좋아요</p></div>
      <div><div><button aria-pressed="false">2</button><button>5</button></div>
      <button aria-label="더보기 메뉴"></button></div></div>"""
    item = extract(html, "community_posts")[0]
    assert item.body == "$AAPL 좋아요" and item.author == "투자자"
    assert item.likes == "2" and item.comment_count == "5"
    assert item.tickers == ["AAPL"]


def test_login_wall_is_not_an_empty_success():
    from trader.sources.saveticker.dom import authentication_required

    assert authentication_required("<h2>로그인이 필요해요</h2>")
    assert not authentication_required("<button>로그인</button><p>뉴스 목록</p>")


def test_calendar_months_roll_over_years():
    from trader.sources.saveticker.dom import months_between

    assert months_between("2026-12", "2027-02") == ["2026-12", "2027-01", "2027-02"]


def test_community_index_does_not_include_sidebar_top_posts():
    from trader.sources.saveticker.dom import extract

    html = """<section><div><button>지수</button><button>상승</button><button>하락</button></div>
      <div role="button" data-clickable aria-label="QQQ 나스닥100"><span>QQQ</span>
      <span>$704.28</span><span>-0.03%</span></div></section>
      <aside><div data-stretched-link-card><h4>코어위브</h4><span>CRWV</span>
      <span>$54.26</span><span>-2.41%</span></div></aside>"""
    assert [x.tickers for x in extract(html, "community_index_live")] == [["QQQ"]]


def test_comments_keep_parent_and_do_not_become_posts():
    from trader.sources.saveticker.dom import comments

    html = """<div data-type="comment"><div><div><span class="text-small-bold">독자</span>
      <span>4시간</span><button aria-label="더보기 메뉴"></button></div>
      <p>의견 내용</p><button aria-label="좋아요">3</button></div></div>"""
    result = comments(html, parent_id="p1")
    assert len(result) == 1
    assert result[0].parent_id == "p1" and result[0].body == "의견 내용"
    assert result[0].likes == "3" and result[0].kind == "comment"


def test_news_detail_retains_body_and_absolute_time():
    from trader.sources.saveticker.dom import news_detail
    from trader.sources.saveticker.models import Item

    html = """<div><p>2026. 10. 25. 06:42:11</p><h2>기사 제목</h2>
      <div>본문 언어<button>번역</button><button>원문</button></div>
      <div class="gap-6 p-6"><p class="whitespace-pre-wrap">실제 기사 본문</p></div>
      <div>기사 원문 등록 시간 2026. 10. 25. 오전 06:42:11</div></div>"""
    result = news_detail(html, Item(id="n", title="기사 제목"))
    assert "실제 기사 본문" in result.body
    assert result.published_at == datetime(2026, 10, 24, 21, 42, 11, tzinfo=UTC)


def test_calendar_range_uses_aria_label_and_year_not_grid_month():
    from trader.sources.saveticker.dom import calendar_month

    html = """<div>2026년 12월</div><div><button aria-label="이전"><svg/></button>
      <span>2027년 1월</span><button aria-label="다음"><svg/></button></div>"""
    assert calendar_month(html) == "2027-01"


def test_news_discovery_includes_logo_only_source_tab():
    from trader.sources.saveticker.dom import news_sources

    html = """<div role="tablist"><button role="tab">전체</button>
      <button role="tab"><img alt="SAVE PICK" src="logo.svg"></button>
      <button role="tab">관심 뉴스</button></div>"""
    assert news_sources(html) == ["전체", "SAVE PICK", "관심 뉴스"]


def test_calendar_does_not_drop_all_day_or_unannounced_time_events():
    from trader.sources.saveticker.dom import extract

    html = """<div><h3>1월 2일 금요일</h3><div>
      <div data-stretched-link-card><span>종일</span><h4>휴장일</h4></div>
      <div data-stretched-link-card><h4>시간 미정 실적 발표</h4></div>
      <div data-stretched-link-card><span>21:30</span><h4>고용</h4></div>
      </div></div>"""
    items = extract(html, "calendar_2027-01", month="2027-01")
    assert len(items) == 3
    assert items[0].scheduled_at is None and items[0].scheduled_date == "2027-01-02"
    assert items[1].scheduled_at is None
    assert items[2].scheduled_at == datetime(2027, 1, 2, 12, 30, tzinfo=UTC)


def test_gain_rankings_exclude_sidebar_and_avatar_initials():
    from trader.sources.saveticker.dom import extract

    html = """<section><div><button>상승</button><button>하락</button></div>
      <div data-stretched-link-card><span>1</span><div><span>A</span></div>
      <div><h3>알렉터</h3><span>ALEC</span></div><span>$5.12</span><span>+12.1%</span></div>
      </section><aside><div data-stretched-link-card><h4>다른 종목</h4><span>OTHER</span>
      <span>$12</span><span>+1%</span></div></aside>"""
    items = extract(html, "community_gain_live")
    assert len(items) == 1 and items[0].tickers == ["ALEC"]


def test_market_rankings_exclude_sidebar_market_cards():
    from trader.sources.saveticker.dom import extract

    html = """<section><div><button>거래대금</button><button>상승</button></div>
      <div data-stretched-link-card><div><h3>마이크로소프트</h3><span>MSFT</span></div>
      <span>$524.49</span><span>+0.03%</span></div></section><aside>
      <div data-stretched-link-card><h4>다른 종목</h4><span>OTHER</span>
      <span>$12</span><span>+1%</span></div></aside>"""
    assert [x.tickers for x in extract(html, "market_turnover_live")] == [["MSFT"]]


def test_deeply_nested_market_tab_does_not_fall_back_to_whole_page():
    from trader.sources.saveticker.dom import extract

    tab = "<div>" * 6 + "<button>거래대금</button>" + "</div>" * 6
    card = (
        "<div data-stretched-link-card><h3>주 종목</h3><span>MAIN</span>"
        "<span>$100</span><span>+1%</span></div>"
    )
    sidebar = (
        '<aside><div role="button" data-clickable aria-label="SPY 지수">'
        "<span>SPY</span><span>$200</span><span>+2%</span></div></aside>"
    )
    html = "<main><section>" + tab + card + "</section></main>" + sidebar
    assert [x.tickers for x in extract(html, "market_turnover_live")] == [["MAIN"]]


def test_news_body_ignores_controls_hidden_header_and_user_comments():
    from trader.sources.saveticker.dom import news_detail
    from trader.sources.saveticker.models import Item

    html = """<section><header><h2>리포트 제목</h2></header>
      <div class="hidden"><h2>리포트 제목</h2></div>
      <div class="flex flex-col gap-6 p-6"><div role="group" aria-label="본문 언어">
      <button>번역</button><button>원문</button></div><div class="flex flex-col">
      <p class="whitespace-pre-wrap">시장 분석 첫 문단</p>
      <p class="whitespace-pre-wrap">시장 분석 둘째 문단</p></div></div>
      <div data-type="comment"><p class="whitespace-pre-wrap">개인 의견</p></div></section>"""
    detail = news_detail(html, Item(id="n", title="리포트 제목"))
    assert detail.body == "시장 분석 첫 문단\n시장 분석 둘째 문단"
    assert detail.detail_status == "ok"


def test_news_controls_before_body_ready_are_not_cached_as_success():
    from trader.sources.saveticker.dom import news_detail
    from trader.sources.saveticker.models import Item

    html = (
        '<section><h2>제목</h2><div role="group" aria-label="본문 언어">'
        "<button>번역</button><button>원문</button></div></section>"
    )
    detail = news_detail(html, Item(id="n", title="제목"))
    assert detail.detail_status != "ok" and not detail.body


def test_trends_and_top5_read_only_widget_not_people_posts():
    from trader.sources.saveticker.dom import extract

    html = """<section><h3>10월 23일 트렌드</h3><p>전일 장 종료 기준 · 전체 누적</p>
      <ul><li><button aria-label="1위 반도체 뉴스 검색">반도체</button></li></ul></section>
      <section><h3>종목별 커뮤니티 TOP 5</h3><div data-stretched-link-card><h4>엔비디아</h4>
      <span>NVDA · 의견 12</span><span>$200</span><span>+1%</span></div></section>
      <div><p class="whitespace-pre-wrap">개인 글</p>
      <button aria-label="더보기 메뉴"></button></div>"""
    trends = extract(html, "community_trends")
    top5 = extract(html, "community_top5")
    assert [(r.title, r.rank, r.kind) for r in trends] == [("반도체", 1, "trend")]
    assert "전일 장 종료 기준" in trends[0].basis
    assert [(r.tickers, r.rank) for r in top5] == [(["NVDA"], 1)]
    assert all(not r.body and not r.author for r in trends + top5)


def test_news_source_classification_preserves_category_and_breaking_flag():
    from trader.sources.saveticker.dom import extract

    html = """<div id="news-tabpanel"><div role="button" data-read="false"
      data-breaking="true" aria-label="새 뉴스"><div><div><div>SAVE PICK</div>
      <span>·</span><span>정보</span></div><span>방금 전</span></div><p>새 뉴스</p>
      <div><div>10</div></div></div></div>"""
    row = extract(html, "news_all")[0]
    assert row.source_group == "SAVE PICK"
    assert row.publisher == "SAVE PICK" and row.categories == ["정보"]
    assert row.is_breaking is True


def test_client_application_error_screen_is_not_treated_as_empty_data():
    from trader.sources.saveticker.dom import client_error_visible

    assert client_error_visible(
        "<h2>Application error: a client-side exception has occurred "
        "while loading www.saveticker.com</h2>"
    )
    assert not client_error_visible("<p>기사: Application error의 원인</p>")
