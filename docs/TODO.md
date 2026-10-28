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
- [x] 완료 기준: Windows 에서 `uv sync` 후 `uv run trader status` 동일 동작 (2026-10-22 GitHub Actions windows-latest: `uv sync`·pytest·번들 `trader.exe status` 스모크 통과로 확인)

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
- [x] 네이버 검색 API 401 해결(2026-10-20): 원인은 키가 아니라 **API 이관** — 네이버 검색 API 가 NAVER API HUB(네이버 클라우드)로 옮겨가 호스트 `naverapihub.apigw.ntruss.com/search/v1/news`·헤더 `X-NCP-APIGW-API-KEY-ID/-KEY` 를 써야 한다. `sources/news.py` 전환 후 `collect --force` 오류 절 0건. `secrets/.env` 의 `NAVER_CLIENT_ID/SECRET` 는 Stock 의 HUB 키(`trader secrets import` 로 복사). 개발자센터 방식은 2027-06 종료
- [x] DART 점검 종료 확인(2026-10-20, `collect --force` 재무 절 정상) → `features backfill --years 3` 실행 (결과는 아래 ml train 항목)

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
- [x] 체크포인트 `call.model` 이 LLM 쓰기에선 None → `tool checkpoint write --model` 로 기록 (Phase 3 이연, 이미 구현됨 — 2026-10-17 확인)
- [x] 세션 cwd 를 저장소 밖(tempfile)으로 옮겨 상위 CLAUDE.md 자동 탐색 가정 제거 (Phase 3 이연, Phase 6 에서 구현됨 — 2026-10-17 확인)
- [x] 리서처·트레이더 입력을 파이썬이 조립하는 `tool checkpoint bundle`(24KB 바이트 한도) 추가. 실기 3차($1.80)에서 전문 전달이 예산 $2 를 소진해 중단 → 예산 $5 + bundle 로 4차($0.71, 재개 경로)에서 트레이더 묶음 17.8KB 전달·결정 확인 (2026-10-09)
- [ ] 실기(사용자 확인 대기): 처음부터 끝까지 bundle 경로로 1회 실행해 리서처 라운드 0·1 묶음이 Agent 프롬프트에 전문으로 들어가는지 세션 기록으로 확인 (`uv run trader analyze --ticker 005930 --dry-run`, 체크포인트 비운 뒤, 약 $2.5)
- [x] 완료 기준: Windows 에서 FAKE_CLAUDE dry-run 동일 동작 (2026-10-22 Windows CI 의 FAKE analyze 테스트 통과로 확인; 실제 PC 더블클릭은 아래 번들 항목)
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
- [x] Windows: Phase 4 테스트 전체 통과 (2026-10-22 GitHub Actions windows-latest pytest 914 passed)
- [x] 리뷰 Minor 이연(Phase 4) 회수(2026-10-17, 이연 회수 플랜): metrics KST 창·telegram_offset 전진·KR 호가단위(`kis order-test`)는 이미 해결. 이연 유지: approvals.decision_id 0(결정 없는 승인은 손절 경로뿐), 승인 시점 게이트 재평가(설계상 30분 내 변화 감수)

## Phase 5 웹 UI
- [x] FastAPI 라우터 (accounts·portfolio·runs·orders·charts·ml(자리)·control·system) + SSE(`/api/events`, 원장 카운터 폴링) + bearer 토큰 (`WEB_TOKEN` 미설정이면 제어 API 401)
- [x] React 앱(Vite 8 + React 19 + TS, `web/`): 전체 대시보드·계좌 상세·계좌 비교
- [x] 오늘의 실행·리포트(토론) 뷰어 (체크포인트 JSON 트리, 토론은 논거·반박 분리)
- [x] 주문/승인·차트(Lightweight Charts 5, 결정·주문 마커)·데이터 탐색(00_inputs; 피처 parquet 는 Phase 7) (웹 승인은 Phase 4 의 `approvals` 행·`executor.approve` 를 그대로 쓴다)
- [x] 설정/제어 페이지 (토큰 저장·일시정지·계좌 모드, 실전 auto 는 LIVE 확인)
- [x] 완료 기준: 계좌 전환, 산출물 열람, 웹 승인 주문, 차트 마커, 모의 주문이 실전 뷰에 안 섞임, 토큰 없는 제어 401 (2026-10-09 확인: TestClient 27개 — 계좌 분리·401·승인 2회 409·실전 auto confirm·경로 탈출; Vitest 5개; 브라우저 실기 — 대시보드·오늘의 실행(000660 토론 뷰)·차트(005930 KIS 일봉)·계좌·주문 페이지 렌더, 틀린 토큰으로 일시정지 → "토큰이 필요합니다" 표시. 웹 승인 주문 실기는 Phase 4 의 승인 실기(사용자 확인 대기)와 함께)
- [ ] 사용자 확인 대기: `secrets/.env` 에 `WEB_TOKEN` 설정 후 `uv run trader web` → 설정 페이지에 토큰 입력 → 일시정지/재개가 반영되는지, 승인 요청이 있을 때 주문/승인 페이지에서 승인 → 주문 접수 확인
- [x] 리뷰 Minor 이연(Phase 5) 회수(2026-10-17, 이연 회수 플랜): `_validate_ids` 점 티커 거부(이미 해결), `favicon`(inline SVG). `list_decisions` 창은 metrics 와 같이 KST 날짜 기준
- [x] Phase 5 이연 회수(2026-10-17, 이연 회수 플랜): SSE 스냅샷에 `triggers`·`watchlist` 추가, 관심종목 웹 제어(`/api/control/watchlist` + 설정 페이지), 차트 SMA20/60 오버레이, ML 페이지 실데이터(Phase 7)·성과 페이지(Phase 8). 분봉 차트는 아래 줄
- [x] 분봉 차트(2026-10-17, 사용자 결정으로 진행): `GET /api/charts/minute`(당일 1분봉 UTC epoch 초·60초 캐시·주문/트리거 분 마커, KR 은 KIS 30봉 역방향 페이징 최대 390), 차트 페이지 간격 선택(일봉|1분). 이연 유지: US 120봉 초과 페이징(`NEXT`/`KEYB`, 실기 확인), 3·5·15분 집계·자동 갱신(사용자 결정)
- [x] 리뷰 이연(2026-10-17 전체 리뷰): 계좌 성향을 시장별로 — 웹 `/api/accounts`·STATUS 가 `stance_by_market` (2026-10-18 처리, 수집기 `httpx.Client` 정리·`ml/rl` 포맷도 같은 날)
- [x] 리뷰 Minor 이연(분봉 차트): KR 분봉 페이지 수 상한(`MINUTE_MAX_PAGES`), `stck_cntg_hour` 빈 봉 제외, 날짜 혼합 응답 시 전일 봉 제거 (2026-10-18 처리)

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
- [x] Phase 6 이연 회수(2026-10-17, 이연 회수 플랜): UI push 카운터·관심종목 웹 제어·차트 지표 오버레이, US 휴장일은 NYSE 규칙 계산(`daemon.us_holidays`, 연도 제한 없음). 이연 유지(사용자 결정): KRX 공매도·대차잔고(API 약관·키), 네이버 검색트렌드(데이터랩 키)
- [ ] 사용자 확인 대기(실기): US 체결 조회(`fills sync`)의 날짜 기준(ET vs KST)·`_local_dt(ET)` 가정, `OVRS_EXCG_CD=NASD` 고정으로 NYS/AMS 체결이 조회되는지 — US 모의 체결 1건 뒤 `uv run trader fills sync --account paper-main --date <ET 날짜>` 로 확인
- [x] 리뷰 Minor 이연(Phase 6) 회수(2026-10-17, 이연 회수 플랜): 데몬 기동 시 1시간 넘은 running 세션을 error 로 정리, quant 는 신호 결측 행 제외, `features.latest_rows` 가 최신 파일부터 부분 읽기. 이연 유지: `pid_alive` PermissionError(POSIX 의미상 생존이 맞음)

## Phase 7 ML 1단계
- [x] `ml.yaml` 로더(`MlConfig`), `ml/dataset`(적재·5거래일 3분류 라벨·피처 열), 라벨 엠바고
- [x] 피처 스토어 news_count·community_count 집계(같은 날 00_inputs 에서) (Phase 2 이연). US 연도별 비율(EDGAR 과거 연도)·동종업 중앙값 대비 비율은 데이터 없음 → Phase 8
- [x] quant 모델(GBDT 3분류: `lightgbm` import 가능하면 LGBM, 아니면 sklearn HistGradientBoosting) + ranker(P(up)·0.6 + 정량·0.4)
- [x] 워크포워드 비중첩 폴드(`ml/walkforward`, 엠바고 gap 검사) + 라벨 누출 역검증(라벨 섞기 → 우연 수준, 타깃 열 피처 금지)
- [x] registry(`state/models/quant/<버전>/model.joblib+meta.json`, 승격 플래그)
- [x] 05_quant.json 투입(`tool collect`·`analyze`·데몬 뒤 `tools.ensure_quant`, 트레이더 `context.quant`), scan ML 랭킹(`ml_model` 표시)
- [x] UI ML 페이지(모델 목록·폴드 성능·permutation 중요도) + `/api/ml/models[/{version}]` + `trader ml train|models|promote` + 데몬 `ml_train` 잡 + STATUS "ML 모델" 절
- [x] 완료 기준(실기 2026-10-17): `uv run trader ml train --market KR` → 4,940행·6폴드 정확도 0.315 / 기준선 0.465 / 알파 −0.003 → **미승격**(승격 규칙: 정확도 > 기준선 ∧ ≥ 0.40 ∧ 알파 > 0 ∧ 폴드별 up 예측 ≥ 5건. 잡음 데이터 4 seed 전부 미승격 테스트 확인. 파이프라인은 ML 없이 돈다). 합성 신호 데이터에서는 승격·05_quant·랭킹 전부 테스트로 확인. UI 성능·중요도는 `/api/ml/models` 응답으로 확인
- [x] (Phase 12 에서 `backend: auto` 로 전환, libomp 설치는 사용자) LightGBM 을 쓰려면 `brew install libomp` → `uv sync --extra lightgbm` → `config/ml.yaml` 의 `models.quant.backend: lightgbm`(기본은 플랫폼 동일 동작을 위해 `sklearn`)
- [x] Phase 7 이연(리뷰, Phase 8 회수): close 대비 비율 피처 `sma_5/20/60_rel·bb_pos·atr_pct·macd_hist_rel` 추가(옛 parquet 행은 NaN → `features backfill` 재실행 후 `ml train`). 중요도는 마지막 폴드 홀드아웃 기준
- [x] Phase 7 이연: `features snapshot` 은 데몬 reflect 잡 앞에서 매일 실행(`DaemonDeps.snapshot_fn`, 실패는 통보). 재학습은 사용자 실기(`features backfill --years 3` → `ml train`)
- [x] 3년 백필 후 `ml train` 재실행(2026-10-20): `features backfill --years 3` → 피처 파일 802일(2023-09~2026-10), 21,924+7,560행. KR 정확도 0.343 / 기준선 0.354 / 알파 +0.0023, US 0.385 / 0.399 / +0.0020 → **둘 다 미승격**(정확도 < 0.40, 기준선 이하). 승격 전까지 05_quant·ML 랭킹은 비활성 유지. 백필 중 EGW00201 1회 재시도로 종목이 빠져 재시도를 3회 백오프로 보강, WMT 는 KIS 가 NAS 코드로만 줘 유니버스 수정
- [x] 리뷰 Minor 이연(이연 회수 플랜): stale running 정리는 상태 표시·오류율 정확성용(세션 상한 `sessions_today` 는 created_at 기준이라 그대로), 신호 결측으로 제외된 종목이 `candidates` 에서 "피처 없음" 으로 표시, `scan --asof` 가 `latest_features` 에 전달되지 않음(과거 asof 스캔은 최신 파일) → 2026-10-20 전부 처리(후속 플랜 Task F: stale running 매일 정리, '피처는 있으나 필터·신호 결측으로 제외' 표시, `scan --asof` 를 `quant.candidates(asof=)` 로 전달)
- [x] (Phase 12 구현) Phase 7 이연: US 연도별 재무비율·동종업 중앙값 비율(데이터 설계 필요), 피처 행 탐색 UI, 하이퍼파라미터 탐색

## Phase 8 학습·평가·보정
- [x] memory 3층·엠바고·서킷브레이커 (`memory_entries` v5, 12차원 상황 벡터, 반감기 14/90/365, 워밍업 50, `control.memory_breaker`)
- [x] reflector md + `reflect` 명령 (`outcomes.mature` → reflector 세션 `kind=reflect` → `_reflect.json`·`reflection`, 데몬 잡)
- [x] calibration 모델 (결정 60건 미만이면 비활성, `35_calibration` 은 활성일 때만 생성, `calibration build`)
- [x] 성과 페이지 (수익률·MDD·Sortino·벤치마크 1/N·B&H·퀀트·비용 민감도·회전율·결정 분포·수렴율) + `GET /api/performance` + STATUS "성과(30일)"
- [x] 간이 백테스트 (컷오프 이후만, `backtest --since`)
- [x] 실전 approve 전환 절차 (사용자 수동): `docs/runbook-live-transition.md` + `trader readiness`(읽기 전용) + `trader backtest --since`
- [x] 실기: `FAKE_CLAUDE=1 analyze --ticker 000660 --dry-run` → `memory_entries` 1행(워밍업·`35_calibration` 없음), `reflect --dry-run` 0건, `backtest --since 2026-10-01` n=0, `readiness` 불합격 항목 표시·모드 불변, `/api/performance` 200 (2026-10-17 확인)
- [ ] 완료 기준(사용자 확인 대기): 모의 4주 이상 성과 리포트(웹 `/performance`·`trader readiness` 통과), 보정 모델 활성(만기 결정 60건), 사용자 판단으로 실전 approve(`docs/runbook-live-transition.md`)
- [x] 파생 비율 피처 반영 재학습(2026-10-20): 위 항목과 같은 실행 — op_margin·roe·debt_ratio 가 KR 피처에 포함됐으나 기준선 미달. 다음 시도는 유니버스 확대 또는 라벨·피처 설계 변경이 필요(사용자 결정)
- [x] 리뷰 Minor 이연(Phase 8) 회수(2026-10-17, 이연 회수 플랜): `decided_at` 운영일 규칙(오늘이면 now, 아니면 15:30 KST), 상황 벡터·05_quant 는 run date 이하 피처 행, `cost_sensitivity`·`nav_history` KST 날짜 창, `outcomes.mature(today)` 로 만기 봉 날짜 제한, 백테스트 HOLD 적중 = |가격수익| ≤ 2%, `/watch` 사용법(이미), 관심종목 시장 구분(`KR:`/`US:` 접두사·코드 형식 추정), `submit_*` 디렉터리는 `run_store.market_dir`, `calibration_runs` 하루 1행, `sma_*_rel` 부호 통일(close/sma−1), `record_decision(decision_id=None)` 거부, reflect 잡 시장별 격리, `run_error_rate` 분모에서 interval 잡 제외

## Phase 9 RL 사이징(실험)
- [x] gym env (`ml/rl/env.SizingEnv`: 관측 16차원 = 상황 벡터 12 + 확신·LLM 비중 + 현재 비중·드로다운, 행동 0~0.25, 보상 = w·r − 회전 비용 − 드로다운 페널티; gymnasium 어댑터는 선택 extra)
- [x] 워크포워드 × 시드 3회 학습·기준선(고정 10%·LLM 비중·변동성 타깃) 비교 (`rl train`, 폴드·엠바고는 quant 와 동일). 백엔드는 `cem`(numpy, 기본) / `ppo`(stable-baselines3, `uv sync --extra rl`)
- [x] 모의 계좌 한정 섀도/적용 (`ml.yaml rl_sizer.enabled` ∧ `rl promote` 수동 승격 → executor 가 **모의 계좌·BUY/ADD** 에만 `rl_shadow` 기록, `mode: apply` 면 target_weight 교체·게이트 상한은 그대로. 실전 계좌는 기록도 없음)
- [x] UI 실험 로그 (ML 페이지 "RL 실험" 절, `/api/rl/experiments|shadow|report`, STATUS `rl_sizer` 줄, 데몬 ml_train 이 enabled 일 때 함께 학습, `rl report`)
- [x] 실기(2026-10-17): `rl train --market KR --seeds 1`(피처 스토어 4,940 스텝·6폴드, CEM) → 테스트 창 평균 수익 RL −0.13% / 고정 10% +0.33% / 변동성 타깃 +0.89%, 정책은 변동성에 반응(atr 1% → 비중 0.20, 5% → 0.006) → **미승격**(1시드·1년 데이터로는 기준선 미달, 승격은 수동) → `rl report` → `FAKE_CLAUDE=1 analyze --dry-run`(HOLD·dry-run 이라 `rl_shadow` 0행, 모드 불변) → `/api/rl/*` 200 → STATUS 줄. 리뷰 전 버전 `20261017_063439` 는 정규화 전 수치(수익 2673%)라 무시
- [ ] 완료 기준(사용자 확인 대기): 8주 워크포워드 비교 리포트 — 모의 운영 8주 뒤 `uv run trader rl train` → `rl report --account paper-main --weeks 8` 로 RL vs 고정·LLM 비교, 승격은 `rl promote <버전>` 수동(그 뒤 `ml.yaml rl_sizer.enabled: true`, 처음엔 `mode: shadow`)
- [x] (Phase 12: darwin x86_64 는 torch 2.2.2·SB3 2.3 핀) PPO 백엔드 — `uv sync --extra rl`(torch 휠 필요, 이 머신은 x86_64 Python 이라 설치가 안 될 수 있음) 후 `ml.yaml rl_sizer.backend: ppo` 로 `rl train`
- [x] Phase 9 이연 회수(2026-10-17, 이연 회수 플랜): 섀도 관측·quant 는 `latest_rows` 부분 읽기(학습 `load_all` 은 전체 필요). 이연 유지(사용자 결정): 멀티 종목 동시 포트폴리오 환경, RL 실험 전용 페이지
- [x] 리뷰 Minor 이연(Phase 9) 회수(2026-10-17, 이연 회수 플랜): `--seeds` 범위 1~10, `rl_shadow.applied` 는 게이트 승인 뒤 기록, 메모리 스텝 BUY/ADD 만, `/api/rl/report?market=` 검증, docstring·yaml 주석. 이연 유지: `uv.lock` torch(extra 전용), 학습/서빙 drawdown 정의(멀티 종목 환경과 함께)

## 2026-10-18 전체 코드 검토 수정 (`docs/superpowers/plans/2026-10-18-review-correctness-fixes.md`)
- [x] US 운영일 통일 (`run_store.run_date_for`: US 는 뉴욕 현지 날짜) — 데몬 scan/analyze/monitor·CLI monitor 가 쓰고 `today_buy_tickers/today_order_amount/trades_this_week` 가 시장 현지 하루 창 (KST 자정 이후 US 재평가가 `30_decision` 을 못 찾던 결함)
- [x] 리스크 규칙: `target_weight` 를 BUY/ADD/TRIM 모두 "주문 뒤 총 비중" 으로 통일(이미 목표 이상이면 ADD 0주 거부), BUY 수량은 지정가 기준, BUY/ADD 의 `stop_loss ≥ 현재가` 거부
- [x] `submit-decision` 멱등 분기: `30_decision` 만 있고 `40_risk_check_*` 가 없으면 실행을 재개(`resumed_execution`), 원장 연결 검사를 저장 앞으로
- [x] 텔레그램 오프셋이 걸러진(다른 채팅) 업데이트도 반영 (`TelegramReceiver.last_update_id`)
- [x] 체결↔주문 매칭을 같은 현지 날짜 주문으로 제한 (`repo.day_bounds`)
- [x] 주문 접수 뒤 장부 기록 실패 보호 (`OrderBookkeepingError`: CRITICAL + 접수번호 알림, `approve` 는 failed 로)
- [x] 같은 종류 트리거는 운영일 1회만 재평가(실패는 제외), 같은 틱 트리거는 세션 1회가 모두 처리
- [x] 검토 잔여(정확성) 회수(2026-10-18, `docs/superpowers/plans/2026-10-18-review-correctness-fixes-2.md`): `min_cash_pct` 유보 적용, `readiness` interval 잡 종류 정합, `/api/accounts`·STATUS·`control show` 실효 모드, `web --host` 비루프백 Host 제한 해제, `_technical`/수급 `asof` 컷오프 + `meta.last_candle_date`, `portfolio/refresh` `memory_settings`, `write_rows` 구스키마 reindex, `realized_pnl_5d` 묶음 평균·Decimal, `_ymd('')` 빈 일봉 행, 토큰 캐시 실패 기록 재읽기, `Lock` O_EXCL + 종료 시 `shutdown(wait=True)`, `run_session` 사전 단계 실패 시 runs error·cwd 정리. 이연 유지: 고아 `claude` 프로세스 killpg(세션을 프로세스 그룹으로 띄우는 변경 필요), `invalidate()` 가 다른 프로세스 토큰을 지우는 경우(Low)
- [x] 검토 잔여(성능) 회수(2026-10-18, `docs/superpowers/plans/2026-10-18-review-performance.md`): `cli` 지연 import(1.03→0.31 s), scan `latest_rows` 유니버스 전달(1.10→0.09 s)·`latest_row` 메모이즈, `KisBroker.get_balance`(KR 잔고 API 1회), `held_since(tickers=)`, `nav_history` SQL 창, `init_schema` 조기 반환, `outcomes.mature` `since/until`, SSE 연결 재사용·`agent_calls` 제외, `QuantModelCfg.max_iter`(테스트 3:35→2:34), `backfill` 10종목 묶음 쓰기, 수집 3절 스레드풀, 보정 JOIN, `memory.retrieve` 상위 k hydrate, CEM 벡터화, 차트 페이지 lazy(470→288 KB). 이연 유지: `performance.report` 의 퀀트 벤치마크 `latest_rows` 는 유니버스를 몰라 그대로(요청마다 60파일), `held_since` 증분 계산, 프로세스 간 KIS 레이트리밋 공유
- [x] 검토 잔여(구조) 회수(2026-10-18, `docs/superpowers/plans/2026-10-18-review-structure.md`): `config.KST`(24개 모듈 사본 제거), `logging_setup.quiet_httpx`, `broker/_parse`, `sources/base.strip_tags·utc_now`, `features.zscore`, `repo.day_bounds` 재사용, `run_store.market_dir·checkpoint_payload`, `costs.round_trip_bp`, `commands.approval_line`, `performance.metrics` 별칭·`calibration` COUNT 중복 제거; 죽은 코드(`has_unhandled_trigger`·`tools.ANALYST_ROLES`·`rank_fn_or_none`·`tr_ids.names`·`*_PSBL_ORDER`·`LlmSettings.max_parallel`·`claude_bin_path`) 삭제; `cli._config_errors` 데코레이터로 try/except 16곳 제거. 이연 유지: `cli.py` 부트스트랩(`configure`·`load_*`) 묶기, `SessionResult.num_turns` 활용, `views.get_decision`/`repo.latest_decision` hydrate 공유
- [x] 전수 리뷰 후속(2026-10-19, `docs/superpowers/plans/2026-10-19-review-followup-fixes.md`, main 포함 스킬 리뷰 2회 + 계층별 리뷰 4개, 35건): 손절선 ≥ 현재가 결정 제출 거부(모든 action), KIS 응답 미수신 `NETWORK_UNKNOWN` → `orders.status='unknown'`(fills sync 가 확정), 손절 경로 `OrderBookkeepingError` 격리·당일 재발주 차단, 체결된 TRIM 이 손절을 막지 않음, 손절은 결정 단위 중복 조건 미적용, `approve()` 실효 모드 재확인, 거부 주문 기록 실패 비승격; `Lock` os.link 원자 생성·시그널 재진입 무시·plist `ExitTimeOut`·`_scan` 부재 알림/error·공유 로그 회전 재오픈; US `collect/analyze/scan/tool collect/counts_from_inputs` 운영일 `run_date_for`, 실패 세션 `실패:` 표시, 오늘 결정 없으면 재평가 세션 생략, `Position.exchange`, `41_/51_` 재평가 체크포인트, 가격 조건 파싱, `held_since` 현지 날짜; 라벨 `fwd_date` 누출 필터, 퀀트 캐시 mtime, 백필 flush 보류, 스냅샷 유령 보유 삭제, 메모리 재평가 갱신, 실현손익 매수 기록 없는 SELL 제외, `risk/ticks` 호가단위, 유보 사유 1건, macro asof, 분봉 페이징, RL 스텝 KST, `TRADER_HOME` 절대경로, 차트 US 마커, `/status` 실효 모드, `_config_errors` 확대. 이연 유지(사용자 결정): `unknown` 주문 마감 뒤 자동 만료, scan 부재 시 analyze 인라인 scan, 로그 파일 프로세스별 분리
- [x] 전수 리뷰 후속 2차(2026-10-20, 같은 플랜 Task F): 체결 동기화가 마감 뒤 체결 없는 `accepted`·`unknown` 주문을 `expired` 로(`repo.DEAD_STATUSES` 정본), scan→analyze 단일 잡 `<시장>_scan_analyze`(scan 실패도 통보 뒤 analyze 시도, yaml `analyze` 키는 미사용), stale running 정리를 매일 scan_analyze 앞에서도, `scan --asof`·데몬 scan 이 `quant.candidates(asof=)` 로 운영일 이하 피처만, 신호 결측 종목을 "피처 없음" 과 구분 표시. 이연 유지(사용자 결정): 로그 파일 프로세스별 분리, 고아 `claude` 프로세스 killpg, 퀀트 벤치마크 `latest_rows` 유니버스, `cli.py` 부트스트랩 묶기
- [x] 후속 수정 독립 리뷰 + 리뷰 공백 반영(2026-10-20, 같은 플랜 Task G, 18건): 손절 dedupe 전 체결 1회 동기화, 데몬 종료 플래그로 남은 세션 중단, scan 실패 알림 1건, schedule `analyze` 키 선택·제거, 호가단위 정본 단일화, unknown 덧알림 제거, 재평가 현재가 = 트리거 가격, 트레이더 묶음 `previous_decision`, 섹션 `quote` 동봉, screener·reflector 런타임 스키마, 공통 규칙 접두 비교 10개 md, trader/analyst/orchestrator md 입력 설명 갱신, 웹 기본 날짜·refresh 운영일, Performance 시장 초기화 (`npm run typecheck`·`npm test` 통과)
- [x] 남은 보완 3건(2026-10-20, 같은 플랜 Task H): 세션을 프로세스 그룹으로 띄워 타임아웃 시 `killpg`(고아 tool 서브프로세스 방지), 퀀트 벤치마크 `latest_rows(tickers=유니버스)`, `trader web|telegram loop|monitor` 는 `trader-<name>.log` 로 분리. 이연 유지(사용자 결정, 동작 불변 정리): `cli.py` 부트스트랩 묶기, `SessionResult.num_turns` 활용, decision hydrate 공유
- [x] 순수 정리 3건(2026-10-20, 같은 플랜 Task I): `cli._boot` 부트스트랩 헬퍼(14개 명령), `agent_calls.num_turns`(v9), 결정 hydrate `repo._hydrate_decision` 공유. 이연 항목 없음(Phase 7 별도 Phase 만 남음)

## Phase 10 번들 배포 (`docs/superpowers/plans/2026-10-20-phase10-bundle.md`)
- [x] `platform/bundle.py`(frozen 루트·리소스·도구 명령·daemon argv·first_run_init), `paths.web_dist/agents_dir`
- [x] 세션 `--allowedTools`·프롬프트를 `bundle.tool_command()` 로 (번들은 `trader tool`), PATH 앞에 `tool_bin_dir`
- [x] 인스톨러 argv 등록(`[exe, daemon]`), `trader init`·`trader app`(더블클릭 진입), `templates/`
- [x] `packaging/trader.spec`·`packaging/build.py`·`.github/workflows/build.yml`; macOS 로컬 빌드·스모크 통과(2026-10-20, 268MB/zip 144MB; frozen 에서 `--help`·`init`·`status`·`tool status`·`web --help`·`control show` 정상)
- [x] `docs/bundle-usage.md`, CLAUDE.md 규칙
- [x] `v*` 태그 푸시 → GitHub Actions 가 번들을 릴리스에 첨부 (2026-10-22 v0.1.3: macOS arm64·**x86_64(Intel, macos-15-intel 추가)**·Windows 3종. v0.1.0~0.1.2 는 Windows 경로·cp1252 인코딩·로그 회전 문제로 Windows zip 이 빠졌었음)
- [ ] 실기(사용자 확인 대기): v0.1.3 zip 을 풀어 더블클릭 → 웹 UI → `kis ping` (macOS 우클릭 열기·Windows SmartScreen 1회). Intel Mac 은 `trader-<버전>-macos-x86_64.zip`(v0.1.3 까지는 `trader-darwin-x86_64.zip`)
- 범위 조정: 코드 서명·공증(개인 사용), 트레이 앱, lightgbm·torch 번들, 자동 업데이트는 제외(플랜 표)

## Phase 11 ML 2단계 (`docs/superpowers/plans/2026-10-20-phase11-ml2.md`)
- [x] `label.mode: relative_tercile`(같은 날·시장 안 순위 삼분위), `promote_metric: alpha`(상위 5 알파 `alpha_topn` 폴드 평균 ≥ 0.003 ∧ 양인 폴드 ≥ 0.66 ∧ up 예측), `ml train --horizon`(1~60 검증), `05_quant.label_mode`
- [x] `trader universe check`(KIS 시세 검증·US 거래소 제안), 유니버스 KR 20→199(KOSPI 200)·US 10→51, screener 폴백 score None 제외, 개장 전 거래량 0 스텁 봉 제외
- [x] 백필 187,675행(2026-10-20, 실패 0) → 재학습 결과(상대 라벨·알파 기준):
  - KR h5: 정확도 0.376 / 기준선 0.336 / 알파 +0.0012 / 양 폴드 3/6 → 미승격
  - KR h10: 정확도 0.375 / 기준선 0.336 / 알파 +0.0028 / 양 폴드 5/6 → 미승격(평균 알파 0.0028 < 0.003, 근접)
  - US h5: 0.348 / 0.333 / −0.0000 / 3/6, US h10: 0.347 / 0.333 / +0.0003 / 3/6 → 미승격(신호 없음)
  - 리뷰 반영 뒤 승격 지표를 랭커와 같은 **상위 5 알파(alpha_topn)** 로 바꿔 재측정: KR h5 alpha_topn −0.0002(양 폴드 4/6), KR h10 −0.0019(4/6, 폴드별 −0.025~+0.022) → 미승격
  - 해석: 정확도는 기준선(1/3)을 4pp 넘고 삼분위 스프레드(풀링 알파)는 양이지만, **상위 5 선택은 잡음**(폴드 간 부호가 바뀜). 즉 '상위 1/3 vs 하위 1/3' 수준의 약한 신호는 있으나 상위 5 랭킹엔 못 쓴다. 기준은 그대로 두고 매주 ml_train 재평가. US 는 신호 없음(비율 피처 부재)
- [x] 사용자 결정(2026-10-20): `horizon_days` 5 유지(추천대로), US 비율 피처·Phase 7 이연·백엔드는 Phase 12 로 진행. 원문: 기본 `horizon_days` 를 10 으로 바꿀지(풀링 알파로는 h10 이 5/6 로 보였지만 상위 5 알파로는 4/6·음수라 근거가 약하다. 바꾸면 라벨·엠바고·보정 창이 함께 바뀜) / US 비율 피처(EDGAR 연도별) 추가 여부

## Phase 12 ML 3단계 (`docs/superpowers/plans/2026-10-20-phase12-ml3.md`)
- [x] US 연도별 재무비율(EDGAR companyfacts, 사업연도 Y 는 Y+1 년 4월 1일 제출분까지) → 피처 스토어 US 행 `op_margin·roe·debt_ratio`
- [x] 동종업 중앙값 상대 비율 `*_vs_sector`(`config/sectors.yaml` KR 199·US 51 → 11개 대분류, 3종목 미만 NaN, write_rows 전체 재계산·멱등), `features derive`, `00_inputs.fundamental.peer`(v3)
- [x] `ml tune`(dev 폴드 선택·홀드아웃 보고·자동 적용 없음), `QuantModelCfg.learning_rate/num_leaves`
- [x] 웹 `/api/features/latest|history` + Data 페이지 피처 행 탐색(정렬·종목 이력)
- [x] 백엔드: `ml.yaml backend: auto`(quant·rl_sizer), `rl` extra 플랫폼 핀(darwin x86_64 torch 2.2.2·SB3 2.3), `selfcheck.optional`
- [x] 실기(2026-10-20): US 백필 38,489행(비율 50/51 종목 채움) → `features derive` 802파일 → 스냅샷 199행. 리뷰 반영(early_stopping=False) 뒤 재학습:
  - US h5: 정확도 0.360 / 기준선 0.333 / 상위 5 알파 +0.0033 / 양 폴드 6/6 → 공통 기준 0.003 으로는 승격(20261020_090042)됐으나 **US 기준을 왕복 50bp(0.005)로 분리(`promote_min_alpha_by_market`)하면서 해제**. 비율·`*_vs_sector` 피처가 들어가며 US 가 처음으로 양 폴드 6/6 — 다음 주간 재학습에서 0.005 를 넘으면 자동 승격
  - KR h5: 정확도 0.377 / 기준선 0.336 / 상위 5 알파 −0.0018 / 양 폴드 2/6 → 미승격 (early_stopping=auto 였던 직전 실행은 +0.0004·3/6)
  - `ml tune --market KR`: dev 최선 {lr 0.03, leaves 7, iter 100} alpha_topn +0.0050(5폴드 중 4 양) 이지만 홀드아웃 −0.0005(기본값 −0.0084) → 적용 권고 없음. `--market US`: dev 최선 같은 조합 +0.0052, 홀드아웃 +0.0035 < 기본값 +0.0080 → 적용 권고 없음(기본값 유지)
  - `rl train`: auto → cem(Intel macOS 는 torch↔NumPy 불일치), 186,425 스텝, RL 0.0015 vs 고정 0.0113·변동성 타깃 0.0289 → 미승격(수동 승격 없음)
- [ ] 사용자 실행(macOS LightGBM): `sudo chown -R $(whoami) /usr/local/share/man/man8 && chmod u+w /usr/local/share/man/man8` 뒤 `brew install libomp` → `uv run trader selfcheck` 의 `optional.lightgbm: true` 확인 → 다음 `ml train` 부터 lightgbm 백엔드(모델 메타 `backend`)

## 2026-10-20 정리(Phase 12 보류 항목 회수, 추천대로 진행)
- [x] US 승격 알파 기준 0.005(왕복 50bp) — `promote_min_alpha_by_market: {US: 0.005}`, `min_alpha_for(market)`; 승격돼 있던 US 모델 20261020_090042 해제, 재학습 결과는 Phase 12 절
- [x] 백필 묶음 쓰기는 파생 열 없이(`write_rows(derive=False)`), 끝에 건드린 날짜 파일만 `rederive(paths=)` 1회
- [x] lightgbm 모델 로드 실패 경고에 원인·대처(demote / sklearn 재학습) 안내
- [x] `fundamental.peer`·`sector_medians` 에 `dates{min,max}` (스냅샷 빠진 종목의 옛 날짜 혼입을 드러냄)
- [x] scan 제외 사유 분리(`quant.drop_reasons`: 필터 탈락·신호 결측·피처 없음·상위 N 밖), `universe check` 일시 오류(`TRANSIENT_CODES`) 구분(Phase 11 보류분)

## Phase 13 US 재무 history·섹터 자동 분류 (`docs/superpowers/plans/2026-10-20-phase13-us-history-sectors.md`)
- [x] US `fundamental.history`: EDGAR 10-K 3개년 `years` + 최신 10-K 뒤 최근 10-Q `latest_quarter`(asof 이전 제출분만), companyfacts 메모. 실기 AAPL: years 2023~2025, 10-Q end 2026-06-27(filed 07-31)
- [x] `trader universe sectors --market KR|US [--all] [--write]`: KIS 업종(KR `bstp_kor_isnm`, US `e_icod` — 둘 다 한글 업종명)을 `kr_industry_to_sector`/`us_industry_to_sector` 표로 바꿔 빠진 종목만 제안, `--write` 는 `kr:`/`us:` 블록 끝에 덧붙임(기존 항목·주석 불변). tr_id `OVS_PRICE_DETAIL`
- [x] 실기(2026-10-20): KR 199·US 51 전체 조회 → 표에 없던 업종 보완(KR 일반서비스→기타·의료·정밀기기, US 는 코드가 아니라 업종명으로 와 표를 업종명으로 재작성). 수동 매핑과 KRX 업종이 다른 KR 41종목(지주사 '금융', 2차전지 '전기·전자' 등)은 의도된 수동 판단이라 유지. US 3종목(HON·LIN·WMT)은 KIS 가 업종을 주지 않음(수동 항목 있음)
- [x] 보류 항목 회수(2026-10-20): EDGAR 정정 공시(10-K/A·10-Q/A)는 같은 결산일 원본을 덮음(asof 까지 제출분만), 상대 라벨은 market 결측 행을 조용히 버리지 않고 ValueError, `universe sectors --write` 는 templates 를 KIS 제안이 아닌 config 값으로 동기화(`sector_suggest.sync`, config 에만 있던 수동 항목 포함)

## Phase 14 웹 UI 재설계 (`docs/superpowers/plans/2026-10-23-phase14-web-redesign.md`)
- [x] impeccable init: `PRODUCT.md` (2026-10-23)
- [x] 방향 선택: 재굴림 1 → "신문 증권면 시세표" 배정, 사용자 선택 (2026-10-23). 방향 계약 `.impeccable/surfaces/web-src-app-tsx.md`
- [x] Task 1 토큰·베이스·Pretendard·테마
- [x] Task 2 아이콘·표 원시요소·공통 컴포넌트·용어 사전
- [x] Task 3 셸(상단 바·사이드 내비·서랍·테마)·App·SSE 타입 일치
- [x] Task 4 차트 헬퍼 chart.ts·팔레트 토큰화·차트/성과 페이지
- [x] Task 5 대시보드(운영 상태 줄·오늘의 시세표·주문/승인·성과 블록)
- [x] Task 6 오늘의 실행 파이프라인 판·데이터
- [x] Task 7 주문/승인·계좌·계좌 비교
- [x] Task 8 ML·설정/제어
- [x] Task 9 검증(1280·390 캡처, 라이트/다크 실시간 점검)·detect 0건·finish reviewer 3라운드(fix→fix→ship)·documenter(DESIGN.md·design.json)·문서
- [x] 완료 기준: `npm run typecheck && npm test && npm run build`·`uv run pytest`·`ruff` 통과, 10페이지가 방향 계약대로 라이트/다크·390px 에서 동작, finish reviewer disposition ship, `DESIGN.md` 존재 (2026-10-23 확인)
- [ ] 사용자 확인 대기: 브라우저에서 10페이지를 직접 보고 어색한 곳 피드백 → `/impeccable polish` 또는 `critique` 로 다듬기. 리뷰어가 캡처하지 못한 것: 다크 모드 전체 페이지, 차트 페이지(KIS 키 필요)

## Phase 15 번들 형식 정비 (`docs/superpowers/plans/2026-10-23-phase15-bundle-form.md`)
- [x] Task 1 `--version`·zip 이름에 버전·README 동봉·심링크 보존·CI 태그↔버전 검사 (2026-10-23)
- [x] Task 2 앱 아이콘(`packaging/icons/`)·창 없는 `TraderApp` 실행파일 + 콘솔 CLI `trader` (`bundle.cli_executable`)
- [x] Task 3 macOS `Trader.app` — TRADER_HOME 은 `.app` 옆, CLI 심링크, 배포 폴더 조립·스모크
- [x] Task 4 트레이 메뉴 모델·pystray 어댑터 (`platform/tray.py`) (Phase 10 이연 회수)
- [x] Task 5 `trader app --tray` — 데몬 스레드 + 메인 스레드 시그널/트레이, 종료가 세션을 기다린 뒤 잠금 해제
- [x] Task 6 문서·CLAUDE.md·TODO·로컬 실빌드 (2026-10-23: `dist/trader/{Trader.app, trader→, README.md}`, `trader-0.1.3-macos-x86_64.zip` 심링크 405개 보존·309MB, 스모크 통과)
- [x] 완료 기준(코드): `uv run pytest` 937건·`ruff` 통과, 로컬 `build.py` 로 `dist/trader/{Trader.app, trader→, README.md}` + `trader-0.1.3-macos-x86_64.zip`(심링크 보존) (2026-10-23 확인)
- [ ] 실기(사용자 확인 대기): zip 풀고 `Trader.app` 더블클릭 → 메뉴바 ▲ 아이콘·브라우저 → 메뉴에서 일시정지/재개·로그 폴더·종료(실행 중 세션 대기 후 잠금 해제). 우클릭 열기 1회 필요. Windows 는 CI zip 으로 `TraderApp.exe` 작업표시줄 아이콘·콘솔 창 없음 확인. 릴리스 태그는 `pyproject` 버전과 같아야 CI 가 통과(v0.1.4 는 Windows 테스트 파일명 버그로 macOS zip 만 첨부됨 → v0.1.5 로 재빌드)

## Phase 16 운영 가시성 (`docs/superpowers/plans/2026-10-23-phase16-ops-visibility.md`)
- [x] Task 1 종목명 — 원장 `ticker_names`(잔고·00_inputs 관측 시 upsert) + `names.py`(캐시 JSON 폴백, 네트워크 없음) + 알림·STATUS·텔레그램 '이름 코드' 표기 + `/api/names` + 프런트 `Ticker` (2026-10-23)
- [x] Task 2 Claude 사용량 — 상태줄 관측 파일(`llm.usage_file`, `~/.claude/statusline-command.sh` 1줄) + `agent_calls` 5시간·7일 창 + `/api/system/usage` + 대시보드 블록
- [x] Task 3 데몬 하트비트 — `heartbeat.py`(`state/daemon_heartbeat.json`) + `run_job`·APScheduler 리스너 + `/api/system/daemon` + 대시보드 데몬 상태·잡 표 + 텔레그램 `/status` 한 줄
- [x] Task 4 '작동 방식' 페이지(`/how`) + 오늘의 실행 단계 링크
- [x] 완료 기준(코드): `uv run pytest`·`ruff`·`npm run typecheck && npm test && npm run build` 통과, Chrome 으로 대시보드·오늘·주문·/how 확인 (2026-10-23 확인)
- [ ] 사용자 확인 대기: 데몬을 켠 상태(`trader daemon` 또는 Trader.app)에서 대시보드 상태줄이 '데몬 실행 중 · n시간째' 로 바뀌고 잡 표의 마지막 실행·다음 예정이 채워지는지, 텔레그램 `/status` 에 "데몬 pid … 최근 잡:" 줄이 오는지
- [ ] 사용자 확인 대기: 터미널에서 대화형 `claude` 를 한 번 연 뒤 대시보드 'Claude 사용량' 블록에 5시간·7일 사용률이 뜨는지(`~/.claude/rate_limits.json` 생성). 값은 대화형 세션이 상태줄을 그릴 때만 갱신된다

## Phase 17 작업 대기열·하락장 대응 (`docs/superpowers/plans/2026-10-24-phase17-job-queue.md`)
- [x] Task 1 작업 대기열 모듈 `jobs.py` + 원장 v11 `job_requests`(kind·params pydantic 검증, 중복 거부, 조건부 claim, stale·만료 정리) (2026-10-24)
- [x] Task 2 데몬 `job_queue` interval 잡(10초, 1건)·`heavy_lock`(cron 은 대기, 큐는 다음 틱)·`_analyze_targets` 분리·RL 주간 학습 enabled 무관·기동 부트스트랩(피처 스토어 비면 백필+학습)·MAX_INSTANCES 예외
- [x] Task 3 CLI `_daemon_features_fn`(backfill|snapshot|derive) → `DaemonDeps.features_fn`
- [x] Task 4 `GET /api/jobs`·`POST /api/control/jobs`(데몬 꺼짐 409·중복 409)·`/cancel`, SSE 스냅샷 `job_requests|jobs_active|jobs_done`(progress 제외)
- [x] Task 5 프런트 `RunJobButton`·`JobRequests` 블록·잡 표 '실행' 열·오늘/데이터/ML 빈 화면 액션(design:design-system 확장 문서 `docs/design/2026-10-24-phase17-ui-patterns.md`)
- [x] A 거시 FRED `dxy`(DTWEXBGS)·`wti`·`nasdaq` (investing.com 미사용)
- [x] C 네이버 블로그 공개 RSS 소스(`channels.naver_blog: [ranto28]`, 키워드 무관 최근 5건 → `sentiment.community.sources.naver_blog/<id>`)
- [x] M 세이브티커 재조사(Cloudflare 403 + robots AI 크롤러 금지 → 수집 코드 없음) → 시장 공통 수동 입력 `_market_<시장>.md` + 데이터 페이지 '읽은 기사 붙여 넣기' 폼(`POST /api/control/manual-inputs`)
- [x] H1 `HedgeCfg`(`settings.yaml hedge:`)·`risk.max_sector_pct 0.40`·`risk/regime.py`(3지표·히스테리시스·`control.regime:<market>`)
- [x] H2 게이트: neutral 한도 축소(15%·3종목)·bear 신규 매수 거부·섹터 상한·`is_hedge` 면제
- [x] H3 원장 v12 `decisions.source`·`executor.execute(is_hedge=True)`(46_/56_)·`build_portfolio(sector_of, hedge_tickers)`·트레이더 `context.regime`
- [x] H4 `orchestrator/hedge.rebalance`(bear 진입 −1x/−2x·해제 전량 매도·운영일 1회)·데몬 scan_analyze 앞 `_regime_body`·monitor 첫 틱 `_hedge_body`(paused 보류 1회)·monitor 헤지 ETF 제외
- [x] H5 `trader regime show|backtest`·`/api/system/regime`·대시보드 국면 칩·STATUS '## 국면·헤지'·텔레그램 `/status` 줄
- [x] S `views.sector_weights` → `/api/portfolio.sectors`(+`max_sector_pct`)·계좌 페이지 섹터 비중 표(SVG 막대·상한선·초과 황색)
- [x] 완료 기준(코드): `uv run pytest`(Linux 컨테이너 전용 실패 `test_cli_install_dry_run` 1건은 베이스에서도 동일) · `ruff` 0건 · `npm run typecheck && npm test(135) && npm run build` 통과 (2026-10-24)
- [ ] 실기(사용자 확인 대기): `uv run trader universe check --market US` 와 같은 방식으로 헤지 ETF `SH`·`SDS`·`SPY` 가 KIS 거래소 코드 `AMS` 로 시세가 오는지 확인(아니면 `settings.yaml hedge.instruments/index_proxy` 의 exchange 수정); KR `114800`·`252670`·`069500` 시세 확인
- [ ] 실기(사용자 확인 대기): `uv run trader regime backtest --market KR` / `--market US` 로 2024-08·2025-04 급락 구간이 bear 로 잡히는지, 전환 횟수(채찍질)가 과하지 않은지 보고 `hedge.bear_min_signals`·`exit_streak_days`·`momentum_min` 확정. 결과를 여기에 기록
- [ ] 실기(사용자 확인 대기): `FAKE_CLAUDE=1 uv run trader daemon --dry-run` → 대시보드 상태줄 '국면' 칩·잡 표 '실행' 열 → `KR 스캔 → 분석` 클릭 → 작업 대기열 "대기 → 실행 중 → 완료" → 데이터 페이지 '3년 백필 실행'(진행 문구) → ML 페이지 모델 생성; 계좌 페이지 섹터 비중 표(보유 없으면 '현금' 행); 데몬 끈 `trader web` 에서 버튼 비활성·409
- [ ] 실기(사용자 확인 대기, 모의): 국면이 bear 로 바뀐 첫 영업일 09:00 monitor 틱에 `[헤지 진입]` 알림·`46_hedge_paper-main`·`56_order_hedge_paper-main`·`decisions.source='hedge'` 확인, 해제 때 `[헤지 해제]`
- [x] 이연(Phase 21 회수): 텔레그램 `/run <kind>`, CLI `trader jobs list|run|kinds`, 헤지 상품 교체(`hedge.switch_instruments`), 국면 보조 지표 외인 수급(`hedge.flow_signal`; 신용잔고는 KIS 공식 API 미확인으로 제외) (2026-10-25)

## Phase 18 시장 세션 — 거시 애널리스트 + 강세/약세 토론 + 전략가 헤지 제안 (`docs/superpowers/plans/2026-10-24-phase18-market-session.md`)
- [x] M1 `schemas.HedgeProposal`·`registry.MARKET_AGENTS`(`analyst-market`·`researcher-bull/bear`·`strategist`)·md 신규 2개(공통 규칙 6문장)·`orchestrator.md` `## market 모드`·researcher md `scope: market` 단락·trader md `context.market_view` (2026-10-24)
- [x] M2 `tools.market_bundle|market_write|market_summarize|submit_market_view`(`_market_*` 단계, 전제조건·round 상한·side/round 일치, 플래그 `control.market_view:<market>`) + CLI `tool market bundle|write|summarize`·`tool submit-market-view`
- [x] M3 `orchestrator/market_view.build_inputs|save_inputs|run_market_view|load_view|clamp_weight` + `SessionSpec.kind="market"`(run kind `market`, `MARKET_AGENTS`) + `fake.run_fake_market`
- [x] M4 `HedgeCfg.market_session·min_weight_pct`(settings/templates) + `hedge.rebalance` 제안 반영(bear 진입 비중 clamp·레버리지 거부·bear 아니면 무시·어제 제안 무시)
- [x] M5 데몬 `_market_view_body`(regime 뒤, 운영일 1회, 알림 `[시장 뷰]`) + 대기열 kind `market_view` + 트레이더 `context.market_view` + `regime.summary.view`/`line` '제안 n%' + 대시보드 칩·STATUS 줄·텔레그램
- [x] 완료 기준(코드): `uv run pytest`·`ruff`·`npm run typecheck && npm test && npm run build` 통과 (2026-10-24)
- [ ] 실기(사용자 확인 대기): `FAKE_CLAUDE=1 uv run trader daemon --dry-run` 뒤 scan 시각(또는 대시보드 `KR 스캔 → 분석` '지금 실행')에 `state/runs/<date>/KR/_market_inputs.json`·`_market_analyst`·`_market_debate_r0_*`·`_market_summary`·`_market_view.json` 5종과 `[시장 뷰]` 알림, 대시보드 국면 칩 꼬리 "(KR 제안 n%)"(bear 일 때만)
- [ ] 실기(사용자 확인 대기, 약 $1~2): `FAKE_CLAUDE` 없이 시장 세션 1회 — 아침 scan_analyze 로그의 `[시장 뷰]` 와 `_market_view.json` 의 rationale 이 거시 입력을 실제로 인용하는지. 하루 세션 상한(14 = 종목 12 + 시장 세션 KR·US 2, `llm.market_session_cap` 2)에 시장 세션이 포함되므로 analyze 종목이 많은 날은 `llm.daily_session_cap` 조정
- [x] 보완(2026-10-24): 세션 상한 14(= 종목 12 + 시장 세션 2) + `llm.market_session_cap` · 제안 적중률 복기(원장 v13 `market_views`, reflect 잡이 지수 proxy 수익으로 채점, `regime.summary.scorecard`·STATUS 줄·칩 툴팁) · US 조기 마감(`daemon.us_early_closes`, fills 만기 13:00 ET) · CLAUDE.md 모듈 상세를 `docs/architecture.md` 로 이동(74KB → 43KB)
- [ ] 결정 대기(사용자): 제안 적중률이 쌓인 뒤(만기 20건 이상) `hedge.min_weight_pct` 를 0 으로 내려 전략가의 "헤지 불필요" 를 허용할지. 적중률 50% 미만이면 `hedge.market_session: false` 로 끄고 Phase 17 규칙만 쓰는 것도 선택지
- [x] 이연(Phase 21 회수): 오늘의 실행 페이지 '시장 단계' 블록(`/api/market-checkpoint`), 스크리너 `context`(국면·시장 세션 제안) 전달. PR 분리는 하지 않음(main 직접 머지) (2026-10-25)

## Phase 20 응답없음 수정 + 전체 코드 리뷰 수정 (`docs/superpowers/plans/2026-10-25-phase20-hang-review.md`)
- [x] G 공개 저장소 대비: `config/accounts.yaml` 계좌번호 자리표시자 + `secrets/.env` `KIS_PAPER_CANO/KIS_REAL_CANO` 폴백(`secrets import` 매핑 `KIS_CANO`·`KIS_LIVE_CANO`), PR CI(`.github/workflows/ci.yml`, Linux: ruff·pytest·웹) (2026-10-25)
- [x] H 응답없음: 트레이 종료 비동기(`TrayController`, '종료 중')·캐시 상태(메인 스레드 SQLite 금지)·AppKit 메인 스레드(`on_main`)·데몬 사망 감지(`finished`), 세션 종료 유예(`run_claude(cancel=)`, SIGTERM→10s→SIGKILL), SSE 리더 탭(`events.ts` BroadcastChannel), 웹 KIS 대기 상한(`RateLimiter.max_wait` → 503), YouTube·수집 절 타임아웃, 스케줄러 풀 16 (2026-10-25)
- [x] F high 7: NAV 총평가·손절 전 미체결 SELL 취소/매도가능수량·dry-run 40_·US 3거래소·재평가 큐·missed cron·잠금 pid+시작 시각 (2026-10-25)
- [x] F medium 22 + low 저비용: 묶음1(executor/주문)·묶음2(데몬/큐/피처/ML)·묶음3(웹/수집/텔레그램) — 상세 `docs/architecture.md` 「전체 리뷰 수정(Phase 20)」 (2026-10-25)
- [x] 완료 기준(코드): `uv run pytest`·`ruff`·`npm run typecheck && npm test && npm run build` 통과 (2026-10-25)
- [ ] 실기(사용자 확인 대기, macOS): `Trader.app` 에서 분석 세션이 도는 중 메뉴 '종료' → 메뉴바 제목이 '종료 중' 으로 바뀌고 비치볼 없이 수 초~10초 안에 내려가는지; 대시보드 탭을 8개 열어도 표가 갱신되는지(리더 탭 1개만 `/api/events` 연결, 개발자 도구 Network 로 확인)
- [ ] 실기(사용자 확인 대기, 모의): KR 잔고 응답 `output2.tot_evlu_amt`·`nass_amt`·종목 `ord_psbl_qty` 필드 존재(`uv run trader kis ping` 뒤 로그·`/api/portfolio` NAV 가 KIS 앱 총평가와 같은지), US 잔고·체결이 NASD·NYSE·AMEX 3회 조회로 합쳐지는지(`fills sync --account paper-main` 로그), 재부팅 뒤 `daemon.lock` 이 stale 로 넘겨지는지
- [ ] 실기(사용자 확인 대기): 아침 TRIM 지정가가 미체결인 채 손절선에 닿는 날 — `[손절]` 앞에 "손절 전 미체결 SELL 취소" 로그와 `order-rvsecncl` 접수, 취소 실패면 `[손절 보류]` 알림
- [ ] 사용자 확인: `state/logs/trader*.log` 에서 '응답없음' 당시의 시그널·세션 타임아웃·database is locked·KIS 초당 한도 줄과 `~/Library/Logs/DiagnosticReports/TraderApp*.hang` 유무 — 위 다섯 원인 밖의 것이 있으면 다음 Phase
- [x] 이연(Phase 21 회수): 야간 KIS 대조 잡, 제출 시 재시세, 재평가 우선순위 큐, SSE 끊김 표시·60초 liveness, 메모리 브레이커 다계좌 합산, `MAX_PAST` 정본 `sources/base` (2026-10-25)

## Phase 19 뉴스 저장소 + 에이전트 종목 발굴 + 전략가 국면 지표 (`docs/superpowers/plans/2026-10-25-phase19-news-discovery.md`)
- [x] N 시장 전체 뉴스 저장소: 원장 v14 `news_items`, `sources/news_store`(질의 묶음 `news.market_queries`·멱등·태깅·`recent(asof)`·prune), 데몬 `news_collect`(30분, heavy 밖)·큐 kind, 시장 세션 `macro_news` 저장소 우선, 종목 세션 `news.stock` 합침(`InputDeps.conn_factory`), `GET /api/news` + 데이터 페이지 '시장 뉴스' (2026-10-25)
- [x] S scout 발굴: `.claude/agents/scout.md`(13번째 md)·`schemas.Discovered`·`registry.SCREEN_AGENTS=("scout","screener")`, `scan/discovery.build_inputs/resolve/merge/summary`, `tool scan discovery-inputs`·`tool submit-discovery`(ToolContext.market_client), orchestrator.md screen 2단계·screener.md discovery 규칙, `run_screen(discovery=True)`(데몬·CLI), FAKE 흉내 (2026-10-25)
- [x] R 전략가 4번째 지표: `RegimeSignals.agent_risk_off`·`regime.reapply`·`count_text`, 데몬 `_regime_apply_view`(시장 세션 뒤), `hedge.rebalance` RISK_ON 경계 보류(`deferred`), `HedgeCfg.agent_signal/agent_min_confidence`, 웹 `countText`·라벨 (2026-10-25)
- [x] 완료 기준(코드): `uv run pytest`·`ruff`·`npm run typecheck && npm test && npm run build` 통과 (2026-10-25)
- [ ] 실기(사용자 확인 대기): 데몬을 켠 뒤 30분 안에 데이터 페이지 '시장 뉴스' 에 KR·US 기사가 쌓이고 태그 종목이 붙는지(`/api/news?market=KR`), `news_items` 행 수가 `store_retention_days` 안에서 유지되는지
- [ ] 실기(사용자 확인 대기, 약 $0.5~1): `FAKE_CLAUDE` 없이 scan 1회 — `state/runs/<date>/KR/_discovery_inputs.json`·`_discovery.json` 과 `_scan.json.discovery`(유니버스 k·밖 m·미해결 u), scout 의 `catalyst` 가 저장소 뉴스 id 를 인용하는지, 유니버스 밖 종목이 picks 에 들어가면 `00_inputs.meta.name`·분석이 정상인지(피처·섹터 없음)
- [ ] 실기(사용자 확인 대기): 시장 세션 제안이 RISK_OFF(확신 ≥ 0.7)인 날 `[국면 전환] … (n/3+전략가)` 알림과 대시보드 칩 'n/3+전략가'; RISK_ON 이고 지표 2/3 인 날 `[헤지 보류]` 알림
- [ ] 결정 대기(사용자): 2~4주 뒤 `regime.summary.scorecard`(제안 적중률)와 발굴 종목의 결정 결과(`_scan.json.discovery` ↔ 복기)를 보고 `hedge.agent_signal`·`MAX_LISTED`·`news.market_queries` 조정
- [x] 세이브티커 로컬 폴더 리더 `sources/saveticker.py`(사용자 샘플 2026-10-25 구조: `state/external/saveticker/data/<날짜>/*.jsonl`·`latest/*.json`·`attachments`): 뉴스·SAVE 리포트 → `news_items(source='saveticker:<출처>')` 멱등 적재(데몬 `news_collect`), 트렌드·TOP5·지수·상승/하락/거래대금·캘린더·리포트 발췌 → `snapshot(asof)` 로 종목 세션 `sentiment.saveticker`·시장 세션·발굴 입력 `saveticker` 절. 설정 `sources.yaml news.saveticker_dir`(없으면 건너뜀) (2026-10-25)
- [ ] 실기(사용자 확인 대기): 수집기가 돌아 `state/external/saveticker/data/<오늘>/news.jsonl` 이 쌓인 뒤 데몬 `news_collect` 로그의 `saveticker: inserted n` 과 데이터 페이지 '시장 뉴스' 의 `saveticker:로이터` 등 소스, 종목 `00_inputs.sentiment.saveticker.mentions`(QQQ·SPY 같은 TOP5 종목), `_market_inputs.saveticker.calendar` 확인. 수집기 폴더 구조가 바뀌면(`schema_version` 2) 리더를 맞춘다
- [x] 이연(Phase 21 회수): 발굴 입력 네이버 블로그 글(`blog_posts`), 오늘 페이지 '오늘 스캔' 블록(picks 사유·발굴 배지), `[스캔]` 알림·STATUS '오늘 스캔'·텔레그램 `/status` 의 `discovery.line`, 유니버스 밖 발굴 종목 피처 스냅샷(`features.snapshot(tickers=)`) (2026-10-25)

## Phase 21 보완 묶음 — 이연 항목 회수 (`docs/superpowers/plans/2026-10-25-phase21-polish.md`)
- [x] C1 발굴 가시성: `/api/today.scan`(picks 사유·발굴 촉매·`discovery_line`) + 오늘 페이지 '오늘 스캔' 블록(`pipeline.pickReason/scanHeadline`), 데몬 `scan_line` `[스캔]` 알림, STATUS '## 오늘 스캔'·텔레그램 `/status`(`status.scan_lines`), 발굴 입력 `blog_posts`(`discovery._blog_posts`, KR·`channels.naver_blog`), 유니버스 밖 발굴 종목 `_snapshot_listed`(`features.snapshot(tickers=)`) (2026-10-25)
- [x] C2 우선순위 큐: 원장 v15 `job_requests.priority`(`jobs.PRIORITY` reevaluate 10, `claim_next` priority DESC·id), 텔레그램 `/run <kind> [KR|US] [종목]`(`commands.run_command`, 데몬 꺼짐 거부), CLI `trader jobs list|run|kinds`, 웹 대기열 '우선' 배지 (2026-10-25)
- [x] C3 제출 시 재시세: `tools._requote`(`00_inputs.meta.asof` 가 `REQUOTE_AFTER_S`=600s 넘고 `ToolContext.market_client` 있으면 현재가 재조회 → 게이트 수량·손절 검사, 판정 note "재시세 a→b", 실패는 입력가), `executor.execute(extra_note=)` (2026-10-25)
- [x] C4 야간 KIS 대조: `orchestrator/reconcile.py`(브로커 보유 vs 원장 스냅샷, 마감 뒤 미정리 주문 → `[대조 불일치]` 알림·`control.reconcile:<acct>:<market>:<date>`·스냅샷을 KIS 값으로 보정, 주문 행 불변), 데몬 cron `<시장>_reconcile`(`schedule.yaml reconcile` 기본 16:40 현지), 큐 kind `reconcile`(가벼움) (2026-10-25)
- [x] C5 헤지: `hedge.switch_instruments`(기본 끔, 판정과 보유 상품이 `switch_streak_days` 연속 어긋나면 전량 매도 → 다음 운영일 재진입, `hedge_switch_streak:<market>`), 국면 보조 지표 `flow_weak`(`hedge.flow_signal` 기본 끔, KR 지수 proxy 외인 20일·5일 순매수 합 음수, `regime.flow_weak`, `count_text` '+수급') (2026-10-25)
- [x] C6 웹: SSE 끊김 배지 '실시간 끊김'(`events.ts` onStatus 리더→탭 중계, `AppState.connected`) + 끊긴 동안 60초 `/api/system/health` liveness 폴링으로 tick 갱신 (2026-10-25)
- [x] C7 세이브티커 `events.upcoming`(7일 경제 일정, `saveticker.upcoming`)·`schema_version` 경고 1회, `MAX_PAST` 정본 `sources/base`, 메모리 브레이커 모의 계좌 NAV 합산(`executor._paper_nav_series`), 스크리너 `context`(국면·시장 세션 제안, `screener.screen_context`), 오늘 페이지 '시장 단계' 블록(`/api/market-checkpoint`) (2026-10-25)
- [x] 완료 기준(코드): `uv run pytest`·`ruff`·`npm run typecheck && npm test && npm run build` 통과, PR [#20](https://github.com/km91hsm/trading-agent/pull/20) main 머지 (2026-10-25)
- [ ] 실기(사용자 확인 대기): 첫 운영일 16:40 KST 뒤 `[대조 불일치]` 알림이 없거나(정상) 있으면 그 내용이 KIS 앱 잔고와 맞는지; `/api/today` 의 '오늘 스캔'·'시장 단계' 블록; 텔레그램 `/run scan_analyze KR` 이 대기열에 들고 데몬이 실행하는지; 아침 scan 뒤 10분 넘게 지난 종목의 `40_risk_check` note 에 "재시세" 가 붙는지
- [ ] 결정 대기(사용자): 2~4주 데이터 뒤 `hedge.flow_signal`(외인 수급 지표)·`hedge.switch_instruments`(상품 교체)를 켤지, `jobs.PRIORITY` 에 다른 kind 를 올릴지
- [ ] 미구현(근거 부족): 국면 지표에 신용잔고 — KIS 공식 API 에서 일별 신용잔고 조회 경로를 확인한 뒤 `regime.flow_weak` 옆에 추가

## Phase 22 전략가 기울기 게이트 — 에이전트 몫 확대 (사용자 요청 2026-10-25)
- [x] `hedge.agent_gate`(기본 켬)·`stance.tilt_of/limits_for_regime(tilt)`·`rules.evaluate(agent_tilt=)`·`executor._agent_tilt`: neutral+RISK_ON 20%·4, neutral+RISK_OFF 10%·2, bull+RISK_OFF neutral 한도, bear+RISK_ON 신규 매수 10%·2 조건부 허용(헤지·손절 무관, 설정 한도 초과 없음), trader.md 갱신 (2026-10-25)
- [x] 완료 기준(코드): `uv run pytest`·`ruff`·웹 검증 통과 (2026-10-25)
- [ ] 결정 대기(사용자): 2~4주 뒤 `regime.summary.scorecard`(전략가 적중률)이 50% 아래면 `hedge.agent_gate: false`; 반대로 좋으면 `TILT_UP` 을 25%·5(= bull) 까지 넓힐지
- [ ] 이연: RL 사이저 `mode: apply`(비중 결정 모델 위임)는 `rl promote` 뒤 `ml.yaml` 수동 전환 그대로 — 자동 승격 없음 원칙 유지

## Phase 23 장중 뉴스 발굴 + 보유기간 프로필 + 단타 층 + 운영 점검·재호가·watchdog·결정 추적 (`docs/superpowers/plans/2026-10-25-phase23-intraday-discovery.md`)
- [x] D 장중 뉴스 발굴: 원장 v16 `news_items.novel`·`news_store.novel_items`(유사 기사 파이썬 판정), 데몬 `_news_trigger`(종목 재분석: 오늘 결정 있으면 트리거 kind `news` + `reevaluate`, 없으면 `analyze_ticker`; 새 뉴스 ≥1 이면 큐 `discover`), 세션 kind `discover`(scout 만, `_discovery_inputs_r<n>`·`_discovery_r<n>`, FAKE), 뉴스 재평가 재수집·재시세, `llm.daily_session_cap` 40, `/api/today.news_jobs` + 오늘 페이지 '장중 발굴' (2026-10-25)
- [x] T 보유기간 프로필: `RiskLimits.horizon_profiles`(short/mid/long)·`risk/horizon.py`, monitor 임계·간격·`periodic` 트리거, 보유 아침 재분석 주기(`_skip_held_by_profile`), 뉴스 `news_min_novel`, 보유 표 '보유기간' 열, trader.md (2026-10-25)
- [x] S 분 단위 단타(모의 전용·기본 끔): `ScalpCfg`, `scalp/signals.py`·`scalp/engine.py`, `rules.evaluate(is_scalp)`·`executor.execute(scalp_leg)` 47_/57_, 데몬 `<시장>_scalp` 60초, CLI `scalp backtest|report`, `/api/system/scalp`·대시보드 칩·오늘 페이지 '단타'·STATUS, `HedgeProposal.scalp_ok`. **플랜과 다른 점**: `decisions` 행을 만들지 않는다(UNIQUE 키가 오늘 LLM 결정을 덮음) — `orders.decision_id=NULL` + 상태 파일 (2026-10-25)
- [x] A 운영 점검: `ops/health.py` 다섯 영역·단계, 데몬 `ops_check`(30분, 격상 때만 알림), `/api/system/ops`, 대시보드 `Advisory`, 텔레그램 `/status`·STATUS (2026-10-25)
- [x] B 주문 안전: unknown 체결 매칭은 후보 1건일 때만(`fill_ambiguous` 플래그·알림), 아침 미체결 재호가 `orchestrator/reprice.py`(`risk.reprice`, 42_/52_, 추격 1% 상한, monitor 틱 연결) (2026-10-25)
- [x] C watchdog: `ops/watchdog.py`·CLI `trader watchdog [--quiet]`·`install --watchdog`(launchd StartInterval 300 / 작업 스케줄러 PT5M) (2026-10-25)
- [x] E 결정 추적: `views.decision_trace`·`/api/decisions/{id}/trace`, 주문 페이지 '추적' 열(단계별 줄·지정가 거리·unknown 경고) (2026-10-25)
- [x] G 웹 구조: `nav.ts`(묶음·모바일 탭·최근 추가·팔레트 후보·g 단축키), `CommandPalette`(이동 전용), `Table responsive`/`core`, CSV `?format=csv` + `CsvLink`, `useLoad` 가려진 탭 (2026-10-25)
- [x] 완료 기준(코드): `uv run pytest`·`ruff`·`npm run typecheck && npm test && npm run build` 통과 (2026-10-25)
- [ ] 실기(사용자 확인 대기): 데몬을 켠 장중 30분 뒤 `[뉴스 발굴]` 알림과 오늘 페이지 '장중 발굴' 표에 요청이 쌓이는지, 같은 사건의 다른 매체 기사가 `novel=0` 으로 걸러지는지(`SELECT title, novel FROM news_items`), 하루 세션 수가 40 안에서 머무는지(대시보드 사용량)
- [ ] 실기(사용자 확인 대기): 단기(horizon_days ≤10) 결정 보유분이 60분마다 `periodic` 트리거로 재평가되는지(`triggers.kind='periodic'`), 장기 보유분이 아침 분석에서 건너뛰어지는지(`skipped_held` 로그)
- [ ] 결정 대기(사용자): 모의에서 `trader scalp backtest` 로 며칠 신호를 본 뒤 `scalp.enabled: true` 로 켤지(모의 전용, 실전은 코드로 막힘). 켠 뒤 1~2주 `scalp report` 승률·`daily_stop` 중단 빈도를 보고 임계(`volume_mult`·`take_profit_pct`·`stop_pct`) 조정
- [ ] 실기(사용자 확인 대기): 개장 30분 뒤 지정가 미체결이 있는 날 `[재호가]` 알림·`42_reprice_*` 체크포인트·원 주문 `cancelled`; 데몬을 꺼 둔 뒤 `trader watchdog` 가 `[감시] 데몬이 꺼져 있음` 을 보내는지; `trader install --watchdog --dry-run` 출력 확인 뒤 등록
- [ ] 결정 대기(사용자): 종목 일정(권리·실적 발표) 블랙아웃은 DART 기준일 소스를 확인한 뒤 다음 Phase(소스 미확인으로 미구현)
- [ ] 이연: ⌘K 팔레트의 종목명 검색은 `/api/names` 가 이미 받은 이름만 본다(원장·캐시) — 전체 상장 검색은 종목 마스터가 생기면

## Phase 24 웹 보고서·실시간·사용률 (`docs/superpowers/plans/2026-10-26-phase24-25.md` 단계 1~3)
- [x] 단계 1(PR [#23](https://github.com/km91hsm/trading-agent/pull/23) 머지) 웹 보고서 형식·전체/계좌 분리·섹터·목표가 (`feat/phase24-1-web-report`): 섹터 원인 진단(보유 행 없음·대시보드 블록 없음), 보유 행 `plan`(목표가·손절·지정가·보유기간·거리), `/api/portfolio/overview`(시장 × 모의/실전 합산), `report.ts`·`StageReport`(JsonView 는 '원본 보기'), 계좌 선택기 '전체' (2026-10-26)
- [x] 단계 2(PR [#24](https://github.com/km91hsm/trading-agent/pull/24) 머지) 실시간 시세·종목명 검색 (`feat/phase24-2-live-quotes`): `/api/quotes`(정규장만, 5초 캐시, 레이트리미터·stale 폴백), 보이는 탭 10초 폴링(`live.ts`), `/api/names/search`, 차트 URL·자동완성·⌘K (2026-10-26)
- [x] 단계 3(PR [#25](https://github.com/km91hsm/trading-agent/pull/25) 머지) 구독 사용률·세션 상한·텔레그램 문구 (`feat/phase24-3-usage-telegram`): stream-json `rate_limit_event` → 원장 `llm_rate_limits`(v17), 상한 40→80 + 사용률 감속(80% 재평가만·95% 중지), `notify/messages.py`(최종 의견 블록·'종목명(코드)'·골든 테스트) (2026-10-26)
- [ ] 결정 대기(사용자): 2~4주 `llm_rate_limits`·`agent_calls` 통계로 `llm.daily_session_cap` 고정값 재설정
- [x] KIS 실시간 웹소켓 — 국내 체결가 `H0STCNT0`, 화면 임차 40칸, 1초 화면 갱신(2026-10-27, 「국내 실시간 시세 웹소켓」)

## Phase 25 테마·LLM 판정자·공시·리밸런스 (같은 플랜 단계 4~7)
- [ ] 단계 4 테마 열기 감지·안전장치 (`feat/phase25-4-theme`, 머지 전 사용자 확인): KIS 순위 3종(관대한 파서), `scan/theme.py`, 원장 v18 `theme_heat`, 데몬 `<시장>_theme`·알림·CLI·API·대시보드, scout 규칙 1 완화, 게이트(+10% 추격 금지·테마 20%·테마 종목 10일·익절가) — 구현·테스트 완료, PR 확인 대기
- [ ] 실기(사용자 확인 대기): 장중 `uv run trader theme show --live --market KR` 의 `signals.ranks` 가 0 이 아닌지(순위 API 파라미터·필드명 확인), 대시보드 '테마 열기' 블록과 `[테마 열기]` 알림
- [ ] 결정 대기(사용자): 2~4주 뒤 `theme_heat.fwd_ret_5d`(상위 테마 사후 초과수익)로 `theme.weights`·`top_n`·`chase_limit_pct` 조정
- [x] 단계 5 분 단위 LLM 판정자 (`feat/phase25-5-scalp-judge`): `quick.ask_json`·`scalp-judge`(haiku)·`ScalpVerdict/ScalpAdvice`·엔진 비동기 판정(다음 틱 회수·0.3% 이탈·시간 초과·재시작=진입 없음·클램프)·보유 조언·GO vs NO_GO 리포트 — PR #32 머지(2026-10-26)
- [ ] 결정 대기(사용자): 단타를 켠 뒤(`scalp.enabled`) 1~2주 `scalp report --live` 의 GO/NO_GO 사후 수익을 보고 `scalp.judge` 를 켤지
- [x] 단계 6 승인 지문·공시 중요도 (`feat/phase25-6-approval-disclosure`): 원장 v19 `approvals.fingerprint`·`news_items.severity`, `sources/disclosures.py`(DART 키워드·8-K item), 데몬 `_disclosure_trigger`(critical 즉시 재분석·알림 1회), 운영 점검 `disclosure` 영역 (2026-10-26)
- [ ] 실기(사용자 확인 대기): DART 키가 있는 날 데몬 `news_collect` 로그의 `disclosure:KR` inserted·critical, EDGAR `submissions` 가 User-Agent 로 200 을 주는지
- [ ] 단계 7 주간 리밸런스·세금 로트 (`feat/phase25-7-rebalance-tax`, 머지 전 사용자 확인): `rebalance.drift_legs/run`·executor `is_rebalance`(43_/53_)·데몬 금요일 cron·드리프트 열, `ledger/lots.py` FIFO·US 장단기·워시세일·`/api/tax-lots`·CSV·계좌 '실현 로트' — 구현·테스트 완료, PR 확인 대기
- [ ] 실기(사용자 확인 대기, 모의): 첫 금요일 14:30 KST 뒤 `control.rebalance_done:KR:<date>`·`43_rebalance_*`·`[리밸런스]` 알림과 계좌 표 드리프트 열


## 전체 검토 (2026-10-26, `docs/reviews/2026-10-26-full-review.md` — Claude 4 + Codex 2 교차검증)
- [x] 보완 1차(비주문): W1 실전 승인 확인 우회, W2 unknown 승인 안내, W3·W4·W6·W7·W11 '전체' 대시보드 범위·실전 표식·오래된 점검·시장 현지 날짜, T1·T2 텔레그램 승인 요청·실전 확인어·명령 안내, P1 재평가·복기 cancel, P3 novel_items 잠금, P5 enqueue 경합, P10 로그 소음, L4 퀀트 모델 시장 (2026-10-26)
- [x] 보완 2차(주문 경로): O1 US NAV 원화 혼입(실기 확인 포함), O2 재호가가 손절·헤지 재제출, O3 filled TRIM 이 재평가 SELL 차단, O4 POST 5xx·비JSON → unknown, O5 일시정지·off 계좌의 손절 전 SELL 취소, O9 risk_check 만 남은 재개, O11 대체된 결정의 승인 실행, O13 청산 때 미체결 BUY 취소, O14 −2x 판정 count, O15 off/dry-run 손절 플래그, O16 음수 가격 검증, O17 만료 승인 예약, L5 손절 단조 유지 (2026-10-26)
- [x] 보완 3차(투자 방식, 사용자 결정 2026-10-26): L9 게이트 강화(BUY/ADD 손절·익절 필수, 확신 가중 기대값 ≥ 왕복비용, 1회 손실 한도 NAV×2%), L8 `hedge.agent_signal` 끔(기울기만), L11 약세 리서처 sonnet(토론 모델 대칭) (2026-10-26)
- [x] 보완 4차(L1, 사용자 결정 '섀도 포트폴리오 + 기준 교체'): `ledger/shadow` 운영일 1회 llm·후보 1/N·정량 상위 기록 → reflect 5거래일 만기, readiness `llm_alpha`(20쌍 ∧ 부트스트랩 하한 > 0), 1/N 수익률 비교는 정보, 성과 페이지 'LLM 종목 선택 효과' (2026-10-26)
- [x] 보완 5차(묶음 C 주문 경로, 사용자 결정 2026-10-26): O8 실행 키 선점(원장 v21 `exec_keys`, 거부·실패면 해제), O10 예상 포트폴리오(체결 전 BUY·pending 승인을 종목 수·섹터·테마 합계에), O12 승인 직전 재판정(bear∧기울기≤0·일일 손실 → expired) (2026-10-26)
- [x] 보완 F(L17 중 '애널리스트 합의 시 토론 생략'만, 사용자 결정): `settings.debate.skip_on_consensus`(기본 켬)·`consensus_min_conviction`(0.7) — `tool checkpoint summarize` 가 토론 턴이 없을 때 애널리스트 4역할 같은 방향(NEUTRAL·UNKNOWN 제외)·확신 전부 ≥ 기준이면 `25_research_summary{skipped_debate: true, reason}`(수렴 감시 없음), 아니면 `debate_required`. `submit-decision` 전제조건 불변 (2026-10-26)
- [ ] 2~4주 뒤(2026-11-09~11-23): 섀도 포트폴리오(`ledger/shadow`, readiness `llm_alpha`)로 토론 생략 전후 비교 — 생략 종목(`25_research_summary.skipped_debate`)과 토론 종목의 llm 수익·적중률, 세션 비용(`agent_calls`) 감소분. 생략 쪽 성과가 나쁘면 `consensus_min_conviction` 상향 또는 끔
- [ ] 결정 대기(사용자): L17 나머지 비용 절감(애널리스트 모델 하향·정량 중립 종목 제외 — 미선택)
- [x] 보완 D(단타 청산, 사용자 결정 2026-10-26): O7 청산 점검을 허용 판정보다 먼저(bear·전략가 금지·일일 손실·일시정지는 신규 진입만), O6 계좌별 다리(진입 접수 `pending_entry` → `fills` 체결분만 `held`) · 청산은 미체결 진입 BUY 취소 → 계좌별 체결 수량만 SELL(`executor.execute(max_qty=)`, 미진입 계좌 skip) · 실패·미체결은 `closing` 유지 재시도 · 수익률은 체결가 (2026-10-26)
- [x] 보완 4차 묶음 A(프로세스·성능): P2 빠른 대기열 레인(`job_queue_fast`: 재평가·체결 동기화·뉴스 수집·대조는 heavy 락 밖), P4 데몬 OS 잠금(`daemon.lock.os`, 수명 동안), P7 수집 공통 기한·풀 비대기 종료, P8 heavy 대기 카운터, P9 derive 파일 락 밖 진행 콜백, P11 `platform/fsutil.atomic_replace` 재시도·피처 읽기 실패 시 덮어쓰기 금지, P12 `ops/retention.prune`(reflect 끝, `retention_days` 120), P13 레이트 스토어 스레드 연결 재사용, 뉴스 `(market, received_at)` 인덱스(원장 v22) (2026-10-26)
- [x] 보완 4차 묶음 B(웹·문구): W5 SSE 테이블 지문, W8 `livePlan`, W9 `DetailRow`, W10 `RowButton`, W12 통과·관망·주문 없이 판정·`sideLabel`, T3 해요체·영문 코드 한글화(`regime_change`·`market_view`·`mode_changed`·`ml_trained`, `regime.line`)·단타 `scalp_digest`(`scalp.digest_every_min` 30분, 첫 묶음·중단·마감 즉시, 전송 실패면 재시도) (2026-10-26)
- [ ] 실기(사용자 확인 대기): 데몬 재부팅·강제 종료 뒤 `state/daemon.lock.os` OS 잠금이 풀려 다음 기동이 되는지(macOS·Windows), Windows 에서 웹이 날짜 파일을 읽는 중 데몬 스냅샷 쓰기가 재시도로 성공하는지
- [x] 보완 5차 E(L2·L3, 사용자 결정 '재설계', `feat/review-memory-scoring`): 원장 v23 `memory_entries.round`(재평가 별도 엔트리, `UNIQUE(decision_id, round)`)·`held_at_decision`·`direction_score`·`avoided_loss`·`realized_pnl_pct`, 검색·보정 round 0 우선, 트레이더·복기 입력 방향 점수 기준 (2026-10-26)
- [x] L6(P1, 2026-10-28 반영 — 묶음 3): 백필에 없는 수급·뉴스·커뮤니티 건수 열(`COUNT_COLUMNS`·`FLOW_COLUMNS`)을 학습 열에서 제외 — 현재 `dataset.feature_columns` 는 전부 NaN 인 열만 뺀다 (2026-10-26 교차검증 잔여, 2026-10-27 등록 → 2차 2-L13)
- [x] L7(P1, 부분 반영 → 2-L1·2-P4 로 반영 2026-10-27): 유니버스 밖 발굴·사건 후보의 분석 전 스냅샷은 있음(`daemon._snapshot_listed`, #47·#53). 남은 것 — 보유 종목 + 미만기 memory/shadow 창 종목을 일일 reflect 스냅샷 대상에 포함, 장중은 D−1 완성 봉까지만(현재는 거래량 0 봉만 제외), `shadow.mature` 가 종가 없는 종목을 빼고 재정규화하지 않게 (2026-10-26 잔여 → 2차 2-L1·2-P4, **P1**)
- [x] L10(P2, 2026-10-28 반영 — 묶음 3 `context.evidence_stats`): 트레이더 묶음이 `drop_evidence=True` 인데 프롬프트는 근거 수를 세라 함 — 역할별 근거 수·`data_gaps`·신호 일치도를 파이썬이 숫자로 (2026-10-26 잔여 → 2차 2-L19)
- [ ] L12(P2, 미반영): 보정을 벤치마크 대비 부호·이항(Wilson) 신뢰구간 하한·수축으로, 근거 할인은 역할·이웃 n ≥ 30 일 때만 (2026-10-26 잔여 → 2차 2-L14)
- [ ] L13(P2, 부분 반영 2026-10-28 — lesson 승격 n ≥ 3 은 묶음 3): 남은 것 — 메모리 검색 relevance 가중↑(현재 0.4/0.4/0.2), warmup 50 뒤 실제 검색 결과로 결정 (2026-10-26 잔여 → 2차 2-L15)
- [ ] L14(P2, 부분 반영): PIT 유니버스(원장 v24, #47 — 기록 시작 이후만, 과거 구간은 `pit.strict` 정책)·생존 편향 경고(#44·#45) 반영. 남은 것 — `performance.report` 가 `ranker.current()` 를 과거 창에 적용(평가 시점에 있던 모델 또는 섀도 `quant_topn` 으로), 고정 홀드아웃·비용 차감, 승격 평가와 실제 랭킹 경로 일치 (2026-10-26 잔여 → 2차 2-L16)
- [x] L15(P2, 반영): 10일 모멘텀 스크린의 과거 검증·대조군이 저장소에 없음(리뷰어 2인 grep 확인) — 섀도 `quant_topn − candidates_1n` 전방 비교만. 피처 스토어로 `roc_10` 상위 N vs 유니버스 1/N 백테스트 CLI (2026-10-26 잔여 → 2차 2-L17) — 2026-10-28 `trader screen backtest` 로 반영(묶음 3)
- [ ] L16(P2, 미반영): 무효화 조건을 구조화 필드 `{metric, op, value, days}` 로 — `parse_price_conditions` 가 US $100 미만 무시·'이하'→`<`·'3일 연속' 을 즉시 조건으로 (2026-10-26 잔여 → 2차 2-L18)
- [ ] L18(P3, 부분 반영): trader.md 에 테마 안전장치 설명은 있음. 남은 것 — `context.theme`(종목별 테마·사건 소속), 퀀트 5일 확률 vs 결정 기간 정합, 익절 도달 정책과 최소 보유 3일 일치 (2026-10-26 잔여 → 2차 2-L20)

## 웹 실시간·시계·업종 비중·사용량 (2026-10-26)
- [x] 상단 1초 시계(KST·뉴욕·장 상태), 도움말 클릭 팝오버, 업종 비중 자동 분류(KIS 업종·ETF 상표, 표시용), 화면 전체 실시간(SSE 지문 확장·키별 재조회), 데몬 잔고 주기 갱신·접속 즉시 갱신(조회만), Claude 사용량 확인 잡(30분)·초기화 시각 표기 (2026-10-26)
- [ ] 실기(사용자 확인 대기): 데몬을 켠 장중에 대시보드를 열어 둔 채 순자산·현재가가 10초마다, 잔고(현금·수량)가 5분 안에 새로고침 없이 바뀌는지, Claude 사용량 블록의 5시간 창 초기화 시각이 30분 안에 채워지는지(`llm_rate_limits` source=probe)
- [ ] 결정 대기(사용자): 자동 분류된 보유 종목(업종 표의 '자동')을 `config/sectors.yaml` 에 굳혀 리스크 게이트 업종 상한에도 반영할지

## Phase 17 SaveTicker 브라우저 수집 (`docs/superpowers/plans/2026-10-25-saveticker-collection.md`)
- [x] 사용자 승인 계정 파일(`secrets/saveticker.env`, Git 제외·0600)과 일반 브라우저 자동 로그인. headless HTTP 403은 우회하지 않고 실패로 기록
- [x] 뉴스 출처·카테고리 22개 스트림, 커뮤니티 글·댓글 및 지수/상승/하락, 증시 거래대금/상승/하락, 정규장/실시간 기준, 기간 지정 캘린더
- [x] SQLite 버전·변경 이력, 증분 JSONL·최신 캡처·스트림 상태·실행 상태, 중단 파일 복구, 시점 조회·중복 억제·순위 이탈 반영
- [x] 실기(2026-10-25): 일반 창에서 36개 화면 1회 읽기, 커뮤니티 지수 5·상승/하락 각 20·증시 거래대금/상승/하락 각 20, 캘린더 9월 133·10월 136·11월 79건. 12월/2027-01 빈 결과는 partial
- [x] 실기(2026-10-25): 뉴스 상세·커뮤니티 댓글을 파일로 저장. 본문 처리 예산·가상 스크롤 끝 미확인은 partial로 보존
- [x] 완료 기준(코드): 최종 전체 pytest 1,010 passed·1 skipped, 수집기 49개 테스트·ruff 통과. 상시 프로세스 반복 갱신·핵심 목록 실측 75~85초 확인 (2026-10-25 확인)
- [ ] Windows 실기: Chromium 설치·일반 창 자동 로그인·종료 및 UTF-8 파일 확인 (크로스플랫폼 코드는 구현, Windows 장비 미검증)
- [ ] 완료 기준(운영 검증): 전체 과거 본문·모든 댓글의 완전 수집 및 장중 폭주 시 누락 여부 확인. 현 코드는 횟수 한도·끝 미확인을 partial로 명시하며 무한 과거 완료를 주장하지 않음

## 세이브티커 수집기 합치기·번들 (2026-10-27, 사용자 결정 '공개 main 머지 + Chromium 첫 실행 설치')
- [x] 수집기 브랜치(`feat/phase17-saveticker`)를 main 위로 병합, 패키지 `trader.saveticker`(리더 `trader.sources.saveticker` 와 이름 충돌 해소) (2026-10-27)
- [x] 저장 위치 = 리더 설정(`news.saveticker_dir`), 하위 프로세스 argv `bundle.cli_argv()`(번들 `trader`), `playwright`·`pypdf` 기본 의존성 + PyInstaller 드라이버 포함, Chromium 첫 실행 설치(`chromium.ensure`·`saveticker install-browser`·`selfcheck`) (2026-10-27)
- [x] 데몬 잡 `saveticker`(60초): 계정 파일 있을 때 서비스 분리 실행·재기동(인증 실패는 계정 파일이 바뀐 뒤, 재기동 한도는 1시간 뒤)·텔레그램 장애 알림·운영일 1회 보관 기간 정리(`saveticker_retention_days` 30), 종료 때 트리 정리 (2026-10-27)
- [x] 저장량: 뉴스 조회수·좋아요·댓글 수만 바뀐 행은 새 버전 아님. 리더: 트렌드 제외·오래된 화면 제외(`snapshot.stale`) (2026-10-27)
- [ ] 실기(사용자 확인 대기, 다른 PC): 번들 폴더를 복사 → `secrets/saveticker.env` 확인 → `Trader.app` 실행 → 1분 안에 `state/external/saveticker/runtime/daemon-service.log` 에 Chromium 설치·수집 시작, 브라우저 창 4개, `./trader saveticker status` 의 스트림 `ok`
- [ ] 실기(사용자 확인 대기, Windows): Chromium 자동 설치·일반 창 로그인·앱 종료 때 `taskkill /T` 로 브라우저까지 닫히는지
- [x] 결정(사용자 2026-10-27): 이 맥의 launchd 수집기(`com.user.saveticker.collector`·`monitor`)를 내리고 앱(데몬)이 수집을 맡는다 — plist 는 `~/Library/LaunchAgents/disabled-saveticker/` 로 옮김(되돌리려면 제자리로 옮겨 `launchctl bootstrap`). 앱이 꺼져 있는 동안은 수집이 멈춘다

## 국내 실시간 시세 웹소켓 (2026-10-27, 사용자 요청 — Stock 프로젝트 중단·이 프로젝트로 일원화)
- [x] `broker/kis_ws`(프레임 묶음·부호·핑퐁·제어)·`KisAuth.approval_key`·`web/live_quotes.QuoteHub`(임차·백오프·거절)·`/api/quotes` 허브 우선 + `stream`·프런트 1초 주기, 실기: 접속키 발급·`SUBSCRIBE SUCCESS`/`UNSUBSCRIBE SUCCESS` 확인(장 밖) (2026-10-27)
- [ ] 실기(사용자 확인 대기, 장중): 대시보드를 열어 둔 채 국내 보유·관심 종목 현재가가 1초 단위로 바뀌는지, 표 머리 '실시간 n종목 · 체결 즉시', `state/logs` 에 '실시간 시세 접속'·끊김 재접속 로그
- [x] 지수(KOSPI·KOSDAQ) 상단 실시간·차트 호가 10단·데몬 체결통보 → 즉시 체결 동기화(본문 미복호화) — 실기: 구독 SUCCESS(지수·호가·모의/실전 국내·해외 통보, 장 밖) (2026-10-27)
- [ ] 실기(사용자 확인 대기, 장중): 상단 바 KOSPI·KOSDAQ 1~2초 갱신, 차트 호가 10단, 모의 주문 체결 뒤 10~20초 안에 주문 상태 filled·잔고 반영(`job_requests.source='notice:paper'`)

## Stock 교훈 1차 — 안전장치·판단 근거 (2026-10-27, `docs/superpowers/plans/2026-10-27-stock-lessons-1.md`, 사용자 결정 '웹 수동 주문 제외 전부')
- [x] 원장 백업(매일 17:10, 14일 + 월요일 8개)·누적 손실 킬스위치(−15%, 해제는 CLI·메모)·실전 자본 상한(`capital_cap`)·매매 제한 종목 차단·지수 섀도 기준선(`index_hold`·`index_trend`)·모의 체결 대 가정 주간 점검·수정주가 소급 변경 감지 재백필·생존 편향 경고 (2026-10-27)
- [ ] 실기(사용자 확인 대기): 데몬을 켠 날 17:10 뒤 `state/backups/trader-*.db` 생성, 토요일 09:00 `[모의 체결 점검]` 알림, 성과 페이지에 '지수 보유'·'지수 추세' 행(만기 5거래일 뒤부터 숫자)
- [ ] 실기(사용자 확인 대기): 관리·경고 종목 현재가 응답에 `iscd_stat_cls_code`·`mrkt_warn_cls_code`·`temp_stop_yn` 이 실제로 오는지(`00_inputs.technical.quote.data`)
- [ ] 결정 대기(사용자, 실전 전): `config/accounts.yaml` 실전 계좌 `capital_cap` 첫 금액(예: KR 300만·US 2000달러)
- [ ] 운영 규칙(Stock D-120): `scalp.enabled` 를 켜기 전 수개월 분봉 재생·비용 반영·보류 구간 1회 검증(코드 강제 없음)
- [x] 시점 기준 유니버스 — 오늘부터 매일 구성원 기록(원장 v24 `universe_members`), 학습은 기록 뒤 행을 그날 구성원만·coverage 보고·strict·거래대금 상위 N 옵션 (2026-10-27)
- [ ] 결정 대기(사용자, 기록 3~6개월 뒤): `ml.yaml train.pit.strict: true` 로 기록된 기간만 학습할지(학습 행 수와 알파 비교 후)

## Stock 교훈 2차 — 운용·화면 (2026-10-27, `docs/superpowers/plans/2026-10-27-stock-lessons-2.md`)
- [x] 장 마감 일일 리포트(현지 16:20)·`/daily`, 위험 카드(VaR·CVaR·베타·상관·위험 기여), 스냅샷 기준 시각 오래됨, ML 생존 편향 표시, 아침 브리핑(07:20 haiku), 권리 일정 블랙아웃(기본 끔·수집은 매일), 현금 단기금리 ETF(기본 끔·하락 국면만), Tailscale 폰 접속(설정 시) (2026-10-27)
- [ ] 실기(사용자 확인 대기): 평일 07:05 뒤 `control.ksd_events:KR` 에 일정이 쌓이는지(`errors` 비었는지) — 예탁원 TR 4종은 Stock 대조값, 이 저장소 실기 미확인
- [ ] 실기(사용자 확인 대기): 07:20 `[아침 브리핑]`·16:20 `[일일 리포트]` 텔레그램, 계좌 페이지 '위험' 블록(피처 스토어 종가 20일 이상 필요)
- [ ] 결정 대기(사용자): 2~4주 모의 뒤 `cash_park.enabled`·`event_blackout.enabled` 를 켤지(모의에서 먼저)
- [ ] 사용자 설정(선택): 폰 접속 — Tailscale 설치 → `tailscale serve --bg 8787` → `settings.yaml web.tailscale_hosts`·`tailscale_users`

## 계좌별 키·연결 키 입력 (2026-10-27, 사용자 요청 — 같은 명의 계좌 여럿·.env/HTS ID 를 설정 화면에서)
- [x] 계좌 전용 키 묶음(`KIS_<ID>_*`·`credentials`) → 없으면 env 공용, 앱키 단위 클라이언트·토큰 캐시·레이트리미터, 같은 키 공유 경고 (2026-10-27)
- [x] 체결통보 (env, HTS ID) 단위 리스너 하나 + 시세 허브와 겹치지 않는 키 우선 (2026-10-27)
- [x] 설정 화면 '연결 키' — 값 비노출·토큰 + 이 PC 직접 접속·백업·재시작 안내, 웹 토큰 자동 생성·같은 출처 화면에 자동 전달 (2026-10-27)
- [ ] 실기(사용자 확인 대기): 같은 명의 두 번째 실전 계좌를 `accounts.yaml` 에 추가하고 설정 화면 '계좌 전용' 칸에 그 계좌 앱키·시크릿·계좌번호를 넣은 뒤 재시작 → `uv run trader kis ping` 이 두 계좌 모두 OK, 한쪽 계좌 체결 때 체결통보 리스너 하나로 반영되는지

## claude 경로·시스템 현황 화면 (2026-10-27, 사용자 보고 — 텔레그램 ClaudeNotFound·사용량 멈춤·대시보드 길이·뉴욕 시각)
- [x] `find_claude` 가 PATH 밖 흔한 설치 위치(`~/.local/bin` 등)도 본다 — Trader.app 의 최소 PATH 에서 세션·사용량 확인이 실패하던 원인 (2026-10-27)
- [x] 대시보드의 Claude 사용량·모델·작업 대기열·데몬 잡을 시스템 메뉴 '시스템 현황'(`/system`)으로, 상태줄 칩이 링크 (2026-10-27)
- [x] 상단 시계 뉴욕도 날짜·요일·초, 데몬 잡 이름 한글(일일 리포트·원장 백업 등)·요일 표기 (2026-10-27)
- [ ] 사용자 확인 대기: 번들 재빌드 뒤 Trader.app 을 다시 켜고 30분 안에 시스템 현황의 Claude 사용량 '5시간 창'·'7일 창'이 '데몬 확인 · n분 전' 으로 바뀌고 초기화 시각이 보이는지, 텔레그램 ClaudeNotFound 가 멈추는지

## 전체 검토 1차 — 돈·보안·실행 환경 (2026-10-27, 사용자 요청 '전체 소스 검토 → 전부 진행')
- [x] 승인 직전 킬스위치 재확인, 결정 반전·킬스위치·손절 때 미체결 BUY 취소·pending BUY 승인 만료 (2026-10-27)
- [x] 손절·단타 청산 실행 키에 수량(일부 체결 뒤 잔량 손절), 리밸런스 dry-run 완료 표시 제거 (2026-10-27)
- [x] 웹 `testserver` Host 제거·ensure-fresh 같은 출처만 (2026-10-27)
- [x] 빌드가 dist/trader 사용자 데이터 보존, 창 없는 앱(uvicorn log_config·스트림·시작 실패 기록·Translocation), Windows 세션 트리 정리 (2026-10-27)
- [ ] 사용자 확인 대기(Windows): CI zip 의 `TraderApp.exe` 더블클릭 → 트레이·웹이 뜨는지, 분석 세션 타임아웃 때 `trader tool` 프로세스가 남지 않는지(작업 관리자)

## 전체 검토 2차 — 프로세스 (2026-10-27)
- [x] 재시작 따라잡기·하트비트 상태 이어받기, 놓친 잡의 휴장·일시정지·마감 게이트, 자동 요청 게이트, US 조기 마감 (2026-10-27)
- [x] 체결 동기화 실패 = 오류, 지난 날짜 미정리 주문 재조회, 대조 전 체결 동기화 (2026-10-27)
- [x] 알림 보관함·재전송, 점검 격상 새 항목·watchdog 경보 피로, 대조 점검 시각·휴장, 끊긴 요청 재등록·알림, 대기열 양보 (2026-10-27)

## 전체 검토 4차 — 나머지 (2026-10-27)
- [x] 데몬 생존 판정 로캘 고정(ps C 로캘), launchd 정상 종료 시 재기동 안 함·잠금 먼저 확인 (2026-10-27)
- [x] 텔레그램 보낸 사람 확인(1:1 또는 TELEGRAM_ALLOWED_USERS, 그룹은 안전 명령만), 킬스위치 자동 발동은 그 계좌만 (2026-10-27)
- [ ] 사용자 확인(선택): 텔레그램을 그룹 채팅으로 쓰면 설정 화면 '연결 키' 의 '명령 허용 사용자 ID' 에 본인 id 를 넣기(없으면 그룹에서는 조회·일시정지·킬스위치만)

## 전체 검토 3차 — 성능 (2026-10-27)
- [x] 유사 뉴스 판정 색인(숫자 토큰 묶음·토큰 역색인·길이 창·글자 다중집합·matcher 재사용) — 판정은 전수 비교와 같음(테스트), 미판정 상한 1000건(오래된 밀린 항목은 novel=0) (2026-10-27)
- [x] 종가표 close 열만 읽기(`dataset.close_table`)·위험 카드 캐시·계좌 화면 `tickOf` (2026-10-27)
- [x] `load_universe`·`load_sectors` mtime 메모(frozen 모델)·overview 섹터 맵 1회 (2026-10-27)
- [x] 대시보드 시세표 체크포인트 요청 제거(`/api/decisions` 에 손절·익절·근거) (2026-10-27)
- [x] 오늘 화면: 고른 종목 단계 목록이 바뀔 때만·새/변경 가능 파일만 다시 받기 (2026-10-27)
- [x] 시세 장 밖 대체 값 한 쿼리 + 원장 v25 색인, 시세 캐시·종목 락 정리 (2026-10-27)
- [x] 분봉 캐시 미스 키별 락·재확인, 분봉 마커 현지 하루 경계를 SQL 로 (2026-10-27)
- [x] 가려진 탭은 첫 읽기 뒤 SSE 변경에도 다시 읽지 않음 (2026-10-27)
- [x] 같은 값 다시 쓰기가 SSE 지문을 흔들지 않게(`set_flag` 무변경 생략·브레이커/ops_level at 유지·positions 값 해시·하트비트 분 단위·내부 커서 제외) (2026-10-27)
- [x] `latest_rows` 필요한 열만·메모, theme 은 스토어에 있는 종목만 (2026-10-27)
- [x] 작업 대기열 진행 폴링은 SSE 리더 탭만 + BroadcastChannel 중계 (2026-10-27)
- [x] 단타 판정자 제출 기록 30분 만료 (2026-10-27)

## 전체 검토 2차 (2026-10-27, `docs/reviews/2026-10-27-full-review-2.md` — Claude 4 + Codex 2 교차검증, `a5ca4ec..d112633` 31 커밋·339 파일)
- [x] 교차검증 2차: 변경 파일 339개 전부 담당 매핑(미배정 0), 읽기 전용·금지 경로·pathspec 한정 diff, 리뷰 6건 완료 뒤 문서화. P0 0·P1 2(2-O1 같은 날 재평가 손절선, 2-L1 유니버스 밖 매수 종목 만기 봉)·P2 32·P3 32, 10-26 반영 항목은 전부 해결 확인 (2026-10-27)
- [x] 묶음 1 즉시(비주문, 2026-10-28 반영, PR #59 → architecture.md 「묶음 1 즉시 비주문」): 2-T1 알림 보관함+호출부 재시도 중복 전송, 2-W1 시세 요청 실패 뒤 이전 시세 '실시간', 2-W2 SSE decisions 지문이 재평가 UPDATE 를 못 봄, 2-W7 `/api/system/settings` 의 tailscale_users 이메일·usage_file 경로, 2-W8 IPv6 Host 파싱, 2-P3 reflect·ledger_backup·universe_members 따라잡기, 2-P5 `process_start` TimeoutExpired, 2-P9 스냅샷 수정주가 감지 close 열만, 2-P14 architecture.md `job_queue` SLOW_KINDS, 2-L2 부트스트랩 n<block·웹 쌍 수 하한, 2-L6 일부 실패 날 섀도 생략, 2-L10 사건 감지 중복 기사·부분 문자열, 2-L11 공시 '해제·해소' critical·300건 상한, 2-L12 모의 체결 점검 재호가·슬리피지 기준, 2-L22 `watch_index` 대소문자, 2-L23 토론 생략 여부 원장 기록
- [x] 묶음 2 주문 경로(PR 본문 diff 요약 → 확인 뒤 머지; 2026-10-27 반영, PR #58, 2026-10-28 사용자 확인 뒤 main 머지): **2-O1(P1)** `31_decision_r<n>` upsert 가 `max_stop_since` 의 손절선을 지움(라운드 보존 또는 max 유지), 2-O2 단조 유지가 SELL/TRIM 결정 손절도 바닥으로, 2-O3 US 손절 지정가가 최신 SELL/TRIM 결정 id 로 재호가 후보, 2-O4 종목당 비중 상한에 pending_buys 미포함, 2-O5 재호가 재제출에 등락률·매매 제한 미전달, 2-O6 재평가 ADD `change_pct=None`, 2-O7 FAKE 세션 고정 NAV 가 킬스위치 기준, 2-O8 단타·리밸런스·재호가 주문이 주 2회·5일 실현손익 집계에(`orders.origin`), 2-O9 권리 일정 조회 실패가 이전 일정 삭제, 2-O11 미정리 주문 재조회 7일 창, 2-O13 실행 키 해제 CLI, 2-O15 paused 중 리밸런스 done, 2-P2 재평가가 빠른 레인에서 체결 동기화 점유, 2-P4 장중 미완성 봉 저장(거래량 0 만 제외), **2-L1(P1)** 일일 스냅샷에 보유·미만기 종목 + `shadow.mature` 보류·partial
- [x] 결정 반영 묶음 3(2026-10-28, 사용자 선택안 + Codex 보완, `docs/reviews/2026-10-28-decision-review.md` → architecture.md 「결정 반영 — 묶음 3」): 2-T2 텔레그램 신뢰할 수 없는 발신자는 조회만, 2-W13 EDGAR User-Agent 를 secrets 로, 2-P13 실시간 시세 양보 사유 + runbook 실전 전용 앱키, 2-O16 입출금 의심 특보(기준 불변), 2-P8 레이트리미터 꺼짐 노출·재시도, 2-W9 Origin/Referer 같은 출처, L6·L10·L13(lesson)·L15
- [ ] 사용자 실행(묶음 3 머지 뒤): 설정 화면 '연결 키' 의 `EDGAR_USER_AGENT`(이름 + 연락 이메일)를 입력 — `config/sources.yaml` 이 자리표시자라 입력 전까지 EDGAR(미국 공시·재무) 수집이 꺼진다
- [ ] 결정 반영 묶음 4(설계, 사용자 선택안 + Codex 보완): 2-O17 현금 ETF 전환 세대별 상태, 2-L3·2-L4·2-L5 섀도 공통 후보·현금 효과 분리·시장별 판정, 2-L8 `post_fill_return` 분리·FIFO 실현손익, 2-L21 입장 변경 이유 기반 수렴 판정, L14 성과 퀀트 기준선을 섀도 `quant_topn` 으로, L16 무효화 조건 구조화
- [ ] 결정 대기(사용자, 2026-10-28 결정 문서 §2·§3·§4): 7번 2-O10 동시 판정 — 최종 재확인 + 원자적 예약(별도 주문 경로 PR) 여부, 19번 L18 — `context.theme`(asof 검증) + 퀀트 기간 해석 한 줄 + 익절·최소 보유 정책 명시, 14번 L12 — 개선 전 보정 자동 활성화(만기 60건) 차단 여부
- [ ] 실기·격리 확인(사용자 확인 대기): 2-O14 US 부분 체결 취소에 원 수량 전체(KIS 거부 여부), 2-O12 단타 보유를 monitor 가 먼저 팔면 `closing` 반복, 2-P1 수집 기한 뒤 `trader tool collect` 프로세스 종료 지연, 2-L24 `rate_limit_event.utilization` 단위(0~1 vs 0~100), 2-P6·2-P11 Windows 파일 락·빌드, 2-W4 SSE 리더 탭 중복, 2-W12 세이브티커 로그인 입력 범위
- [ ] 결정 대기(사용자): `AGENTS.md`(untracked, Phase 12 시점 CLAUDE.md 사본)를 현재판으로 갱신해 커밋할지 — Codex 가 구판 계약으로 오판할 수 있다

## 사건 수혜 (2026-10-27, 사용자 요청 — 전쟁·AI 메모리 부족 등 사건 → 수혜주 발굴)
- [x] `config/events.yaml` 사건 31종·수혜 종목(DART·KIS 시세 확인), `trader events show|check` (2026-10-27)
- [x] 뉴스 키워드 감지(`scan/events`) → scout `event_watch`·tier event·파이썬 검증, shock 사건 테마 안전장치, `[사건 감지]` 알림, 데이터 화면 '사건 감지' (2026-10-27)
- [ ] 사용자 확인 대기: 사건 표(`config/events.yaml`)의 사건·종목·키워드를 보고 빼거나 더할 것(특히 키워드 오탐 — 데이터 화면 '사건 감지'와 텔레그램 `[사건 감지]` 로 며칠 지켜보기)
- [ ] 실기(사용자 확인 대기): 사건이 감지된 날 scan 의 `_discovery.json` 에 `tier: "event"` 후보와 `event_id` 가 남는지, 그 종목 결정이 BUY 면 게이트 사유·`take_profit`·`horizon_days ≤ 10`(shock) 이 지켜지는지

## 사용량·잔고 즉시 갱신 (2026-10-27, 사용자 요청 — 재시작 뒤 사용량 멈춤·장 밖 이체 미반영)
- [x] `usage_probe` 데몬 기동 1분 뒤 첫 실행(`START_SOON_S`), 대기열 kind `usage_probe`(force) + 시스템 현황 '지금 확인'
- [x] 페이지 접속·새로고침 때 관측 5분 넘으면 `POST /api/system/usage/ensure-fresh` 가 확인 요청(60초 1회, 같은 출처)
- [x] 잔고 장 밖 갱신: 데몬 30분·페이지 접속 10분 기준, 체결통보 → 체결 동기화 직후 잔고 즉시
- [ ] 사용자 확인 대기: Trader.app 재시작 1~2분 뒤 시스템 현황 사용량이 '확인 · 방금' 으로 바뀌는지, 장 밖에 이체 뒤 대시보드 새로고침하면 순자산이 바뀌는지

## Phase 26 선물옵션 방향성 레인 (2026-10-27, 사용자 요청 — `docs/superpowers/plans/2026-10-27-phase26-fo.md`, 스펙 `docs/superpowers/specs/2026-10-27-fo-lane-design.md`)
- [x] 단계 1 계좌·브로커·마스터·설정·가격모형 (`feat/phase26-1-fo-broker`): `AccountConfig.asset`·`FoCfg`·`split_accounts`, FO TR 표·모델, `fo_codes`·`fo_master`, `KisFoBroker`(엄격 파서·`get_order_state`)·`FakeFoBroker`, 파생 시세·체인, FO 장 시간·호가단위·Black-76, CLI `_Boot` 세 목록·`fo verify|contracts|chain|order-test`, 거부 목록
- [x] 단계 1 Codex·Claude 교차 리뷰 → 수정 → PR(main): Codex P0 3·P1 2·P2 1 + Claude 7 반영(`docs/reviews/2026-10-27-phase26-fo-review.md`) (2026-10-27)
- [ ] 단계 2 저장 계층·상태기계·게이트·실행·원장 (`feat/phase26-2-store-gate-exec`): 원장 v26, `fo/store`(예약 트랜잭션)·`positions`(복구)·`rules`(대조표·단조)·`sizing`(수명 예산·스트레스·증거금 3겹)·`executor`(진입/축소 분기·회수·청산 절차), 체결·잔고·대조·점검·성과 asset 분기
- [ ] 단계 2 교차 리뷰 → 수정 → PR
- [ ] 단계 3 세션·도구·에이전트 (`feat/phase26-3-session`): `fo-trader.md`·orchestrator fo 모드, `FO_AGENTS`, kind `fo|fo_resume`·`usage.RESUME_KINDS`·상한 트랜잭션, `fo/inputs`, `tool fo bundle|write|summarize`·`submit-fo-decision`, `fo session`
- [ ] 단계 3 교차 리뷰 → 수정 → PR
- [ ] 단계 4 데몬·모니터·재평가·웹 (`feat/phase26-4-daemon-web`): `fo/triggers`·`engine`(보호선·유지 조건·회수)·`reeval`(CAS), 데몬 잡·큐 kind, `/api/fo/*`·`/fo` 페이지, E2E 수명 시나리오·현물 회귀 스냅샷
- [ ] 단계 4 교차 리뷰 → 수정 → PR
- [ ] 단계 5 문서·전체 교차 리뷰 B·체인 ff 머지 (`feat/phase26-5-docs-review`)
- [ ] 실기 1(사용자 확인 대기, 조회 전용): `uv run trader fo verify --account paper-fo` → 시세·마스터·주문가능·잔고 필드 확인(빈 잔고면 방향·NAV 미검증으로 남음) → 비식별 fixture 로 미확인 항목 확정
- [ ] 실기 2(사용자 실행, 거부 목록): `uv run trader fo order-test --account paper-fo --vehicle FUTURES` → `OPTION_BUY` → `OPTION_SELL` 1계약 왕복 → 수단별 `fo_verified` 플래그
- [ ] 실기 3(사용자 확인 대기, 약 $1~2): `fo.enabled: true` 뒤 `FAKE_CLAUDE` 없이 `uv run trader fo session` 1회 → 결정·배수·진입·재평가·청산 로그
- [ ] 완료 기준: 단계 5 머지 뒤 `uv run pytest`·`ruff`·web 게이트 통과, `FAKE_CLAUDE=1 uv run trader fo session --dry-run`·`fo monitor --once --dry-run` 체크포인트 확인, 현물 기본 동작 회귀 없음(스냅샷 테스트)
