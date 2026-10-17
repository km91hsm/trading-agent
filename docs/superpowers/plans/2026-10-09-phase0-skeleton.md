# Phase 0 골격 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `python -m trader status` 가 설정·원장·TODO 를 읽어 `state/STATUS.md` 를 만들고, 새 claude 세션이 그것을 자동 인식하는 뼈대를 세운다.

**Architecture:** `src/trader/` 패키지에 경로(`platform/`)·설정(`config.py`)·원장(`ledger/`)·런타임 제어(`control/`)·로깅·STATUS 생성기·typer CLI 를 둔다. 모든 경로는 `platform/paths` 를 거치고, 설정은 pydantic `extra="forbid"` 모델로 검증하며, 원장은 스키마 버전 + 순서 마이그레이션을 처음부터 가진다. 훅·설정 파일은 요청 3 에서 이미 있으므로 테스트로 고정한다.

**Tech Stack:** Python 3.12, uv, typer, pydantic v2, pyyaml, python-dotenv, tzdata, sqlite3(표준), pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-10-09-trading-agent-design.md` — §1.3 디렉터리, §6.2 계좌 모드·control, §6.3 원장, §7 세션 복구, §10.2 크로스플랫폼, §11 Phase 0, §13 검증.

## Global Constraints

- 경로는 `pathlib`, 루트는 `platform/paths.project_root()`(`TRADER_HOME` → 없으면 cwd 에서 위로 `pyproject.toml`). 파일명에 `:` 금지.
- 저장 시각 UTC ISO8601, 표시 KST. `zoneinfo` + `tzdata`.
- pydantic 모델 전부 `extra="forbid"`. 검증·파일 오류는 `ConfigError`.
- SQLite WAL, timeout 30s, `row_factory=sqlite3.Row`. `schema_version` 테이블 + 순서 마이그레이션 리스트(`MIGRATIONS: list[tuple[int, str]]`), `init_schema` 멱등.
- subprocess `shell=False, encoding="utf-8", errors="replace"`. 셸 스크립트 없음.
- 사용자 대면 문자열·로그·docstring 한국어. 로거 `trader.<계층>`. `httpx`·`httpcore` 는 WARNING.
- 테스트는 `tmp_path` 만, 네트워크 없음. `ruff check src tests` 0건.
- 커밋 트레일러 `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Windows 동일 동작 확인은 사용자 항목(TODO 에 "사용자 확인 대기").

## 범위 조정 (스펙 중 Phase 0 에서 제외)

| 항목 | 이유 | 재개 Phase |
|---|---|---|
| `platform/installer.py` | 데몬과 함께 | 6 |
| `runs`·`agent_calls` 외 원장 테이블 | 쓰는 코드가 생길 때 마이그레이션으로 추가 | 3·4 |
| `config/ml.yaml`·`schedule.yaml` 로더 | 파일만 두고 로더는 쓰는 Phase 에서 | 6·7 |
| STATUS.md 의 NAV·포지션·미승인 주문·ML 절 | 원장 데이터가 생긴 뒤 | 4·7 |

## Review Focus

1. `TRADER_HOME` 이 없고 cwd 가 프로젝트 밖이면 `ProjectRootNotFound` 로 명확히 실패해야 한다 → Task 2 `test_root_not_found`.
2. `accounts.yaml` 에 실전 계좌가 `mode: auto` 로 적혀 있으면 yaml 로드 단계에서 거부해야 한다(스펙 §6.2) → Task 3 `test_real_account_auto_in_yaml_rejected`.
3. 이미 버전 1 로 초기화된 DB 에 `init_schema` 를 다시 돌려도 테이블·버전이 그대로여야 하고, 버전이 더 높은 마이그레이션만 적용돼야 한다 → Task 4 `test_migration_applies_only_newer`.
4. `.env` 와 환경변수가 둘 다 있으면 비어 있지 않은 환경변수가 이긴다 → Task 3 `test_env_overrides_file`.
5. 한글 경로·cp949 콘솔에서도 `status` 출력이 깨지지 않아야 한다 → Task 6 `test_status_cli_utf8_korean_path`(`tmp_path/"한글"` 아래에서 실행).

---

## 파일 구조

```
pyproject.toml                       dependencies, [project.scripts] trader, pytest·ruff 설정
src/trader/__init__.py  __main__.py  cli.py
src/trader/platform/__init__.py  paths.py  claude_bin.py
src/trader/config.py
src/trader/ledger/__init__.py  db.py
src/trader/control/__init__.py  control.py
src/trader/logging_setup.py
src/trader/status.py
config/settings.yaml  accounts.yaml(복원, 값 비출력)  sources.yaml  universe.yaml  schedule.yaml  ml.yaml
secrets/.env.example
tests/conftest.py  tests/platform/  tests/test_config.py  tests/ledger/  tests/control/
tests/test_logging_setup.py  tests/test_status.py  tests/test_cli_status.py  tests/test_session_hook.py
```

---

### Task 1: 패키지 뼈대와 CLI 진입점

**Files:** Modify `pyproject.toml`; Create `src/trader/__init__.py`, `src/trader/__main__.py`, `src/trader/cli.py`, `tests/conftest.py`, `tests/test_cli_smoke.py`

**Interfaces:** `trader.cli:app`(typer, `no_args_is_help=True`), `python -m trader` 가 `app()` 호출. `tests/conftest.py` 의 `project` 픽스처: `tmp_path` 에 `config/ state/ docs/ secrets/` + `pyproject.toml` 을 만들고 `TRADER_HOME` 과 cwd 를 그곳으로.

- [ ] Step 1: `tests/test_cli_smoke.py::test_help_lists_status` — `CliRunner().invoke(app, ["--help"])` exit 0, 출력에 `status`.
- [ ] Step 2: 실패 확인 (ImportError).
- [ ] Step 3: `uv add typer pydantic pyyaml python-dotenv tzdata`, `uv add --dev pytest ruff`. pyproject 에 `[project.scripts] trader = "trader.cli:app"`, `[tool.pytest.ini_options] testpaths=["tests"]`, `[tool.ruff] line-length=100 target-version="py312"`, hatchling 빌드. `cli.py` 에 `status` 명령 자리(Task 6 에서 구현, 지금은 "아직 구현 전" 출력).
- [ ] Step 4: `uv run pytest -q` PASS, `uv run ruff check src tests` 0건.
- [ ] Step 5: Commit `feat: 패키지 뼈대와 trader CLI 진입점`

### Task 2: `platform/paths.py`, `platform/claude_bin.py`

**Files:** Create `src/trader/platform/{__init__,paths,claude_bin}.py`, `tests/platform/test_paths.py`, `tests/platform/test_claude_bin.py`

**Interfaces:** `paths.project_root() -> Path`(`ProjectRootNotFound`), `config_dir()`, `state_dir()`(생성), `secrets_path()`(= `secrets/.env`), `docs_dir()`. `claude_bin.find_claude(override: str | None = None) -> Path`(`ClaudeNotFound`; Windows 는 `claude.cmd/.exe` 우선).

- [ ] Step 1: 테스트 — `test_trader_home_wins`, `test_search_up_for_pyproject`, `test_root_not_found`(cwd 를 빈 tmp 로, env 제거), `test_find_claude_override_missing_raises`, `test_find_claude_on_path`(`monkeypatch.setattr(shutil, "which", ...)`).
- [ ] Step 2: 실패 확인. Step 3: 구현. Step 4: PASS. Step 5: Commit `feat(platform): 프로젝트 경로와 claude 실행파일 탐색`

### Task 3: 설정 로더 5개와 설정 파일

**Files:** Create `src/trader/config.py`, `config/settings.yaml`, `config/sources.yaml`, `config/universe.yaml`, `config/schedule.yaml`, `config/ml.yaml`, `secrets/.env.example`, `tests/test_config.py`; 복원 `config/accounts.yaml`(스크래치패드 보관본을 `cp`, 키 이름만 검사하고 값은 출력하지 않음)

**Interfaces:**
- `ConfigError(Exception)`.
- `Settings{markets: list[Literal["KR","US"]], risk: RiskLimits{max_position_pct=0.25, max_positions=5, daily_loss_stop_pct=-0.05, stop_loss_pct=-0.15, min_cash_pct=0.0}, models: dict[str,str], debate_rounds: int(0~2)=1, anonymize: bool=False, claude_bin: str|None, llm: LlmSettings{budget_usd=2.0, timeout_s=600, max_parallel=4, daily_session_cap=12}, web: WebSettings{bind="127.0.0.1", port=8787}}`.
- `AccountConfig{id, env: Literal["paper","real"], cano, acnt_prdt_cd="01", markets, mode: Literal["auto","approve","off"], initial_capital: Decimal|None}`; `load_accounts()` 는 `id` 중복과 실전 `auto` 를 거부.
- `SourcesConfig{news{enabled, google_rss, naver, max_items, macro_items}, macro{enabled, fred, ecos}, dart{enabled}, edgar{enabled, user_agent}, community{enabled, reddit, clien, dcinside, fmkorea, toss, naver_board, max_posts}, channels{youtube: list[str], telegram: list[str]}, disabled_sites: list[str]}` 기본값은 스펙 §0.3 정책(reddit·clien True, dcinside·fmkorea·toss·naver_board False). `edgar.user_agent` 필수.
- `Universe{kr: list[str], us: list[UsSymbol{symbol, exchange: NAS|NYS|AMS}], exclude: list[str], watch: list[str]}`.
- `Secrets`(필드명 대문자 = 환경변수명, 전부 `str|None`): `KIS_PAPER_APP_KEY/SECRET, KIS_REAL_APP_KEY/SECRET, DART_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, NAVER_CLIENT_ID/SECRET, FRED_API_KEY, ECOS_API_KEY, WEB_TOKEN`. `.env` 를 읽고 비어 있지 않은 환경변수가 덮어쓴다.
- 로더: `load_settings/load_accounts/load_sources/load_universe/load_secrets(path=None)`. yaml 없으면 기본값(단 `sources.yaml` 은 `edgar.user_agent` 필수라 없으면 `ConfigError`).

- [ ] Step 1: 테스트 — `test_defaults_load`, `test_unknown_key_is_config_error`, `test_accounts_unique_ids`, `test_real_account_auto_in_yaml_rejected`, `test_debate_rounds_range`, `test_sources_defaults_follow_policy`(reddit True, dcinside False), `test_sources_requires_user_agent`, `test_universe_load`, `test_secrets_from_env_when_file_missing`, `test_env_overrides_file`, `test_bad_yaml_is_config_error`.
- [ ] Step 2: 실패 확인. Step 3: 구현 + 설정 파일(universe 는 KR 20·US 10 수동 목록, settings 모델 배정 `{orchestrator: sonnet, screener: haiku, analyst: sonnet, researcher_bull: sonnet, researcher_bear: opus, trader: opus, reflector: sonnet}`). `accounts.yaml` 복원 후 `python -c` 로 키 이름만 검사.
- [ ] Step 4: PASS. Step 5: Commit `feat(config): 설정·계좌·소스·유니버스·시크릿 로더`

### Task 4: 원장(`ledger/db.py`)과 런타임 제어(`control/control.py`)

**Files:** Create `src/trader/ledger/{__init__,db}.py`, `src/trader/control/{__init__,control}.py`, `tests/ledger/test_db.py`, `tests/control/test_control.py`; `tests/conftest.py` 에 `conn` 픽스처·`make_account()` 추가

**Interfaces:**
- `db.now_iso() -> str`, `db.connect(path=None) -> sqlite3.Connection`(WAL, timeout 30, Row), `db.SCHEMA_VERSION`, `db.MIGRATIONS: list[tuple[int, str]]`(v1: `accounts, control, runs, agent_calls, schema_version`), `db.init_schema(conn)`(현재 버전보다 큰 마이그레이션만 순서대로 적용, 멱등), `db.sync_accounts(conn, accounts)`, `db.start_run(conn, kind, market) -> int`, `db.finish_run(conn, run_id, status, error=None)`.
- `control.get_flag/set_flag(conn, key, value)`(JSON), `is_paused`, `get_account_mode(conn, account) -> Mode`(control 값 우선), `set_account_mode(conn, account, mode, *, confirm=None)`(실전 `auto` 는 `confirm="LIVE"` 없으면 `ConfirmationRequired`).

- [ ] Step 1: 테스트 — `test_init_schema_idempotent`, `test_migration_applies_only_newer`(v1 DB 에 테스트용 v2 마이그레이션 monkeypatch → 적용 1회, 버전 2), `test_sync_accounts_upsert`, `test_run_lifecycle`, `test_flag_roundtrip`, `test_mode_default_from_yaml_then_control`, `test_real_auto_requires_confirm`.
- [ ] Step 2~4. Step 5: Commit `feat(ledger,control): SQLite 원장 스키마·마이그레이션과 런타임 제어`

### Task 5: 로깅

**Files:** Create `src/trader/logging_setup.py`, `tests/test_logging_setup.py`; `tests/conftest.py` 에 autouse `_reset_logging`

**Interfaces:** `configure(state_dir: Path, level="INFO")`(파일 `state/logs/trader.log` 자정 회전 14일, 콘솔, 여러 번 호출 안전, `httpx/httpcore` WARNING), `reset()`.

- [ ] Step 1: `test_configure_creates_log_and_quiets_httpx`, `test_configure_twice_no_duplicate_handlers`. Step 2~4. Step 5: Commit `feat: UTF-8 파일·콘솔 로깅`

### Task 6: STATUS.md 생성기와 `trader status`

**Files:** Create `src/trader/status.py`, `tests/test_status.py`, `tests/test_cli_status.py`; Modify `src/trader/cli.py`

**Interfaces:** `status.todo_progress(todo_path) -> dict[str, tuple[int,int]]`(`## Phase ...` 절마다 완료/전체), `status.build_status(conn, settings, accounts, todo_path, now=None) -> str`(≤8,000자: 생성 시각 KST, 실행 제어(일시정지·토론 라운드), 계좌 표(id·env·모드·시장), 마지막 실행 5건(`runs`), 오늘 LLM 세션 수(`agent_calls`), Phase 진행도, 다음 할 일 5개), `status.write_status(text, path=None) -> Path`. CLI `status`: `configure` → `load_settings/load_accounts` → `connect/init_schema/sync_accounts` → `build_status` → `write_status` → 경로 출력. 설정 오류는 메시지 + exit 1.

- [ ] Step 1: `test_todo_progress_counts`, `test_build_status_sections`, `test_status_cli_writes_file`, `test_status_cli_utf8_korean_path`, `test_status_cli_config_error_exit1`.
- [ ] Step 2~4. Step 5: Commit `feat: STATUS.md 생성기와 trader status 명령`

### Task 7: 훅·설정 고정 테스트와 Phase 0 마무리

**Files:** Create `tests/test_session_hook.py`; Modify `docs/TODO.md`, `CLAUDE.md`(어긋난 곳만), `state/STATUS.md`(명령으로만)

- [ ] Step 1: `test_hook_prints_status_and_todos`(subprocess 로 훅 실행, `CLAUDE_PROJECT_DIR=tmp`, 출력에 STATUS 내용과 `- [ ]`), `test_hook_truncates_at_10000`, `test_settings_json_has_hook_and_denies_secrets`.
- [ ] Step 2: 구현(훅은 이미 있음; 테스트가 요구하는 동작과 어긋나면 훅 수정).
- [ ] Step 3: `uv run ruff check src tests` 0건, `uv run pytest -q` 전체 PASS, `python -m trader status` 실행 → `state/STATUS.md` 생성, 새 세션 인식은 훅 테스트로 대체.
- [ ] Step 4: TODO Phase 0 체크(Windows 항목은 "사용자 확인 대기" 유지), CLAUDE.md 대조, STATUS 재생성, 커밋 `docs: Phase 0 골격 완료`, PR → `main`.

## Self-Review 메모
- 스펙 §11 Phase 0 항목(uv·CLI·platform·config 5·원장·로깅·STATUS·훅·TODO) 전부 Task 1~7 에 대응. `installer`·추가 테이블·ml/schedule 로더는 범위 조정 표.
- 타입: `AccountConfig`·`Mode`·`ConfigError` 는 Task 3 정의를 Task 4·6 이 사용. `conn` 픽스처는 Task 4 에서 추가.
