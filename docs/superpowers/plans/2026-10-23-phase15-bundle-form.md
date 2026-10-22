# Phase 15 번들 형식 정비 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 번들을 "받아서 더블클릭하면 메뉴바/작업표시줄에 아이콘이 뜨고 브라우저가 열리는 앱" 으로 만든다 — 버전·README·심링크 보존 zip, 앱 아이콘, 콘솔 없는 실행파일 + CLI 실행파일 2개, macOS `.app`, 트레이/메뉴바 제어.

**Architecture:** PyInstaller 한 `Analysis`·`COLLECT` 에서 EXE 둘(`trader` 콘솔 CLI, `TraderApp` 창 없음)을 만들고 macOS 는 `BUNDLE` 로 `Trader.app` 을 감싼다. frozen 분기는 `platform/bundle.py` 하나에만 두며 TRADER_HOME 은 `.app` 의 **부모** 폴더(10월 20일 결정: 실행파일 옆에 `config/ secrets/ state/ docs/`). 트레이는 `platform/tray.py` 에서 순수 메뉴 모델과 pystray 어댑터로 나누고, 데몬은 백그라운드 스레드에서 돌며 시그널·트레이는 메인 스레드가 맡는다. Phase 10 범위 조정의 "트레이/메뉴바 앱" 이연을 회수한다.

**Tech Stack:** PyInstaller 6(`build` 그룹), `pystray`(+ macOS `pyobjc-framework-Cocoa`), `pillow`(아이콘·트레이 이미지), launchd/작업 스케줄러 등록은 기존 `platform/installer.py`.

**Spec:** 플랜 모드 플랜 `~/.claude/plans/clever-pondering-ritchie.md` Phase 15 절, `docs/superpowers/plans/2026-10-20-phase10-bundle.md`(번들 구조 결정·범위 조정), `docs/bundle-usage.md`, 스펙 §10.

## Global Constraints

- CLAUDE.md 규칙 전부: 셸 스크립트 금지(전부 파이썬), OS 분기는 `platform/` 만, 경로는 `pathlib`, 의존성은 Windows 휠이 있는 것만, 파일명에 `:` 금지, 콘솔 UTF-8.
- `secrets/`·`state/` 는 번들에 넣지 않는다. 서명·공증은 하지 않는다(문서로 우클릭 열기·SmartScreen 안내).
- `bundle.tool_bin_dir()`·`daemon_argv()`·`install` 이 가리키는 실행파일은 **항상 콘솔 CLI `trader`** (오케스트레이터 세션의 `trader tool`, launchd 의 `trader daemon`).
- `TRADER_HOME` env 우선 규칙 유지. `templates/` 동기화 규칙 유지.
- 버전의 출처는 `pyproject.toml` 하나(`importlib.metadata.version("trader")`); CI 태그 `v*` 와 다르면 빌드 실패.

## Review Focus

1. `.app` 안의 CLI 실행파일이 `TRADER_HOME` 을 `.app` 의 부모로 잡아야 한다 — `Trader.app/Contents/MacOS/trader status` 를 치면 `.app` 옆 `state/` 에 쓴다 → Task 3 `test_bundle_root_outside_app_bundle`.
2. 창 없는 `TraderApp` 은 stdout/stderr 가 `None` 이어도 죽지 않아야 한다(로깅 스트림 핸들러·`_force_utf8`) → Task 2 `test_force_utf8_tolerates_none_streams`, `test_logging_configure_without_stdout`.
3. 트레이 "종료" 는 실행 중 analyze 세션을 기다린 뒤 잠금을 풀어야 한다(launchd 가 두 번째 데몬을 띄우지 않도록) → Task 5 `test_tray_quit_waits_for_scheduler_and_releases_lock`.
4. zip 안의 심링크(`trader` → `Trader.app/Contents/MacOS/trader`, framework 내부 링크)가 링크로 보존돼야 한다(풀면 268MB 가 396MB 가 되던 문제) → Task 1 `test_make_zip_preserves_symlinks`.
5. 이름 충돌: Windows 는 대소문자를 무시하므로 `Trader.exe`/`trader.exe` 를 함께 두면 안 된다 → 창 없는 실행파일 이름은 `TraderApp` 으로 고정, Task 2 `test_spec_exe_names_differ_case_insensitively`.

---

## 파일 구조

```
src/trader/cli.py                 --version 콜백, app_cmd --tray
src/trader/__main__.py            실행파일 이름이 TraderApp* 이면 app --tray
src/trader/platform/bundle.py     cli_executable(), bundle_root() 의 .app 규칙, app_bundle_dir()
src/trader/platform/tray.py       MenuItem·TrayState·menu_model()·title_text() (순수) + run_tray() (pystray)
src/trader/daemon.py              run(..., install_signals=True)
src/trader/logging_setup.py       stdout 이 None 이면 스트림 핸들러 생략
packaging/trader.spec             EXE 둘 + COLLECT + (darwin) BUNDLE
packaging/build.py                dist_name()·make_zip 심링크·README 동봉·macOS 배포 폴더 조립·스모크
packaging/make_icons.py           favicon.svg(마크) → trader.icns / trader.ico / tray-{light,dark}.png
packaging/icons/                  생성물 커밋
.github/workflows/build.yml       태그↔버전 검사, artifact 이름 = zip 이름
docs/bundle-usage.md, README.md   폴더 트리·더블클릭·종료 방법 갱신
tests/test_packaging_build.py, tests/platform/test_bundle.py, tests/platform/test_tray.py, tests/test_cli_app.py
```

---

### Task 1: 버전·README·zip 정리

**Files:**
- Modify: `src/trader/cli.py`(`--version` 콜백), `packaging/build.py`, `.github/workflows/build.yml`, `docs/bundle-usage.md`, `README.md`, `docs/TODO.md`(v0.1.3 zip 이름 항목)
- Test: `tests/test_cli_app.py`, `tests/test_packaging_build.py`

**Interfaces:**
- `cli.app` 에 typer 콜백 `--version` → `trader <버전>` 출력 후 exit 0(`importlib.metadata.version("trader")`, 실패 시 `0.0.0`).
- `packaging/build.py`: `dist_name(version: str, sys_platform: str, machine: str) -> str` → `trader-<version>-<macos|windows|linux>-<arch>` (`darwin→macos`, `win32→windows`, `machine.lower()` 에서 `amd64→x86_64`). `make_zip(dist_dir: Path, name: str) -> Path` 는 심링크를 `ZipInfo(external_attr=(stat.S_IFLNK | 0o755) << 16)` + `writestr(os.readlink)` 로 보존하고, 압축 전에 `docs/bundle-usage.md` 를 `dist_dir/README.md` 로 복사한다. `project_version() -> str` 는 `pyproject.toml` 의 `version`.
- CI: 태그 빌드에서 `python -c` 한 줄로 `project_version()` 과 `GITHUB_REF_NAME[1:]` 비교, 다르면 exit 1. artifact 이름 `${{ steps.build.outputs.zip_name }}`(build.py 가 `zip` 키로 출력하는 파일명).

- [ ] **Step 1: 테스트** — `test_version_flag_prints_version`(CliRunner `--version` → `trader 0.1.` 접두), `test_dist_name`(`dist_name("0.1.3","darwin","arm64") == "trader-0.1.3-macos-arm64"`, `("0.1.3","win32","AMD64") == "trader-0.1.3-windows-x86_64"`), `test_make_zip_preserves_symlinks`(tmp 트리에 파일 + 심링크 → zip 열어 `external_attr >> 16 & S_IFLNK`, 내용 = 링크 대상), `test_make_zip_includes_readme`.
- [ ] **Step 2: 실패 확인** `uv run pytest tests/test_packaging_build.py tests/test_cli_app.py -q`
- [ ] **Step 3: 구현**
- [ ] **Step 4: 통과 확인** + `uv run ruff check src tests packaging`
- [ ] **Step 5: Commit** `build: 버전 플래그·zip 이름에 버전·README 동봉·심링크 보존·CI 태그 검사`

### Task 2: 아이콘 + 실행파일 2개(콘솔 숨김)

**Files:**
- Create: `packaging/make_icons.py`, `packaging/icons/{trader.icns,trader.ico,tray-light.png,tray-dark.png}`
- Modify: `pyproject.toml`(`build` 그룹에 `pillow`), `packaging/trader.spec`, `src/trader/__main__.py`, `src/trader/cli.py`(`_force_utf8`), `src/trader/logging_setup.py`, `src/trader/platform/bundle.py`
- Test: `tests/test_cli_app.py`, `tests/test_main_entry.py`, `tests/platform/test_bundle.py`, `tests/test_logging_setup.py`(있으면 추가)

**Interfaces:**
- `make_icons.py`: `render_png(svg_path: Path, size: int) -> Image`(마크를 Pillow 로 직접 그린다 — SVG 래스터라이저 의존성 없이 `favicon.svg` 와 같은 기하: 둥근 사각 먹 바탕 + 미색 ▲; 트레이용은 투명 바탕에 단색 ▲, light/dark), `write_ico(out: Path, sizes=(16,32,48,256))`, `write_icns(out: Path)`(macOS 에서만 `iconutil` 호출, 없으면 건너뛰고 경고). 생성물은 커밋한다(CI 가 다시 만들지 않음).
- spec: `exe_cli = EXE(..., name="trader", console=True, icon=ICON)`, `exe_app = EXE(..., name="TraderApp", console=False, icon=ICON)`, `COLLECT(exe_cli, exe_app, ...)`. `ICON` 은 darwin `trader.icns`, win32 `trader.ico`.
- `__main__.main`: frozen ∧ `len(argv)==1` ∧ `Path(sys.executable).stem.lower().startswith("traderapp")` → `cli.app(["app", "--tray"])`; `trader` 더블클릭(인자 없음)은 지금처럼 `app`(콘솔). `_pause_if_console` 은 `sys.stdin` 이 None 이면 건너뜀.
- `cli._force_utf8`: 스트림이 `None` 이면 건너뜀. `logging_setup.configure`: `sys.stdout is None` 이면 `StreamHandler` 를 붙이지 않는다.
- `bundle.cli_executable() -> Path`: frozen 이면 실행파일 폴더의 `trader[.exe]`(없으면 `executable()`), 개발 환경은 `executable()`. `tool_bin_dir()`·`daemon_argv()` 는 `cli_executable()` 기준.

- [ ] **Step 1: 테스트** — `test_main_traderapp_routes_to_tray`(monkeypatch `sys.executable="/x/TraderApp"`, frozen → `cli.app(["app","--tray"])` 호출), `test_force_utf8_tolerates_none_streams`, `test_logging_configure_without_stdout`, `test_cli_executable_prefers_console_binary`(frozen, `TraderApp` 폴더에 `trader` 존재 → 그 경로), `test_spec_exe_names_differ_case_insensitively`(spec 텍스트에서 `name="..."` 둘을 읽어 `.lower()` 가 다름).
- [ ] **Step 2~4** 실패 → 구현 → 통과
- [ ] **Step 5: Commit** `build: 앱 아이콘·창 없는 TraderApp 실행파일 추가(CLI trader 와 분리)`

### Task 3: macOS `.app` 번들

**Files:**
- Modify: `packaging/trader.spec`(BUNDLE), `src/trader/platform/bundle.py`, `packaging/build.py`(배포 폴더 조립·스모크), `docs/bundle-usage.md`
- Test: `tests/platform/test_bundle.py`, `tests/test_packaging_build.py`

**Interfaces:**
- spec(darwin 만): `BUNDLE(coll, name="Trader.app", icon=ICON, bundle_identifier="dev.trader.app", info_plist={"CFBundleShortVersionString": VERSION, "CFBundleVersion": VERSION, "LSUIElement": True, "NSHighResolutionCapable": True})`. `VERSION` 은 spec 이 `pyproject.toml` 에서 읽는다.
- `bundle.app_bundle_dir() -> Path | None`: frozen 이고 실행파일 경로 부모들 중 이름이 `.app` 으로 끝나고 그 아래가 `Contents/MacOS` 면 그 `.app` 경로. `bundle_root()`: `app_bundle_dir()` 가 있으면 **그 부모**, 아니면 지금처럼 실행파일 폴더(Windows·개발). `resource_dir()` 는 `sys._MEIPASS` 그대로(PyInstaller 6 은 `Contents/Frameworks`).
- `build.py`: PyInstaller 뒤 `assemble_dist(dist: Path) -> Path` 가 배포 폴더 `dist/trader/` 를 만든다 — darwin: `Trader.app` 이동 + 심링크 `trader → Trader.app/Contents/MacOS/trader` + `README.md`; win32: `trader.exe`·`TraderApp.exe`·`_internal/`·`README.md`. 스모크는 `dist/trader/trader`(심링크)로 `--help/init/status/selfcheck/ml models/rl models` 를 돌리고, darwin 은 `Trader.app/Contents/Info.plist` 존재와 `TRADER_HOME` 없이 `init` 했을 때 **`.app` 옆** 에 `config/` 가 생기는지(임시 폴더에 `.app` 을 복사해) 확인한다.

- [ ] **Step 1: 테스트** — `test_bundle_root_outside_app_bundle`(frozen, `sys.executable="/tmp/x/trader/Trader.app/Contents/MacOS/trader"` → `bundle_root() == /tmp/x/trader`, `tool_bin_dir() == .../Contents/MacOS`), `test_bundle_root_windows_flat`(기존 규칙 유지), `test_assemble_dist_darwin_layout`(tmp 에 가짜 `Trader.app/Contents/MacOS/trader` 만들고 조립 → 심링크·README 확인).
- [ ] **Step 2~4** 실패 → 구현 → 통과. 로컬 실빌드 `uv run --group build python packaging/build.py --skip-web` 로 `dist/trader/` 트리·zip 확인(사용자 더블클릭 실기는 TODO).
- [ ] **Step 5: Commit** `build: macOS Trader.app 번들 — TRADER_HOME 은 .app 옆, CLI 심링크·README 동봉`

### Task 4: 트레이/메뉴바 (순수 모델 + pystray)

**Files:**
- Create: `src/trader/platform/tray.py`, `tests/platform/test_tray.py`
- Modify: `pyproject.toml`(`pystray>=0.19`, `pyobjc-framework-Cocoa; sys_platform == 'darwin'`), `packaging/trader.spec`(hiddenimports `pystray._darwin`·`pystray._win32`, `packaging/icons/tray-*.png` datas)

**Interfaces:**
- `@dataclass TrayState(paused: bool, pending_approvals: int, last_error: str | None, web_url: str)`.
- `@dataclass MenuItem(key: str, label: str, enabled: bool = True, checked: bool = False)`; `menu_model(state: TrayState) -> list[MenuItem]` → 순서 `open_web("웹 UI 열기")`, `toggle_pause("일시정지" | "재개", checked=paused)`, `open_logs("로그 폴더 열기")`, `quit("종료")`; `title_text(state) -> str` → `"trader · 일시정지"` | `"trader · 미승인 n"` | `"trader · 실행 중"` (우선순위 그 순서).
- `read_state(conn_factory, web_url) -> TrayState`(원장 `control.is_paused`, pending 승인 수 `SELECT COUNT(*) FROM approvals WHERE status='pending'`, 최근 `runs` error).
- `run_tray(*, state_fn: Callable[[], TrayState], on_toggle_pause: Callable[[], None], on_quit: Callable[[], None], icon_dir: Path, poll_s: float = 30) -> None` — **메인 스레드**에서 `pystray.Icon(name="trader", icon=PIL.Image.open(tray-dark|light.png), title=title_text(state), menu=...)`; 메뉴는 `pystray.Menu` 의 콜러블(열 때마다 `menu_model` 평가); 30초마다 `state_fn` 으로 title 갱신. `pystray` import 실패면 `TrayUnavailable` 을 올린다.

- [ ] **Step 1: 테스트** — `menu_model` 의 라벨·순서·`toggle_pause.label == "재개"`(paused), `title_text` 우선순위 3케이스, `read_state` 가 tmp 원장에서 paused·pending 을 읽는다.
- [ ] **Step 2~4** 실패 → 구현 → 통과(pystray 는 테스트에서 import 하지 않는다 — `run_tray` 는 lazy import).
- [ ] **Step 5: Commit** `feat(platform): 트레이 메뉴 모델·pystray 어댑터`

### Task 5: `trader app --tray` — 데몬 스레드 + 메인 스레드 트레이

**Files:**
- Modify: `src/trader/cli.py`(`app_cmd` `--tray`), `src/trader/daemon.py`(`run(..., install_signals: bool = True)`), `docs/bundle-usage.md`
- Test: `tests/test_cli_app.py`, `tests/test_daemon.py`(기존 run 테스트에 `install_signals=False` 케이스)

**Interfaces:**
- `daemon.run(deps, *, web=True, scheduler_factory=None, lock_path=None, install_signals=True)`: `install_signals=False` 면 `_install_signals` 를 부르지 않는다(비메인 스레드에서 `signal.signal` 은 ValueError).
- `cli.app_cmd(..., tray: bool = typer.Option(False, "--tray"))`: tray 면 (1) `scheduler_factory` 가 만든 `BlockingScheduler` 를 `holder["scheduler"]` 에 쥐고 (2) `threading.Thread(target=daemon.run, kwargs=dict(install_signals=False, scheduler_factory=...))` 시작, (3) 메인 스레드가 `signal.signal(SIGINT/SIGTERM, handler)` 를 걸되 handler 는 `daemon.make_signal_handler(scheduler, stop_event)` 를 부른 뒤 `icon.stop()`, (4) `tray.run_tray(...)` — `on_quit` 은 같은 핸들러 경로(`scheduler.shutdown(wait=True)` → `stop_event.set()` → 트레이 종료), `on_toggle_pause` 는 `control.set_flag(conn, "paused", not paused)`, `open_web` 은 `webbrowser.open(url)`, `open_logs` 는 `paths.state_dir()/"logs"` 를 OS 열기(`platform/procs.open_folder(path)` 추가: darwin `open`, win32 `os.startfile`). 데몬 스레드가 잠금 실패로 먼저 끝나면(`result["ok"] is False`) 트레이를 띄우지 않고 로그에 남긴 뒤 exit 1. `TrayUnavailable` 이면 경고 로그 + 기존 블로킹 `app` 으로 폴백.
- 브라우저 자동 열기는 지금처럼 포트 대기 스레드.

- [ ] **Step 1: 테스트** — `test_app_tray_runs_daemon_in_thread_and_quits`(가짜 `run_tray` 가 즉시 `on_quit()` 호출 → 데몬 스레드 join, `scheduler.shutdown(wait=True)` 호출됨, 잠금 해제됨), `test_app_tray_falls_back_without_pystray`, `test_daemon_run_skips_signals_when_asked`.
- [ ] **Step 2~4** 실패 → 구현 → 통과. 문서: "창을 닫으면 멈춥니다" → "메뉴바(작업표시줄) 아이콘 → 종료".
- [ ] **Step 5: Commit** `feat(cli): trader app --tray — 데몬 스레드 + 메뉴바/작업표시줄 아이콘 제어`

### Task 6: 문서·CLAUDE.md·TODO·실빌드

- [ ] `docs/bundle-usage.md` 폴더 트리(`Trader.app`/`TraderApp.exe` + `trader` + `README.md`)·첫 실행·종료·CLI 사용법, README 번들 절, CLAUDE.md 번들 규칙 한 줄씩(실행파일 2개·`.app` 루트 규칙·트레이 메인 스레드·zip 이름), `docs/TODO.md` Phase 15 체크·사용자 실기 항목(더블클릭 → 메뉴바 아이콘·브라우저·종료; Windows 작업표시줄).
- [ ] `uv run --group build python packaging/build.py --skip-web` 로컬 빌드 → `dist/trader/` 트리·zip 이름·심링크·README 확인, `uv run pytest`, `uv run ruff check src tests packaging`, `uv run trader status`.
- [ ] Commit `docs: Phase 15 번들 형식 문서·TODO`
