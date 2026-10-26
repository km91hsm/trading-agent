"""universe sectors: KIS 업종 → sectors.yaml 종목 섹터 제안·기록 (빠진 종목만, 주석 보존)."""

from datetime import date
from decimal import Decimal

import pytest
import yaml
from typer.testing import CliRunner

from trader.broker.fake import FakeMarket
from trader.cli import app
from trader.config import SectorMap, Universe, UsSymbol, load_sectors
from trader.scan import sector_suggest

SECTORS = SectorMap(
    sectors=["반도체", "금융", "기타"],
    kr={"005930": "반도체"},
    us={"JPM": "금융"},
    kr_industry_to_sector={"전기전자": "반도체", "은행": "금융"},
    us_industry_to_sector={"710": "반도체", "610": "금융"},
)
YAML = """# 종목 → 섹터 (사람이 관리)
sectors:
  - 반도체
  - 금융
  - 기타
kr_industry_to_sector:
  "전기전자": 반도체
  "은행": 금융
us_industry_to_sector:
  "710": 반도체
  "610": 금융
kr:
  "005930": 반도체      # 삼성전자
us:
  JPM: 금융             # 유지할 주석
"""


def _with_manual(text: str, line: str) -> str:
    """kr: 블록의 삼성전자 줄 뒤에 수동 항목 한 줄을 넣은 yaml."""
    anchor = '  "005930": 반도체      # 삼성전자\n'
    return text.replace(anchor, anchor + line)


def _market():
    return FakeMarket(
        prices={t: Decimal(1) for t in ("005930", "000660", "105560", "JPM")},
        industries={"005930": "전기전자", "000660": "전기전자", "105560": "은행", "NVDA": "710"},
    )


def test_suggest_only_missing_and_maps_industry():
    uni = Universe(kr=["005930", "000660", "105560"])
    rows = {r["ticker"]: r for r in sector_suggest.suggest(uni, SECTORS, _market(), market="KR")}
    assert set(rows) == {"000660", "105560"}  # 005930 은 이미 있음
    assert rows["000660"]["industry"] == "전기전자" and rows["000660"]["suggested"] == "반도체"
    assert rows["105560"]["suggested"] == "금융" and rows["105560"]["current"] is None
    all_rows = sector_suggest.suggest(uni, SECTORS, _market(), market="KR", only_missing=False)
    assert {r["ticker"] for r in all_rows} == {"005930", "000660", "105560"}
    assert next(r for r in all_rows if r["ticker"] == "005930")["current"] == "반도체"


def test_suggest_notes_unmapped_and_failed():
    class Market(FakeMarket):
        def get_industry_us(self, symbol, exchange):
            if symbol == "XOM":
                return "010"  # 표에 없는 업종
            if symbol == "BAD":
                raise ValueError("조회 불가")
            return super().get_industry_us(symbol, exchange)

    uni = Universe(
        us=[
            UsSymbol(symbol="NVDA", exchange="NAS"),
            UsSymbol(symbol="XOM", exchange="NYS"),
            UsSymbol(symbol="BAD", exchange="NYS"),
            UsSymbol(symbol="NONE", exchange="NYS"),
        ]
    )
    m = Market(industries={"NVDA": "710"})
    rows = {r["ticker"]: r for r in sector_suggest.suggest(uni, SECTORS, m, market="US")}
    assert rows["NVDA"]["suggested"] == "반도체" and rows["NVDA"]["exchange"] == "NAS"
    assert rows["XOM"]["suggested"] is None and "us_industry_to_sector" in rows["XOM"]["note"]
    assert rows["BAD"]["suggested"] is None and "조회 실패" in rows["BAD"]["note"]
    assert rows["NONE"]["suggested"] is None and rows["NONE"]["industry"] is None


def test_apply_inserts_once_preserving_comments(tmp_path):
    p = tmp_path / "sectors.yaml"
    p.write_text(YAML, encoding="utf-8")
    rows = [
        {"ticker": "000660", "industry": "전기전자", "current": None, "suggested": "반도체"},
        {"ticker": "005930", "industry": "전기전자", "current": "반도체", "suggested": "반도체"},
        {"ticker": "999999", "industry": "x", "current": None, "suggested": None},
    ]
    assert sector_suggest.apply(p, "KR", rows, today=date(2026, 10, 20)) == 1
    assert sector_suggest.apply(p, "KR", rows, today=date(2026, 10, 20)) == 0  # 멱등
    text = p.read_text(encoding="utf-8")
    assert text.count('"000660"') == 1 and "# 유지할 주석" in text and "# 삼성전자" in text
    assert "(auto 2026-10-20)" in text
    m = load_sectors(p)
    assert m.kr == {"005930": "반도체", "000660": "반도체"} and m.us == {"JPM": "금융"}
    us_rows = [{"ticker": "NVDA", "industry": "710", "current": None, "suggested": "반도체"}]
    assert sector_suggest.apply(p, "US", us_rows, today=date(2026, 10, 20)) == 1
    assert load_sectors(p).us == {"JPM": "금융", "NVDA": "반도체"}


def test_cli_universe_sectors_write_updates_config_and_template(project, monkeypatch):
    from trader import cli

    (project / "config" / "sectors.yaml").write_text(YAML, encoding="utf-8")
    (project / "templates" / "config").mkdir(parents=True)
    (project / "templates" / "config" / "sectors.yaml").write_text(YAML, encoding="utf-8")
    (project / "config" / "universe.yaml").write_text(
        yaml.safe_dump({"kr": ["005930", "000660", "105560"]}), encoding="utf-8"
    )
    monkeypatch.setattr(cli, "_build_market", lambda s: _market())
    monkeypatch.setattr(cli, "_templates_config_dir", lambda: project / "templates" / "config")
    r = CliRunner().invoke(app, ["universe", "sectors", "--market", "KR"])
    assert r.exit_code == 0, r.stdout
    assert "000660" in r.stdout and "005930" not in r.stdout  # 빠진 종목만
    r = CliRunner().invoke(app, ["universe", "sectors", "--market", "KR", "--write"])
    assert r.exit_code == 0, r.stdout
    assert load_sectors().kr == {"005930": "반도체", "000660": "반도체", "105560": "금융"}
    tmpl = load_sectors(project / "templates" / "config" / "sectors.yaml")
    assert tmpl.kr == load_sectors().kr
    # 표에 없는 업종이 있으면 exit 1 (매핑표를 보완하라는 신호)
    bad = FakeMarket(prices={"005930": Decimal(1)}, industries={"282330": "유통업"})
    monkeypatch.setattr(cli, "_build_market", lambda s: bad)
    (project / "config" / "universe.yaml").write_text(
        yaml.safe_dump({"kr": ["282330"]}), encoding="utf-8"
    )
    r = CliRunner().invoke(app, ["universe", "sectors", "--market", "KR"])
    assert r.exit_code == 1 and "kr_industry_to_sector" in r.stdout


def test_apply_inline_mapping_rules(tmp_path):
    """`kr: {}` 만 블록으로 바꾸고, 비어 있지 않은 인라인 매핑은 지우지 않고 ConfigError (리뷰 #2).
    쓰기 전에 검증해 깨진 파일을 남기지 않는다."""
    from trader.config import ConfigError

    rows = [{"ticker": "000660", "industry": "전기·전자", "current": None, "suggested": "반도체"}]
    p = tmp_path / "a.yaml"
    p.write_text("sectors: [반도체]\nkr: {}  # 비어 있음\nus: {}\n", encoding="utf-8")
    assert sector_suggest.apply(p, "KR", rows, today=date(2026, 10, 20)) == 1
    assert load_sectors(p).kr == {"000660": "반도체"} and load_sectors(p).us == {}
    q = tmp_path / "b.yaml"
    original = 'sectors: [반도체, 기타]\nkr: {"005930": 기타}\nus: {}\n'
    q.write_text(original, encoding="utf-8")
    with pytest.raises(ConfigError):
        sector_suggest.apply(q, "KR", rows, today=date(2026, 10, 20))
    assert q.read_text(encoding="utf-8") == original  # 파일 불변


def test_apply_keeps_header_comment_and_escapes_ticker(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text(
        'sectors: [반도체]\nkr:  # 수동 관리\n  "005930": 반도체\nus: {}\n', encoding="utf-8"
    )
    rows = [{"ticker": 'A"B', "industry": "x", "current": None, "suggested": "반도체"}]
    assert sector_suggest.apply(p, "KR", rows, today=date(2026, 10, 20)) == 1
    text = p.read_text(encoding="utf-8")
    assert "kr:  # 수동 관리" in text and "\r" not in text
    assert load_sectors(p).kr == {"005930": "반도체", 'A"B': "반도체"}


def test_sync_copies_config_entries_missing_in_template(tmp_path):
    """config 에는 있고 templates 에만 빠진 종목은 KIS 제안이 아니라 config 값으로 채운다."""
    cfg_p = tmp_path / "config.yaml"
    tmpl_p = tmp_path / "template.yaml"
    cfg_p.write_text(_with_manual(YAML, '  "000660": 반도체  # 수동\n'), encoding="utf-8")
    tmpl_p.write_text(YAML, encoding="utf-8")
    assert sector_suggest.sync(cfg_p, tmpl_p, "KR", today=date(2026, 10, 20)) == 1
    assert load_sectors(tmpl_p).kr == load_sectors(cfg_p).kr
    assert sector_suggest.sync(cfg_p, tmpl_p, "KR", today=date(2026, 10, 20)) == 0  # 멱등
    assert "# 유지할 주석" in tmpl_p.read_text(encoding="utf-8")


def test_cli_universe_sectors_write_syncs_template_drift(project, monkeypatch):
    from trader import cli

    (project / "config" / "sectors.yaml").write_text(
        _with_manual(YAML, '  "105560": 금융  # 수동\n'),
        encoding="utf-8",
    )
    (project / "templates" / "config").mkdir(parents=True)
    (project / "templates" / "config" / "sectors.yaml").write_text(YAML, encoding="utf-8")
    (project / "config" / "universe.yaml").write_text(
        yaml.safe_dump({"kr": ["005930", "000660", "105560"]}), encoding="utf-8"
    )
    monkeypatch.setattr(cli, "_build_market", lambda s: _market())
    monkeypatch.setattr(cli, "_templates_config_dir", lambda: project / "templates" / "config")
    r = CliRunner().invoke(app, ["universe", "sectors", "--market", "KR", "--write"])
    assert r.exit_code == 0, r.stdout
    assert load_sectors().kr == load_sectors(project / "templates" / "config" / "sectors.yaml").kr
    assert load_sectors().kr == {"005930": "반도체", "000660": "반도체", "105560": "금융"}


# --- 자동 분류 캐시(웹 섹터 비중: yaml 에 없는 보유 종목) -----------------------------------


def test_auto_classify_caches_only_missing_and_resolver_prefers_yaml(tmp_path):
    """yaml 에 없는 보유 종목만 KIS 업종으로 분류해 캐시하고, 이미 캐시된 종목은 다시 조회하지
    않는다. 해석기는 yaml → 자동 캐시 순이고 자동으로 정해진 종목을 기억한다."""
    m = FakeMarket(industries={"357870": "전기전자", "440110": "알수없음"})
    st = tmp_path / "state"
    got = sector_suggest.auto_classify(
        SECTORS, m, market="KR", tickers=["005930", "357870", "440110"], state=st
    )
    assert got == {"357870": "반도체"}
    assert m.calls.count("get_industry_kr") == 2  # 005930 은 yaml 에 있어 조회 없음
    # 다시 불러도 캐시(미분류 포함)가 있어 네트워크 0회
    sector_suggest.auto_classify(SECTORS, m, market="KR", tickers=["357870", "440110"], state=st)
    assert m.calls.count("get_industry_kr") == 2
    r = sector_suggest.resolver(SECTORS, "KR", state=st)
    assert r("005930") == "반도체" and r("357870") == "반도체" and r("440110") is None
    assert r.auto_tickers == {"357870"}


def test_auto_classify_tolerates_lookup_failure(tmp_path):
    class Boom(FakeMarket):
        def get_industry_kr(self, ticker):
            raise RuntimeError("KIS 503")

    got = sector_suggest.auto_classify(
        SECTORS, Boom(), market="KR", tickers=["357870"], state=tmp_path
    )
    assert got == {}
    # 실패는 캐시하지 않는다(다음 관측 때 다시 시도)
    assert sector_suggest.load_auto(tmp_path).get("KR", {}) == {}


def test_auto_classify_groups_etf_by_brand_name(tmp_path):
    """KIS 가 업종을 주지 않는 ETF 는 운용사 상표 이름으로 'ETF' 로 묶는다(표시 전용)."""
    m = FakeMarket(industries={})
    got = sector_suggest.auto_classify(
        SECTORS,
        m,
        market="KR",
        tickers=["357870", "336260"],
        state=tmp_path,
        names={"357870": "TIGER CD금리투자KIS(합성)", "336260": "두산퓨얼셀"},
    )
    assert got == {"357870": "ETF"}
