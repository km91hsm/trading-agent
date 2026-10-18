# 전체 코드 검토 구조 정리 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 2026-10-18 검토의 구조 잔여(중복 헬퍼 통합·죽은 코드 제거·`cli.py` 보일러플레이트)를 동작 변화 없이 정리한다. PR #18 위에 쌓는다.

**Architecture:** 공용 헬퍼는 이미 그 계층의 "정본" 모듈에 둔다 — 시간대는 `config`, 로깅은 `logging_setup`, KIS 응답 파싱은 `broker/_parse`, 수집 문자열 처리는 `sources/base`, 통계는 `ml/features`, 날짜 창은 `ledger/repo`, 체크포인트 봉투는 `run_store`. 기존 테스트가 안전망이고 새 헬퍼마다 작은 테스트를 둔다.

**Tech Stack:** Python 3.12, pytest, ruff.

**Spec:** CLAUDE.md "코딩 규칙"(OS 분기는 platform, 경로는 pathlib, 로거 이름) 과 "여러 파일에 걸친 규칙" 절.

## Global Constraints
- 동작·출력(JSON 키, 메시지) 불변. 공개 함수 시그니처 변경은 호출자·테스트를 같은 커밋에서 고친다.
- `uv run pytest`·`uv run ruff check src tests` 0건.

## Review Focus
- `rows()` 통합 뒤 KIS 잔고 응답의 빈 `output2` 가 예전처럼 빈 dict 로 처리되는가 (S1).
- `zscore(fill=0.0)` 와 `zscore()` 가 각자의 기존 동작(NaN 유지 vs 0)과 같은가 (S1).
- `@_config_errors` 가 typer 시그니처(옵션 파싱)를 유지하는가 (S3).
- `max_parallel` 제거 뒤 `settings.yaml` 로드가 되는가 (S2).

---

### Task S1: 공용 헬퍼 통합
- [x] `config.KST` 추가(`MARKET_TZ["KR"]` 와 같은 객체). 24개 모듈의 `_KST = ZoneInfo("Asia/Seoul")`/`KST = …` 를 `from trader.config import KST` 로.
- [x] `logging_setup.quiet_httpx()` 공개, 5벌 `_quiet_httpx` 제거.
- [x] `broker/_parse.py`: `dec(v, default="0")`, `to_int(v)`, `rows(v)`(빈 dict 제외). `kis_market`·`accounts` 가 import.
- [x] `sources/base.py`: `utc_now()`, `strip_tags(text)` (HTML 두 번 unescape). `news`·`community`·`youtube`·`telegram_channel` 이 import.
- [x] `ml/features.zscore(s, *, fill=None)`; `ranker`·`scan/quant` 가 쓴다.
- [x] `repo.day_bounds` 를 `performance._orders_between`·`status._today_calls`·`tools.sessions_today` 에서 쓴다.
- [x] `run_store.market_dir` 로 `screener.market_dir`·`tools.status`·`web/routers/runs.today` 통일(`screener.market_dir` 삭제).
- [x] `run_store.checkpoint_payload(data, call=None)` 로 `tools._wrap`·`executor._wrap` 통일.
- [x] `tools` 의 `ROUND_TRIP_COST_BP` → `costs.round_trip_bp(market)`. `cli._approval_line` → `commands.approval_line`. `performance.metrics` 별칭 제거(호출자는 `metrics_of`). `calibration.build_calibration` 의 COUNT → `store.matured_count`.
- [x] 커밋 `refactor: 공용 헬퍼 통합 (KST·quiet_httpx·KIS 파싱·태그 제거·zscore·날짜 창·market_dir·체크포인트 봉투)`

### Task S2: 죽은 코드·시그니처 정리
- [x] 삭제: `repo.has_unhandled_trigger`, `tools.ANALYST_ROLES`, `ranker.rank_fn_or_none`(테스트는 `ranker.current()` 로), `tr_ids.names()`·`DOM/OVS_PSBL_ORDER`, `LlmSettings.max_parallel`(+`settings.yaml`·테스트), `run_session(claude_bin_path=)`(테스트는 `claude_bin=`).
- [x] `cli._session_brokers(accounts)` 로 축소, `cli.scan` 의 `get_watchlist` 1회, `tools.checkpoint_write(model=)` → `schema_model` 아닌 의미 명확한 이름 `model_name`.
- [x] 커밋 `refactor: 죽은 코드·중복 인자 제거`

### Task S3: `cli.py` 설정 오류 처리 데코레이터
- [x] `_config_errors(fn)`: `ConfigError`·`ProjectRootNotFound` 를 받아 `오류: …` 출력 + `Exit(1)`. `functools.wraps` 로 typer 시그니처 유지. 단일 except 만 있는 try 블록 18곳 중 패턴이 정확히 맞는 곳을 데코레이터로 바꾼다.
- [x] 테스트: 설정이 없는 프로젝트에서 `trader status`·`kis ping` 이 exit 1 + "오류:" (기존 테스트 유지 확인).
- [x] 커밋 `refactor(cli): 설정 오류 처리를 데코레이터로`

### Task S4: 마무리
- [x] `uv run pytest`, `ruff`, TODO.md 체크, CLAUDE.md 갱신, PR (`perf/review-performance` 대상).
