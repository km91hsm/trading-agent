"""universe sectors: KIS 업종 → sectors.yaml 종목 섹터 제안·기록 (빠진 종목만, 주석 보존)."""

from datetime import date
from decimal import Decimal

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
