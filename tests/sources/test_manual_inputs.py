from trader.sources import manual_inputs


def test_manual_inputs_md_and_json(project):
    d = project / "state" / "manual_inputs"
    d.mkdir()
    assert manual_inputs.read("005930") is None
    (d / "005930.md").write_text("# 메모\n사용자 관찰: 신제품 발표", encoding="utf-8")
    r = manual_inputs.read("005930")
    assert (
        r["text"].startswith("# 메모")
        and r["format"] == "md"
        and r["updated_at"].endswith("+00:00")
    )
    (d / "AAPL.json").write_text('{"thesis": "서비스 매출", "risk": "규제"}', encoding="utf-8")
    r = manual_inputs.read("AAPL")
    assert r["data"]["thesis"] == "서비스 매출" and r["format"] == "json"
    (d / "BAD.json").write_text("{not json", encoding="utf-8")
    assert manual_inputs.read("BAD")["error"]


def test_manual_inputs_rejects_path_escape(project):
    (project / "state" / "manual_inputs").mkdir()
    assert manual_inputs.read("../secrets") is None


def test_market_notes_append_read_and_asof(project):
    from datetime import UTC, datetime

    t1 = datetime(2026, 10, 24, 1, 0, tzinfo=UTC)
    t2 = datetime(2026, 10, 24, 2, 0, tzinfo=UTC)
    manual_inputs.append_market(
        "KR",
        title="반도체 | 수출 호조",
        url="https://www.saveticker.com/news/1",
        note="메모\n## 가짜 머리",
        now=t1,
    )
    manual_inputs.append_market("KR", title="둘째", now=t2)
    assert manual_inputs.read_market("US") == []
    items = manual_inputs.read_market("KR")
    assert [i["title"] for i in items] == ["둘째", "반도체 / 수출 호조"]
    assert items[1]["url"] == "https://www.saveticker.com/news/1"
    assert items[1]["note"] == "메모\n ## 가짜 머리"  # 머리줄로 오인되지 않는다
    # asof 이전 항목만 (룩어헤드)
    assert [i["title"] for i in manual_inputs.read_market("KR", asof=t1)] == ["반도체 / 수출 호조"]
    try:
        manual_inputs.append_market("KR", title="", now=t1)
    except ValueError:
        pass
    else:
        raise AssertionError("빈 제목은 거부")
    try:
        manual_inputs.append_market("KR", title="x", url="javascript:alert(1)", now=t1)
    except ValueError:
        pass
    else:
        raise AssertionError("http(s) 가 아닌 URL 은 거부")
    try:
        manual_inputs.append_market("JP", title="x", now=t1)
    except ValueError:
        pass
    else:
        raise AssertionError("모르는 시장은 거부")
