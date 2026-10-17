# Phase 3 에이전트 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `uv run trader analyze --ticker 005930 --dry-run` 이 오케스트레이터 `claude -p` 세션을 띄워(또는 `FAKE_CLAUDE=1` 이면 파이썬으로 흉내 내) 서브에이전트 7명을 소집하고, `python -m trader tool …` 4개만 통해 체크포인트를 쌓아 `30_decision.json` 을 만들며, 중간 파일이 있으면 그 단계를 건너뛴다.

**Architecture:** 접근 B(스펙 §1). 파이썬 `orchestrator/session.py` 가 `claude -p` 를 **정확히 설치된 CLI 의 플래그**로 띄우고(`--tools "Bash,Agent"`, `--allowedTools "Bash(python -m trader tool *)"`, `--setting-sources ""`, 빈 cwd), 오케스트레이터 에이전트(`.claude/agents/orchestrator.md`)가 서브에이전트를 부른 뒤 결과 JSON 을 `tool checkpoint write` 로 넘긴다. 파이썬은 스키마 검증·순서 전제조건(`submit-decision` 은 `10_analyst_*` ≥ 2 + `25_research_summary`)·수렴 감시·요약 병합·비용 집계를 맡는다. `FAKE_CLAUDE=1` 이면 `agents/fake.py` 가 같은 도구 순서로 고정 JSON 을 쓴다.

**Tech Stack:** 기존 + 표준 `subprocess`·`uuid`. 새 의존성 없음.

**Spec:** `docs/superpowers/specs/2026-10-09-trading-agent-design.md` — §0.3 Claude 헤드리스(2.1.294 플래그), §1.1 두 층·도구 4개, §4 전체, §6.3 `agent_calls`, §7, §11 Phase 3, §13.

## Global Constraints

- Phase 0~2 규칙 전부.
- **`claude -p` 플래그는 `claude --help` 에 있는 것만**: `-p --output-format json --model <m> --tools "Bash,Agent" --allowedTools "Bash(python -m trader tool *)" --permission-mode dontAsk --setting-sources "" --disable-slash-commands --session-id <uuid> --max-budget-usd <b>`. `--max-turns`·`--append-system-prompt-file` 미사용, `--bare` 금지(구독 OAuth 차단). 오케스트레이터 지시문은 `--append-system-prompt <md본문>` 로 넘긴다. cwd 는 `state/claude_cwd/`(빈 디렉터리), env 에 `CLAUDE_CODE_RETRY_WATCHDOG=1` 과 `TRADER_HOME`(도구가 프로젝트를 찾도록) 추가.
- 서브에이전트 md 는 `.claude/agents/*.md`(프런트매터 `name, description, tools: [], model`). 오케스트레이터 세션이 `--setting-sources ""` 로 프로젝트 설정을 끊으면 `.claude/agents` 도 안 읽히므로 **서브에이전트 정의는 `--agents <json 파일>` 로 명시 전달**한다(파이썬이 md 7개를 읽어 JSON 으로 변환, `agents/registry.py`).
- subprocess 는 `shell=False, encoding="utf-8", errors="replace"`, 타임아웃 `settings.llm.timeout_s`. stdin 에 종목 목록 JSON.
- `tool` 명령은 **stdout 에 JSON 만** 출력한다(LLM 이 파싱). 로그는 stderr/파일. 오류도 `{"ok": false, "error": "..."}` JSON + exit 1.
- 모든 체크포인트는 `{"schema_version": 1, "data": <pydantic dump>, "call": {agent, model, session_id, cost_usd, duration_ms} | null}`. `checkpoint write` 는 스키마 검증 실패 시 `{"ok": false, "error": <pydantic 메시지>}` 로 거부해 LLM 이 1회 재작성하게 한다.
- 비용·호출 집계는 `agent_calls` 테이블(Phase 0 스키마)에 세션 단위 1행 + 도구 호출은 `runs` 와 연결. `settings.llm.daily_session_cap` 초과 시 세션을 띄우지 않고 `PipelineError`.
- `FAKE_CLAUDE=1` 이면 `claude` 를 찾지도 띄우지도 않는다(Windows dry-run).
- 프롬프트 공통 규칙 6문장(스펙 §4.1)을 8개 md 모두에 같은 문장으로.

## 범위 조정

| 항목 | 이유 | 재개 Phase |
|---|---|---|
| `05_quant.json`·`35_calibration.json`·memory | 모델·데이터 없음. 페이로드에 `null`/`[]` 자리 | 7·8 |
| `submit-decision` 의 리스크 게이트·주문 | Phase 3 는 `30_decision` 저장 + `40_risk_check_<계좌>` 에 `{"approved": null, "reason": "phase4"}` 자리만 | 4 |
| 트레이더 입력 `portfolio` | 계좌 집계는 executor 와 함께. `null` | 4 |
| `--resume` 재평가, screener, reflector | monitor·scan·reflect 명령과 함께 | 6·8 |
| 오케스트레이터 세션 병렬(시장 2개 동시) | 데몬이 띄울 때 | 6 |

## Review Focus

1. 오케스트레이터가 애널리스트를 건너뛰고 `submit-decision` 을 부르면 도구가 거부해야 한다 → Task 3 `test_submit_requires_reports_and_summary`.
2. 서브에이전트 JSON 이 스키마에 안 맞으면(`score` 범위 초과 등) `checkpoint write` 가 거부하고 체크포인트가 남지 않아야 한다 → Task 3 `test_checkpoint_write_rejects_invalid`.
3. 토론 뒤 양측이 같은 방향으로 붕괴하면 `25_research_summary` 에 `sycophantic: true, preferred: round0` 이 기록돼야 한다 → Task 4 `test_sycophantic_convergence`.
4. `claude -p` 가 0 으로 끝나도 `is_error: true` 이거나 결과가 JSON 이 아니면 세션 실패로 기록되고 체크포인트는 그대로 남아 재실행 시 이어가야 한다 → Task 5 `test_session_error_recorded_and_resumable`.
5. `analyze` 를 두 번 돌리면 두 번째는 남은 단계만 호출해야 한다(FAKE 모드에서 도구 호출 수로 확인) → Task 6 `test_rerun_only_missing_stages`.

---

## 파일 구조

```
src/trader/agents/__init__.py  schemas.py  registry.py  fake.py  anonymize.py
src/trader/orchestrator/tools.py  session.py  summary.py
src/trader/cli.py                       tool (collect|checkpoint|submit-decision|status), analyze
.claude/agents/orchestrator.md  analyst-technical.md  analyst-fundamental.md  analyst-news-macro.md  analyst-sentiment.md  researcher-bull.md  researcher-bear.md  trader.md
tests/agents/test_schemas.py  test_registry.py  test_fake.py  test_anonymize.py
tests/orchestrator/test_tools.py  test_summary.py  test_session.py
tests/test_cli_tool.py  tests/test_cli_analyze.py
```

---

### Task 0: fundamental 입력 확장 (Phase 2 리뷰 이연 — 스펙 §3.1)

**Files:** Modify `src/trader/sources/dart.py`, `src/trader/sources/inputs.py`; Test `tests/sources/test_dart.py`, `tests/sources/test_inputs.py`

**Interfaces:** `DartClient.financials(..., fs_div="CFS")` 가 빈 결과면 `OFS` 로 1회 재조회하고 결과에 `"_fs_div"` 표시. `DartClient.financial_history(corp_code, asof) -> dict{years: {Y-2, Y-1, Y: {...}}, latest_quarter: {reprt_code, year, ...}|None}`. `00_inputs.fundamental` 에 `history` Section 추가, `inputs.SCHEMA_VERSION = 2`.

- [ ] Step 1: `test_ofs_fallback_when_cfs_empty`, `test_financial_history_three_years_and_quarter`, `test_inputs_fundamental_history_section`. Step 2~4. Step 5: Commit `feat(sources): DART 3개년·최근 분기 재무와 OFS 폴백 (SCHEMA_VERSION 2)`

### Task 1: 출력 스키마 (`agents/schemas.py`)

**Interfaces:** `Signal, Horizon, AnalystRole, Side, Stance`, `Evidence{claim, source_id}`, `AnalystReport`, `Timing`, `DebateTurn`, `Decision`, `CallMeta{agent, model, session_id, cost_usd, duration_ms}`, `CHECKPOINT_SCHEMAS: dict[str, type[BaseModel]]`(`10_analyst_<role>` → AnalystReport, `20_debate_r<n>_<side>` → DebateTurn, `30_decision` → Decision; 정규식 매핑 `schema_for(name) -> type|None`), `json_schema(model) -> str`(압축). 스펙 §4.2~4.4 의 범위·길이 제약 그대로.

- [ ] Step 1: `test_bounds_and_forbid`, `test_schema_for_names`(`10_analyst_technical`, `20_debate_r1_bear`, `30_decision`, `25_research_summary`→None). Step 2~4. Step 5: Commit `feat(agents): 에이전트 출력 스키마`

### Task 2: 에이전트 md 8개와 레지스트리 (`agents/registry.py`)

**Interfaces:** `.claude/agents/<name>.md` 프런트매터(`name, description, tools: [], model: haiku|sonnet|opus`) + 본문(`# 역할, # 입력, # 분석 절차, # 출력 규칙(JSON 스키마 전문 포함), # 공통 규칙`). `registry.load_agent(name, base=None) -> Agent{name, description, model, prompt}`(프런트매터 파싱, 없으면 `ConfigError`), `registry.agents_json(names, settings) -> dict`(`--agents` 형식 `{name: {description, prompt, tools: [], model}}`, 모델은 `settings.models` 가 우선), `registry.orchestrator_prompt(settings) -> str`(orchestrator.md 본문 + 절차: `tool status` → 종목마다 `tool collect` → `tool checkpoint list` → analyst×4 병렬(Agent 도구) → `checkpoint write 10_analyst_<role>` → bull/bear 라운드 0 병렬 → 라운드 1 순차(상대 r0 전달) → `checkpoint write 20_debate_*` → `tool checkpoint summarize`(파이썬 병합) → trader → `tool submit-decision`). 모든 md 에 공통 규칙 6문장. trader.md 에 "`research_summary.preferred == "round0"` 이면 라운드 0 우선", "기본 HOLD", "왕복비용 KR 30bp/US 50bp 초과 기대수익 없으면 거래 금지", "US 는 LIMIT".

- [ ] Step 1: `test_all_agents_load_with_common_rules`, `test_agents_json_uses_settings_models`(researcher-bear → opus), `test_missing_agent_config_error`. Step 2~4. Step 5: Commit `feat(agents): 에이전트 정의 md 8개와 레지스트리`

### Task 3: `tool` CLI 4개 (`orchestrator/tools.py`)

**Interfaces:** 함수형 API + typer 서브커맨드 `trader tool …`(stdout JSON 만).
- `tool_status(conn, date, market) -> dict{date, market, tickers: {ticker: [체크포인트...]}, sessions_today, cap}`
- `tool_collect(ticker, market, exchange, asof, …) -> dict{ok, path, errors}` (`collect_and_save` 재사용)
- `tool_checkpoint_list(ticker, market, date) -> dict{ok, stages: [...]}`, `tool_checkpoint_read(…, stage) -> dict{ok, data}`, `tool_checkpoint_write(…, stage, payload: dict, call: dict|None) -> dict{ok, path}|{ok: false, error}`(`schema_for(stage)` 로 검증, `settings.anonymize` 면 `deanonymize` 후 저장), `tool_checkpoint_summarize(…)`(Task 4 의 `research_summary` 호출 → `25_research_summary` 저장)
- `tool_submit_decision(…, payload) -> dict{ok, path, risk: "phase4"}` — 전제조건: `10_analyst_*` ≥ 2 **and** `25_research_summary` 존재, 아니면 `{ok: false, error: "전제조건 미충족: …"}` exit 1. 통과 시 `Decision` 검증 → `30_decision` 저장 + 계좌마다 `40_risk_check_<id>` 에 `{"approved": null, "reason": "phase4"}`.
- 페이로드 입력은 `--json <문자열>` 또는 stdin.

- [ ] Step 1: `test_submit_requires_reports_and_summary`, `test_checkpoint_write_rejects_invalid`, `test_checkpoint_write_ok_and_list`, `test_tool_outputs_json_only`(CliRunner, stdout 이 `json.loads` 가능), `test_tool_error_is_json_exit1`. Step 2~4. Step 5: Commit `feat(orchestrator): LLM 용 tool CLI (collect·checkpoint·submit-decision·status)`

### Task 4: 수렴 감시·요약 병합 (`orchestrator/summary.py`) + 익명화 (`agents/anonymize.py`)

**Interfaces:** `signed_conviction(turn) -> float`(`conviction × {BULLISH:+1, NEUTRAL:0, BEARISH:-1}`), `conviction_gap(bull, bear)`, `research_summary(round0: dict[side, DebateTurn], final: dict[side, DebateTurn], rounds: int) -> dict{rounds, round0, final, gap_round0, gap_final, timing_agree, sycophantic(rounds>0 and gap_final<=0.2 and gap_round0>0.2), preferred, issues}`. `anonymize.Alias{ticker, name, ticker_alias="TICKER_X", name_alias="종목X"}`, `anonymize(obj, alias)`, `deanonymize(obj, alias)`(문자열 값만, `\b` 경계).

- [ ] Step 1: `test_sycophantic_convergence`, `test_independent_agreement_not_sycophantic`(r0 gap 이미 ≤0.2), `test_rounds_zero`, `test_anonymize_roundtrip`, `test_ticker_boundary`. Step 2~4. Step 5: Commit `feat(orchestrator): 수렴 감시·연구 요약 병합과 익명화`

### Task 5: 오케스트레이터 세션 실행 (`orchestrator/session.py`) + FAKE (`agents/fake.py`)

**Interfaces:** `SessionSpec{market, date, tickers, dry_run}`; `build_command(spec, settings, claude_bin, agents_file, prompt) -> list[str]`(Global Constraints 플래그 순서, 테스트로 고정); `run_session(spec, settings, conn, *, runner=subprocess.run, claude_bin=None, now=…) -> SessionResult{session_id, ok, cost_usd, duration_ms, num_turns, error}` — `agents_json` 을 `state/claude_cwd/agents.json` 에 쓰고 `--agents` 로 전달, stdin 에 `{"market","date","tickers","dry_run"}`, 결과 JSON 의 `is_error/result/total_cost_usd/duration_ms/session_id` 해석, `agent_calls` 에 1행(`agent="orchestrator"`), `runs` 시작·종료. 일일 세션 상한 검사. `FAKE_CLAUDE=1` 이면 `fake.run_fake_session(spec, settings)` 가 **같은 도구 함수들을 파이썬으로 직접 호출**해(collect → 10_analyst×4 → 20_debate_r0/r1 → summarize → submit) 고정 JSON 을 쓴다(기존 체크포인트는 건너뜀).

- [ ] Step 1: `test_build_command_flags`(정확한 플래그 목록·순서, `--bare` 없음), `test_session_error_recorded_and_resumable`(runner 가 `is_error: true` → `SessionResult.ok False`, `agent_calls.status="error"`, 기존 체크포인트 보존), `test_daily_cap_blocks`, `test_fake_session_writes_all_stages`, `test_fake_skips_existing`. Step 2~4. Step 5: Commit `feat(orchestrator): claude -p 오케스트레이터 세션 실행과 FAKE 모드`

### Task 6: CLI `analyze` + STATUS 갱신

**Interfaces:** `trader analyze --ticker 005930 [--market KR|US] [--exchange NAS] [--asof ISO] [--dry-run] [--skip-collect]` — `00_inputs` 없으면 수집, `SessionSpec` 으로 `run_session`, 결과 요약 출력(`결정: <action> target_weight=… confidence=…`, `세션 비용 $…`, `저장: 30_decision 경로`). Phase 3 는 `--dry-run` 유무와 무관하게 결정에서 멈춘다(플래그 없으면 안내 한 줄). `status.build_status` 에 오늘 세션 수·비용(이미 있음)과 마지막 결정 3건.

- [ ] Step 1: `test_cli_analyze_fake_creates_decision`(FAKE_CLAUDE=1, `_build_input_deps` 교체), `test_rerun_only_missing_stages`, `test_cli_analyze_session_failure_exit1`. Step 2~4. Step 5: Commit `feat(cli): trader analyze`

### Task 7: 실기·문서·PR

- [ ] Step 1: ruff 0건, pytest 전부.
- [ ] Step 2: `FAKE_CLAUDE=1 uv run trader analyze --ticker 005930 --dry-run` → 체크포인트 10개, `20_debate_r1_bear.json` 삭제 후 재실행 → 그 단계만.
- [ ] Step 3: 실제 `claude -p` 1회(구독): 서브에이전트 호출·도구 제한(허용 외 Bash 거부)·체크포인트·`agent_calls` 비용 확인. 출력에 CLAUDE.md·훅 문구 유입 없는지. 문제 시 md 만 수정 후 재실행.
- [ ] Step 4: TODO Phase 3 체크(Windows 항목 사용자 확인 대기), CLAUDE.md(도구 계약·세션 플래그·FAKE·체크포인트 구조), `uv run trader status`, 커밋, PR → `feat/phase2-sources`.

## Self-Review 메모
- §1.1 도구 4개·전제조건 → Task 3. §4.1 세션·플래그·비용 → Task 5. §4.2~4.4 스키마·md → Task 1·2. §4.3 수렴 → Task 4. §4.5 재개 → Task 3·5·6. §3.3 익명화 → Task 4.
- `--setting-sources ""` 가 `.claude/agents` 로딩을 막는다는 가정은 Task 7 실기에서 확인; 안 막히면 `--agents` 와 중복될 뿐 무해.
