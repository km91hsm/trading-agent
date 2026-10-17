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
