# Phase 8 학습·평가·보정 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 결정의 결과를 메모리로 축적·복기(reflect)해 트레이더 입력에 되먹이고, 애널리스트 신호의 적중률을 보정 모델로 계량하며(결정 60건 미만이면 비활성), 계좌별 성과(수익률·MDD·Sortino·벤치마크·비용 민감도·회전율)를 페이지로 보여 주고, 실전 approve 전환을 사용자가 판단할 수 있는 준비도 점검 명령을 둔다.

**Architecture:** `memory/`(원장 `memory_entries` v5: 상황 벡터 12차원·결과·반성·3층 반감기·엠바고·서킷브레이커·워밍업 50건)가 `submit-decision` 때 엔트리를 만들고, `reflect` 명령이 만기 결정의 실현수익·벤치마크 대비 알파를 피처 스토어 종가로 채운 뒤 reflector 세션(`kind=reflect`)에 넘겨 반성 2종을 받아 저장한다. `calibration/`은 `analyst_reports` 신호 × 결과로 역할별 적중률(전체·상황 이웃)을 계산해 `35_calibration.json` 으로 트레이더에 준다. `ledger/performance.py` 는 `positions_snapshot`·`orders` 로 성과 지표를 계산하고 웹 `/api/performance` + 성과 페이지가 보여 준다. `backtest` 는 컷오프 이후 결정만 피처 스토어 가격으로 재평가한다. `readiness` 는 §0.2 #18 기준을 점검해 출력만 하고 모드는 사용자가 바꾼다.

**Tech Stack:** SQLite(v5 마이그레이션), numpy(코사인·반감기), pandas(성과·벤치마크), 기존 세션 러너(`kind=reflect`)·FAKE 스텁, FastAPI·React.

**Spec:** `docs/superpowers/specs/2026-10-09-trading-agent-design.md` §6.5(메모리), §9.3(신호 보정), §11 Phase 8 행, §13(운영 평가), §12(FinMem·FinAgent·FinCon 반영), §0.2 #18(실전 전환 기준), §6.1(stance).

## Global Constraints

- Phase 0~7 규칙 전부(CLAUDE.md). 금액 `Decimal`, 시각 UTC, 한국어, 라인 100자. 원장 마이그레이션은 **v5 추가만**.
- **메모리 엔트리** `{id, ticker, market, account_id(모의 기준), decided_at, decision_id, situation_vector(12), situation_summary, decision(action·weight·confidence), horizon_days, realized_return, alpha_vs_benchmark, reflection(JSON), outcome_at, tier(short|mid|long), importance, created_at}`. 결과는 **모의 계좌 기준**(`accounts` 중 env=paper 첫 계좌).
- **3층 반감기** 14/90/365일: 점수 = `recency(2^(−age/half_life)) × 0.4 + relevance(코사인) × 0.4 + importance × 0.2`. 승격: `|realized_return| ≥ 5%` 또는 반성에 `lesson` 이 있으면 상위 층으로(short→mid→long). 삭제: short 층에서 90일 지나고 importance < 0.2.
- **Outcome Embargo**: `outcome_at` 이 없는(만기 전) 엔트리는 검색에서 제외. **워밍업**: 결과가 채워진 엔트리 50건 미만이면 검색 결과는 빈 목록 + `warmup: true`. **서킷브레이커**: 모의 계좌 드로다운(최근 30일 NAV 고점 대비) ≤ −10% 또는 `settings.memory.vix_spike_pct`(기본 30%)를 넘는 VIX 급등(00_inputs `news_macro.macro.vix` 가 있을 때)이면 검색 중단 + 사유 기록.
- **보정**: 결정(`decisions` 행 중 결과가 채워진 것) < `ml.yaml models.calibration.min_decisions`(60) 이면 `35_calibration` 을 쓰지 않고 트레이더 `context.calibration` 은 null. 활성일 때 역할별 `{hit_rate, n, neighbor_hit_rate(k=20 코사인 이웃), bias}`.
- **벤치마크**: 1/N 유니버스(피처 스토어 종가 등가중)와 B&H(결정 시점 종가 보유)를 기본으로, 퀀트 단독(승격 모델 상위 5 등가중)은 모델이 있을 때만. 비용 민감도 0/10/20/30bp 는 주문 금액 × bp 를 누적 손익에서 뺀다.
- **실전 전환은 수동**: 어떤 코드도 실전 계좌 모드를 바꾸지 않는다. `readiness` 는 점검 결과만 출력한다(`control mode real-main approve` 는 사용자가 친다).
- reflector 세션은 LLM 비용이 드므로 `reflect` 는 사용자 요청 또는 데몬 `reflect` 잡(장 마감 후 1회, 만기 결정이 있을 때만)에서만 띄우고 `FAKE_CLAUDE=1` 로 전부 테스트한다.

## Review Focus

1. 만기 전 결정은 메모리 검색에 나오면 안 된다(Outcome Embargo) → Task 1 `test_retrieve_excludes_unmatured`.
2. 결과 엔트리 50건 미만이면 검색이 빈 목록을 돌려주고 트레이더 입력은 `memory: []` 여야 한다(워밍업) → Task 1 `test_warmup_blocks_retrieval`, Task 4 `test_trader_context_memory_empty_during_warmup`.
3. 드로다운 −10% 면 검색이 중단되고 사유가 남아야 한다 → Task 1 `test_circuit_breaker_on_drawdown`.
4. 결정 60건 미만이면 `35_calibration` 이 없고 `context.calibration` 은 null, 60건 이상이면 생성된다 → Task 3 `test_calibration_inactive_below_60`, `test_calibration_active_at_60`.
5. 어떤 명령도 실전 계좌 모드를 바꾸지 않는다: `readiness` 실행 전후 control 테이블이 같다 → Task 6 `test_readiness_never_changes_modes`.

## 범위 조정

| 항목 | 이유 | 재개 Phase |
|---|---|---|
| 상황 벡터 임베딩(LLM) | 피처 12차원 수치 벡터로 충분(결정적·비용 0) | — |
| 벤치마크 지수(KOSPI·S&P 실데이터) | 피처 스토어에 지수 이력이 없음. 1/N·B&H 로 대체, 지수는 Phase 9 에서 `features` 에 지수 열 추가 뒤 | 9 |
| 반성 결과로 프롬프트 자동 수정 | 메모리 검색으로 전달만. 자동 프롬프트 변경은 위험 | — |
| 피처 정규화(sma·bb·atr 비율) 재백필 | Task 7 에 파생 열 추가(새 스냅샷부터 채움). 3년 재백필은 사용자 실기 | 사용자 |
| RL 사이징 | Phase 9 | 9 |

---

## 파일 구조

```
src/trader/ledger/db.py            MIGRATIONS v5: memory_entries, calibration_runs
src/trader/memory/__init__.py
src/trader/memory/vector.py        situation_vector(row) 12차원, summary
src/trader/memory/store.py         record_decision, fill_outcome, set_reflection, retrieve, promote_and_prune, circuit_breaker
src/trader/memory/outcomes.py      mature(conn, state, today) → 실현수익·알파 채움 (피처 스토어 종가, 1/N)
src/trader/calibration/__init__.py
src/trader/calibration/model.py    hit_rates(conn) → {role: …}, build_calibration(...) → 35_calibration 데이터
src/trader/ledger/performance.py   nav_series, metrics(수익률·MDD·Sortino), benchmarks(1/N·B&H·quant), cost_sensitivity, turnover
src/trader/orchestrator/tools.py   submit_decision → memory.record_decision; trader_context memory·calibration; submit-reflection 도구
src/trader/orchestrator/reflect.py run_reflect(…) 세션 kind=reflect (FAKE 포함)
.claude/agents/reflector.md
src/trader/agents/schemas.py       Reflection, CalibrationReport
src/trader/backtest.py             simple_backtest(conn, state, since) 
src/trader/readiness.py            check(conn, accounts, settings, state) → 항목별 통과 여부
src/trader/config.py               Settings.memory{half_lives, warmup, drawdown_stop_pct, vix_spike_pct}
src/trader/daemon.py               reflect 잡 → run_reflect (만기 결정 있을 때만)
src/trader/cli.py                  reflect, backtest, readiness, tool submit-reflection, control watchlist
src/trader/web/routers/performance.py, web/src/pages/Performance.tsx
src/trader/sources/indicators.py   파생 비율 열(sma_5_rel, sma_20_rel, sma_60_rel, bb_pos, atr_pct, macd_hist_rel) (Phase 7 이연)
src/trader/daemon.py               US 휴장일 정적 목록(NYSE) (Phase 6 이연)
src/trader/notify/commands.py      /pause /resume /mode (Phase 6 이연)
tests/memory/, tests/calibration/, tests/ledger/test_performance.py, tests/test_backtest.py, tests/test_readiness.py, tests/web/test_performance.py, tests/orchestrator/test_reflect.py
```

---

### Task 1: 메모리 저장소 (v5·상황 벡터·3층·엠바고·워밍업·서킷브레이커)

**Files:** Modify `src/trader/ledger/db.py`, `src/trader/config.py`(`MemorySettings{half_lives: {short: 14, mid: 90, long: 365}, warmup: 50, drawdown_stop_pct: -0.10, vix_spike_pct: 0.30, top_k: 5}` → `Settings.memory`, `settings.yaml` 키 추가). Create `src/trader/memory/__init__.py`, `vector.py`, `store.py`. Test `tests/memory/test_vector.py`, `tests/memory/test_store.py`.

**Interfaces:**
- v5: `memory_entries(id, ticker, market, account_id, decision_id, decided_at, situation_vector TEXT(json), situation_summary TEXT, decision TEXT(json), horizon_days INTEGER, realized_return REAL, alpha_vs_benchmark REAL, reflection TEXT(json), outcome_at TEXT, tier TEXT, importance REAL, created_at TEXT, UNIQUE(decision_id))`, `calibration_runs(id, created_at, n_decisions, active INTEGER, json TEXT)`, `control` 플래그 `memory_breaker`(JSON {active, reason, at}).
- `vector.DIMS = ("roc_10","rsi_14","vol_z_20","high_52w_pos","atr_pct","macd_hist_rel","sma_5_rel","sma_20_rel","flow_foreign_sign","news_log","community_log","is_us")`, `vector.situation_vector(row: dict, market: str) -> list[float]`(NaN→0, rsi 는 (x−50)/50, 비율 열이 없으면 close 로 계산, news·community 는 log1p/5 캡 1), `vector.summary(row, market) -> str`(한 줄 한국어).
- `store.record_decision(conn, *, decision_id, ticker, market, account_id, decided_at, decision: dict, row: dict | None, horizon_days) -> int`(멱등: decision_id UNIQUE → 기존 id), `store.fill_outcome(conn, entry_id, *, realized_return, alpha, outcome_at) -> None`(tier 승격 규칙 적용), `store.set_reflection(conn, entry_id, reflection: dict) -> None`(`lesson` 있으면 승격), `store.retrieve(conn, *, vector, now, settings: MemorySettings, market: str | None = None, k: int = 5) -> dict{entries: [...], warmup: bool, breaker: dict | None}`(엠바고·워밍업·브레이커 적용, 점수식), `store.promote_and_prune(conn, now, settings) -> dict{promoted, deleted}`, `store.circuit_breaker(conn, *, nav_series: list[tuple[str, Decimal]], vix_change: float | None, settings) -> dict | None`(드로다운 ≤ −10% 또는 VIX 급등이면 플래그 세움, 아니면 해제).

- [ ] Step 1 실패 테스트: `test_situation_vector_12_dims_and_nan_safe`, `test_record_decision_idempotent`, `test_retrieve_excludes_unmatured`, `test_warmup_blocks_retrieval`(49건 → 빈 목록·warmup True, 50건 → 결과), `test_retrieve_scores_recency_relevance_importance`(같은 벡터·최근 엔트리가 먼저), `test_promote_on_big_return_and_lesson`, `test_prune_old_low_importance_short`, `test_circuit_breaker_on_drawdown`(NAV 100→89 → 플래그, retrieve 빈 목록 + breaker 사유; 회복 시 해제).
- [ ] Step 2~4. Step 5 Commit `feat(memory): 메모리 저장소 v5 (상황 벡터·3층·엠바고·워밍업·서킷브레이커)`

### Task 2: 결과 채우기·reflector 세션·`reflect` 명령

**Files:** Create `src/trader/memory/outcomes.py`, `src/trader/orchestrator/reflect.py`, `.claude/agents/reflector.md`. Modify `src/trader/agents/schemas.py`(`Reflection{entry_id, signal_review: {role: {verdict: HIT|MISS|UNKNOWN, note}}, decision_review: {verdict: GOOD|BAD|NEUTRAL, note}, lesson: str | null, confidence: float}`), `src/trader/agents/registry.py`(`REFLECT_AGENTS=("reflector",)`), `src/trader/orchestrator/session.py`(`kind="reflect"`, stdin `entries`), `src/trader/agents/fake.py`(`run_fake_reflect`), `src/trader/orchestrator/tools.py`(`submit_reflection(ctx, *, payload)` → `store.set_reflection` + `state/runs/<date>/_reflect.json` 추가 기록), `src/trader/cli.py`(`reflect [--market] [--dry-run]`, `tool submit-reflection`), `src/trader/daemon.py`(`reflect` 잡 본체), `.claude/agents/orchestrator.md`(reflect 모드). Test `tests/memory/test_outcomes.py`, `tests/orchestrator/test_reflect.py`, `tests/test_cli_reflect.py`.

**Interfaces:**
- `outcomes.mature(conn, *, state, today, horizon_default=5) -> list[dict]`: `outcome_at` 이 없고 `decided_at + horizon_days` 거래일이 지난 엔트리마다 피처 스토어 종가로 `realized_return`(결정일 종가 → 만기일 종가; SELL 은 부호 반전, HOLD 는 0 수익이 아닌 "보유 안 함" 으로 실현수익 = 0·알파 = −벤치마크), 벤치마크 = 같은 창 1/N 유니버스 수익, `alpha = realized − benchmark` 채움. 종가가 없으면 건너뛴다. 반환: 채운 엔트리 요약 목록.
- `reflect.run_reflect(settings, conn, accounts, *, market, date, entries: list[dict], state, now, dry_run) -> SessionResult`: 세션 kind=reflect, stdin `{"kind":"reflect","entries":[{entry_id, ticker, decided_at, decision, situation_summary, realized_return, alpha_vs_benchmark, analyst_signals: {role: signal}}]}`; 오케스트레이터는 reflector 에게 전문을 주고 `tool submit-reflection --json '<Reflection[]>'` 로 제출. FAKE: 각 엔트리에 `{verdict NEUTRAL, lesson null}`.
- 데몬 `reflect` 잡: `mature()` 결과가 비면 세션 없이 종료, 있으면 `run_reflect` + `promote_and_prune`.

- [ ] Step 1 실패 테스트: `test_mature_fills_return_and_alpha_from_feature_store`(BUY 결정·5거래일 뒤 +4%, 1/N +1% → realized 0.04·alpha 0.03·tier 유지), `test_mature_skips_without_prices`, `test_sell_decision_return_inverted`, `test_reflection_schema`, `test_fake_reflect_session_sets_reflections`, `test_submit_reflection_promotes_on_lesson`, `test_cli_reflect_fake`, `test_daemon_reflect_job_skips_when_nothing_matured`.
- [ ] Step 2~4. Step 5 Commit `feat(memory): 결과 채우기·reflector 세션·reflect 명령`

### Task 3: 보정 모델 (`35_calibration`)

**Files:** Create `src/trader/calibration/__init__.py`, `model.py`. Modify `src/trader/agents/schemas.py`(`CalibrationReport{asof, n_decisions, active, roles: {role: {hit_rate, n, neighbor_hit_rate, neighbor_n, bias}}}`), `src/trader/orchestrator/tools.py`(`ensure_calibration(ctx, *, ticker, market, date)` → 활성일 때만 `35_calibration` 저장, `trader_context.calibration`), `src/trader/cli.py`(`calibration build` 명령, `tool collect`·`analyze` 뒤 호출), `src/trader/daemon.py`. Test `tests/calibration/test_model.py`, `tests/orchestrator/test_tools.py`(추가).

**Interfaces:**
- `model.signal_hits(conn) -> pd.DataFrame[decision_id, role, signal(+1/0/−1), realized_sign(+1/0/−1), vector]`(`analyst_reports` × `memory_entries`(outcome 있음) 조인; BUY/STRONG_BUY → +1, SELL/STRONG_SELL → −1, NEUTRAL/UNKNOWN → 0).
- `model.hit_rates(df, *, query_vector: list[float] | None, k: int = 20) -> dict[role, {hit_rate, n, neighbor_hit_rate, neighbor_n, bias(평균 signal − 평균 realized_sign)}]`(hit = signal ≠ 0 이고 signal == realized_sign).
- `model.build_calibration(conn, *, min_decisions: int, query_vector, asof) -> dict | None`(결과 엔트리 수 < min_decisions 면 None, 아니면 CalibrationReport dump; `calibration_runs` 기록).
- 트레이더 md: `context.calibration` 이 있으면 적중률 낮은 역할의 근거 가중을 낮춘다는 지시 1문장.

- [ ] Step 1 실패 테스트: `test_signal_hits_join_and_sign_mapping`, `test_hit_rates_overall_and_neighbors`, `test_calibration_inactive_below_60`(59건 → None, `35_calibration` 없음, `context.calibration` None), `test_calibration_active_at_60`(60건 → 생성·역할 4개), `test_cli_calibration_build`.
- [ ] Step 2~4. Step 5 Commit `feat(calibration): 신호 적중률 보정 (결정 60건 미만 비활성)·35_calibration 투입`

### Task 4: 트레이더 입력 되먹임 (memory·calibration) + 서킷브레이커 갱신 지점

**Files:** Modify `src/trader/orchestrator/tools.py`(`trader_context`: `memory` = `store.retrieve(...)["entries"]` 상위 5(summary·decision·realized_return·alpha·reflection.lesson 만), `calibration` = `35_calibration` data; `submit_decision` 뒤 `record_decision`; `memory_ids_used` 를 엔트리에 역기록), `src/trader/orchestrator/executor.py`(`build_portfolio` 뒤 모의 계좌면 `store.circuit_breaker` 갱신), `.claude/agents/trader.md`. Test `tests/orchestrator/test_tools.py`(추가), `tests/orchestrator/test_executor.py`(추가).

- [ ] Step 1 실패 테스트: `test_trader_context_memory_empty_during_warmup`, `test_trader_context_memory_top5_after_warmup`, `test_submit_decision_records_memory_entry_and_ids_used`, `test_executor_updates_circuit_breaker_from_nav`.
- [ ] Step 2~4. Step 5 Commit `feat(orchestrator): 트레이더 입력에 메모리·보정 되먹임, 서킷브레이커 갱신`

### Task 5: 성과 계산·API·페이지

**Files:** Create `src/trader/ledger/performance.py`, `src/trader/web/routers/performance.py`, `web/src/pages/Performance.tsx`(+ 라우트 `/performance`, Nav). Modify `src/trader/status.py`("## 성과(30일)" 절: 계좌별 수익률·MDD). Test `tests/ledger/test_performance.py`, `tests/web/test_performance.py`, `tests/test_status.py`.

**Interfaces:**
- `performance.nav_series(conn, account_id, market, *, days) -> list[tuple[date, Decimal]]`(positions_snapshot 일별 최신 nav), `performance.metrics(navs) -> {return, mdd, sortino, days, start, end}`(일간 수익률, Sortino = 평균/하방편차×√252, 하방 0 이면 None), `performance.benchmarks(state, market, start, end, *, quant_model=None) -> {one_over_n, buy_and_hold, quant}`(피처 스토어 종가), `performance.cost_sensitivity(conn, account_id, market, start, end, bps=(0,10,20,30)) -> {bp: net_return}`, `performance.turnover(conn, account_id, market, start, end, navs) -> float`, `performance.report(conn, state, account_id, market, *, days=30) -> dict`(전부 + `metrics.decision_distribution`·`convergence_rate`).
- `GET /api/performance?account_id=&market=&days=30` → report. 페이지: 계좌·시장 선택, 지표 카드, NAV vs 1/N vs B&H 라인(lightweight-charts LineSeries), 비용 민감도 표, 결정 분포·수렴율.

- [ ] Step 1 실패 테스트: `test_nav_series_and_metrics`(합성 NAV → return·MDD·Sortino 값 검증), `test_benchmarks_from_feature_store`, `test_cost_sensitivity_subtracts_bps`, `test_turnover`, `test_report_account_filter_and_empty`, `test_api_performance_requires_account_and_market`, `test_status_shows_performance`.
- [ ] Step 2~4 (`npm test`·`tsc`·build). Step 5 Commit `feat(performance): 성과 지표·벤치마크·비용 민감도·API·페이지`

### Task 6: 간이 백테스트·준비도 점검·실전 전환 절차 문서

**Files:** Create `src/trader/backtest.py`, `src/trader/readiness.py`, `docs/runbook-live-transition.md`. Modify `src/trader/cli.py`(`backtest --since DATE [--market]`, `readiness`), `docs/TODO.md`. Test `tests/test_backtest.py`, `tests/test_readiness.py`.

**Interfaces:**
- `backtest.simple_backtest(conn, state, *, since: date, market: str | None = None, horizon_default=5) -> dict{n, hit_rate, avg_return, avg_alpha, by_action: {...}, cutoff: since}`: `since` 이후 결정만(룩어헤드 방지: 피처 스토어 종가는 결정일 이후 봉만), 메모리 결과가 있으면 그것을, 없으면 `outcomes` 와 같은 계산.
- `readiness.check(conn, *, accounts, settings, state, today) -> dict{items: [{key, ok, value, threshold, note}], ok: bool}`: 항목 = 모의 운영일 ≥ 20거래일(`runs` 의 analyze ok 날짜 수), 최근 28일 runs error 비율 ≤ 10%, 주문 failed 비율 ≤ 10%, 모의 30일 수익률 ≥ 1/N 벤치마크, MDD ≥ −10%, 보정 활성 여부(정보), 미승인 오래된 승인 0건. **모드는 바꾸지 않는다**.
- `docs/runbook-live-transition.md`: `readiness` 통과 → `trader control mode real-main approve` → 텔레그램 승인 1주 → `control mode real-main auto --confirm LIVE` 순서와 되돌리는 법.

- [ ] Step 1 실패 테스트: `test_backtest_only_after_cutoff`, `test_backtest_hit_rate_and_alpha`, `test_readiness_items_and_thresholds`, `test_readiness_never_changes_modes`, `test_cli_backtest_and_readiness`.
- [ ] Step 2~4. Step 5 Commit `feat: 간이 백테스트·실전 준비도 점검·전환 런북`

### Task 7: 이연 항목 — 파생 비율 피처·US 휴장일·텔레그램 제어 명령·관심종목

**Files:** Modify `src/trader/sources/indicators.py`(`COLUMNS` 에 `sma_5_rel, sma_20_rel, sma_60_rel, bb_pos, atr_pct, macd_hist_rel` 추가 — 기존 parquet 는 NaN), `src/trader/ml/features.py`(FEATURE_COLUMNS 자동 반영 확인), `src/trader/daemon.py`(`US_HOLIDAYS` 정적 집합 2026~2027 NYSE + `should_skip` US 분기), `src/trader/notify/commands.py`(`/pause`, `/resume`, `/mode <계좌> <모드>`(실전 auto 는 거부 안내), `/watch add|remove <ticker>`), `src/trader/control/control.py`(`get_watchlist/set_watchlist`), `src/trader/scan/quant.py`(`candidates(..., watch_extra=…)`) + `cli scan`/데몬. Test `tests/sources/test_indicators.py`(추가), `tests/test_daemon.py`, `tests/notify/test_commands.py`, `tests/scan/test_quant.py`.

- [ ] Step 1 실패 테스트: `test_derived_ratio_columns`, `test_us_holiday_skipped`, `test_telegram_pause_resume_mode_watch`, `test_real_auto_via_telegram_rejected`, `test_scan_includes_runtime_watchlist`.
- [ ] Step 2~4. Step 5 Commit `feat: 파생 비율 피처·US 휴장일·텔레그램 제어 명령·관심종목`

### Task 8: 실기·문서·PR

- [ ] Step 1: ruff·pytest·`npm test`·`tsc`·build.
- [ ] Step 2 실기: `FAKE_CLAUDE=1 uv run trader analyze --ticker 000660 --dry-run --skip-collect` 뒤 `memory_entries` 1행(워밍업 중이라 context.memory 빈 목록); `uv run trader reflect --market KR --dry-run`(만기 결정 없음 → 세션 없음); `uv run trader backtest --since 2026-10-01`; `uv run trader readiness`(실패 항목 표시, 모드 불변); 웹 `/api/performance?account_id=paper-main&market=KR`.
- [ ] Step 3: TODO Phase 8 체크(완료 기준 "모의 4주 성과·보정 활성·실전 approve" 는 사용자 확인 대기), CLAUDE.md(메모리·보정·성과·준비도·전환 절차 규칙), `uv run trader status`, 커밋, 리뷰(fable), PR → `feat/phase7-ml`.

## Self-Review 메모
- §6.5 엔트리·3층·검색·승격삭제·엠바고·반성 2종·`memory_ids_used`·서킷브레이커·워밍업 → Task 1·2·4. §9.3 → Task 3. §13 운영 평가(수익률·MDD·Sortino·벤치마크·비용 민감도·회전율·결정 분포·수렴율) → Task 5. 간이 백테스트·실전 전환 절차(수동) → Task 6. 이연(피처 정규화·US 휴장일·텔레그램 명령·관심종목) → Task 7.
- 타입 일치: `store.retrieve` 반환 `{entries, warmup, breaker}` 를 Task 4 가 쓴다. `outcomes.mature` 가 채우는 `realized_return·alpha_vs_benchmark` 를 Task 3·6 이 읽는다. `vector.situation_vector(row, market)` 를 Task 1·3·4 가 같은 시그니처로 쓴다.
