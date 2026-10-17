# 구현 진행 체크리스트 (스펙: docs/superpowers/specs/2026-10-09-trading-agent-design.md §11)

## Phase 0 골격
- [x] uv 프로젝트·CLI 진입점 (typer)
- [x] platform/paths·claude_bin (OS 분기는 여기만)
- [x] config 로더 5개 (settings·accounts·sources·universe·secrets), extra=forbid, ConfigError
- [x] SQLite 원장: 스키마 버전·순서 마이그레이션·control 테이블
- [x] 로깅 (UTF-8, 파일 회전, httpx WARNING)
- [x] STATUS.md 생성기 + `trader status`
- [x] CLAUDE.md·SessionStart 훅·settings.json 검증 테스트
- [x] 완료 기준: macOS 에서 `uv run trader status` 동작 + 새 claude 세션이 STATUS.md 자동 인식 (2026-10-09 확인, 훅 서브프로세스 테스트)
- [ ] 완료 기준: Windows 에서 `uv sync` 후 `uv run trader status` 동일 동작 (사용자 확인 대기)

## Phase 1 브로커
- [x] KisAuth (env 별 토큰 캐시, 1분 1회 발급 보호)
- [x] KisClient 레이트리밋(실전 20/s, 모의 2/s)·EGW00201 재시도·KisApiError
- [x] tr_ids 단일 파일, accounts.py
- [x] KisMarket (실전 env 만): 시세·일봉·휴장일 (분봉·호가·투자자동향·지수는 Phase 2 이연)
- [x] KisBroker (계좌 env): 잔고·예수금 (주문·취소·체결은 Phase 4)
- [x] FakeBroker·FakeMarket
- [x] 텔레그램 발신 (NullNotifier 포함)
- [x] 완료 기준(실기): `uv run trader kis ping` 으로 모의·실전 잔고, 005930·AAPL 가격 확인 (2026-10-09 확인, 실패 0건)
- [x] 완료 기준(실기): `uv run trader notify-test` 로 텔레그램 수신 확인 (2026-10-09 확인)

## Phase 2 수집
- [x] KisMarket 분봉·호가·투자자동향·지수 조회 + tr_id 추가 (Phase 1 이연)
- [x] indicators (SMA·RSI·MACD·KDJ·볼린저·ATR·ROC·거래량 Z·52주 위치)
- [x] dart, edgar 수집기 (공통 비율 키)
- [x] news (구글 RSS·네이버 API), macro (FRED·ECOS·KIS 지수) 수집기
- [x] community 수집기: Reddit·클리앙 기본 on, DC·에펨코리아 구현하되 off, 세이브티커 비활성 자리
- [x] 00_inputs 조립 (섹션 격리·received_at·asof 필터) + `collect` 명령
- [x] 피처 스토어 + 3년 과거 적재
- [x] 완료 기준: 1종목 00_inputs.json, state/features/ parquet, 수집기 단위테스트 (2026-10-09 확인: 005930·AAPL 수집, KR 1년 백필 5,040행. DART 는 점검(800)으로 절 오류 기록, 네이버 뉴스 API 는 401)
- [ ] 사용자 확인 대기: 네이버 검색 API 키가 401 — 애플리케이션에 '검색' API 권한이 있는지, 키 값이 맞는지 확인 후 `uv run trader collect --ticker 005930 --force` 재실행
- [ ] 사용자 확인 대기: DART 점검 종료 후 `collect`·`features backfill --years 3` 재실행으로 재무비율 적재

## Phase 3 에이전트
- [x] fundamental 입력 확장: DART 3개년 추세 + 최근 분기 재무, CFS 없으면 OFS 폴백, `inputs.SCHEMA_VERSION` 2 (Phase 2 이연, 스펙 §3.1)
- [x] `tool` CLI 4개 (collect·checkpoint·submit-decision·status) + 전제조건 검증
- [x] 출력 스키마 (AnalystReport·DebateTurn·Decision·CallMeta)
- [x] `.claude/agents/` md 8개 (orchestrator·analyst×4·researcher×2·trader)
- [x] 오케스트레이터 세션 실행 (`session.py`: 실제 `claude --help` 플래그만, 예산·호출 집계, agent_calls)
- [x] 수렴 감시·research_summary 병합
- [x] 체크포인트 재개, anonymize (익명화는 모듈만; 세션 적용은 Phase 6 에서 `settings.anonymize` 와 연결)
- [x] FAKE_CLAUDE 스텁 + `analyze --dry-run`
- [x] 완료 기준: `FAKE_CLAUDE=1 analyze --ticker 005930 --dry-run` → 30_decision.json, 중간 파일 삭제 후 해당 단계만 재실행 (2026-10-09 확인)
- [x] 완료 기준(실기): 실제 `claude -p` 1회로 서브에이전트 호출·도구 제한 동작 확인 (2026-10-09, 2회: 1차 $1.58 에서 절 출력 잘림 발견 → 압축 후 2차 $1.89/4분 23초, 애널리스트 4·토론 2라운드·요약·HOLD 결정, 스키마 거부→재제출 동작 확인)
- [x] HTML 엔티티(`&lt;` 등, Claude Code 가 서브에이전트 출력을 이스케이프)를 도구 저장 시 복원 (2026-10-09 리뷰 반영)
- [x] 세션 `duration_ms` 를 파이썬 측 측정값으로, running 행 선기록 (2026-10-09 리뷰 반영)
- [ ] 체크포인트 `call.model` 이 LLM 쓰기에선 None → `tool checkpoint write --model` 또는 agents.json 에서 역추적 (Phase 3 이연)
- [ ] 세션 cwd 를 저장소 밖(tempfile)으로 옮겨 상위 CLAUDE.md 자동 탐색 가정 제거 (Phase 3 이연)
- [x] 리서처·트레이더 입력을 파이썬이 조립하는 `tool checkpoint bundle`(24KB 바이트 한도) 추가. 실기 3차($1.80)에서 전문 전달이 예산 $2 를 소진해 중단 → 예산 $5 + bundle 로 4차($0.71, 재개 경로)에서 트레이더 묶음 17.8KB 전달·결정 확인 (2026-10-09)
- [ ] 실기(사용자 확인 대기): 처음부터 끝까지 bundle 경로로 1회 실행해 리서처 라운드 0·1 묶음이 Agent 프롬프트에 전문으로 들어가는지 세션 기록으로 확인 (`uv run trader analyze --ticker 005930 --dry-run`, 체크포인트 비운 뒤, 약 $2.5)
- [ ] 완료 기준: Windows 에서 FAKE_CLAUDE dry-run 동일 동작 (사용자 확인 대기)
- [ ] 사용자 확인 대기(Windows): `claude.cmd` 가 다줄 9KB `--append-system-prompt` 를 cmd.exe 재해석 없이 받는지 실제 `claude -p` 1회. 깨지면 `claude.exe` 우선 또는 `--agent` 파일 전달로 전환

## Phase 4 리스크·계좌별 실행
- [x] rules·stance·costs (TDD)
- [x] 원장 테이블 추가 (마이그레이션 v2): analyst_reports·debate_turns·decisions·risk_checks·orders·fills·positions_snapshot·approvals
- [x] executor (auto/approve/off) + `submit-decision` 게이트·주문 연결 (dry_run 은 세션 env `TRADER_DRY_RUN` 으로 전달, 실전 auto 는 `live_confirmed` 기록 필수)
- [x] KisBroker 주문·취소 (KR 현금주문·US 지정가, 취소 tr_id), 모의 주문 (체결 조회·fills 자동 적재는 Phase 6)
- [x] 승인 흐름: 텔레그램 수신 /approve /reject /status + `trader approvals list|approve|reject`, `trader telegram poll`(1회), `trader control show|mode|pause|resume` (/pause /resume /mode 텔레그램 명령은 Phase 6)
- [x] 결정 분포 메트릭 (STATUS: 결정 분포·수렴율·미승인 주문·계좌 성향)
- [x] 완료 기준: 모의 자동 체결 + 실전 off 기록, 한도·회전율 초과 거부 테스트, 실전 계좌 off 전용 테스트 (2026-10-09 확인: 테스트 328개·FAKE `analyze --ticker 000660`(dry-run 해제)로 `40_risk_check_{paper,real}` 기록·실전 off 주문 없음. 모의 서버 실주문은 휴장일(한글날)이라 `kis order-test` 가 "40100000 모의투자 영업일이 아닙니다" 로 거부됨 → 주문 경로·오류 매핑만 확인, 체결은 아래 항목)
- [ ] 실기(사용자 확인 대기, 영업일 09:00~15:30 KST): `uv run trader kis order-test --account paper-main` → 1주 지정가(현재가 −10%) 접수·취소 메시지 확인
- [ ] 실기(사용자 확인 대기): 텔레그램 승인 — `uv run trader control mode paper-main approve` → `FAKE_CLAUDE=1` 없이 `uv run trader analyze --ticker 005930`(결정이 BUY 일 때만 승인 요청, 약 $2) 또는 `uv run trader approvals list` 로 요청 확인 → 텔레그램에서 `/approve <id>` → `uv run trader telegram poll` → 주문 접수 확인 → `uv run trader control mode paper-main auto` 원복
- [ ] 사용자 확인 대기(Windows): Phase 4 테스트 전체 통과 (`uv run pytest`)
- [ ] 리뷰 Minor 이연: approvals.decision_id 0 기록, metrics 30일 창의 UTC/KST 하루 오차, telegram_offset 이 타 chat 메시지로 전진 안 함, KR limit_price 호가단위·범위 검증, 승인 시점 게이트 재평가 (Phase 5~6 에서 해당 모듈 손댈 때)

## Phase 5 웹 UI
- [x] FastAPI 라우터 (accounts·portfolio·runs·orders·charts·ml(자리)·control·system) + SSE(`/api/events`, 원장 카운터 폴링) + bearer 토큰 (`WEB_TOKEN` 미설정이면 제어 API 401)
- [x] React 앱(Vite 8 + React 19 + TS, `web/`): 전체 대시보드·계좌 상세·계좌 비교
- [x] 오늘의 실행·리포트(토론) 뷰어 (체크포인트 JSON 트리, 토론은 논거·반박 분리)
- [x] 주문/승인·차트(Lightweight Charts 5, 결정·주문 마커)·데이터 탐색(00_inputs; 피처 parquet 는 Phase 7) (웹 승인은 Phase 4 의 `approvals` 행·`executor.approve` 를 그대로 쓴다)
- [x] 설정/제어 페이지 (토큰 저장·일시정지·계좌 모드, 실전 auto 는 LIVE 확인)
- [x] 완료 기준: 계좌 전환, 산출물 열람, 웹 승인 주문, 차트 마커, 모의 주문이 실전 뷰에 안 섞임, 토큰 없는 제어 401 (2026-10-09 확인: TestClient 27개 — 계좌 분리·401·승인 2회 409·실전 auto confirm·경로 탈출; Vitest 5개; 브라우저 실기 — 대시보드·오늘의 실행(000660 토론 뷰)·차트(005930 KIS 일봉)·계좌·주문 페이지 렌더, 틀린 토큰으로 일시정지 → "토큰이 필요합니다" 표시. 웹 승인 주문 실기는 Phase 4 의 승인 실기(사용자 확인 대기)와 함께)
- [ ] 사용자 확인 대기: `secrets/.env` 에 `WEB_TOKEN` 설정 후 `uv run trader web` → 설정 페이지에 토큰 입력 → 일시정지/재개가 반영되는지, 승인 요청이 있을 때 주문/승인 페이지에서 승인 → 주문 접수 확인
- [ ] 리뷰 Minor 이연(Phase 5): `_validate_ids` 가 `.`·`..` 티커를 허용(경로 탈출은 없음), `list_decisions` 30일 창 UTC/KST 하루 오차, `favicon` 미지정
- [ ] Phase 5 이연: SSE 를 트리거·데몬 이벤트 푸시로(Phase 6), 관심종목 제어(Phase 6), 차트 지표 오버레이·분봉(Phase 6), ML 페이지 실데이터(Phase 7), 성과 페이지(Phase 8)

## Phase 6 scan·monitor·데몬
- [x] 정량 스크리너(`scan/quant`, 피처 스토어 기반) + screener 세션(haiku, `kind=screen`, `tool submit-screen`) + `trader scan` (ML 랭킹은 `rank_fn` 자리, Phase 7)
- [x] monitor·트리거(`triggers` v4)·재평가 세션(`kind=resume` → `31_decision_r<n>`)·손절 즉시 매도(approve 도 승인 없이, off 는 기록만) + `trader monitor [--once]`
- [x] daemon (APScheduler, web 스레드, `state/daemon.lock` 단일 인스턴스·stale 인계, 주말·휴장일·paused 건너뜀, SIGTERM 정리) + `trader daemon [--no-web]`
- [x] install (launchd plist / 작업 스케줄러 XML 파이썬 템플릿) + `trader install [--dry-run|--uninstall]`
- [x] YouTube 자막(RSS + youtube-transcript-api)·텔레그램 공개채널(t.me/s 미리보기) 수집, manual_inputs(`state/manual_inputs/<ticker>.md|json`). UI 실시간 push 는 Phase 8 로(SSE 카운터 폴링으로 충분)
- [x] 체결 동기화: `KisBroker.get_fills`(KR TTTC0081R·US TTTS3035R) + `fills.sync_fills`(멱등, external 주문 행, filled/partial) + `trader fills sync`, 데몬 잡 (Phase 4 이연)
- [x] 텔레그램 수신 루프 `trader telegram loop` + 데몬 잡(30초). /pause /resume /mode 텔레그램 명령은 웹·CLI `control` 로 대체(명령 추가는 Phase 8 선택)
- [x] 손절 자동 매도·트리거 재평가 (Phase 4 이연)
- [x] 트레이더 묶음 `context.portfolio` 를 계좌별 최신 스냅샷으로, 휴장일 반영 보유 거래일(`holiday_fn`) (Phase 4 이연)
- [x] 토스증권·네이버 종목토론실: 공개 API 없음·robots 차단·약관 리스크로 **영구 off** 결정(스펙 §0.3 표 갱신) (Phase 2 이연)
- [x] 이벤트 절 `news_macro.events`: 최근 7일 공시를 종류(실적·배당·증자…)로 분류, 예정 이벤트(`upcoming`)는 소스 미확정으로 빈 목록 (Phase 2 이연)
- [x] Reddit OAuth(`REDDIT_CLIENT_ID/SECRET/USER`, `client_credentials`) + Reddit 형식 User-Agent, 키 없으면 RSS 유지 (Phase 2 이연)
- [x] 커뮤니티 키워드: ASCII 티커 단어 경계 (Phase 2 이연)
- [x] 공시 조회 종료일 KST, `collect` 건너뜀 표시, 미래 asof(24시간 초과) 거부 (Phase 2 이연)
- [x] Phase 3·5 이연: `checkpoint write --model`(call.model 기록), 세션 cwd 를 저장소 밖 임시 디렉터리로, `_validate_ids` 가 `.`/`..` 거부, 30일 창 KST
- [x] 완료 기준(실기 일부): `FAKE_CLAUDE=1 trader scan --market KR` → 정량 점수 20종목·picks 5·`_scan.json`; `trader monitor --once`(보유 2건 점검, 신호 없음); `trader install --dry-run` plist 출력; `trader fills sync --account paper-main` 0건(휴장일); `trader daemon --no-web` 14초 기동 → 잡 10개 등록·잠금 생성, SIGTERM 으로 잠금 해제 확인 (2026-10-09)
- [ ] 완료 기준(사용자 확인 대기): 평일 자동 1주 무사고 — `uv run trader install` 로 등록 후 `state/logs/daemon.out` 과 `trader status` 로 매일 확인. 트리거→재평가→계좌별 주문은 보유 종목이 ±7%/익절선에 닿을 때 `triggers`·`31_decision_r*` 로 확인
- [ ] 완료 기준(사용자 확인 대기, Windows): `uv run trader install` 후 로그온 시 작업 스케줄러에서 데몬 기동 확인, `uv run pytest`
- [ ] 사용자 확인 대기: `config/sources.yaml` 의 `channels.youtube`(채널 ID)·`channels.telegram`(공개채널명) 입력 후 `uv run trader collect --ticker 005930 --force` 로 `sentiment.community.sources.youtube/*` 확인. Reddit OAuth 는 `secrets/.env` 에 `REDDIT_CLIENT_ID/SECRET/USER` 입력
- [x] Phase 6 이연(Phase 8 에서 회수): 텔레그램 `/pause /resume /mode <계좌> <모드> /watch add|remove|list`(실전 auto 는 거부), US 휴장일 정적 NYSE 달력 2026~2027(`daemon.US_HOLIDAYS`, KIS 해외 휴장 API 대신), 런타임 관심종목(`control.watchlist` → scan 후보·분석 대상)
- [ ] Phase 6 이연(Phase 9 이후): KRX 공매도·대차잔고·검색트렌드, UI 실시간 push·관심종목 웹 제어·차트 지표 오버레이, US 휴장일 2028+ 달력 갱신
- [ ] 사용자 확인 대기(실기): US 체결 조회(`fills sync`)의 날짜 기준(ET vs KST)·`_local_dt(ET)` 가정, `OVRS_EXCG_CD=NASD` 고정으로 NYS/AMS 체결이 조회되는지 — US 모의 체결 1건 뒤 `uv run trader fills sync --account paper-main --date <ET 날짜>` 로 확인
- [ ] 리뷰 Minor 이연(Phase 6): SIGTERM 중 실행 중인 claude 세션은 launchd SIGKILL 로 `agent_calls` running 행이 남아 상한에 집계됨(기동 시 stale running 정리), `pid_alive` 가 PermissionError 를 생존으로 봄, quant 가 NaN 피처 행을 z=0 으로 통과시킴, `latest_features` 가 parquet 전부를 읽음

## Phase 7 ML 1단계
- [x] `ml.yaml` 로더(`MlConfig`), `ml/dataset`(적재·5거래일 3분류 라벨·피처 열), 라벨 엠바고
- [x] 피처 스토어 news_count·community_count 집계(같은 날 00_inputs 에서) (Phase 2 이연). US 연도별 비율(EDGAR 과거 연도)·동종업 중앙값 대비 비율은 데이터 없음 → Phase 8
- [x] quant 모델(GBDT 3분류: `lightgbm` import 가능하면 LGBM, 아니면 sklearn HistGradientBoosting) + ranker(P(up)·0.6 + 정량·0.4)
- [x] 워크포워드 비중첩 폴드(`ml/walkforward`, 엠바고 gap 검사) + 라벨 누출 역검증(라벨 섞기 → 우연 수준, 타깃 열 피처 금지)
- [x] registry(`state/models/quant/<버전>/model.joblib+meta.json`, 승격 플래그)
- [x] 05_quant.json 투입(`tool collect`·`analyze`·데몬 뒤 `tools.ensure_quant`, 트레이더 `context.quant`), scan ML 랭킹(`ml_model` 표시)
- [x] UI ML 페이지(모델 목록·폴드 성능·permutation 중요도) + `/api/ml/models[/{version}]` + `trader ml train|models|promote` + 데몬 `ml_train` 잡 + STATUS "ML 모델" 절
- [x] 완료 기준(실기 2026-10-17): `uv run trader ml train --market KR` → 4,940행·6폴드 정확도 0.315 / 기준선 0.465 / 알파 −0.003 → **미승격**(승격 규칙: 정확도 > 기준선 ∧ ≥ 0.40 ∧ 알파 > 0 ∧ 폴드별 up 예측 ≥ 5건. 잡음 데이터 4 seed 전부 미승격 테스트 확인. 파이프라인은 ML 없이 돈다). 합성 신호 데이터에서는 승격·05_quant·랭킹 전부 테스트로 확인. UI 성능·중요도는 `/api/ml/models` 응답으로 확인
- [ ] 사용자 확인 대기: LightGBM 을 쓰려면 `brew install libomp` → `uv sync --extra lightgbm` → `config/ml.yaml` 의 `models.quant.backend: lightgbm`(기본은 플랫폼 동일 동작을 위해 `sklearn`)
- [x] Phase 7 이연(리뷰, Phase 8 회수): close 대비 비율 피처 `sma_5/20/60_rel·bb_pos·atr_pct·macd_hist_rel` 추가(옛 parquet 행은 NaN → `features backfill` 재실행 후 `ml train`). 중요도는 마지막 폴드 홀드아웃 기준
- [x] Phase 7 이연: `features snapshot` 은 데몬 reflect 잡 앞에서 매일 실행(`DaemonDeps.snapshot_fn`, 실패는 통보). 재학습은 사용자 실기(`features backfill --years 3` → `ml train`)
- [ ] 사용자 확인 대기: 3년 백필(`uv run trader features backfill --years 3`, DART 정상 시) 후 `ml train` 재실행 — 1년·20종목으로는 기준선을 못 넘음. 승격 전까지 05_quant·ML 랭킹은 비활성
- [ ] Phase 7 이연: US 연도별 비율·동종업 비율·피처 행 탐색 UI·하이퍼파라미터 탐색(Phase 8)

## Phase 8 학습·평가·보정
- [x] memory 3층·엠바고·서킷브레이커 (`memory_entries` v5, 12차원 상황 벡터, 반감기 14/90/365, 워밍업 50, `control.memory_breaker`)
- [x] reflector md + `reflect` 명령 (`outcomes.mature` → reflector 세션 `kind=reflect` → `_reflect.json`·`reflection`, 데몬 잡)
- [x] calibration 모델 (결정 60건 미만이면 비활성, `35_calibration` 은 활성일 때만 생성, `calibration build`)
- [x] 성과 페이지 (수익률·MDD·Sortino·벤치마크 1/N·B&H·퀀트·비용 민감도·회전율·결정 분포·수렴율) + `GET /api/performance` + STATUS "성과(30일)"
- [x] 간이 백테스트 (컷오프 이후만, `backtest --since`)
- [x] 실전 approve 전환 절차 (사용자 수동): `docs/runbook-live-transition.md` + `trader readiness`(읽기 전용) + `trader backtest --since`
- [x] 실기: `FAKE_CLAUDE=1 analyze --ticker 000660 --dry-run` → `memory_entries` 1행(워밍업·`35_calibration` 없음), `reflect --dry-run` 0건, `backtest --since 2026-10-01` n=0, `readiness` 불합격 항목 표시·모드 불변, `/api/performance` 200 (2026-10-17 확인)
- [ ] 완료 기준(사용자 확인 대기): 모의 4주 이상 성과 리포트(웹 `/performance`·`trader readiness` 통과), 보정 모델 활성(만기 결정 60건), 사용자 판단으로 실전 approve(`docs/runbook-live-transition.md`)
- [ ] 사용자 확인 대기: 파생 비율 피처 반영 재학습 — `features backfill --years 3` → `ml train` 으로 기준선 초과 여부 확인
- [ ] 리뷰 Minor 이연(Phase 8): `tool` 컨텍스트 `now` 를 stdin asof 로(`decided_at` 과 run date 불일치), 상황 벡터는 run date 이하 피처 행으로, `cost_sensitivity` 미체결 주문 포함·UTC/KST 하루 오차, `nav_history(days)` 는 스냅샷 개수 기준, `outcomes.mature(today)` 미사용, 백테스트 HOLD 적중 정의, `/watch add` 인자 없을 때 사용법, 관심종목 시장 구분 없음, `submit_reflection` 디렉터리 직접 생성, `calibration_runs` 종목마다 1행 누적, 메모리 차원 `sma_5_rel` 과 피처 열 부호 반대, `record_decision(decision_id=None)` 멱등 깨짐, reflect 잡 `PipelineError` 시 남은 시장 건너뜀, `run_error_rate` 분모에 interval 잡 성공분 없음(보수적)

## Phase 9 RL 사이징(실험)
- [x] gym env (`ml/rl/env.SizingEnv`: 관측 16차원 = 상황 벡터 12 + 확신·LLM 비중 + 현재 비중·드로다운, 행동 0~0.25, 보상 = w·r − 회전 비용 − 드로다운 페널티; gymnasium 어댑터는 선택 extra)
- [x] 워크포워드 × 시드 3회 학습·기준선(고정 10%·LLM 비중·변동성 타깃) 비교 (`rl train`, 폴드·엠바고는 quant 와 동일). 백엔드는 `cem`(numpy, 기본) / `ppo`(stable-baselines3, `uv sync --extra rl`)
- [x] 모의 계좌 한정 섀도/적용 (`ml.yaml rl_sizer.enabled` ∧ `rl promote` 수동 승격 → executor 가 **모의 계좌·BUY/ADD** 에만 `rl_shadow` 기록, `mode: apply` 면 target_weight 교체·게이트 상한은 그대로. 실전 계좌는 기록도 없음)
- [x] UI 실험 로그 (ML 페이지 "RL 실험" 절, `/api/rl/experiments|shadow|report`, STATUS `rl_sizer` 줄, 데몬 ml_train 이 enabled 일 때 함께 학습, `rl report`)
- [x] 실기(2026-10-17): `rl train --market KR --seeds 1`(피처 스토어, CEM) → `rl models`·`rl report` → `FAKE_CLAUDE=1 analyze --dry-run`(HOLD 라 `rl_shadow` 0행, 모드 불변) → `/api/rl/*` 200 → STATUS 줄
- [ ] 완료 기준(사용자 확인 대기): 8주 워크포워드 비교 리포트 — 모의 운영 8주 뒤 `uv run trader rl train` → `rl report --account paper-main --weeks 8` 로 RL vs 고정·LLM 비교, 승격은 `rl promote <버전>` 수동(그 뒤 `ml.yaml rl_sizer.enabled: true`, 처음엔 `mode: shadow`)
- [ ] 사용자 확인 대기: PPO 백엔드 — `uv sync --extra rl`(torch 휠 필요, 이 머신은 x86_64 Python 이라 설치가 안 될 수 있음) 후 `ml.yaml rl_sizer.backend: ppo` 로 `rl train`
- [ ] Phase 9 이연: 멀티 종목 동시 포트폴리오 환경, RL 실험 전용 페이지, 피처 스토어 날짜별 parquet 전체 읽기(학습·섀도 관측마다 수 초)
- [ ] 리뷰 Minor 이연(Phase 9): `rl train --seeds` 가 pydantic 범위(1~10) 검증을 우회, `rl_shadow.applied` 가 게이트 전 기록(거부돼도 applied=1)·재평가(`31_decision_r<n>`) 는 첫 행 유지, 메모리 스텝이 SELL/TRIM/HOLD 결정도 매수 비중으로 취급(LLM 기준선 불리), `/api/rl/report?market=` 미검증, `ml/models/__init__` docstring 의 "rl_sizer 는 Phase 9" 문구·`ml.yaml` horizon_days 주석, `uv.lock` 에 torch 계열 항목(extra 전용) — 학습 drawdown(에피소드 피크) 과 서빙 drawdown(30일 스냅샷 피크) 정의 차이는 메타 `obs_definitions` 에 명시만 함
