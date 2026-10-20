# Phase 13 US 재무 history·섹터 자동 분류 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** US 종목의 `fundamental.history`(3개년 10-K + 최근 10-Q)를 KR 과 같은 공통 키로 채우고, 유니버스에 새 종목을 넣을 때 `config/sectors.yaml` 의 종목→섹터 항목을 KIS 업종 정보로 **제안·기록**하는 `trader universe sectors` 를 만든다.

**Architecture:** EDGAR companyfacts 한 번으로 연도별 10-K 와 최근 10-Q 를 뽑는 `edgar.financial_history_us` 를 추가하고(`asof` 이전 제출분만, DART `financial_history` 와 같은 `{years, latest_quarter}` 형태), `EdgarClient.companyfacts` 는 인스턴스 안에서 메모이즈해 ratios·history 가 한 번만 받는다. 섹터 분류는 결정적 데이터를 유지한다: 사람이 관리하는 **업종→대분류 표**(`kr_industry_to_sector`, `us_industry_to_sector`)를 `sectors.yaml` 에 두고, 명령은 KIS 가 주는 업종(KR `bstp_kor_isnm`, US `e_icod`)을 그 표로 바꿔 **빠진 종목만** 제안하며 `--write` 때만 yaml 의 `kr:`/`us:` 블록 끝에 줄을 덧붙인다(주석 보존, 기존 항목 불변).

**Tech Stack:** httpx/respx, pydantic, typer, pyyaml(읽기만; 쓰기는 텍스트 삽입).

**Spec:** `docs/superpowers/specs/2026-10-09-trading-agent-design.md` §3(수집 표 "US: EDGAR companyfacts 동일 비율", 3.3 룩어헤드), §2(KIS tr_id 는 `broker/tr_ids.py` 가 유일한 출처). 이연 출처: Phase 12 플랜 `범위 조정` 표(US 분기 history, 섹터 자동 분류).

## Global Constraints
- 룩어헤드: 10-K·10-Q 모두 `filed <= asof`. 분기 기간형 값은 `60 <= (end − start).days <= 120`, 잔액형은 같은 `end`.
- 공통 키(매출액·영업이익·당기순이익·자산총계·부채총계·자본총계)만 — `dart.basic_ratios` 와 같은 축.
- `tr_ids.py` 에만 새 TR 추가(`OVS_PRICE_DETAIL`, 공식 예제 값 `HHDFS76200200`, `/uapi/overseas-price/v1/quotations/price-detail`). 시세는 실전 env 클라이언트만.
- `sectors.yaml` 쓰기는 `--write` 일 때만, 빠진 종목만, 기존 줄·주석 불변. `templates/config/sectors.yaml` 도 같이 써서 패리티 유지.
- 메시지 한국어, pathlib, `extra="forbid"`, `uv run pytest`·`ruff` 0건.

## Review Focus
- 회계연도가 12월이 아닌 회사(AAPL 9월 결산): `years` 키는 10-K `end` 의 연도이고 최근 10-Q 가 그 10-K 보다 뒤에 제출됐을 때만 `latest_quarter` 가 된다 (Task 1 테스트).
- 10-Q 가 asof 뒤에 제출됐으면 이전 10-Q 로 물러나야 하며 없으면 `latest_quarter: None` (Task 1 테스트).
- `apply` 를 두 번 돌려도 같은 종목 줄이 두 번 들어가지 않고, 기존 항목이 있는 종목은 건드리지 않는다 (Task 2 테스트).
- KIS 가 업종을 주지 않거나(빈 문자열) 표에 없는 업종이면 제안 없이 `note` 로 알리고 exit 1 (Task 2 테스트).
- `--write` 뒤 `load_sectors()` 가 그대로 읽히고 패리티 테스트가 통과한다 (Task 2 테스트).

---

### Task 1: US `fundamental.history` (EDGAR 10-K 3개년 + 최근 10-Q)
**Files:** Modify `src/trader/sources/edgar.py`(`financial_history_us`, `companyfacts` 메모), `src/trader/sources/inputs.py`(US `history`), `.claude/agents/analyst-fundamental.md`(history 설명), `tests/helpers_sources.py`(FakeEdgar 에 10-Q); Test `tests/sources/test_edgar.py`, `tests/sources/test_inputs.py`.
**Interfaces (Produces):** `edgar.financial_history_us(facts: dict, asof: date) -> dict[str, Any]` = `{"years": {<end 연도:int>: fin}, "latest_quarter": {"form": "10-Q", "end": iso, "filed": iso, **fin} | None}` — years 는 `filed <= asof` 인 10-K 중 end 내림차순 최신 3개(각 연도 fin 은 그 end 의 기간형·잔액형 값, `_전기` 없음), latest_quarter 는 `filed <= asof` 이고 end 가 최신 10-K end 보다 뒤인 10-Q 중 최신. `EdgarClient.companyfacts(cik)` 는 `self._facts: dict[str, dict]` 메모.
- [ ] Step 1 실패 테스트 `test_financial_history_us_three_years_and_latest_quarter`(FakeEdgar facts: 10-K FY2023·2024·2025(2025 는 2026-05-15 제출) + 10-Q end 2026-03-31 filed 2026-05-10, end 2025-09-30 filed 2025-11-01; asof 2026-04-01 → years {2023, 2024}, latest_quarter end 2025-09-30; asof 2026-06-01 → years {2023, 2024, 2025}, latest_quarter end 2026-03-31), `test_financial_history_us_quarter_none_when_all_after_asof`, `test_companyfacts_memoized`(respx 호출 1회). `tests/sources/test_inputs.py::test_us_history_section_from_edgar`(FakeEdgar 주입 → `fundamental.history.error is None`, `years` 키 문자열 연도, `latest_quarter.form == "10-Q"`), 기존 `us["fundamental"]["history"]["error"] == "phase7"` 기대는 edgar 없음 → `"no edgar client"` 로.
- [ ] Step 2 RED → Step 3 구현 → Step 4 `uv run pytest tests/sources -q` PASS → Step 5 md 갱신, 커밋 `feat(sources): US fundamental.history (EDGAR 10-K 3개년 + 최근 10-Q)`.

### Task 2: 섹터 자동 제안 `trader universe sectors`
**Files:** Modify `src/trader/broker/tr_ids.py`(`OVS_PRICE_DETAIL`), `src/trader/broker/kis_market.py`(`get_industry_kr/us`), `src/trader/broker/fake.py`(`industries=`), `src/trader/config.py`(`SectorMap.kr_industry_to_sector/us_industry_to_sector`, `classify`), `config/sectors.yaml`+템플릿(업종표), `src/trader/cli.py`(`universe sectors`); Create `src/trader/scan/sector_suggest.py`, `tests/scan/test_sector_suggest.py`, `tests/broker/fixtures/price_detail_us.json`; Test `tests/broker/test_kis_market.py`, `tests/test_config_ml.py`.
**Interfaces:** `KisMarket.get_industry_kr(ticker) -> str | None`(DOM_PRICE `output.bstp_kor_isnm`, 빈 값 None), `get_industry_us(symbol, exchange) -> str | None`(OVS_PRICE_DETAIL `output.e_icod` 3자리). `SectorMap.classify(industry: str | None, market) -> str | None`. `sector_suggest.suggest(universe, sectors, market_client, *, market, only_missing=True) -> list[dict{ticker, exchange, industry, current, suggested, note}]`(KIS 실패는 `note="조회 실패: <오류>"`, 표에 없으면 `note="업종 매핑 없음: <industry> → <market>_industry_to_sector 에 추가"`). `sector_suggest.apply(path: Path, market, rows, *, today: date) -> int` — `suggested` 가 있고 `current` 가 없는 행만 `  "<ticker>": <sector>  # <industry> (auto <YYYY-MM-DD>)` 로 해당 `kr:`/`us:` 블록 마지막 항목 뒤에 삽입, 반환 추가 수; 이미 있는 종목은 건너뛴다. CLI `universe sectors --market KR|US [--all] [--write]`: 표 출력(ticker, 업종, 현재, 제안, note), `--write` 면 `config/sectors.yaml` 과 `templates/config/sectors.yaml` 둘에 apply, 제안 없는(note) 행이 있으면 exit 1.
- [ ] Step 1 실패 테스트 `tests/broker/test_kis_market.py::test_industry_kr_and_us`(price_kr.json 의 `bstp_kor_isnm`, 새 픽스처 `e_icod`), `tests/test_config_ml.py::test_sector_map_classify_by_industry`, `tests/scan/test_sector_suggest.py::test_suggest_only_missing_and_maps_industry`, `::test_suggest_notes_unmapped_and_failed`, `::test_apply_inserts_once_preserving_comments`(두 번 apply → 줄 1개, 주석·기존 줄 보존, `load_sectors` 로 읽힘), `::test_cli_universe_sectors_write_updates_config_and_template`.
- [ ] Step 2 RED → Step 3 구현(업종표: KR 은 KIS `bstp_kor_isnm` 기준 KOSPI 업종명 → 11개, US 는 KIS `e_icod` 3자리 → 11개; Stock `sector_map.yaml` 의 축을 따른다) → Step 4 `uv run pytest tests/scan tests/broker tests/test_config_ml.py tests/test_cli_app.py -q` PASS → Step 5 커밋 `feat(scan): universe sectors — KIS 업종으로 섹터 제안·기록`.

### Task 3: 실기·문서
- [ ] `uv run trader universe sectors --market KR --all`·`--market US --all` 로 유니버스 전체의 업종→제안을 받아 현재 수동 매핑과 다른 종목·표에 없는 업종을 확인하고, 표(`*_industry_to_sector`)를 보완한다(수동 항목은 바꾸지 않음). `FAKE_CLAUDE=1 trader analyze --ticker AAPL --market US --exchange NAS --dry-run --skip-collect` 는 쓰지 않고 `trader collect --ticker AAPL --market US --exchange NAS --force` 로 `fundamental.history` 확인.
- [ ] CLAUDE.md(수집 절 history US, 커뮤니티·뉴스 정책 절의 유니버스 변경 절차에 `universe sectors`, tr_ids 추가), TODO Phase 13 절, `.claude/agents/analyst-fundamental.md`. 커밋 `docs: Phase 13`.

## 범위 조정
| 제외 항목 | 이유 | 재개 |
|---|---|---|
| 업종표 자동 생성(KIS 업종 코드 전체 목록) | 결정적 데이터는 사람이 관리; 표에 없는 업종은 명령이 알려 준다 | 필요 시 |
| US `e_icod` 000(미분류) 종목의 이름 기반 분류 | 외부 프로필 API 필요 | 사용자 결정 |
| 분기 재무로 TTM 비율 계산 | 비율 정의 변경은 ML 피처 영향 | 별도 Phase |
