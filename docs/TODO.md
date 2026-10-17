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
- [ ] rules·stance·costs (TDD)
- [ ] 원장 테이블 추가 (마이그레이션): analyst_reports·debate_turns·decisions·risk_checks·orders·fills·positions_snapshot·approvals
- [ ] executor (auto/approve/off) + `submit-decision` 게이트·주문 큐 연결
- [ ] KisBroker 주문·취소·체결, 모의 주문
- [ ] 승인 흐름 (텔레그램 수신: /approve /reject /pause /resume /mode /status)
- [ ] 결정 분포 메트릭
- [ ] 완료 기준: 모의 자동 체결 + 실전 off 기록, 한도·회전율 초과 거부 테스트, 실전 계좌 off 전용 테스트

## Phase 5 웹 UI
- [ ] FastAPI 라우터 (accounts·portfolio·runs·orders·charts·ml·control·system) + SSE + bearer 토큰
- [ ] React 앱: 전체 대시보드·계좌 상세·계좌 비교
- [ ] 오늘의 실행·리포트(토론) 뷰어
- [ ] 주문/승인·차트(Lightweight Charts, 마커)·데이터 탐색
- [ ] 설정/제어 페이지
- [ ] 완료 기준: 계좌 전환, 산출물 열람, 웹 승인 주문, 차트 마커, 모의 주문이 실전 뷰에 안 섞임, 토큰 없는 제어 401

## Phase 6 scan·monitor·데몬
- [ ] 정량 스크리너 + screener 세션 (haiku)
- [ ] monitor·트리거·`--resume` 재평가·손절 즉시 매도
- [ ] daemon (APScheduler + web 동시 실행, 단일 인스턴스 잠금, 휴장일 건너뜀)
- [ ] install (launchd / Windows 작업 스케줄러, 파이썬 템플릿)
- [ ] YouTube 자막·텔레그램 채널 수집, manual_inputs, UI 실시간
- [ ] KRX 공매도·대차잔고, 검색트렌드 (Phase 2 이연)
- [ ] 토스증권·네이버 종목토론실 API 조사 후 구현 여부 결정 (Phase 2 이연)
- [ ] 7일 이벤트 캘린더 소스 확정 (Phase 2 이연, 현재 `events` 없음)
- [ ] Reddit 429 대응: OAuth API 전환 + User-Agent 를 Reddit 형식(`<platform>:<app>:<ver> (by /u/<user>)`)으로 (Phase 2 이연)
- [ ] 커뮤니티 키워드 매칭을 ASCII 티커는 단어 경계로 (짧은 티커 과잉 매칭, Phase 2 이연)
- [ ] 공시 조회 종료일을 KST 기준으로, `collect` 가 기존 파일 건너뜀을 표시, 미래 asof 거부 (Phase 2 이연)
- [ ] 완료 기준: 평일 자동 1주 무사고, 트리거→재평가→계좌별 주문, Windows 에서 install 후 로그온 시 데몬 기동 확인

## Phase 7 ML 1단계
- [ ] features/dataset/labels (라벨 엠바고)
- [ ] 피처 스토어 news_count·community_count 집계, US 연도별 비율(EDGAR), 동종업 중앙값 대비 비율 (Phase 2 이연)
- [ ] quant 모델 + ranker (LightGBM)
- [ ] 워크포워드 비중첩 평가 + 라벨 누출 역검증 테스트
- [ ] registry
- [ ] 05_quant.json 투입, scan 랭킹
- [ ] UI ML 페이지
- [ ] 완료 기준: 승격 기준 통과 모델이 파이프라인 반영, UI 에서 성능·피처 중요도 확인

## Phase 8 학습·평가·보정
- [ ] memory 3층·엠바고·서킷브레이커
- [ ] reflector md + `reflect` 명령
- [ ] calibration 모델 (결정 60건 미만이면 비활성)
- [ ] 성과 페이지 (수익률·MDD·Sortino·벤치마크·비용 민감도·회전율·결정 분포·수렴율)
- [ ] 간이 백테스트 (컷오프 이후만)
- [ ] 실전 approve 전환 절차 (사용자 수동)
- [ ] 완료 기준: 모의 4주 이상 성과 리포트, 보정 모델 활성, 사용자 판단으로 실전 approve

## Phase 9 RL 사이징(실험)
- [ ] gym env (상태·행동·드로다운 페널티)
- [ ] PPO 워크포워드, 시드 3회
- [ ] 모의 계좌 한정 섀도 적용
- [ ] UI 실험 로그
- [ ] 완료 기준: 8주 워크포워드 비교 리포트, 승격은 수동
