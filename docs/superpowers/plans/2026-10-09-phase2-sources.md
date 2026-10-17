# Phase 2 수집 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 종목 하나의 애널리스트 입력을 수집해 `state/runs/<date>/<market>/<ticker>/00_inputs.json` 으로 조립하고(`uv run trader collect --ticker 005930`), 유니버스 전체의 일별 피처 스냅샷을 `state/features/<date>.parquet` 에 쌓는다(3년 백필 포함).

**Architecture:** `sources/` 아래 수집기 하나당 파일 하나. 모든 수집기는 `Section{data, received_at, error}` 으로 결과를 감싸고 실패해도 다음 절로 진행한다. KIS 호출은 `KisMarket` 을 확장(분봉·호가·투자자동향·지수)해 재사용하고, DART·EDGAR·뉴스·거시·커뮤니티는 얇은 httpx 클라이언트다. `sources/inputs.py` 가 애널리스트 역할별 절(technical/fundamental/news_macro/sentiment)로 묶어 `orchestrator/run_store` 로 저장한다. `ml/features.py` 는 `indicators.py` 의 시계열 함수로 과거 날짜별 피처 행을 만든다.

**Tech Stack:** 기존 + pandas, pyarrow(parquet), beautifulsoup4(HTML). RSS 는 표준 `xml.etree`.

**Spec:** `docs/superpowers/specs/2026-10-09-trading-agent-design.md` — §0.3 커뮤니티 정책·뉴스·DART, §3 전체(3.1 입력표, 3.2 방침, 3.3 룩어헤드, 3.4 피처 스토어), §4.5 체크포인트 경로, §11 Phase 2, §13.

## Global Constraints

- Phase 0·1 규칙 전부. 저장 시각 UTC ISO8601(`ledger/db.now_iso()` 재사용), 표시만 KST.
- 모든 수집 항목에 `received_at`. 뉴스·커뮤니티·공시는 `asof` 이후 발행분 제외, EDGAR 는 `filed <= asof`.
- 수집기는 예외를 던지지 않는다: `base.run_section(fn, enabled=...)` 이 `Section` 으로 감싼다. 호출 없이 건너뛸 때는 `skipped_section(reason)`. `SKIP_REASONS = {"disabled", "phase6", "not available for US"}` 는 `errors` 목록에 집계하지 않는다.
- `sources.yaml` 로 소스별 on/off(기본값은 스펙 §0.3 정책). 꺼진 소스는 호출하지 않는다.
- 시세·지표·수급은 **실전 env `KisMarket`**. 새 HTTP 클라이언트의 키는 `params`/헤더로만, 로그·URL·예외에 남기지 않는다(`_quiet_httpx()` 패턴).
- 외부 HTTP 는 `httpx.Client(timeout=10)`, 공통 User-Agent, 커뮤니티는 소스당 요청 간격 ≥ 2초. 테스트는 `respx` 전부 차단(`respx_mock` 픽스처, `assert_all_called=False`).
- SEC EDGAR `User-Agent: "<이름> <이메일>"` 필수. DART `corpCode.xml` 7일 캐시(`state/cache/`), 재무제표는 호출마다, 공시는 `rcept_dt`.
- 과거 `asof` 재수집 금지: `collect_and_save` 와 `features.snapshot` 은 `asof` 가 now 보다 24시간 넘게 과거면 `ConfigError`.
- `00_inputs.json` 저장 전 `Decimal` → 문자열, 날짜 → ISO. 구조를 바꾸면 `inputs.SCHEMA_VERSION` 을 올린다.
- 의존성은 Windows 휠 제공 라이브러리만. 셸 스크립트 없음.

## 범위 조정 (스펙 §3 중 Phase 2 에서 제외, 절은 `null` + `error: "phase6"`)

| 항목 | 이유 | 재개 Phase |
|---|---|---|
| KRX 공매도·대차잔고 | KRX 포털은 OTP 폼 POST 방식이라 깨지기 쉬움 | 6 |
| 검색트렌드(네이버 DataLab·Google Trends) | 별도 키·비공식 라이브러리 | 6 |
| 토스증권·네이버 종목토론실 | SPA 내부 API 를 브라우저로 조사해야 함(스펙 §0.3) | 6 |
| YouTube 자막·텔레그램 채널·`manual_inputs/` | 스펙이 Phase 6 명시 | 6 |
| 동종업 중앙값 대비 비율 | 유니버스 전체 비율이 피처 스토어에 쌓인 뒤 | 7 |
| 7일 이벤트 캘린더 | 공식 소스 미정. `events: []` 자리만 | 6 |
| US 재무비율 과거 연도별(피처 스토어) | EDGAR 연도별 재구성은 Phase 7 | 7 |
| 투자자동향 과거 3년 | KIS 가 최근 30영업일만 반환. 백필은 가격 지표·DART 분기 비율만 | — |

## Review Focus

1. 신규 상장주처럼 일봉이 120일 미만이면 `sma_120`·`high_52w_pos` 는 `None` 이고 크래시 없어야 한다 → Task 3 `test_insufficient_history_returns_none`.
2. `asof` 가 과거(재실행)일 때 그 이후 발행된 뉴스가 입력에 섞이면 안 된다 → Task 7 `test_lookahead_items_filtered`.
3. 수집기 하나가 예외·타임아웃이어도 `00_inputs.json` 은 만들어지고 해당 절만 `null` + `error` 여야 한다 → Task 10 `test_failed_section_recorded_and_file_written`.
4. DART 가 `status: "013"`(조회 결과 없음)을 주는 종목에서 비율은 전부 `None` 이고 예외가 없어야 한다 → Task 5 `test_no_financials_gives_none_ratios`.
5. 클리앙 게시판 HTML 구조가 바뀌어 글을 하나도 못 찾으면 빈 목록 + 경고이지 예외가 아니어야 한다 → Task 9 `test_clien_unexpected_html_gives_empty`.

---

## 파일 구조

```
pyproject.toml                          pandas, pyarrow, beautifulsoup4 추가
src/trader/broker/tr_ids.py             DOM_MINUTE, DOM_ORDERBOOK, DOM_INVESTOR, DOM_INDEX, OVS_MINUTE, OVS_ORDERBOOK
src/trader/broker/kis_market.py         get_minute_kr/us, get_orderbook_kr/us, get_investor_kr, get_index_kr
src/trader/broker/models.py             OrderBook
src/trader/orchestrator/__init__.py  run_store.py   run_dir(), write_checkpoint(), read_checkpoint()
src/trader/sources/__init__.py  base.py  indicators.py  dart.py  edgar.py  news.py  macro.py  community.py  inputs.py
src/trader/ml/__init__.py  features.py
src/trader/cli.py                       collect, features snapshot|backfill, _build_input_deps, _build_dart
tests/orchestrator/test_run_store.py  tests/sources/{conftest,test_base,test_indicators,test_dart,test_edgar,test_news,test_macro,test_community,test_inputs}.py
tests/sources/fixtures/   tests/ml/test_features.py  tests/test_cli_collect.py  tests/test_cli_features.py  tests/helpers_sources.py
```

---

### Task 1: 의존성·체크포인트 저장소·Section 래퍼

**Files:** Modify `pyproject.toml`; Create `src/trader/orchestrator/{__init__,run_store}.py`, `src/trader/sources/{__init__,base}.py`, 테스트 `tests/orchestrator/test_run_store.py`, `tests/sources/test_base.py`

**Interfaces:**
- `run_store.run_dir(d: date, market: Market, ticker: str, state: Path|None=None) -> Path`(= `state/runs/<d>/<market>/<ticker>/`, 생성), `write_checkpoint(dir, name, data: dict) -> Path`(임시파일 후 `os.replace`), `read_checkpoint(dir, name) -> dict|None`.
- `base.Section(TypedDict){data, received_at: str, error: str|None}`, `base.SKIP_REASONS`, `base.run_section(fn, *, enabled=True, now=...) -> Section`, `base.skipped_section(reason, *, now) -> Section`.

- [ ] Step 1: `test_run_dir_layout`, `test_write_and_read_checkpoint`(원자적, 없으면 None), `test_run_section_ok/error/disabled`, `test_skipped_section`. Step 2~4. Step 5: Commit `feat: 체크포인트 저장소와 Section 래퍼`

### Task 2: `KisMarket` 확장 — 분봉·호가·투자자동향·지수 (Phase 1 이연)

**Files:** Modify `src/trader/broker/{tr_ids,kis_market,models}.py`; Test `tests/broker/test_kis_market.py` + 픽스처

**Interfaces:** tr_id 는 공식 예제에서 2026-10-09 확인(실전·모의 동일, 단 시세는 실전 env 로만 호출):

| 이름 | 경로 | tr_id | 파라미터 | 응답 |
|---|---|---|---|---|
| DOM_MINUTE | `/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice` | FHKST03010200 | `FID_ETC_CLS_CODE="", FID_COND_MRKT_DIV_CODE=J, FID_INPUT_ISCD, FID_INPUT_HOUR_1=HHMMSS, FID_PW_DATA_INCU_YN=N` | `output2[stck_bsop_date, stck_cntg_hour, stck_prpr, stck_oprc, stck_hgpr, stck_lwpr, cntg_vol]` (30건/호출, 최신→과거) |
| DOM_ORDERBOOK | `/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn` | FHKST01010200 | `FID_COND_MRKT_DIV_CODE=J, FID_INPUT_ISCD` | `output1[askp1..10, bidp1..10, askp_rsqn1..10, bidp_rsqn1..10]` |
| DOM_INVESTOR | `/uapi/domestic-stock/v1/quotations/inquire-investor` | FHKST01010900 | `FID_COND_MRKT_DIV_CODE=J, FID_INPUT_ISCD` | `output[stck_bsop_date, frgn_ntby_qty, orgn_ntby_qty, prsn_ntby_qty]` (최근 30영업일, 당일은 장 종료 후) |
| DOM_INDEX | `/uapi/domestic-stock/v1/quotations/inquire-index-price` | FHPUP02100000 | `FID_COND_MRKT_DIV_CODE=U, FID_INPUT_ISCD=0001(KOSPI)|1001|2001` | `output[bstp_nmix_prpr, bstp_nmix_prdy_ctrt, acml_vol]` |
| OVS_MINUTE | `/uapi/overseas-price/v1/quotations/inquire-time-itemchartprice` | HHDFS76950200 | `AUTH="", EXCD, SYMB, NMIN=1, PINC=1, NEXT="", NREC=120, FILL="", KEYB=""` | `output2[xymd, xhms, open, high, low, last, evol]` |
| OVS_ORDERBOOK | `/uapi/overseas-price/v1/quotations/inquire-asking-price` | HHDFS76200100 | `AUTH="", EXCD, SYMB` | `output2[pask1..10, vask1..10, pbid1..10, vbid1..10]` |

응답 컬럼명은 예제 코드에 없어 이전 구현 경험값이며, 첫 실기(`collect`)에서 확인해 다르면 고친다. `get_minute_kr(ticker) -> list[Candle]`(당일, `time` 채움, 오름차순), `get_minute_us(symbol, exchange)`, `OrderBook{ticker, market, bids[(price, qty)×10], asks[...], asof}` + `get_orderbook_kr/us`, `get_investor_kr(ticker, days=20) -> list[InvestorFlow]`(오름차순), `get_index_kr(code="0001") -> Quote`.

- [ ] Step 1: 메서드별 파싱 테스트 + 오름차순 검증. Step 2~4. Step 5: Commit `feat(broker): 분봉·호가·투자자동향·지수 조회`

### Task 3: 기술 지표 (`indicators.py`)

**Files:** Create `src/trader/sources/indicators.py`, `tests/sources/test_indicators.py`

**Interfaces:** `to_frame(candles) -> pd.DataFrame`(date 인덱스, float 컬럼), `indicator_frame(df) -> pd.DataFrame`(열: `sma_5/20/60/120, rsi_14, macd, macd_signal, macd_hist, kdj_k/d/j, bb_upper/mid/lower, atr_14, roc_10, vol_z_20, high_52w_pos`; 워밍업 구간은 NaN), `latest(candles) -> dict[str, float|None]`(마지막 행, NaN → None). 모든 비율은 소수(0.05 = 5%).

- [ ] Step 1: `test_sma_rsi_known_values`(손계산 값), `test_insufficient_history_returns_none`, `test_frame_has_all_columns`. Step 2~4. Step 5: Commit `feat(sources): 기술 지표`

### Task 4: DART 수집기

**Files:** Create `src/trader/sources/dart.py`, `tests/sources/test_dart.py`, 픽스처 `dart_corpcode.zip|xml, dart_fnltt.json, dart_list.json, dart_company.json`

**Interfaces:** `DartClient(api_key, cache_dir, *, http=None, now=...)`: `corp_code(ticker) -> str|None`(corpCode.xml 7일 캐시), `corp_name(ticker)`, `financials(corp_code, year, reprt_code="11011") -> dict[str, Decimal]`(한글 계정명 키: 매출액·영업이익·당기순이익·자산총계·부채총계·자본총계), `shares(corp_code, year) -> int|None`, `disclosures(corp_code, asof, days=30) -> list[dict{title, rcept_dt(ISO), url}]`(`rcept_dt <= asof`). `ratios_kr(dart, ticker, asof_date, price) -> dict`(매출성장률·영업이익률·ROE·부채비율·PER/PBR/PSR, 없으면 None) — `basic_ratios()` 는 EDGAR 와 공유.

- [ ] Step 1: `test_corp_code_cached`, `test_financials_parsed`, `test_no_financials_gives_none_ratios`(status 013), `test_disclosures_filtered_by_asof`, `test_api_key_not_in_url_log`. Step 2~4. Step 5: Commit `feat(sources): DART 수집기`

### Task 5: EDGAR 수집기

**Files:** Create `src/trader/sources/edgar.py`, `tests/sources/test_edgar.py`, 픽스처 `edgar_tickers.json, edgar_companyfacts.json`

**Interfaces:** `EdgarClient(user_agent, cache_dir, *, http=None)`: `cik(symbol) -> str|None`(ticker 맵 7일 캐시), `companyfacts(cik) -> dict`, `ratios_us(edgar, symbol, asof_date, price) -> dict`(DART 와 같은 키, `filed <= asof` 인 최신 10-K/10-Q 값).

- [ ] Step 1: `test_cik_lookup_cached`, `test_ratios_same_keys_as_kr`, `test_filed_after_asof_excluded`, `test_user_agent_header`. Step 2~4. Step 5: Commit `feat(sources): SEC EDGAR 수집기`

### Task 6: 뉴스 수집기

**Files:** Create `src/trader/sources/news.py`, `tests/sources/test_news.py`, 픽스처 `news_google.xml, news_naver.json`

**Interfaces:** `GoogleNewsRss(http)`: `search(query, *, lang="ko"|"en", max_items)`; `NaverNews(client_id, secret, http)`: `search(query, max_items)`(키는 헤더). 항목 `{title, summary, source, published_at(ISO), url, received_at}`. `collect_news(cfg, secrets, *, ticker, name, market, asof, http, now) -> {"stock": [...], "macro": [...]}`(`published_at <= asof`, 중복 URL 제거).

- [ ] Step 1: `test_google_rss_parsed`, `test_naver_requires_keys_else_error_section`, `test_lookahead_items_filtered`, `test_dedupe_by_url`. Step 2~4. Step 5: Commit `feat(sources): 뉴스 수집기`

### Task 7: 거시 수집기

**Files:** Create `src/trader/sources/macro.py`, `tests/sources/test_macro.py`, 픽스처 `macro_fred.json, macro_ecos.json`

**Interfaces:** `Fred(api_key, http)`: `latest(series_id) -> {value, date}`; `Ecos(api_key, http)`: `latest(stat_code, item_code)`; `collect_macro(cfg, secrets, *, market_client, asof, http, now) -> dict[str, Section]`(`kospi, sp500_proxy(KIS 지수 또는 FRED SP500), usdkrw, us10y, vix, base_rate_kr, base_rate_us`). 키 없는 소스는 `error: "no api key"`.

- [ ] Step 1: `test_fred_latest`, `test_ecos_latest`, `test_no_key_error_section`, `test_partial_failure_keeps_others`. Step 2~4. Step 5: Commit `feat(sources): 거시 지표 수집기`

### Task 8: 커뮤니티 수집기 (정책: Reddit·클리앙 on, DC·에펨 off)

**Files:** Create `src/trader/sources/community.py`, `tests/sources/test_community.py`, 픽스처 `reddit_new.rss, clien_board.html, dc_board.html, fmkorea_board.html`

**Interfaces:** 소스 클래스 `Reddit(http)`(서브레딧 `.rss` 최신 글, 검색 없음), `Clien(http)`(cm_stock 목록 1페이지, 종목명 포함 글만), `DcInside(http)`(stockus·kospi 마이너갤 목록), `FmKorea(http)`(stock 게시판 검색). 공통 반환 `{title, body, author, posted_at, url, received_at, source}`. `collect_community(cfg, *, ticker, name, market, asof, http, now) -> dict{posts: [...], sources_used: [...]}`; `cfg.community.<source>` 가 False 면 호출하지 않는다. 요청 간격 ≥ 2초(`sleep` 주입).

- [ ] Step 1: `test_reddit_rss_parsed`, `test_clien_list_filtered_by_name`, `test_clien_unexpected_html_gives_empty`, `test_disabled_sources_not_called`, `test_posted_after_asof_excluded`. Step 2~4. Step 5: Commit `feat(sources): 커뮤니티 수집기 (정책 기본값)`

### Task 9: `00_inputs.json` 조립과 `trader collect`

**Files:** Create `src/trader/sources/inputs.py`, `tests/sources/test_inputs.py`, `tests/helpers_sources.py`(`FakeDart`, `FakeEdgar`); Modify `src/trader/cli.py`; Test `tests/test_cli_collect.py`

**Interfaces:** `InputDeps(market, dart, edgar, http=None, now=...)`; `build_inputs(*, ticker, market, exchange, asof, cfg, secrets, deps) -> dict` 구조:
```
{ meta{ticker, market, exchange, name, asof, generated_at, schema_version: 1},
  technical{quote, daily, minute, orderbook, indicators},
  fundamental{ratios},
  news_macro{news, macro(항목별 Section dict)},
  sentiment{investor_flow, short_interest(phase6), community, trends(phase6), manual_inputs(phase6)},
  errors: ["technical.minute: KisApiError: ...", ...] }
```
`collect_and_save(..., state=None, force=False) -> Path`(있으면 건너뜀, 24시간 넘은 과거 asof 거부). CLI `trader collect --ticker [--market] [--exchange] [--asof] [--force]`: `cli._build_input_deps(secrets, cfg)`, 결과 경로와 `오류 절 N건` 출력, 설정·인자 오류만 exit 1.

- [ ] Step 1: `test_build_inputs_sections`, `test_failed_section_recorded_and_file_written`, `test_collect_skips_existing`, `test_past_asof_rejected`, `test_phase6_placeholders_not_in_errors`, `test_cli_collect`, `test_cli_collect_us_without_exchange_fails`. Step 2~4. Step 5: Commit `feat: 00_inputs.json 조립과 trader collect`

### Task 10: 피처 스토어와 3년 백필

**Files:** Create `src/trader/ml/{__init__,features}.py`, `tests/ml/test_features.py`, `tests/test_cli_features.py`; Modify `src/trader/cli.py`

**Interfaces:** `FEATURE_COLUMNS`(`date, ticker, market` + 지표 19열 + `flow_foreign_20d, flow_inst_20d, flow_indiv_20d, op_margin, roe, debt_ratio, news_count, community_count`); `snapshot_rows(...)`, `history_rows(*, ticker, market, candles, ratios_by_year) -> pd.DataFrame`(비율은 사업연도 Y 값을 Y+1 년 4월 1일부터 forward-fill), `write_rows(df, state=None) -> list[Path]`(날짜별 parquet, `(date, ticker)` 멱등), `backfill(*, market_client, dart, universe, markets, years=3, state=None, progress=...) -> int`(종목 실패는 로그 후 계속, 1000봉 상한), `snapshot(...)`. CLI `trader features snapshot [--market] [--asof]`, `trader features backfill [--years 3] [--market]`.

- [ ] Step 1: `test_snapshot_row_columns`, `test_history_rows_forward_fills_ratios_from_april`, `test_write_rows_idempotent`, `test_backfill_continues_on_failure`, `test_cli_features_backfill`. Step 2~4. Step 5: Commit `feat(ml): 피처 스토어와 3년 백필`

### Task 11: 실기 검증·문서·PR

- [ ] Step 1: `uv run ruff check src tests` 0건, `uv run pytest -q` 통과.
- [ ] Step 2: 실기 — `uv run trader collect --ticker 005930`, `uv run trader collect --ticker AAPL --market US --exchange NAS` → 두 `00_inputs.json` 의 `errors` 확인(키 없는 소스는 `no api key` 가 정상). `uv run trader features backfill --years 3 --market KR` → parquet 생성(실전 20/s 로 20종목 수 분).
- [ ] Step 3: TODO Phase 2 체크·날짜, 이연 항목을 Phase 6·7 절에 `(Phase 2 이연)` 으로, CLAUDE.md 수집 계층 규칙(Section·SKIP_REASONS·00_inputs 키 배치·룩어헤드·캐시 범위·피처 스토어 NaN 규칙), `uv run trader status`, 커밋 `docs: Phase 2 수집 완료`, PR → `feat/phase1-broker`.

## Self-Review 메모
- §3.1 표의 입력 중 Phase 2 제외분은 범위 조정 표에 전부 있고 `00_inputs` 에 `phase6` 자리를 둬 Phase 3 스키마가 바뀌지 않게 했다.
- §3.3 룩어헤드: 뉴스(Task 6)·커뮤니티(Task 8)·공시(Task 4)·EDGAR(Task 5)·비율 적용일(Task 10) 모두 `asof` 규칙. 익명화는 Phase 3.
- 타입: `Section`·`InputDeps`·`ratios_*` 키·`FEATURE_COLUMNS` 를 각 태스크 Interfaces 에 같은 이름으로 적었다. `Market` 은 `config.Market`.
