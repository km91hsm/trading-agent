# Phase 10 번들 배포 (macOS·Windows 실행파일) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Python·uv 없이 압축을 풀고 더블클릭하면 데몬 + 웹 UI 가 뜨는 `trader`(macOS)·`trader.exe`(Windows) 폴더 번들을 PyInstaller 로 만들고, GitHub Actions 가 두 OS 에서 테스트·빌드·릴리스한다.

**Architecture:** PyInstaller one-folder(`_internal/` 에 런타임·라이브러리·에이전트 md·웹 dist·설정 템플릿)로 만들고, 실행파일 폴더가 `TRADER_HOME`(`config/ secrets/ state/ docs/` 하위 폴더)이다. frozen 여부·리소스 경로·도구 명령은 `platform/bundle.py` 한 곳이 답하고 세션·인스톨러·웹·레지스트리가 그것을 쓴다. 코드 서명은 하지 않는다(개인 사용, 첫 실행 경고 1회).

**Tech Stack:** Python 3.12, PyInstaller ≥ 6 (dev 그룹 `build`), GitHub Actions(macos-latest·windows-latest), Vite(web/dist).

**Spec:** `docs/superpowers/specs/2026-10-09-trading-agent-design.md` §10.2(크로스플랫폼), §1(두 층·도구 4개), §4.1(세션 플래그). 이 플랜의 배포 폴더 구조는 사용자 결정(2026-10-20): 실행파일 옆에는 `_internal/ config/ secrets/ state/ docs/` 만 둔다.

## Global Constraints
- `claude` CLI 는 번들하지 않는다(구독 인증). `find_claude` 가 PATH·`settings.claude_bin` 에서 찾는다.
- 셸 스크립트 금지: 빌드·초기화는 파이썬(`packaging/build.py`)과 워크플로 yaml 만.
- 경로는 `pathlib`, 루트는 `platform/paths` 로만. frozen 분기는 `platform/bundle.py` 에만 둔다.
- 세션 도구 명령은 frozen 이면 `trader tool …`, 아니면 `python -m trader tool …`. `--allowedTools` 와 오케스트레이터 프롬프트가 같은 문자열을 써야 한다(도구 전제조건이 순서를 강제하므로 LLM 이 다른 명령을 쓸 수 없다).
- `secrets/`·`state/` 는 번들에 넣지 않는다. 템플릿은 `config/*.yaml` 사본이되 계좌번호는 비운다.
- `uv run pytest`·`uv run ruff check src tests` 0건. 기존 개발 방식(`uv run trader …`)은 그대로 동작한다.

## Review Focus
- 번들 실행파일을 다른 폴더로 옮겼을 때 `TRADER_HOME` 없이도 그 폴더를 루트로 쓰는가 (Task 1 테스트).
- frozen 세션에서 LLM 이 받는 프롬프트에 `python -m trader tool` 이 남아 있지 않은가 (Task 2 테스트).
- Windows 더블클릭(인자 없음)이 `trader app` 으로 가고, 터미널에서 `trader.exe status` 는 그대로 CLI 로 가는가 (Task 4 테스트).
- 첫 실행이 기존 `config/`·`secrets/.env` 를 덮어쓰지 않는가 (Task 4 테스트).
- 빌드 산출물에 `secrets/`·`state/`·실제 계좌번호가 들어가지 않는가 (Task 5 스모크).

---

### Task 1: `platform/bundle.py` + 루트·리소스 경로

**Files:**
- Create: `src/trader/platform/bundle.py`, `tests/platform/test_bundle.py`
- Modify: `src/trader/platform/paths.py`, `src/trader/agents/registry.py:agents_dir`, `src/trader/cli.py:_web_deps(dist=…)`

**Interfaces (Produces):**
- `bundle.is_frozen() -> bool` — `getattr(sys, "frozen", False)`.
- `bundle.bundle_root() -> Path` — frozen: `Path(sys.executable).resolve().parent`; 아니면 `paths.project_root()`.
- `bundle.resource_dir() -> Path` — frozen: `Path(sys._MEIPASS)`(= `_internal/`); 아니면 저장소 루트.
- `bundle.tool_command() -> str` — frozen: `"trader tool"`, 아니면 `"python -m trader tool"`.
- `bundle.tool_bin_dir() -> Path` — frozen: 실행파일 디렉터리; 아니면 `Path(sys.executable).parent`(venv bin).
- `paths.project_root()`: `TRADER_HOME` → (frozen 이면) `bundle.bundle_root()` → pyproject 탐색.
- `paths.web_dist() -> Path`: `bundle.resource_dir() / "web" / "dist"`.
- `paths.agents_dir() -> Path`: frozen `resource_dir()/"agents"`, 아니면 `project_root()/".claude"/"agents"`.

- [x] **Step 1: 실패 테스트** `tests/platform/test_bundle.py`
```python
def test_not_frozen_defaults(project):
    assert bundle.is_frozen() is False
    assert bundle.tool_command() == "python -m trader tool"
    assert paths.web_dist() == project / "web" / "dist"
    assert paths.agents_dir() == project / ".claude" / "agents"

def test_frozen_root_is_executable_dir(tmp_path, monkeypatch):
    exe_dir = tmp_path / "dist" / "trader"; (exe_dir / "_internal").mkdir(parents=True)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "trader"))
    monkeypatch.setattr(sys, "_MEIPASS", str(exe_dir / "_internal"), raising=False)
    monkeypatch.delenv("TRADER_HOME", raising=False)
    assert paths.project_root() == exe_dir            # pyproject 없어도 실행파일 폴더
    assert bundle.tool_command() == "trader tool" and bundle.tool_bin_dir() == exe_dir
    assert paths.web_dist() == exe_dir / "_internal" / "web" / "dist"
    assert paths.agents_dir() == exe_dir / "_internal" / "agents"

def test_trader_home_still_wins_when_frozen(tmp_path, monkeypatch): ...  # TRADER_HOME 설정 시 그 경로
```
- [x] **Step 2:** `uv run pytest tests/platform/test_bundle.py -v` → FAIL (`bundle` 없음)
- [x] **Step 3:** 구현. `registry.agents_dir(base)` 기본값을 `paths.agents_dir()` 로, `cli._web_deps` 의 `dist=paths.web_dist()`.
- [x] **Step 4:** `uv run pytest tests/platform tests/agents tests/web -q` → PASS
- [x] **Step 5:** 커밋 `feat(platform): bundle 모듈 — frozen 루트·리소스·도구 명령`

### Task 2: 세션의 도구 명령·PATH 를 bundle 로

**Files:**
- Modify: `src/trader/orchestrator/session.py`(`build_command`, env PATH), `src/trader/agents/registry.py:orchestrator_prompt`
- Test: `tests/orchestrator/test_session.py`, `tests/agents/test_registry.py`

**Interfaces (Consumes):** `bundle.tool_command()`, `bundle.tool_bin_dir()`.

- [x] **Step 1: 실패 테스트**
```python
def test_frozen_session_allows_trader_tool_and_rewrites_prompt(monkeypatch, ...):
    monkeypatch.setattr(bundle, "tool_command", lambda: "trader tool")
    cmd = build_command(spec(), Settings(), Path("/x/claude"), agents_file, session_id="s", prompt=orchestrator_prompt(Settings(), REPO_AGENTS))
    assert "Bash(trader tool *)" in cmd and "Bash(python -m trader tool *)" not in cmd
    prompt = cmd[cmd.index("--append-system-prompt") + 1]
    assert "python -m trader tool" not in prompt and "trader tool status" in prompt
```
- [x] **Step 2:** FAIL 확인
- [x] **Step 3:** `orchestrator_prompt` 가 md 본문의 `python -m trader tool` 을 `bundle.tool_command()` 로 치환, `build_command` 가 `f"Bash({bundle.tool_command()} *)"`, env `PATH` 앞에 `bundle.tool_bin_dir()`.
- [x] **Step 4:** `uv run pytest tests/orchestrator/test_session.py tests/agents -q` → PASS (기존 `"tool status"` 토큰 테스트 유지)
- [x] **Step 5:** 커밋 `feat(session): frozen 번들에서 도구 명령을 trader tool 로`

### Task 3: 인스톨러가 실행파일 경로를 등록

**Files:**
- Modify: `src/trader/platform/installer.py`(`render(program: list[str])`, `install`), `src/trader/cli.py:install_cmd`
- Test: `tests/platform/test_installer.py`

**Interfaces:** `render(platform, *, program: list[str], project_root, …)` — `program` 은 argv (`[python, "-m", "trader", "daemon"]` 또는 `[exe, "daemon"]`). `install(..., program: list[str] | None = None)` 기본은 `bundle.daemon_argv()`.
- `bundle.daemon_argv() -> list[str]` — frozen: `[sys.executable, "daemon"]`; 아니면 `[sys.executable, "-m", "trader", "daemon"]`.

- [x] **Step 1: 실패 테스트** — plist 에 `<string>/dist/trader/trader</string><string>daemon</string>` 만 있고 `-m` 이 없음; XML `<Command>…trader.exe</Command><Arguments>daemon</Arguments>`.
- [x] **Step 2:** FAIL → **Step 3:** 구현(기존 `python=` 인자는 제거, 호출자 갱신) → **Step 4:** `tests/platform` PASS
- [x] **Step 5:** 커밋 `feat(installer): 데몬 등록을 argv 로 (번들 실행파일 지원)`

### Task 4: 첫 실행 초기화 + `trader app` + 더블클릭 진입

**Files:**
- Create: `templates/config/*.yaml`(저장소 `config/*.yaml` 사본, `accounts.yaml` 의 `cano` 는 `"00000000"`), `templates/secrets/.env.example`(기존 `secrets/.env.example` 사본), `tests/test_cli_app.py`
- Modify: `src/trader/platform/bundle.py`(`first_run_init`), `src/trader/cli.py`(`init`, `app` 명령), `src/trader/__main__.py`

**Interfaces:**
- `bundle.first_run_init(root: Path) -> list[str]` — `config/`·`secrets/`·`state/`·`docs/` 를 만들고 템플릿 중 **없는 파일만** 복사. 복사한 상대경로 목록 반환. 템플릿 위치: `resource_dir()/"templates"`(frozen) 또는 저장소 `templates/`.
- CLI `trader init` → 위 호출, 복사 목록 출력. `trader app [--no-browser]` → `init` → `daemon_mod.run(deps, web=True)` 이고 시작 1.5초 뒤 스레드가 `webbrowser.open(f"http://{bind}:{port}")`.
- `__main__`: `bundle.is_frozen() and len(sys.argv) == 1` 이면 `app(["app"])`, 아니면 `app()`.

- [x] **Step 1: 실패 테스트**
```python
def test_first_run_init_copies_missing_only(tmp_path): (project/"config"/"settings.yaml") 미리 쓴 뒤 호출 → 그 파일 불변, accounts.yaml 생성, "00000000" 포함, secrets/.env 는 만들지 않고 .env.example 만
def test_cli_init_prints_created(project)
def test_app_runs_daemon_and_opens_browser(project, monkeypatch): daemon.run·webbrowser.open 을 monkeypatch, 호출 인자 확인(web=True, URL)
def test_main_double_click_routes_to_app(monkeypatch): sys.frozen=True, argv=["trader"] → app 명령 호출
```
- [x] **Step 2:** FAIL → **Step 3:** 구현 → **Step 4:** PASS (`tests/test_cli_app.py tests/test_cli_smoke.py`)
- [x] **Step 5:** 커밋 `feat(cli): init·app 명령과 더블클릭 진입 (첫 실행 템플릿)`

### Task 5: PyInstaller 스펙·로컬 빌드 스모크·GitHub Actions

**Files:**
- Create: `packaging/trader.spec`, `packaging/build.py`(빌드 → 스모크 → zip), `.github/workflows/build.yml`
- Modify: `pyproject.toml`(`[dependency-groups] build = ["pyinstaller>=6"]`), `.gitignore`(`build/`, `dist/` 이미)

**Interfaces:** `uv run --group build python packaging/build.py [--skip-web]` → `dist/trader/`(실행파일 + `_internal/`) 와 `dist/trader-<os>-<arch>.zip`. 스펙: `name="trader"`, `contents_directory="_internal"`, `console=True`, datas `web/dist→web/dist`, `.claude/agents→agents`, `templates→templates`, `docs/runbook-live-transition.md→docs`, hiddenimports `sklearn.*`(collect_submodules), `pyarrow`, `uvicorn.*`, `apscheduler.triggers.cron/interval`, `tzdata`.

- [x] **Step 1:** `uv sync --group build`, 스펙 작성, `uv run --group build pyinstaller packaging/trader.spec --noconfirm`
- [x] **Step 2: 스모크(packaging/build.py 가 수행):** `dist/trader/trader --help` exit 0; `TRADER_HOME=<tmp>` 로 `trader init` → `config/accounts.yaml` 생성; `trader status` exit 0; zip 안에 `secrets/.env`·`state/` 없음, `_internal/agents/orchestrator.md` 있음.
- [x] **Step 3:** `.github/workflows/build.yml`: `on: [push: tags v*, workflow_dispatch]`, matrix `os: [macos-latest, windows-latest]`, steps: checkout → `astral-sh/setup-uv` → `uv sync --group build` → `uv run pytest -q` → `actions/setup-node` + `npm ci && npm run build` (cwd web) → `uv run --group build python packaging/build.py --skip-web` → `actions/upload-artifact`; tag 푸시면 `softprops/action-gh-release` 에 zip 첨부.
- [x] **Step 4:** 로컬 macOS 빌드·스모크 통과 확인(Windows 는 CI 에서; TODO 의 Windows 확인 항목과 연결).
- [x] **Step 5:** 커밋 `build: PyInstaller 번들 스펙·빌드 스크립트·GitHub Actions`

### Task 6: 문서
- [x] `docs/bundle-usage.md`: 설치(압축 해제·claude 설치·첫 실행 경고 넘기기), 폴더 구조, `secrets/.env`·`config/accounts.yaml` 채우기, 더블클릭 → 웹 UI, 터미널 명령(`./trader status` 등), 업데이트(폴더 교체, `config/ secrets/ state/` 유지), 자동 시작(`trader install`).
- [x] CLAUDE.md: 실행 명령에 `init`·`app`·빌드 명령, 아키텍처 규칙에 "frozen 분기는 `platform/bundle` 만, 도구 명령은 `bundle.tool_command()`", 템플릿 규칙(config 바꾸면 templates 도).
- [x] TODO.md Phase 10 절(완료 기준: Windows CI 빌드 통과·더블클릭 실기 확인은 사용자 확인 대기).
- [x] 커밋 `docs: Phase 10 번들 사용법·규칙`

## 범위 조정
| 제외 항목 | 이유 | 재개 |
|---|---|---|
| 코드 서명·공증 | 개인 사용, Apple 개발자 계정 필요 | 사용자 결정 |
| 트레이/메뉴바 앱 | 웹 UI 가 같은 역할 | Phase 11 이후 |
| lightgbm·torch 번들 | 선택 extra, 용량·휠 문제 | 사용자 결정 |
| 자동 업데이트 | 폴더 교체로 충분 | 사용자 결정 |
