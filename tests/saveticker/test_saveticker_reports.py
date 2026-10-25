import pytest


def test_report_cards_are_not_mistaken_for_empty_news():
    from trader.sources.saveticker.dom import extract

    html = """<h2>데일리 리포트</h2><div><div><span>SAVE REPORT</span><span>4일 전</span></div>
      <p>2026년 10월 20일 (화)</p><div><button aria-label="PDF 다운로드">PDF</button></div></div>"""
    rows = extract(html, "news_reports")
    assert len(rows) == 1
    assert rows[0].title == "2026년 10월 20일 (화)"
    assert (
        rows[0].kind == "report"
        and rows[0].report_date == "2026-10-20"
        and rows[0].published_at is None
    )
    assert rows[0].detail_status == "pending"


def test_pdf_original_is_hashed_and_kept_even_when_no_text(tmp_path):
    PdfWriter = pytest.importorskip("pypdf").PdfWriter
    from trader.sources.saveticker.reports import save_pdf

    source = tmp_path / "download"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(source)
    root = tmp_path / "feed"
    result = save_pdf(source, root)
    assert (root / result.pdf_path).read_bytes() == source.read_bytes()
    assert result.size_bytes == source.stat().st_size and result.pages == 1
    assert len(result.sha256) == 64
    assert result.text_chars == 0 and result.text_path is None
    assert save_pdf(source, root) == result


def test_non_pdf_and_oversized_downloads_are_not_imported(tmp_path):
    pytest.importorskip("pypdf")
    from trader.sources.saveticker.reports import save_pdf

    source = tmp_path / "download"
    source.write_text("<html>로그인 필요</html>")
    with pytest.raises(ValueError, match="invalid_pdf"):
        save_pdf(source, tmp_path / "feed")
    source.write_bytes(b"%PDF-" + b"0" * 100)
    with pytest.raises(ValueError, match="pdf_size_limit"):
        save_pdf(source, tmp_path / "feed", max_bytes=20)
    assert not list((tmp_path / "feed").glob("**/*.pdf"))


def test_browser_downloads_report_via_button_and_reuses_persisted_manifest(tmp_path):
    import base64
    import io

    PdfWriter = pytest.importorskip("pypdf").PdfWriter
    api = pytest.importorskip("playwright.sync_api")
    from trader.sources.saveticker.browser import Browser
    from trader.sources.saveticker.dom import extract

    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    buffer = io.BytesIO()
    writer.write(buffer)
    uri = "data:application/pdf;base64," + base64.b64encode(buffer.getvalue()).decode()
    writer.add_blank_page(width=200, height=200)
    second = io.BytesIO()
    writer.write(second)
    uri2 = "data:application/pdf;base64," + base64.b64encode(second.getvalue()).decode()
    with api.sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.set_content(f'''<main><div><p>2026년 10월 20일 (화)</p>
              <div><a href="{uri}" download="daily.pdf">
              <button aria-label="PDF 다운로드">PDF</button></a></div></div>
              <div><p>2026년 10월 19일 (월)</p><div><a href="{uri2}" download="second.pdf">
              <button aria-label="PDF 다운로드">PDF</button></a></div></div></main>''')
            adapter = Browser(page, ("unused", "unused"), artifact_root=tmp_path)
            adapter.pace = lambda: None
            rows = adapter._reports(extract(page.content(), "news_reports"))
            assert [r.attachment.pages for r in rows] == [1, 2]
            assert rows[0].detail_status == "pdf_only"
            # 프로세스 재시작 후에도 원본과 manifest를 재사용한다.
            page.set_content("<p>다운로드 버튼 없음</p>")
            restarted = Browser(page, ("unused", "unused"), artifact_root=tmp_path)
            again = restarted._reports(rows)
            assert again[0].attachment == rows[0].attachment
        finally:
            browser.close()


def test_failed_newest_reports_do_not_starve_later_pdf(tmp_path, monkeypatch):
    import base64
    import io
    from contextlib import contextmanager

    PdfWriter = pytest.importorskip("pypdf").PdfWriter
    api = pytest.importorskip("playwright.sync_api")
    from trader.sources.saveticker.browser import Browser
    from trader.sources.saveticker.dom import extract

    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    buffer = io.BytesIO()
    writer.write(buffer)
    uri = "data:application/pdf;base64," + base64.b64encode(buffer.getvalue()).decode()
    with api.sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.set_content(f'''<main>
              <div><p>2026년 10월 23일 (금)</p>
              <button aria-label="PDF 다운로드">실패1</button></div>
              <div><p>2026년 10월 22일 (목)</p>
              <button aria-label="PDF 다운로드">실패2</button></div>
              <div><p>2026년 10월 21일 (수)</p><a download="third.pdf" href="{uri}">
              <button aria-label="PDF 다운로드">정상3</button></a></div></main>''')
            original = page.expect_download

            @contextmanager
            def fail_download(**kwargs):
                raise RuntimeError("download_failed")
                yield

            adapter = Browser(page, ("unused", "unused"), artifact_root=tmp_path)
            adapter.pace = lambda: None
            monkeypatch.setattr(page, "expect_download", fail_download)
            rows = extract(page.content(), "news_reports")
            first = adapter._reports(rows)
            assert [r.detail_status for r in first] == [
                "download_failed",
                "download_failed",
                "pending",
            ]
            adapter.detail_requested = 0
            monkeypatch.setattr(page, "expect_download", original)
            # 첫 두 실패가 계속 있어도 다음 회차에는 세 번째 원본을 먼저 받는다.
            # 최초 정상 다운로드 뒤 추가 실패는 즉시 반환하도록 훅을 둔다.
            calls = 0

            @contextmanager
            def only_first(**kwargs):
                nonlocal calls
                calls += 1
                if calls > 1:
                    raise RuntimeError("download_failed")
                with original(**kwargs) as result:
                    yield result

            monkeypatch.setattr(page, "expect_download", only_first)
            second = adapter._reports(rows)
            assert second[2].attachment is not None
        finally:
            browser.close()
