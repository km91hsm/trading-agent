# 번들(실행파일) 사용법 — macOS · Windows

Python·uv 를 설치하지 않고 쓰는 배포본입니다. GitHub Releases 의 `trader-<버전>-macos-<arm64|x86_64>.zip`(macOS) 또는
`trader-<버전>-windows-x86_64.zip`(Windows) 을 받아 압축을 풉니다. 압축 안 최상위 `README.md` 가 이 문서입니다.

## 1. 폴더 구조

```
trader/                ← 이 폴더가 TRADER_HOME. 폴더째 다른 PC 로 복사해도 된다
  Trader.app           ← macOS: 더블클릭 = 메뉴바 아이콘 + 데몬 + 웹 UI + 브라우저 (Dock 에는 안 뜬다)
  trader               ← macOS: 터미널용 CLI (Trader.app 안의 실행파일로 가는 링크)
  TraderApp.exe        ← Windows: 더블클릭 = 작업표시줄 아이콘 + 데몬 + 웹 UI + 브라우저 (콘솔 창 없음)
  trader.exe           ← Windows: 터미널용 CLI
  _internal/           ← Windows 전용. 런타임·라이브러리·에이전트 프롬프트·웹 (macOS 는 .app 안에 있다)
  README.md            ← 이 문서
  config/              ← settings·accounts·universe·schedule·sources·ml .yaml (첫 실행 때 생성)
  secrets/.env         ← API 키 (첫 실행 때 .env.example 만 생김 → 복사해서 채운다)
  state/               ← 원장 DB·runs·logs·features·models (실행하며 생김, 백업 대상)
  docs/                ← 이 문서·실전 전환 런북
```

## 2. 준비물

1. **Claude Code CLI** — 번들에 들어 있지 않습니다. https://claude.ai/code 안내대로 `claude` 를 설치하고
   한 번 로그인(구독 인증)해 둡니다. 터미널에서 `claude --version` 이 나오면 됩니다.
2. **KIS Open API 앱키** (모의·실전), **DART**·**FRED** 키, **네이버 API HUB** 앱의 Client ID/Secret
   (NCP 콘솔 > NAVER API HUB > Application), **텔레그램** 봇 토큰·chat_id.
3. (선택) 번들은 scikit-learn 백엔드로 돕니다. LightGBM·PPO 백엔드는 소스 설치(`uv sync --extra lightgbm --extra rl`,
   macOS 는 `brew install libomp`)에서만 쓸 수 있고 설정 `backend: auto` 는 없으면 자동으로 폴백합니다.
   소스 설치에서 LightGBM 으로 학습·승격한 모델(`ml models` 의 backend)은 번들이 로드하지 못해 경고 뒤 ML 없이
   돕니다 — 번들과 `state/` 를 공유하려면 소스 쪽도 `backend: sklearn` 으로 학습하세요.

## 3. 첫 실행

1. `Trader.app`(macOS) 또는 `TraderApp.exe`(Windows) 를 더블클릭합니다. 메뉴바(작업표시줄)에 ▲ 아이콘이
   뜨고, 처음에는 `config/`·`secrets/.env.example`·`docs/` 가 만들어지며 웹 UI(http://127.0.0.1:8787)가
   열립니다. 키가 없으면 수집·세션은 실패하지만 UI 는 뜹니다.
   - macOS: 서명이 없어 "확인되지 않은 개발자" 경고가 뜨면 **우클릭 → 열기** 를 한 번 합니다. 그래도
     "악성 소프트웨어인지 확인할 수 없다" 로 막히면 터미널에서 `xattr -dr com.apple.quarantine trader/`
     를 한 번 실행합니다(압축 파일의 격리 표시 제거).
   - Windows: SmartScreen 경고에서 **추가 정보 → 실행** 을 한 번 누릅니다.
2. `secrets/.env.example` 을 `secrets/.env` 로 복사해 키를 넣습니다. 다른 프로젝트의 `.env` 가 있으면
   터미널에서 `trader secrets import --from <그 파일>` 로 이름 매핑 복사(값은 출력되지 않음).
3. `config/accounts.yaml` 의 `cano` 를 실제 계좌번호로 바꾸고(모의 `auto`, 실전은 `off` 로 시작),
   `config/sources.yaml` 의 `edgar.user_agent` 에 본인 이메일을 넣습니다(SEC 요구).
4. 터미널에서 점검: `./trader kis ping` (Windows: `trader.exe kis ping`), `./trader status`.
5. 리허설(구독 호출 없음) — macOS: `FAKE_CLAUDE=1 ./trader analyze --ticker 005930 --dry-run`,
   Windows PowerShell: `$env:FAKE_CLAUDE="1"; .\trader.exe analyze --ticker 005930 --dry-run`
   (cmd: `set FAKE_CLAUDE=1 && trader.exe analyze …`). `./trader selfcheck` 로 설치 점검.

## 4. 평소 운용

- 더블클릭(= `trader app --tray`) 하나로 스케줄 데몬 + 웹 UI 가 돕니다. 메뉴바(작업표시줄)의 ▲ 아이콘 메뉴:
  **웹 UI 열기 / 일시정지·재개 / 로그 폴더 열기 / 종료**. 종료는 실행 중인 분석 세션이 끝나길 기다린 뒤
  내려갑니다(최대 `llm.timeout_s`). 아이콘 제목(툴팁)이 "일시정지" · "미승인 n" · "실행 중" 을 보여 줍니다.
- 터미널에서 창과 함께 보고 싶으면 `./trader app`(트레이 없음, Ctrl+C 로 종료).
- 로그온 시 자동 시작: `./trader install` (해제 `--uninstall`). 데몬 로그는 `state/logs/`.
- 텔레그램: 알림 수신, `/approve <id>`·`/reject <id>`·`/status`·`/pause`·`/resume`·`/mode <계좌> <모드>`.
- 웹 UI 는 대시보드(운영 상태 한 줄 → 오늘의 시세표 → 주문·승인 → 성과)·오늘의 실행(종목별 파이프라인 판)·주문/승인·계좌·계좌 비교·차트·성과·데이터·ML·설정/제어 10페이지입니다. 상단 바의 계좌 선택이 전역이며 실전 계좌는 어디서나 빨간 표식으로 구분됩니다. 라이트/다크는 상단 바 오른쪽 버튼(기본은 시스템 설정).
- 웹 UI 제어(일시정지·승인·모드·관심종목)는 `secrets/.env` 의 `WEB_TOKEN` 을 설정/제어 페이지에 넣어야 됩니다. 토큰이 없으면 조회 전용입니다.
- 터미널 명령은 개발 환경과 같습니다(`uv run trader …` 대신 `./trader …`):
  `status`, `scan`, `analyze`, `monitor --once`, `fills sync`, `approvals list`, `control show|pause|resume`,
  `features backfill --years 3`, `ml train`, `readiness`, `backtest --since YYYY-MM-DD`.

## 5. 업데이트·백업

- 새 zip 을 풀어 **`Trader.app`·`trader`(macOS) 또는 `_internal/`·실행파일 둘(Windows)만** 교체합니다.
  `config/ secrets/ state/` 는 그대로 둡니다. 버전은 `./trader --version`.
- 백업은 `config/ secrets/ state/` 세 폴더면 충분합니다.

## 6. 문제가 날 때

- "claude 실행파일을 찾을 수 없습니다": PATH 에 `claude` 가 없습니다. `config/settings.yaml` 의
  `claude_bin` 에 전체 경로를 적습니다.
- 웹이 안 열림: 터미널에서 `./trader web` 로 띄우고 오류 메시지를 봅니다. 포트는 `settings.yaml` `web.port`.
- 원장 오류: `state/trader.db` 가 깨진 경우 `state/` 를 백업 뒤 비우면 새로 만듭니다(기록 유실 주의).
- 실전 전환은 `docs/runbook-live-transition.md` 의 수동 절차로만 합니다.
