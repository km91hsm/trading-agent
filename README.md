# trading-agent — LLM 멀티 에이전트 + ML 주식 자동매매 (KIS)

한국투자증권(KIS) Open API 로 모의·실전 계좌를 운용하는 주식 자동매매 시스템입니다.
Claude Code 오케스트레이터 세션이 서브에이전트(스크리너·애널리스트 4·리서처 2·트레이더·복기자)를 소집해
종목을 분석하고, **파이썬 CLI 도구만 통해** 수집·체크포인트·결정 제출을 합니다.
리스크 한도와 주문 실행은 LLM 이 아니라 파이썬이 강제합니다. macOS 에서 개발·운영하고 Windows 도 같은 실행을 보장합니다.

> 현재 상태: Phase 0~18 구현 완료, 번들 v0.1.5 릴리스(작업 대기열·하락장 대응·시장 세션 포함). 남은 일은 모의 4주 이상 무사고 운영과 벤치마크 우위 확인이며,
> 실전 전환은 `docs/runbook-live-transition.md` 의 사용자 수동 절차로만 합니다.

## 1. 구조

```
사용자 ─ 텔레그램 / 웹 UI / CLI
            │
   ┌────────▼─────────────────────────────────────────────┐
   │ 파이썬 층 (src/trader)                                 │
   │  daemon ─ schedule.yaml 대로 scan → analyze → monitor │
   │  sources ─ KIS·DART·EDGAR·뉴스·커뮤니티 수집 (룩어헤드 차단) │
   │  risk ─ 순수 함수 게이트 (비중·종목 수·손실·비용·보유일)  │
   │  executor ─ 계좌별 판정·주문·원장 (주문은 여기서만 나간다) │
   │  ml ─ 피처 스토어·퀀트 랭커·보정·RL 사이저(실험)         │
   └────────▲─────────────────────────────────────────────┘
            │ python -m trader tool <status|collect|checkpoint|submit-decision>
   ┌────────┴─────────────────────────────────────────────┐
   │ LLM 층 (claude -p 세션, cwd 는 저장소 밖 임시 디렉터리)    │
   │  orchestrator → screener / analyst-* / researcher-* /  │
   │                 trader / reflector  (.claude/agents/*.md) │
   └──────────────────────────────────────────────────────┘
```

위 유지보수 명령(scan→analyze·종목 분석·백필·학습·복기·체결 동기화)은 **웹 대시보드 잡 표의 '지금 실행' 버튼**(데이터·ML 페이지의 버튼 포함)으로도 돌릴 수 있습니다 — `WEB_TOKEN` 과 켜져 있는 데몬이 필요하고, 데몬이 요청을 하나씩 순서대로 실행합니다.

- **LLM 은 도구만 씁니다.** 세션은 `--allowedTools "Bash(python -m trader tool *)"` 로 띄우고 외부 세계와는 `trader tool` 로만 접촉합니다.
- **순서는 도구 전제조건이 강제합니다.** 결정 제출은 애널리스트 체크포인트 2개 이상과 리서치 요약이 파일로 있을 때만 받습니다.
- **체크포인트가 상태의 원본입니다.** `state/runs/<날짜>/<시장>/<종목>/<단계>.json` 에 저장하고 있는 단계는 건너뜁니다.
- **dry_run 은 env(`TRADER_DRY_RUN`)로 전달**되며 값이 없으면 주문이 나가지 않는 쪽이 기본입니다.
- **실전 `auto` 는 `--confirm LIVE` 확인 기록이 있어야만 실행**되고, 어떤 코드 경로도 실전 모드를 자동으로 올리지 않습니다.

세부 규칙은 `CLAUDE.md`(여러 파일에 걸친 계약)와 설계 스펙 `docs/superpowers/specs/2026-10-09-trading-agent-design.md` 가 출처입니다.

## 2. 빠른 시작 (소스 설치)

준비물: Python 3.12, [uv](https://docs.astral.sh/uv/), Node.js(웹 UI 빌드), [Claude Code CLI](https://claude.ai/code)(구독 로그인 1회).

```bash
uv sync                                  # 의존성 (선택: --extra lightgbm, --extra rl)
uv run trader init                       # config/ secrets/ state/ docs/ 생성, 템플릿 복사
cp secrets/.env.example secrets/.env     # KIS·DART·FRED·NAVER API HUB·텔레그램·WEB_TOKEN 입력
#   다른 프로젝트의 .env 가 있으면: uv run trader secrets import --from <그 파일>
# config/accounts.yaml 의 계좌번호, config/sources.yaml 의 edgar.user_agent(이메일) 수정
uv run trader kis ping                   # 시세·휴장일·계좌 현금 점검
uv run trader notify-test                # 텔레그램 알림 점검
cd web && npm install && npm run build && cd ..   # 웹 UI 정적 파일 (web/dist)
```

리허설(구독 호출·주문 없음):

```bash
FAKE_CLAUDE=1 uv run trader analyze --ticker 005930 --dry-run
uv run trader status                     # state/STATUS.md 재생성
```

> `python` 이 PATH 에 없는 환경이 있어 모든 명령은 `uv run` 을 앞에 붙입니다.

## 3. 운영

```bash
uv run trader daemon [--dry-run]         # schedule.yaml 대로 scan/analyze/monitor·텔레그램·체결 동기화 + 웹
uv run trader install [--uninstall]      # 로그온 시 데몬 자동 시작 (launchd / 작업 스케줄러)
uv run trader web                        # 웹 UI 만 (기본 http://127.0.0.1:8787)
uv run trader control show|pause|resume
uv run trader control mode <계좌> <auto|approve|off> [--confirm LIVE]
uv run trader approvals list|approve <id>|reject <id>
uv run trader telegram poll|loop         # /approve /reject /status /pause /resume /mode /watch
uv run trader fills sync [--account] [--date]
```

기본 일정(`config/schedule.yaml`): KR 07:30 scan→analyze, 09:00~15:30 monitor(60초), US 는 뉴욕 시간 08:30 / 09:30~16:00,
16:30 reflect, 토요일 10:00 ml train. 계좌 모드는 yaml 값이 기본이고 SQLite `control` 테이블 값이 우선입니다.
모의는 `auto`, 실전은 `off` 로 시작합니다.

### 웹 UI

| 경로 | 내용 |
|---|---|
| `/` | 대시보드 — 운영 상태 한 줄(마지막 잡·세션·미승인·오류·일시정지) → 오늘의 시세표(보유●·선정○·관심☆ 한 줄씩: 현재가·비중·결정·확신·게이트) → 오늘의 주문·승인 → 성과·모델 |
| `/account` `/compare` | 계좌별 요약·포지션·주문, 같은 결정의 모의 vs 실전 판정·주문 비교 |
| `/today` | 종목별 파이프라인 판(입력→애널리스트→토론→요약→결정→게이트→주문, 단계마다 '보탠 것' 한 줄) + 체크포인트 뷰어 |
| `/orders` | 주문·승인 (승인은 토큰 필요) |
| `/chart` | 일봉·분봉 + 주문·트리거 마커 |
| `/data` | 피처 스토어 탐색 |
| `/ml` | 퀀트·RL 모델 레지스트리 |
| `/performance` | NAV·벤치마크·비용 민감도 |
| `/settings` | 토큰 입력, 일시정지·모드 제어 |

조회 API 는 토큰이 없고 제어 API(`/api/control/*`)는 `Authorization: Bearer <WEB_TOKEN>` 입니다. 루프백 Host 만 허용합니다.

디자인은 impeccable 로 고른 시각 세계 "신문 증권면 시세표"(라이트 = 미색 지면·먹 잉크, 다크 = 먹 바탕; 적청은 등락·실전 표식에만) — `DESIGN.md`·`PRODUCT.md`. 상단 바 계좌 선택이 전역이고 실전 계좌는 어디서나 빨간 표식, 390px 에서도 동작합니다.

## 4. 분석·학습 명령

```bash
uv run trader collect --ticker 005930 [--market US --exchange NAS] [--asof ISO] [--force]
uv run trader analyze --ticker 005930 [--dry-run] [--skip-collect]
uv run trader scan --market KR [--dry-run]
uv run trader monitor --market KR --once [--dry-run]
uv run trader reflect --market KR

uv run trader features backfill --years 3 [--market KR|US] / snapshot / derive
uv run trader ml train [--market] / models / promote <버전> / demote <버전> / tune [--grid small|wide]
uv run trader rl train [--market] [--seeds N] / models / promote|demote <버전> / report --account paper-main
uv run trader calibration build
uv run trader backtest --since YYYY-MM-DD
uv run trader readiness                  # 실전 전환 준비도 (읽기 전용)
uv run trader regime show | backtest --market KR   # 시장 국면(bull|neutral|bear)·헤지 설정 / 과거 bear 구간 재현
uv run trader universe check|sectors --market KR|US [--write]
```

ML 투입 지점은 둘뿐입니다. 승격된 퀀트 모델이 있으면 scan 랭킹과 트레이더 입력 `05_quant` 에 쓰이고, 없으면 ML 없이 파이프라인이 돕니다.
RL 사이저는 실험 단계라 모의 계좌·BUY/ADD 에만 섀도 기록하며 승격은 `rl promote` 수동입니다.

## 5. 리스크 규칙 요약 (`src/trader/risk/`, 순수 함수)

- 종목당 비중 25%(conservative 15%), 종목 수 5(conservative 2), 일일 손실 −5% 면 매수 중단
- 당일 중복 매수 금지, 기대수익 < 왕복비용×2 거부(KR 30bp·US 50bp), 최소 보유 3거래일, 주 2회
- BUY/ADD 의 손절선이 현재가 이상이면 거부(저장된 손절선으로 모니터가 즉시 매도하는 사고 방지)
- 거부 사유는 모두 모아 `40_risk_check_<계좌>` 에 남기고, 주문은 `executor` 안에서만 나감
- 손절은 approve 계좌에서도 승인 없이 즉시, `paused` 면 기록만

## 6. 번들(실행파일)

Python 없이 쓰는 배포본은 GitHub Releases 의 `trader-<버전>-macos-{arm64,x86_64}.zip`·`trader-<버전>-windows-x86_64.zip` 입니다.
`Trader.app`(macOS)·`TraderApp.exe`(Windows) 더블클릭 하나로 초기화 → 메뉴바/작업표시줄 ▲ 아이콘 + 데몬 + 웹 UI → 브라우저가 열리고, 아이콘 메뉴에서 일시정지·재개·종료합니다. 터미널용 CLI 는 같은 폴더의 `trader`. 사용법과 주의점은 `docs/bundle-usage.md`.
빌드는 `uv run --group build python packaging/build.py`, CI(`.github/workflows/build.yml`)가 `v*` 태그에 세 OS 번들을 첨부합니다.
`claude` CLI 는 번들하지 않습니다(구독 인증).

## 7. 디렉터리

```
.claude/agents/   서브에이전트 정의 (name·description·model·tools 프런트매터)
config/           settings·accounts·universe·schedule·sources·ml·sectors .yaml
templates/config/ config 의 사본 (계좌번호 "00000000") — config 를 바꾸면 같이 바꾼다
secrets/          .env (커밋·출력 금지)
state/            원장 DB·runs·logs·features·models·STATUS.md (직접 편집 금지)
src/trader/       agents broker sources orchestrator risk ledger scan ml memory calibration notify control web platform
web/              React + Vite 프런트 (npm test · npm run typecheck)
packaging/        PyInstaller 빌드 스크립트
docs/             TODO.md · bundle-usage.md · runbook-live-transition.md · superpowers/{specs,plans}
```

## 8. 개발

```bash
uv run pytest                 # 전체 테스트, 네트워크 없이 전부 돈다 (respx)
uv run ruff check src tests   # 경고 0건 유지
cd web && npm test && npm run typecheck
```

- 브랜치는 Phase 단위 `feat/phase<N>-<이름>` 을 직전 Phase 브랜치 위에 쌓습니다. `.gitattributes` 가 LF 를 강제합니다.
- 새 Phase 는 `docs/superpowers/plans/` 에 플랜을 먼저 쓰고, 완료 시 `docs/TODO.md` 체크와 `CLAUDE.md` 대조를 합니다.
- 금액은 `Decimal`, 경로는 `pathlib`, 저장 시각은 UTC ISO8601, 사용자 대면 문구는 한국어입니다.
- 셸 스크립트는 추가하지 않습니다. 훅·설치·빌드 전부 파이썬입니다.

## 9. 금지·주의

- 실전 주문을 수동으로 내지 않습니다. `.claude/settings.json` 이 `kis order*`·`approvals approve`·`telegram`·`daemon`·`monitor`·`install` 을 거부합니다(사용자만 실행).
- `secrets/` 는 커밋도 읽기도 하지 않습니다. 키는 `trader secrets import` 로만 옮깁니다.
- `uv run trader analyze` 를 `FAKE_CLAUDE=1` 없이 돌리면 구독 호출(세션당 약 $2~3)이 발생합니다.
- 모의 서버는 시세를 주지 않아 시세·수급은 실전 env 클라이언트로 읽고, 주문·잔고만 계좌의 env 를 씁니다.

## 10. 문서

- `CLAUDE.md` — 여러 파일에 걸친 계약·실행 명령·금지사항 (코드 다음으로 권위 있는 문서)
- `docs/superpowers/specs/2026-10-09-trading-agent-design.md` — 설계 스펙
- `docs/superpowers/plans/` — Phase 별 구현 플랜
- `docs/TODO.md` — Phase 진행도와 사용자 확인 대기 항목
- `docs/bundle-usage.md` — 번들 사용법
- `docs/runbook-live-transition.md` — 실전 전환 런북 (approve 1주 → auto, `readiness` 통과 필수)
