# LLM 멀티 에이전트 + ML 주식 자동매매 시스템 설계 스펙

작성: 2026-10-09. 브레인스토밍(18개 요구사항, 질문 18개 + 설계 4절) 결과를 담은 설계 원천 문서다. 구현 플랜은 Phase 마다 `docs/superpowers/plans/` 에 따로 쓴다.

## 0. Context

사용자는 TradingAgents(tauricresearch) 구조를 참고한 **LLM 멀티 에이전트 + 머신러닝 주식 자동매매 프로그램**을 원한다. 한국투자증권(KIS) Open API 로 국내·미국 주식을 모의·실전 계좌에 매매하고, LLM 호출은 전부 Claude Code CLI(`claude -p`, 구독 한도) 로 한다. PC 가 켜져 있으면 무인으로 돌고, 개발 세션이 끊겨도 새 Claude 세션이 상태를 스스로 파악하며, 웹 UI 로 계좌별·전체 정보를 조회·제어한다.

**최종 목표**: 모의 계좌에서 4주 이상 무사고 + 벤치마크 대비 우위를 확인한 뒤 실전 계좌로 수익을 낸다. 실전 전환은 사용자 수동.

### 0.1 요구사항 추적표 (사용자 원문 → 설계 반영 위치)

| # | 사용자 원문 | 반영 위치 |
|---|---|---|
| R1 | 단계적인 프로그램 내용 검토·보완·설계 플랜 | §11 Phase 0~9 (완료 기준 포함) |
| R2 | 세션이 끊기거나 초기화돼도 Claude 가 알아서 확인 | §7 세션 복구(CLAUDE.md + STATUS.md + SessionStart 훅 + 체크포인트 + TODO.md) |
| R3 | KIS API 연동 자동매매 + Claude CLI 이용 | §2 브로커, §4.1 오케스트레이터 세션 |
| R4 | 애널리스트 4명(차트/회사가치/뉴스·거시/투자자 분위기) → 리서치팀 보고 | §4.2 analyst-technical/fundamental/news-macro/sentiment → `AnalystReport` |
| R5 | 리서치팀 2명: 고평가·저평가 논의, 매수·매도 타이밍 | §4.3 researcher-bull ↔ researcher-bear (독립 의견 + 반박 1라운드) |
| R6 | 트레이더: 최종 결론 | §4.4 trader → `Decision` → `submit-decision` 도구 |
| R7 | 참조자료 GitHub | §0.3 (TradingAgents, KIS 공식 예제, python-kis, FinRL, Qlib) |
| R8 | 참조자료 논문 | §12 논문 교훈 → 설계 반영표 |
| R9 | 참조자료 뉴스(세이브티커 등) | §3.2 뉴스 RSS/API + YouTube 자막 + 텔레그램 채널. 세이브티커는 Cloudflare 차단으로 비활성 자리만 |
| R10 | 참조자료 인스타그램 | 제외 (사용자 결정, §0.2) |
| R11 | 참조자료 커뮤니티(토스 등 추가 가능) | §3.2 소스별 조사 결과·on/off 정책 |
| R12 | 참조자료 DART 및 기타 | §3.2 DART, KIS 투자자동향, FRED/ECOS, SEC EDGAR, KRX 공매도 |
| R13 | 에이전트가 후보 선정 | §5.1 scan: 정량 필터 → ML 랭킹 → screener 에이전트 5개 |
| R14 | 장중 보유종목 모니터링 | §5.2 monitor: 규칙 손절 즉시, 트리거는 trader 재호출 |
| R15 | 웹 UI 조회 + 제어 + 차트 | §8 FastAPI + React(Vite), 로컬 전용, 토큰 |
| R16 | 모의·실계좌 분리, 전체 통계 + 계좌 상세 | §2 다계좌, §6.2 계좌별 모드, §8 계좌 선택기·비교 뷰 |
| R17 | ML: 퀀트 예측, 신호 보정, 스크리너 랭킹, RL 사이징 | §9 (데이터 의존 순, Phase 7~9) |
| R18 | macOS 기본, Windows 에서도 실행 | §10 크로스플랫폼, §13 Windows 검증 |

### 0.2 Q&A 로 확정된 사항 (2026-10-09)

| # | 항목 | 결정 |
|---|---|---|
| 1 | 목적 | 모의 계좌 검증 후 실전 수익 |
| 2 | 운영 | 무인 자동(PC 켜져 있으면 데몬이 스케줄 실행) |
| 3 | 계좌 | 모의 1 + 실전 1, 두 환경 앱키 보유, 통합증거금 |
| 4 | LLM 과금 | Claude 구독(Pro/Max) 한도 안. 하루 호출 수 상한을 파이썬이 관리 |
| 5 | 리스크 기본값 | 공격적: 종목당 25%, 최대 5종목, 일일 -5% 시 신규매수 중단, 종목 손절 -15%, 현금 비중 제한 없음 |
| 6 | 분석 규모 | 시장당 신규 후보 5 + 보유종목 (약 15~25종목/일) |
| 7 | 리서처 토론 | 독립 의견 1회 + 반박 1라운드. 독립 의견 보관, 과잉 수렴 플래그 |
| 8 | 실행 | 계좌별 모드. 모의 `auto` 고정, 실전 `off` → `approve` → `auto`. 결정은 종목당 1회, 실행은 계좌마다 |
| 9 | 장중 | 규칙 손절은 파이썬이 즉시, 급등락·익절·공시 트리거는 trader 재호출 |
| 10 | 후보 선정 | 정량 필터 → ML 랭킹 → screener 에이전트가 최종 5개 |
| 11 | 웹 UI | FastAPI + React(Vite) + TradingView Lightweight Charts, 127.0.0.1, bearer 토큰 |
| 12 | ML 순서 | 퀀트·랭킹 → 신호 보정(결정 60건 후) → RL(모의 한정 실험) |
| 13 | 플랫폼 | macOS 개발·운영, Windows 동일 실행 보장 |
| 14 | 뉴스 | RSS/검색 뉴스 + YouTube 자막 + 텔레그램 공개채널. 세이브티커(saveticker.com/news)는 뉴스 모음 사이트이며 자동 수집 불가 |
| 15 | 인스타그램 | 완전 제외 |
| 16 | 커뮤니티 | robots·약관 허용 소스만 기본 on, 회색지대는 구현하되 off, SPA 는 조사 후 결정 |
| 17 | 보유 키 | DART, 텔레그램 봇+chat_id, FRED/ECOS, 네이버 검색 API |
| 18 | 성공 기준 | 모의 4주 이상 무사고 + 벤치마크(B&H·1/N) 대비 우위 + MDD -10% 이내 → 사용자가 수동 전환 |
| 가정 | 추가 규칙 | 회전율 제약(최소 보유 3거래일, 종목당 주 2회, 기대수익 < 왕복비용×2 거부), 손실 후 보수 전환, US 지정가 강제, 환전 API 미사용, 익명화 옵션 기본 off |
| 접근 | 아키텍처 | **B: Claude Code 세션이 서브에이전트를 조율, 파이썬 도구가 집행** (§1) |

### 0.3 조사로 확인된 외부 제약

**Claude Code 헤드리스 (설치 버전 2.1.294, `claude --help` 기준)**
- `-p` 로 비대화 실행. `--output-format json` 이면 결과 JSON(`result`, `session_id`, `total_cost_usd`, `duration_ms`, `is_error`, `structured_output`). `--json-schema <schema>` 로 구조화 출력 강제.
- 서브에이전트: `.claude/agents/*.md`(프런트매터 `name`, `description`, `tools`, `model`) 또는 `--agents <json-or-file>`. 오케스트레이터가 서브에이전트를 부르려면 `--tools` 에 `Agent` 가 포함돼야 한다(Phase 3 에서 실호출로 확인).
- 도구 제한: `--tools "Bash,Agent"` + `--allowedTools "Bash(python -m trader tool *)"` + `--permission-mode dontAsk`. `--setting-sources ""` 로 사용자·프로젝트 설정(훅·플러그인)을 끊고, cwd 는 CLAUDE.md 가 없는 빈 디렉터리로 둔다. `--bare` 는 OAuth(구독)를 끄므로 쓰지 않는다. `--max-turns` 는 이 버전 도움말에 없다.
- `--session-id <uuid>` 발급, `--resume <id>` 재개. 같은 세션 동시 재개 금지. `--max-budget-usd` 로 세션 예산 상한.
- 구독 한도는 모든 `claude -p` 프로세스가 공유. 429 시 `CLAUDE_CODE_RETRY_WATCHDOG=1` + 백오프.
- 무인 스케줄링은 Claude 자체 기능이 아니다 → 파이썬 데몬(APScheduler) + OS 로그인 시작 등록.

**KIS Open API** (공식 GitHub `koreainvestment/open-trading-api` README·커뮤니티 확인)
- 토큰 24h 유효, 발급 1분 1회 → 파일 캐싱 필수. 실전·모의 앱키·도메인·tr_id 가 다르다.
- 호출 한도 실전 20건/초, 모의 2건/초, 초과 시 `EGW00201`(재시도 가능).
- 모의 서버는 순위분석·투자자동향·재무류 미지원 → **시세·스크리닝·수급은 실전 서버, 주문·잔고는 계좌별 서버**.
- 미국주식 모의는 지정가만, 주간거래 미지원. 환전 API 없음(통합증거금) → 당일 누적 주문금액을 원장에서 차감.
- 휴장일 조회 `CTCA0903R` 1일 1회 캐시. 라이브러리는 공식 예제 기반 자체 얇은 클라이언트(python-kis 는 참고만).

**DART Open API**: 인증키 1개, 일 20,000건. `corpCode.xml` 주 1회 캐시, `list.json`(공시), `company.json`, `fnlttSinglAcntAll.json`(재무). 공시는 접수 시각(`rcept_dt`) 사용.

**뉴스·거시**: 구글뉴스 RSS(키 없음), 네이버 검색 API(뉴스, 키 보유, 일 25,000건), FRED(키 보유), ECOS(키 보유), SEC EDGAR(`User-Agent: 이름 이메일` 필수). **세이브티커** `saveticker.com/news`: robots 는 허용이나 Cloudflare 봇 검증으로 모든 요청 403 → 자동 수집 불가, `sources.yaml` 에 비활성 항목만 두고 같은 원천(언론사 기사)을 RSS/API 로 대체. YouTube 자막은 `youtube-transcript-api`, 텔레그램 공개채널은 Telethon(Phase 6).

**커뮤니티 (2026-10-09 직접 확인)**

| 소스 | 확인 결과 | 정책 |
|---|---|---|
| Reddit | 서브레딧 `.rss` 200, 검색 429. 공식 OAuth API 무료(100 QPM) | **기본 on** (RSS → OAuth) |
| 클리앙 주식게시판 | 목록 HTML 200, robots 가 `/service/board/` 허용·검색·쿼리 금지 | **기본 on** (목록 1페이지, 검색 없음) |
| DC 미국주식·코스피 마이너갤 | HTML 200, robots 가 AI 크롤러(ClaudeBot 등) 명시 차단 | 구현, **기본 off** |
| 에펨코리아 주식게시판 | HTML 200 + 검색 가능, robots 가 AI 크롤러 차단 | 구현, **기본 off** |
| 토스증권 커뮤니티 | SPA, robots 허용, 내부 API 는 비공개(GET 405)·약관상 자동 수집 금지 | **영구 off** (Phase 6 결정: 공개 API 없음) |
| 네이버 종목토론실 | `stock.naver.com` SPA, robots 전면 차단 | **영구 off** (Phase 6 결정: robots 차단) |
| StockTwits | 403, 인증 필요 | 제외 |
| X, Seeking Alpha | 로그인·유료 API | 제외 |

수집기는 공통 User-Agent, 소스당 요청 간격 ≥ 2초, 실패 시 빈 결과. 사용자가 `sources.yaml` 에서 켠 소스의 약관 책임은 사용자에게 있음을 설정 파일 주석에 적는다.

**GitHub 참조**: TradingAgents(에이전트 역할·토론 구조), `koreainvestment/open-trading-api`(tr_id·요청 형식), python-kis(설계 참고), FinRL(RL 환경 패턴), Qlib(ML 기준선·워크포워드).

---

## 1. 전체 아키텍처 (접근 B)

**스택**: Python 3.12, uv, pydantic v2, SQLite(WAL), httpx, pandas/pyarrow, LightGBM, scikit-learn, python-telegram-bot, APScheduler, tzdata. 웹은 FastAPI + React(Vite, TypeScript) + TradingView Lightweight Charts. RL 은 gymnasium + stable-baselines3(선택 extra). macOS·Windows 공용(§10).

### 1.1 두 층

| 층 | 무엇 | 책임 |
|---|---|---|
| **바깥: 파이썬 데몬** `python -m trader daemon` | APScheduler 상주 프로세스 | 스케줄에 맞춰 `claude -p` 오케스트레이터 세션을 **하루 정해진 횟수만** 띄움, 장중 monitor 루프, 주말 ML 학습, 웹 서버, 텔레그램 수신, 단일 인스턴스 잠금 |
| **안쪽: 오케스트레이터 세션** `claude -p` | `.claude/agents/orchestrator.md` 가 메인 에이전트 | 종목 목록을 받아 서브에이전트(screener, analyst×4, researcher×2, trader, reflector)를 소집. 외부 세계와는 **파이썬 CLI 도구로만** 접촉 |

LLM 이 쓸 수 있는 도구는 `python -m trader tool <이름>` 뿐이다(`--tools "Bash,Agent"`, `--allowedTools "Bash(python -m trader tool *)"`). 도구 목록:

| 도구 | 역할 | 전제조건(파이썬이 검증) |
|---|---|---|
| `tool collect --ticker --market` | `00_inputs.json` 수집·저장(있으면 건너뜀) | 없음 |
| `tool checkpoint list/read/write --ticker --stage` | 단계 JSON 저장·조회(pydantic 스키마 검증) | `write` 는 스키마 통과 |
| `tool submit-decision --ticker` (stdin JSON) | `Decision` 접수 → 계좌별 리스크 게이트 → 주문 큐 | 해당 종목에 `10_analyst_*` ≥ 2개와 `25_research_summary` 가 **파일로 존재**할 때만 접수 |
| `tool status` | 오늘 진행 상황·남은 종목·호출 수 | 없음 |

주문은 `submit-decision` 안에서만 일어난다. LLM 이 단계를 건너뛰면 도구가 거부하고, 도구를 안 부르면 주문이 없을 뿐이다. 즉 **순서와 리스크는 프롬프트가 아니라 도구의 전제조건이 강제한다.**

### 1.2 실행 루프 (모두 `python -m trader <command>`)

| 명령 | 실행 시점(KST) | LLM | 역할 |
|---|---|---|---|
| `scan` | KR 07:30 / US 21:30 | 시장당 세션 1회(screener) | 정량 필터 ~30 → ML 랭킹 → screener 5개 |
| `analyze` | scan 직후 | 시장당 세션 1회(오케스트레이터가 종목당 서브에이전트 7~9회) | 후보 + 보유종목 분석 → `submit-decision` → 계좌별 주문 |
| `monitor` | 장중 60초 | 트리거 시에만 trader `--resume` | 손절 즉시, 트리거 → 재평가 → 계좌별 주문 |
| `reflect` | 장 마감 후 | 세션 1회(reflector) | 결과 복기 → 메모리 (Phase 8) |
| `ml train` | 토 10:00 | 없음 | 피처 갱신·워크포워드 재학습·레지스트리 |
| `web` | 상시 | 없음 | 웹 UI |
| `daemon` | 로그인 시 | — | 위 전부를 `schedule.yaml` 대로 실행 |
| `tool <이름>` | LLM 이 호출 | — | §1.1 도구 |

### 1.3 디렉터리 구조

```
trading-agent/
├─ CLAUDE.md                       개발 세션 안내서 (§7)
├─ .claude/
│   ├─ agents/                     orchestrator, screener, analyst-technical, analyst-fundamental,
│   │                              analyst-news-macro, analyst-sentiment, researcher-bull,
│   │                              researcher-bear, trader, reflector (10개)
│   ├─ hooks/session_start.py      STATUS.md + TODO.md 주입 (파이썬)
│   └─ settings.json               훅, 허용/거부 도구
├─ config/
│   ├─ settings.yaml               시장, 리스크, 모델 배정, 토론 라운드, 익명화, llm 예산·동시성, web
│   ├─ accounts.yaml               계좌: id, env(paper|real), 계좌번호, 시장, 모드, 초기자본
│   ├─ universe.yaml               스크리닝 조건, 제외 종목, 수동 관심종목
│   ├─ sources.yaml                뉴스·채널·커뮤니티 소스별 on/off, 키 없는 소스 자리
│   ├─ ml.yaml                     피처, 라벨 horizon, 학습 윈도우, 승격 기준
│   └─ schedule.yaml               루프 실행 시각
├─ src/trader/
│   ├─ cli.py                      명령 진입점 (typer)
│   ├─ daemon.py                   APScheduler + web + 잠금
│   ├─ platform/                   paths.py, claude_bin.py, installer.py (OS 분기는 여기만)
│   ├─ orchestrator/               session.py(claude -p 세션 실행·예산·호출 집계), tools.py(tool 명령 구현),
│   │                              run_store.py(체크포인트), executor.py(계좌별 실행)
│   ├─ agents/                     schemas.py(출력 스키마), fake.py(FAKE_CLAUDE 응답), anonymize.py
│   ├─ sources/                    kis_market, indicators, dart, edgar, news, macro, community, youtube, telegram_channel
│   ├─ broker/                     kis_auth, kis_client, tr_ids, orders, accounts, models
│   ├─ risk/                       rules.py, stance.py, costs.py
│   ├─ ml/                         features, dataset, labels, models/{quant,ranker,calibration,rl_sizer}, registry, evaluate
│   ├─ memory/                     store, retrieval, reflect
│   ├─ ledger/                     db.py(스키마·마이그레이션), repo.py
│   ├─ control/control.py          일시정지·계좌 모드·관심종목 런타임 제어
│   ├─ notify/telegram.py          발신·수신
│   ├─ web/                        FastAPI routers, sse.py
│   └─ status.py                   STATUS.md 생성기
├─ web/                            React + Vite (dist/ 를 FastAPI 가 서빙)
├─ state/                          STATUS.md(커밋), runs/, features/, models/, memory/, manual_inputs/,
│                                  claude_cwd/(빈 cwd), trader.db, kis_token_<env>.json, logs/
├─ secrets/.env                    KIS 앱키×2, DART, 텔레그램, 네이버, FRED/ECOS, 웹 토큰 (gitignore)
├─ docs/                           superpowers/specs, superpowers/plans, TODO.md
└─ tests/
```

### 1.4 데이터 흐름

`scan`(정량 → ML → screener) → 종목 목록 → `analyze` 세션: 종목마다 `tool collect` → `00_inputs.json` → analyst×4 병렬(+ quant 리포트, Phase 7) → `10_analyst_*` → researcher-bull/bear 라운드 0(독립) → 라운드 1(반박) → 파이썬 `25_research_summary`(수렴 감시) → trader → `tool submit-decision` → `30_decision` → calibration(Phase 8) → `risk.rules`(계좌별) → `40_risk_check_<계좌>` → executor → broker → `50_order_<계좌>` → ledger → notify → STATUS.md → (보유기간 종료 후) reflect → memory / ML 학습 데이터.

---

## 2. 브로커 계층 (`src/trader/broker/`) — 다계좌

- `KisAuth(env)` 가 env(paper|real)별 토큰을 `state/kis_token_<env>.json` 에 캐시(만료 1시간 전 갱신, 발급 실패 후 60초 재시도 금지). `KisClient(env, auth)` 가 레이트리밋(실전 20/s, 모의 2/s, 토큰 버킷)·재시도(`EGW00201`)·`KisApiError(msg_cd, msg1, tr_id)` 통일.
- `KisMarket(client)` 은 **실전 env 만 허용**(시세·일봉·분봉·호가·투자자동향·지수·휴장일). `KisBroker(account, client)` 는 계좌별 잔고·주문·취소·체결이고 `client.env == account.env` 를 요구.
- `tr_ids.py` 가 실전/모의·국내/해외 tr_id 의 유일한 출처. `accounts.py` 가 `accounts.yaml` 을 읽어 계좌 객체를 만든다. `FakeBroker` 는 테스트·dry-run 용.
- 금액은 `Decimal`. 일봉·투자자동향은 날짜 오름차순. `OrderRequest.exchange` 는 US 필수.

---

## 3. 데이터 수집 계층 (`src/trader/sources/`)

모든 수집기는 `collect(ticker, asof) -> Section{data, received_at, error}`. 실패해도 계속(절만 비움). `tool collect` 가 애널리스트 역할별로 묶어 `00_inputs.json` 으로 저장. `sources.yaml` 로 소스별 on/off.

### 3.1 애널리스트별 입력 (R4)

| 애널리스트 | 입력 | 수집기 |
|---|---|---|
| technical | 일봉 250, 당일 분봉, 호가, 파이썬 계산 지표(SMA 5/20/60/120, RSI14, MACD, KDJ, 볼린저, ATR, ROC, 거래량 Z-score, 52주 고저 위치) | kis_market, indicators |
| fundamental | KR: DART 3개년 + 최근 분기 재무, 비율(매출성장률·영업이익률·ROE·부채비율·PER/PBR/PSR·동종업 중앙값 대비), 30일 공시. US: EDGAR companyfacts 동일 비율 | dart, edgar |
| news-macro | 종목 뉴스 20건·거시 10건(제목·요약·출처·수신 시각), 지표(KOSPI/S&P 500/나스닥, USD/KRW, 달러지수(연준 광의), WTI, 미 10년물, VIX, 기준금리), 7일 이벤트, 채널 자막 요약 | news(구글 RSS·네이버 API), macro(FRED·ECOS·KIS 지수), youtube, telegram_channel |
| sentiment | 외인·기관·개인 순매수 20일(KIS), 공매도·대차잔고(KRX), 커뮤니티 글 50건(§0.3 정책), 검색트렌드, `manual_inputs/` | kis_market, community, trends |
| quant (LLM 아님) | 피처 스냅샷 → 모델 확률 | ml/features, ml/models/quant |

### 3.2 참조자료 처리 방침 (R7~R12)

- **뉴스 채널(R9)**: `sources.yaml` 에 RSS 목록·YouTube 채널 ID·텔레그램 채널명. 세이브티커는 `enabled: false` 고정 항목(사유 주석). Phase 2 는 RSS/API, 채널 자막·텔레그램은 Phase 6.
- **인스타그램(R10)**: 제외. 텔레그램 봇 자유 입력(링크·텍스트·이미지)이 `state/manual_inputs/<날짜>/` 로 저장돼 sentiment 입력에 들어가는 경로는 유지(Phase 6).
- **커뮤니티(R11)**: §0.3 표의 정책. 수집기는 소스별 파일, 공통 요청 제한·User-Agent, 실패 시 빈 결과. 토스·네이버는 Phase 6 에서 브라우저로 API 를 조사한 뒤 구현 여부 결정.
- **DART(R12)**: `corpCode.xml` 주 1회 캐시, 재무제표는 호출마다, 공시는 접수 시각. **기타**: FRED, ECOS, KRX 공매도(Phase 6), SEC EDGAR(User-Agent).

> 2026-10-25 변경: 사용자가 SaveTicker 전용 브라우저 수집을 승인했다. 직접 HTTP 수집 비활성 정책은 유지하고 독립 `trader saveticker` CLI로 수집한다. 범위·인증·파일·실행 계약은 `2026-10-25-saveticker-design.md` 및 `docs/saveticker-collection.md` 참조.

### 3.3 룩어헤드 방지
모든 항목에 `received_at`. 뉴스·커뮤니티·공시는 `asof` 이후 발행분 제외, EDGAR 는 `filed <= asof`, 재무비율은 공시일 이후부터 적용(사업연도 Y 값은 Y+1 년 4월 1일부터). 과거 `asof` 재수집은 금지(수집기는 현재 데이터만 주므로)하고 Phase 7 백테스트가 피처 스토어로만 지원. 익명화(`anonymize: true`)는 도구가 프롬프트 직전에 종목명·티커를 치환하고 응답에서 복원.

### 3.4 피처 스토어 (ML 공용)
`ml/features.py` 가 매일 유니버스 전체(KR ~200, US ~100)에 대해 지표·수급·재무비율·뉴스/커뮤니티 건수를 한 행으로 `state/features/<date>.parquet` 에 point-in-time 저장. 초기 적재는 KIS 일봉(페이징, 3년)·투자자동향(최근 30영업일만 제공)·DART 분기 재무로 재구성.

---

## 4. 에이전트 (`.claude/agents/`, `src/trader/orchestrator/`, `src/trader/agents/`)

### 4.1 오케스트레이터 세션 (`orchestrator/session.py`)

파이썬이 띄우는 명령(개념):
```
claude -p --output-format json --model <orchestrator 모델>
  --tools "Bash,Agent" --allowedTools "Bash(python -m trader tool *)" --permission-mode dontAsk
  --setting-sources "" --disable-slash-commands --session-id <uuid> --max-budget-usd <b>
  (cwd = state/claude_cwd, env CLAUDE_CODE_RETRY_WATCHDOG=1)
stdin: {"market": "KR", "date": "...", "tickers": [...], "resume": true}
```
- 오케스트레이터 프롬프트(`orchestrator.md`)의 절차: `tool status` → 종목마다 `tool collect` → `tool checkpoint list` 로 남은 단계 확인 → 서브에이전트 호출 순서(analyst×4 병렬 → bull/bear 라운드 0 → 라운드 1 → trader) → 결과를 `tool checkpoint write` → `tool submit-decision`. 완료 종목은 건너뛴다.
- 서브에이전트는 **도구 없음**(순수 추론), 출력은 JSON 만. 오케스트레이터가 받은 JSON 을 그대로 `checkpoint write` 에 넘기고 파이썬이 pydantic 으로 검증(실패 시 오류 메시지를 돌려줘 1회 재작성).
- **비용·호출 상한**: 세션 `--max-budget-usd`, 세션당 종목 수는 파이썬이 정함, `agent_calls` 테이블에 세션·도구 호출을 기록해 일일 상한(설정값) 초과 시 데몬이 다음 세션을 띄우지 않고 알림. 429 는 백오프 후 재시도 1회.
- **모델 배정**(`settings.models`): orchestrator `sonnet`, screener `haiku`, analyst×4 `sonnet`, researcher-bull `sonnet`, researcher-bear `opus`(백본 편향 상쇄), trader `opus`, reflector `sonnet`.
- `FAKE_CLAUDE=1` 이면 `session.py` 가 `claude` 를 띄우지 않고 `agents/fake.py` 가 같은 도구 순서를 파이썬으로 흉내 내 고정 JSON 체크포인트를 만든다(테스트·dry-run·Windows 확인용).
- **프롬프트 공통 규칙**(모든 md): 제공된 데이터만 근거, 수치 생성 금지, `data_gaps` 명시, `UNKNOWN` 허용, 근거는 검증가능한 사실로, 한국어 출력, JSON 외 텍스트 금지.

### 4.2 애널리스트 4명 (R4) + 퀀트

```
AnalystReport { ticker, asof, role, summary(≤300자), bullets[≤7],
  signal: STRONG_BUY|BUY|NEUTRAL|SELL|STRONG_SELL|UNKNOWN, score(-1~1), confidence(0~1),
  horizon: short|mid|long, key_risks[], counter_evidence[], evidence[{claim, source_id}], data_gaps[] }
```
| 에이전트 | 관점 (금융 CoT: 분해 → 조각별 결론 → 종합) |
|---|---|
| analyst-technical | 추세·모멘텀·지지저항·거래량, 진입/이탈 가격대 |
| analyst-fundamental | 성장성·수익성·건전성·밸류에이션(동종업·과거 대비), 적정가치 범위 |
| analyst-news-macro | 종목 뉴스 영향도, 거시 이벤트 리스크, 7일 일정, 부정 뉴스 가중 |
| analyst-sentiment | 수급, 커뮤니티 온도, 과열/공포, 역발상 포인트 |
| quant (Phase 7) | 모델 확률을 같은 스키마로 변환(`role: quant`, evidence 에 상위 피처 기여도). 미승격 시 생략 |

### 4.3 리서처 2명 (R5) — 독립 의견 후 반박 1라운드

```
DebateTurn { side, round, stance: BULLISH|NEUTRAL|BEARISH, valuation_view: UNDERVALUED|FAIR|OVERVALUED|UNKNOWN,
  arguments[≤5], rebuttals[≤5], timing: {action: NOW|CONDITIONAL|WAIT, condition, price_zone},
  conviction(0~1), what_would_change_my_mind[] }
```
1. 라운드 0: bull·bear 가 리포트만 보고 독립 작성.
2. 라운드 1: 상대의 라운드 0 턴을 읽고 반박·수정(`settings.debate_rounds`, 기본 1, 최대 2).
3. 수렴 감시(파이썬, `tool checkpoint write --stage 25_research_summary` 시 계산): 부호 있는 확신 `signed = conviction × {BULLISH:+1, NEUTRAL:0, BEARISH:-1}`, 차이 `gap = |signed(bull) − signed(bear)|`. 라운드 0 의 gap 이 0.2 초과였다가 최종 gap 이 0.2 이하로 붕괴하면 `sycophantic: true`, `preferred: round0`.
4. `25_research_summary.json` = 라운드 0 + 최종 + 쟁점(양측 rebuttals) + 수렴 플래그.
- researcher-bull: 저평가 논거 + 매수 타이밍. researcher-bear: 고평가 논거 + 매도 타이밍·리스크 시나리오, "동조 금지, 악마의 변호인 유지".

### 4.4 트레이더 (R6)

- 입력: 리포트 + research_summary(`preferred` 표시) + 포지션·현금(계좌 집계) + 리스크 한도·성향 + 거래비용 + 메모리 검색(엠바고 통과분, Phase 8) + 보정 모델 출력(Phase 8).
```
Decision { ticker, action: BUY|SELL|HOLD|ADD|TRIM, target_weight(0~0.25), order_type: LIMIT|MARKET, limit_price,
  stop_loss, take_profit, horizon_days, confidence, rationale(≤500자), invalidation_conditions[], memory_ids_used[] }
```
- md 규칙: 검증가능성으로 판단하고 자신감 어조는 무시, 기본값 HOLD, 왕복비용(KR ~30bp / US ~50bp)을 넘는 기대수익 없으면 거래 금지, `sycophantic` 이면 라운드 0 우선, US 는 LIMIT.
- 결정은 계좌와 무관하게 종목당 1회. 오케스트레이터 세션의 `session_id` 와 trader 서브에이전트 산출물을 저장해 monitor 가 재평가 시 컨텍스트로 쓴다.

### 4.5 체크포인트 (R2)
`state/runs/<date>/<market>/<ticker>/`: `00_inputs`, `05_quant`, `10_analyst_<role>`×4, `20_debate_r<n>_<side>`, `25_research_summary`, `30_decision`, `35_calibration`, `40_risk_check_<account>`, `50_order_<account>` (.json). 모든 파일은 `{"schema_version", "data", "call": {agent, model, session_id, cost_usd, duration_ms}}`. 존재하면 건너뛴다. `submit-decision` 전제조건: `10_analyst_*` ≥ 2개 + `25_research_summary`.

---

## 5. 후보 발굴과 장중 모니터링

### 5.1 `scan` (R13)
유니버스(`universe.yaml`: KR 시총 상위 ~200, US ~100, 제외·수동 관심종목) → 정량 필터(거래대금·변동성·모멘텀·52주 위치, 상한 30) → ML 랭킹(Phase 7, 없으면 정량 점수) → screener 세션(`haiku`, 입력: 상위 30 의 요약·뉴스 헤드라인) → `Screened{picks[5]{ticker, reason}}` → `analyze` 종목 목록 = picks + 보유종목. 구독 한도 도달 시 picks 를 3 → 1 로 축소.

### 5.2 `monitor` (R14)
60초 주기로 보유종목 시세. (a) 손절선(결정의 `stop_loss`, 없으면 -15%) 도달 → 파이썬이 즉시 매도(`auto`·`approve` 모두, 사후 통보). (b) 트리거: ±7% 급등락, `take_profit` 도달, 신규 공시, 결정의 `invalidation_conditions` 가격 조건 → `triggers` 기록 → trader 재호출 세션(`--resume`, 입력: 트리거 + 현재 시세 + 원래 결정) → `Decision` → 같은 게이트·executor. 같은 종목 재평가는 30분에 1회.

---

## 6. 리스크 · 계좌별 실행 · 원장 · 알림 · 메모리

### 6.1 `risk/rules.py` (순수 함수, TDD) — 계좌 단위
입력: `Decision`, 해당 계좌 포트폴리오·당일 손익·성향·config → `RiskVerdict{account_id, approved, adjusted_qty, reasons[]}`.
- 종목당 ≤25%, 총 ≤5종목, 일일 손실 ≤-5% 면 신규매수 거부, 가능금액(당일 누적 주문 차감) 초과 금지, 동일 종목 당일 중복 매수 금지.
- 회전율: 최소 보유 3거래일(손절 제외), 종목당 주 2회, 기대수익 < 왕복비용×2 면 거부(`costs.py`: KR 30bp, US 50bp).
- `stance.py`: 최근 5거래일 실현손익 음수면 `conservative`(종목당 15%, 신규 2종목), 회복 시 `normal`.
- US 지정가 강제. confidence < 0.5 면 수량 절반. RL 사이저(Phase 9) 제안은 한도 안에서만.

### 6.2 계좌별 실행 (`orchestrator/executor.py`, R16)
| mode | 흐름 |
|---|---|
| `auto` | 게이트 승인 → 즉시 주문 → 알림. 모의 고정 |
| `approve` | 게이트 승인 → 승인 요청(텔레그램 + 웹, 30분 유효) → 승인 시 주문 |
| `off` | 결정·게이트 결과만 기록. 실전 초기값 |
하나의 `Decision` 에 계좌마다 `RiskVerdict`·주문이 따로 생긴다. 모의 계좌는 그림자 포트폴리오. 모드는 `control` 테이블이 yaml 을 덮어쓰고, 실전 `auto` 전환은 확인 문자열(`LIVE`) 필요. 손절은 §5.2.

### 6.3 `ledger/` (SQLite WAL, 타임아웃 30s)
`accounts, runs, agent_calls, analyst_reports, debate_turns, decisions, calibrations, risk_checks(account_id), orders(account_id), fills(account_id), positions_snapshot(account_id, date), approvals(account_id), triggers, memory_entries, ml_runs, ml_predictions, control, schema_version`. 모든 주문은 `decision_id + account_id`. `schema_version` + 순서 마이그레이션 함수를 Phase 0 부터 둔다. 일별 집계: 계좌별 NAV·손익, 결정 분포, 수렴율, 호출·비용.

### 6.4 `notify/telegram.py`
발신: 일일 요약(계좌별), 트리거, 승인 요청(계좌 표시), 에러, 주간 성과, 호출 상한 도달. 수신: `/status [account]`, `/approve <id>`, `/reject <id>`, `/pause`, `/resume`, `/mode <account> <auto|approve|off>`, 자유 입력 → `manual_inputs/`. 승인 대기는 DB 기록, 재시작 후 유효, 웹과 동일 레코드. 토큰·chat_id 없으면 `NullNotifier`.

### 6.5 `memory/` (Phase 8)
엔트리 `{id, ticker, decided_at, situation_vector(12차원), situation_summary, decision, realized_return, alpha_vs_benchmark, reflection, outcome_at, tier}`, 결과는 모의 계좌 기준. 3층 반감기(14/90/365일), 검색 = 최신성+관련성(코사인)+중요도, 승격·삭제. **Outcome Embargo**: `horizon_days` 경과 전 검색 제외. reflector 복기 2종(신호→가격, 결정→손익), `memory_ids_used` 기록. 서킷브레이커: 드로다운 10% 또는 VIX 급등 시 검색 중단. 워밍업 50건.

---

## 7. 세션 복구 (R2)

- **운영 세션**: 단계 산출물이 전부 체크포인트 파일이므로, 세션이 끊기면 데몬이 같은 날짜로 재실행하고 오케스트레이터가 `tool checkpoint list` 로 남은 단계만 한다. `runs` 테이블에 세션 시작·종료·상태 기록.
- **개발 세션**: `CLAUDE.md`(목적, 에이전트·모델 역할, 디렉터리 지도, "상태는 `state/STATUS.md` → `python -m trader status`", 실행 명령, 금지사항, "다음 할 일은 `docs/TODO.md`", 스펙 경로, 코딩 규칙). `state/STATUS.md`(자동, ≤8,000자): 마지막 실행·결과, 계좌별 모드·성향·NAV·손익·포지션, 미승인 주문, 실패 단계, 오늘 LLM 호출·비용, 결정 분포, ML 모델 버전, 다음 예정 실행, Phase 진행도. `.claude/hooks/session_start.py`(표준 라이브러리, UTF-8)가 STATUS.md + TODO.md 미완료 항목을 출력(10,000자 상한). `settings.json` SessionStart(matcher `startup|resume|clear|compact`) 명령은 `uv run python .claude/hooks/session_start.py`(`python` 이 PATH 에 없는 환경이 있어 `uv run` 을 앞에 붙인다. 2026-10-09 Phase 0 에서 정정). `docs/TODO.md` 는 Phase 별 체크리스트. git 은 Phase 마다 커밋·PR.

---

## 8. 웹 UI (R15, R16) — FastAPI + React(Vite)

- 바인드 `127.0.0.1:8787`. 조회 API 는 토큰 없음, 제어 API(승인·모드·일시정지·관심종목)는 `Authorization: Bearer <WEB_TOKEN>`. 실시간은 SSE(`/events`).
- 페이지: 전체 대시보드(계좌 합산 NAV·손익·결정 분포·호출 비용), 계좌 선택기 → 계좌 상세(포지션·주문·체결·성향), 계좌 비교(모의 vs 실전 동일 결정 결과), 오늘의 실행(종목별 단계 산출물 열람, 토론 뷰어), 주문/승인(대기 목록·승인/거부), 차트(Lightweight Charts, 결정·체결·트리거 마커, 지표 오버레이), 데이터 탐색(00_inputs, 피처), ML(모델 버전·성능·피처 중요도), 설정/제어.
- 라우터: `accounts, portfolio, runs, orders, charts, ml, control, system`. 모의 주문이 실전 뷰에 섞이지 않도록 모든 조회가 `account_id` 필터.

---

## 9. 머신러닝 (R17) — `src/trader/ml/`

- **9.1 퀀트 예측(Phase 7)**: LightGBM, 피처 스토어, 라벨 5거래일 수익률 3분류(상승/보합/하락), 라벨 엠바고, 워크포워드 비중첩 평가. 승격 기준(`ml.yaml`: 검증 구간 정확도·수익 기준선 대비) 통과 시 `05_quant.json` 투입.
- **9.2 스크리너 랭킹(Phase 7)**: 같은 피처로 상위 수익 확률 랭커 → scan 정렬.
- **9.3 신호 보정(Phase 8, 결정 ≥ 60건)**: 애널리스트 신호 × 상황 벡터 → 적중률 → `35_calibration.json` → 트레이더 입력.
- **9.4 RL 사이징(Phase 9, 실험)**: gymnasium env(상태: 결정·포지션·변동성, 행동: 비중 0~0.25), PPO 워크포워드, 드로다운 페널티, 시드 3회, 모의 한정 섀도, 수동 승격.
- **9.5 학습 잡·레지스트리**: 주말 `ml train`, `state/models/<모델>/<버전>/` + 메타(학습 구간·성능), 승격은 기준 자동 판정 + 로그.

---

## 10. 스케줄링·운영·크로스플랫폼 (R18)

- **10.1 데몬**: APScheduler 로 `schedule.yaml`(KR scan 07:30, analyze 직후, monitor 09:00~15:30 60초; US scan 21:30, analyze, monitor 23:30~06:00; reflect 장 마감 후; ml train 토 10:00) 실행, web 동시 기동, `state/daemon.lock` 단일 인스턴스, 휴장일 건너뜀. `install` 명령이 로그인 시 시작 등록(macOS launchd plist / Windows 작업 스케줄러 XML, 파이썬 템플릿).
- **10.2 원칙**: `pathlib`, 하드코딩 절대경로 금지, 파일명에 `:` 금지(`YYYYMMDD_HHMMSS`). 저장 UTC ISO8601, 표시 KST/ET(`zoneinfo`+`tzdata`). 콘솔 UTF-8 강제. subprocess `shell=False, encoding="utf-8", errors="replace"`. SQLite WAL. `uv`. 셸 스크립트 없음(훅·설치·유틸 전부 파이썬). OS 분기는 `platform/` 만. 의존성은 Windows 휠 제공 라이브러리만.

---

## 11. 단계별 구현 계획 (R1)

각 Phase 는 독립 브랜치·커밋·PR, `docs/TODO.md` 로 추적, TDD. Phase 마다 `superpowers:writing-plans` 로 세부 플랜.

| Phase | 내용 | 완료 기준 |
|---|---|---|
| 0 골격 | uv 프로젝트, CLI 진입점, `platform/paths·claude_bin`, config 로더 5개(settings·accounts·sources·universe·secrets), SQLite 원장(스키마 버전·마이그레이션·control), 로깅(UTF-8), STATUS.md 생성기, CLAUDE.md·SessionStart 훅·settings.json, TODO.md | `python -m trader status` 가 macOS 에서 동작하고 새 claude 세션이 STATUS.md 를 자동 인식. Windows 동일 동작(사용자 확인) |
| 1 브로커 | KisAuth(env 별 캐시), KisClient 레이트리밋·재시도, tr_ids 단일 파일, accounts.py, KisMarket(실전 env 만)·KisBroker(시세·일봉·잔고·휴장일), FakeBroker, 텔레그램 발신 | `kis ping` 으로 모의·실전 잔고, 005930·AAPL 가격, 텔레그램 수신 확인 |
| 2 수집 | indicators, dart, edgar, news, macro, community(Reddit·클리앙 on, DC·에펨 off), 00_inputs 조립(섹션 격리·received_at·asof 필터), 피처 스토어 + 3년 백필, `collect` 명령 | 1종목 `00_inputs.json`, `state/features/` parquet, 수집기 단위테스트 |
| 3 에이전트 | `tool` CLI 4개(전제조건 검증), 출력 스키마, `.claude/agents/` md 8개(orchestrator·analyst×4·researcher×2·trader), 오케스트레이터 세션 실행(`session.py`: 실제 `claude --help` 플래그만, 예산·호출 집계), 수렴 감시·요약 병합, 체크포인트 재개, anonymize, FAKE_CLAUDE 스텁, `analyze --dry-run` | `FAKE_CLAUDE=1 analyze --ticker 005930 --dry-run` → `30_decision.json`; 중간 파일 삭제 후 재실행 시 그 단계만; 실제 `claude -p` 1회로 서브에이전트 호출·도구 제한 동작 확인 |
| 4 리스크·계좌별 실행 | rules·stance·costs(TDD), 원장 테이블 추가(마이그레이션), executor(auto/approve/off), `submit-decision` 이 게이트·주문 큐 연결, 모의 주문, 승인 흐름(텔레그램), 결정 분포 메트릭 | 모의 자동 체결 + 실전 `off` 기록, 한도·회전율 초과 거부 테스트, 실전 계좌 off 전용 테스트 |
| 5 웹 UI | FastAPI 라우터 + SSE, React: 대시보드·계좌 상세·비교·오늘의 실행·주문/승인·차트·데이터 탐색·설정/제어, bearer 토큰 | 브라우저에서 계좌 전환, 산출물 열람, 웹 승인 주문, 차트 마커. 모의 주문이 실전 뷰에 안 섞임, 토큰 없는 제어 401 |
| 6 scan·monitor·데몬 | 정량 스크리너 + screener 세션, monitor·트리거·`--resume` 재평가, daemon(APScheduler + web + 잠금), install(launchd/작업 스케줄러), YouTube·텔레그램 채널 수집, manual_inputs, KRX 공매도·검색트렌드, 토스·네이버 커뮤니티 API 조사, UI 실시간, Phase 2~5 이연 항목 | 평일 자동 1주 무사고, 트리거→재평가→계좌별 주문, Windows 에서 install 후 로그온 시 데몬 기동 |
| 7 ML 1단계 | features/dataset/labels(엠바고), quant + ranker, 워크포워드·라벨 누출 역검증, registry, `05_quant.json`, scan 랭킹, UI ML 페이지 | 승격 기준 통과 모델이 파이프라인 반영, UI 에서 성능·피처 중요도 |
| 8 학습·평가·보정 | memory 3층·엠바고·서킷브레이커, reflector·`reflect`, calibration(결정 60건 미만이면 비활성), 성과 페이지(수익률·MDD·Sortino·벤치마크·비용 민감도·회전율·결정 분포·수렴율), 간이 백테스트(컷오프 이후), 실전 approve 전환 절차(수동) | 모의 4주 이상 성과 리포트, 보정 모델 활성, 사용자 판단으로 실전 approve |
| 9 RL 사이징(실험) | gym env, PPO 워크포워드, 시드 3회, 모의 섀도, UI 실험 로그 | 8주 워크포워드 비교 리포트. 승격 수동 |

---

## 12. 논문 교훈 → 설계 반영표 (R8)

| 출처 | 교훈 | 반영 |
|---|---|---|
| TradingAgents (arXiv 2412.20138) | 역할 분리(분석·리서치·트레이더·리스크), 구조화 보고서 전달, 빠른/느린 모델 2단, 토론 1~2라운드 | §4 역할·스키마, §4.1 모델 배정, §4.3 라운드 1 |
| FinMem (2311.13743) | 3층 메모리·감쇠·승격, 고정 성향은 실패 → 자기적응 | §6.5, §6.1 stance |
| FinAgent (2402.18485) | 리플렉션 2종 분리, 전통 지표는 참고 신호 | §6.5, §3.1 |
| FinCon (2407.06567) | 손실 시 보수 전환, 신념 갱신은 워밍업 뒤 | §6.1, §6.5 워밍업 50건 |
| StockAgent (2407.18957) | 백본별 롱/숏 편향, 동질 군집, 테스트 누출 회피 | §4.1 bear 이종 모델, §6.3 결정 분포, §3.3 |
| Lopez-Lira & Tang (2304.07619) | 감성 신호는 부정·소형주에 집중하고 감쇠, 왕복 비용에서 소멸, 채택 확산 시 약화 | §6.1 회전율·비용, §4.2 UNKNOWN·부정 뉴스 가중 |
| 토론 수렴 (2609.29701), 익명화 (2510.07517) | 라운드 증가 시 아첨적 수렴으로 성과 하락, 정체성 편향은 익명화로 완화, 판정자는 자신감에 속음 | §4.3 라운드 0 보관·수렴 감시·악마의 변호인, §4.4 "자신감 무시", §3.3 익명화 옵션 |
| 평가 비판 (2603.27539) | 룩어헤드·생존·과적합·비용 무시·국면 둔감이 수익 부호를 뒤집음, 조정 손익분기 스프레드 | §3.3, §9 워크포워드·엠바고, §13 비용 민감도·벤치마크 |
| Agentic Trading 서베이 (2605.19337), 서베이 (2408.06361) | 뉴스는 발행 시각이 아니라 도달 시각, Outcome Embargo, 생존 편향 | §3.3 `received_at`, §6.5 엠바고 |
| StockBench (2510.02209), Agent Market Arena (2510.11695) | 프레임워크가 백본보다 성과 차이를 만들고 멀티 에이전트는 공격적 포지션 경향, 종목 수 증가 시 하락 | §6.1 포지션 캡, §5.1 후보 5, §4.1 모델 설정화 |
| FinRL·Qlib 계열 | ML 기준선이 다수 LLM 에이전트를 앞섬, RL 은 비정상성·과최적화 | §9.1 퀀트 신호 포함, §13 ML 기준선 비교, §9.4 모의 한정·수동 승격 |
| 피해야 할 함정 | 비현실적 Sharpe 목표, 설명의 그럴듯함 ≠ 성과, 한국 시장 검증 논문 부재 | §13 B&H·1/N·ML 기준선 대비로만 판단, 모의 4주 이상 |

---

## 13. 검증 방법

- **단위(pytest)**: 리스크 규칙·성향·비용, 지표, 출력 스키마, 수렴 감시, 도구 전제조건(`submit-decision` 거부), executor 모드 분기, tr_id 매핑, 토큰 캐시, 메모리 엠바고, 라벨 엠바고·워크포워드 분할. 테스트는 `tmp_path` 만 사용.
- **통합(녹화)**: KIS·DART·뉴스는 `respx`. 오케스트레이터는 `FAKE_CLAUDE=1`. 실제 네트워크 테스트 없음.
- **E2E(모의)**: `analyze --ticker 005930` 을 실제 `claude -p` 로 1회 → 서브에이전트 호출·도구 제한(`Bash(python -m trader tool *)` 외 거부)·체크포인트·비용 기록 확인 → 모의 자동 체결 + 실전 `off` 기록 → 텔레그램·UI.
- **복구**: analyze 도중 kill → 재실행 → 완료 단계 스킵. 새 `claude` 세션에서 STATUS.md 인식.
- **크로스플랫폼**: Windows PC 에서 Phase 0·3·6 완료 시 `uv sync` → `python -m trader status` → `FAKE_CLAUDE=1 analyze --dry-run` → 실제 `claude -p` 1회 → `install`. 한글 경로·cp949 콘솔 인코딩 테스트 포함. GitHub Actions 매트릭스(macos, windows)로 pytest.
- **웹 UI**: TestClient(승인·모드 변경이 DB 반영, 계좌 필터, 401), Vitest, 브라우저 수동.
- **ML**: 워크포워드 성능, 라벨 누출 역검증, 승격 자동 판정, 퀀트 on/off 결정 변화 로그.
- **운영 평가(Phase 8)**: 모의 4주 이상. 계좌별 수익률·MDD·Sortino, B&H·1/N·퀀트 단독 대비, 비용 민감도(0/10/20/30bp), 회전율, 결정 분포, 수렴율, 일일 호출·비용. 실전 전환 기준은 §0.2 #18.
