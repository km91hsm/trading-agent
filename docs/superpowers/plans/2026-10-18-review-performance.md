# 전체 코드 검토 성능 개선 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 2026-10-18 검토의 성능 잔여 15건(`docs/TODO.md` "검토 잔여(성능)")을 고친다. PR #17 위에 쌓는다.

**Architecture:** 동작(결과값)은 바꾸지 않고 비용만 줄인다. 측정 가능한 항목은 전·후 수치를 플랜에 남긴다. 스키마 변경 없음.

**Tech Stack:** Python 3.12, pandas, numpy, SQLite, React 18 (Vite).

**Spec:** `docs/superpowers/specs/2026-10-09-trading-agent-design.md` §3(수집), §5.1(scan), §6(원장), §8(웹), §9(ML).

## 기준선 (2026-10-18, M2)
- `import trader.cli` 1.03 s (그중 `trader.sources.inputs` 0.68 s = pandas/pyarrow).
- `features.latest_rows(None, "KR")` 1.10 s(60파일) / `tickers=[1개]` 0.022 s / 없는 종목 0.905 s.
- `uv run pytest` 3분 35초, 그중 ML 학습 테스트 10개 ≈ 150 s.

## 결과 (2026-10-18)
| 항목 | 전 | 후 |
|---|---|---|
| `import trader.cli` | 1.03 s | 0.31 s |
| `latest_rows` (scan, 유니버스 전달) | 1.10 s | 0.09 s |
| 피처 스토어에 없는 종목 `latest_row` 재조회 | 0.9 s/회 | 캐시 적중 0 s |
| KR 판정의 `inquire-balance` 호출 | 2회 | 1회 |
| `init_schema`(최신 DB) 쓰기 잠금 | 8회 | 0회 |
| 웹 첫 번들 | 470 KB | 288 KB (+ 차트 청크 175 KB, 두 페이지만) |
| `uv run pytest` | 3분 35초 | 2분 34초 |
| CEM 모집단 64 × 5,000스텝 평가 | 3.6 s (루프) | 0.14 s (벡터화, 25배) |

## Global Constraints
- `uv run pytest`·`uv run ruff check src tests` 0건. 웹은 `cd web && npm run typecheck && npm test && npm run build`.
- 어떤 항목도 결과값(판정 수량·점수·학습 지표의 의미)을 바꾸지 않는다. 바꾸는 경우 테스트로 동치를 보인다.

## Review Focus
- 서브프로세스 `tool checkpoint list` 가 pandas 를 import 하지 않는가 (G1-1).
- `get_balance` 가 KR 에서 `DOM_BALANCE` 를 1회만 부르는가 (G1-5).
- CEM 벡터화가 루프 구현과 같은 보상을 내는가 (G2-12).
- `mature` 가 `since` 창 밖 파일을 읽지 않는가 (G1-11).
- `max_iter` 축소가 승격 판정 테스트를 깨지 않는가 (G1-15).

---

### Task G1: 핫 패스 소수정
- [x] 1 `cli.py:24` `from trader.sources.inputs import …` 를 `_build_input_deps`·`collect`·`analyze`·`tool_collect`·`daemon_cmd` 안으로. 테스트: `python -c "import trader.cli; import sys; assert 'pandas' not in sys.modules"`.
- [x] 3 `scan/quant.candidates` 가 `features.latest_rows(state, market, tickers=[…])` 로 부른다(`latest_features(market, state=, tickers=)`). `quant_report.latest_row` 는 `(ticker, market, state, asof, 최신 파일명)` 키 `lru_cache`. 테스트: 두 번째 호출이 parquet 를 다시 읽지 않음(`pd.read_parquet` monkeypatch 카운트).
- [x] 5 `KisBroker.get_balance(market) -> tuple[list[Position], Cash]` (KR 은 `_balance_kr` 1회, US 는 기존 2 API), `FakeBroker` 동일, `executor.build_portfolio` 가 `get_balance` 를 쓴다(없으면 두 메서드 폴백). 테스트: KR `get_balance` 가 요청 1회.
- [x] 6 `repo.held_since(conn, account_id, tickers=None)`: tickers 가 있으면 두 질의에 `AND ticker IN (...)`. `build_portfolio` 가 보유 종목을 넘긴다. 결과 동일(보유 0 은 어차피 제외).
- [x] 7 `repo.nav_history`: SQL 로 날짜별 최신(`GROUP BY date HAVING id=MAX(id)`) + `date >= lo`. `today` 없으면 `MAX(date)` 1행. 기존 테스트 통과.
- [x] 8 `db.init_schema`: `current_version(conn) >= SCHEMA_VERSION` 면 조기 반환(쓰기 잠금 없음). 테스트: 최신 DB 에서 `BEGIN IMMEDIATE` 0회(`conn.set_trace_callback`).
- [x] 11 `outcomes.mature`: 시장별 미결 행의 `min(decided KST date) − 10일` 을 `since`, `today` 를 `until` 로 `load_all`. 테스트: `load_all` monkeypatch 로 kwargs 확인.
- [x] 13 `web/sse.stream`: 연결 1개를 열어 재사용, 스냅샷에서 `calls`(agent_calls) 제외(분석 중 서브에이전트 호출마다 change 가 나던 원인). 테스트 갱신.
- [x] 15 `QuantModelCfg.max_iter`(기본 200) → `QuantModel(max_iter=)`. 테스트는 `helpers_ml.fast_cfg()`(max_iter 40)로. `test_cli_ml` 은 `config/ml.yaml` 에 적는다. 승격 판정 테스트 통과 확인.
- [x] 커밋 `perf: 핫 패스 소수정 (지연 import·latest_rows 종목 전달·잔고 1회·init_schema·nav_history·mature 창·SSE·max_iter)`

### Task G2: 큰 항목
- [x] 2 `features.backfill`: 종목 프레임을 모아 `CHUNK=10` 종목마다 `write_rows(pd.concat(frames))`. 실패 격리 유지. 테스트: `write_rows` 호출 수 ≤ ceil(n/10).
- [x] 4 `inputs.build_inputs`: name → technical 뒤 `fundamental`·`news_macro`·`sentiment` 를 `ThreadPoolExecutor(3)` 로 동시 실행. `DartClient` 지연 초기화에 `threading.Lock`. 테스트: 결과 동일(기존 테스트) + 세 절이 겹쳐 실행됨(지연 주입으로 wall-clock < 합).
- [x] 9 `calibration.signal_hits`: `memory_entries ⋈ analyst_reports` 1회 JOIN(`a.date = date(m.decided_at, '+9 hours')`). 기존 테스트 통과.
- [x] 10 `memory.retrieve`: 점수 열만 SELECT → 상위 k id 로 `_hydrate`. 결과 동일.
- [x] 12 `rl/learners.fit_cem`: 모집단을 한 번의 스텝 루프로 평가(`population_rewards`). 테스트: 루프 `_episode_reward` 와 1e-9 안에서 같음.
- [x] 14 `web/src/App.tsx`: `Chart`·`Performance` 를 `React.lazy` + `Suspense`. `npm run build` 로 청크 분리 확인.
- [x] 커밋 `perf: 백필 묶음 쓰기, 수집 병렬화, 보정 JOIN, 메모리 상위 k hydrate, CEM 벡터화, 차트 lazy`

### Task G3: 마무리
- [x] 전·후 수치 표를 이 플랜에 기록, `docs/TODO.md` 체크, CLAUDE.md 갱신, PR (`fix/review-correctness-2` 대상).
