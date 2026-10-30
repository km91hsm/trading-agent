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
3. (선택) **세이브티커 수집** — 일반 Chrome 확장과 로컬 Native host를 사용하면
   Claude/Codex·트레이딩 앱이 꺼져 있어도 수집합니다. Chrome에서 직접 로그인해야 하고,
   컴퓨터가 깨어 있어야 합니다. 설치 전 아래의 패키지·권한을 확인하세요.
4. (선택) 번들은 scikit-learn 백엔드로 돕니다. LightGBM·PPO 백엔드는 소스 설치(`uv sync --extra lightgbm --extra rl`,
   macOS 는 `brew install libomp`)에서만 쓸 수 있고 설정 `backend: auto` 는 없으면 자동으로 폴백합니다.
   소스 설치에서 LightGBM 으로 학습·승격한 모델(`ml models` 의 backend)은 번들이 로드하지 못해 경고 뒤 ML 없이
   돕니다 — 번들과 `state/` 를 공유하려면 소스 쪽도 `backend: sklearn` 으로 학습하세요.

## 3. 첫 실행

1. `Trader.app`(macOS) 또는 `TraderApp.exe`(Windows) 를 더블클릭합니다. 메뉴바(작업표시줄)에 ▲ 아이콘이
   뜨고, 처음에는 `config/`·`secrets/.env.example`·`docs/` 가 만들어지며 웹 UI(http://127.0.0.1:8787)가
   열립니다. 키가 없으면 수집·세션은 실패하지만 UI 는 뜹니다.
   - macOS: **먼저** 터미널에서 `xattr -dr com.apple.quarantine trader/` 를 한 번 실행합니다(압축
     파일의 격리 표시 제거). 이 단계를 건너뛰고 '우클릭 → 열기' 만 하면 macOS 가 앱을 읽기 전용 임시
     경로(App Translocation)에서 돌려 설정·원장을 만들 수 없습니다 — 그때는 앱이 안내 알림을 띄우고
     `state/logs/startup-error.log`(없으면 홈 폴더)에 남긴 뒤 종료합니다.
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
- 웹 UI 는 대시보드(운영 상태 한 줄 → 오늘의 시세표 → 주문·승인 → 데몬 잡·Claude 사용량·성과)·오늘의 실행(종목별 파이프라인 판)·주문/승인·계좌·계좌 비교·차트·성과·데이터·ML·작동 방식(AI 가 고르고 토론해 결정하는 흐름 설명)·설정/제어 11페이지입니다. 종목은 어디서나 '이름 코드'(예: 삼성전자 005930)로 보입니다. 상단 바의 계좌 선택이 전역이며 실전 계좌는 어디서나 빨간 표식으로 구분됩니다. 라이트/다크는 상단 바 오른쪽 버튼(기본은 시스템 설정).
- 대시보드 상태줄의 **데몬**은 `state/daemon.lock` 의 프로세스가 살아 있는지와 `state/daemon_heartbeat.json`(잡마다 갱신)으로 판정합니다. 10분 넘게 아무 잡도 안 돌면 '응답 없음' 으로 바뀝니다. **Claude 사용량**(5시간·7일)은 터미널에서 대화형 `claude` 를 쓸 때 상태줄이 남기는 마지막 관측값이라 관측 시각이 함께 표시되고, 이 프로그램이 쓴 세션 수·비용은 원장에서 집계합니다.
- 웹 UI 제어(일시정지·승인·모드·관심종목)는 `secrets/.env` 의 `WEB_TOKEN` 을 설정/제어 페이지에 넣어야 됩니다. 토큰이 없으면 조회 전용입니다.
- 스캔→분석·종목 분석·피처 백필·퀀트/RL 학습·복기·체결 동기화는 터미널 없이 **웹 대시보드 '데몬 잡' 표의 '지금 실행'** 과 데이터·ML 페이지의 버튼으로 돌릴 수 있습니다(`WEB_TOKEN` 필요, 데몬이 켜져 있어야 함 — Trader.app 이 켜져 있으면 됩니다). 요청은 '작업 대기열' 블록에 대기→실행 중→완료로 보이고 진행 문구도 보여요. 피처 스토어가 비어 있으면 데몬이 처음 켜질 때 3년 백필과 퀀트 학습을 자동으로 넣습니다.
- **하락장 대응**: 매일 스캔 앞에 지수 proxy(KODEX 200·SPY)로 시장 국면(상승/중립/하락)을 판정해 대시보드 상태줄에 보여 주고, 하락이면 신규 매수를 막고 인버스 ETF(KODEX 인버스·SH, 3지표 모두면 2배)를 계좌의 최대 20% 로 사서 들다가 국면이 풀리면 팝니다. 계좌 모드(off/approve/auto)·일시정지는 그대로 적용됩니다. 설정은 `config/settings.yaml` 의 `hedge:`.
- **세이브티커 수집**: Chrome 확장의 팝업에서 시작·중지·직접 확인 후 재개를 제어합니다.
  별도 수집 창에서 전용 탭을 차례로 활성화하며 사용자 작업 창을 앞으로 가져오지 않습니다.
  수집 창은 최소화하지 말고 열어 두세요. 최소화하면 복원할 때까지 수집을 쉽니다.
  로그인 만료·보안 재확인은 사이트 전체 수집을 멈추며 직접 완료한 뒤 명시적으로 재개해야 합니다.
  실패한 항목은 정상 주기보다 빠르게 재요청하지 않고 지수 대기하며, 예약 시각과 실패 횟수는 재시작 후에도 유지합니다. Chrome 종료·절전 중에는
  수집하지 못하며 Chrome을 다시 열면 활성화 상태를 복구합니다. 자동 부팅 실기는 별도 확인이 필요합니다.
  저장 폴더는 `state/external/saveticker/`(`config/sources.yaml news.saveticker_dir`)이고
  기존 리더와 같은 SQLite·JSONL·최신 JSON·PDF 형식을 사용합니다.
  뉴스·SAVE 리포트·커뮤니티 TOP5/지수/상승/하락·증시 거래대금/상승/하락의 실시간/정규장·
  이전 달부터 3개월 뒤 캘린더를 순환합니다. DOM 가상 목록은 두 번 스크롤하고 뉴스 본문은 작업당 두 건까지
  확인하므로 `partial`을 표시합니다. 모든 과거 기사·본문의 완전 수집을 뜻하지 않습니다.
  리포트 PDF는 nonce로 표시된 버튼 클릭 뒤 최대15초 동안 같은 사이트의 blob 앵커만 전달받고, Chrome이 새로 반환한 다운로드 ID와 발급 파일명·PDF를 확인한 경우에 저장합니다. 확인 불가·사용자 조작·이동·취소 시 미수집으로 남깁니다. 정적 MAIN 도우미는 DOM 앵커만 관찰하며 쿠키·요청을 읽지 않습니다.
  뉴스는 프레임당100카드·512KiB(최대3프레임), 상세는256KiB까지 읽습니다. 캘린더는 선택한 달의 실제 행 또는 명시적 빈 상태만 확인하며 스크롤하지 않습니다.
- **Chrome 수집기 설치**: 먼저 `./trader saveticker extension prepare --destination <새 패키지 경로>`로
  검토용 패키지를 만듭니다. 이 명령은 Chrome이나 backend를 변경하지 않습니다.
  `REVIEW.md`·`extension/manifest.json`을 확인한 뒤
  `./trader saveticker extension install-host <패키지 경로>`로 로컬 host를 등록하고,
  `chrome://extensions` 개발자 모드에서 패키지의 `extension/`을 압축해제된 확장으로 로드합니다.
  권한은 SaveTicker 사이트 접근, 알람, 로컬 저장, Native Messaging, 다운로드입니다.
  설치는 기존 수집기만 중지하고 `secrets/saveticker.env`를 `.extension-backup`으로 이름 변경하여
  구버전 앱의 자동 로그인 재시작도 막습니다. 새 비밀번호 파일이 있으면 복원 시 덮어쓰지 않습니다.
  제거는 확장을 Chrome에서 지운 뒤 `./trader saveticker extension uninstall-host <패키지 경로>`입니다.
  번들로 준비한 패키지는 실행 파일을 자체 포함하므로 작업 폴더·가상환경에 의존하지 않습니다.
  소스 준비는 개발용이며 Windows는 번들 준비만 지원합니다. 설치 패키지 폴더는 제거 전까지 유지하세요.
  상태: `./trader saveticker extension status`. 기존 Chromium 수집기는 확장 모드에서 자동 시작하지 않습니다.
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
