# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# 프로젝트: LLM 멀티 에이전트 + ML 주식 자동매매 (KIS)

## 목적
한국투자증권(KIS) Open API 로 모의·실전 계좌를 운용하는 주식 자동매매 시스템. Claude Code 오케스트레이터 세션(`claude -p`)이 서브에이전트(screener, 애널리스트 4, 리서처 2, 트레이더, reflector)를 소집해 분석하고, **파이썬 CLI 도구만 통해** 수집·체크포인트·결정 제출을 한다. 리스크 한도와 주문 실행은 LLM 이 아니라 파이썬이 강제한다. macOS 개발·운영, Windows 동일 실행 보장. 목표는 모의 4주 이상 무사고 + 벤치마크 우위 확인 후 사용자가 실전 전환. 현재 Phase 0~4(골격·브로커·수집·에이전트·리스크/계좌별 실행)까지 구현됐고 Phase 5(웹 UI)부터 미구현이다.

## 세션 시작 시 상태 확인
- `state/STATUS.md` 는 SessionStart 훅(`.claude/hooks/session_start.py`)이 세션 시작·`/clear`·compact 때 자동 주입한다 (STATUS.md + `docs/TODO.md` 미완료 상위 항목).
- 최신화는 `uv run trader status` (`python` 이 PATH 에 없는 환경이 있어 항상 `uv run` 을 앞에 붙인다).
- 다음 할 일은 `docs/TODO.md` 의 첫 미완료(`- [ ]`) 항목. 작업 완료 시 체크 표시를 갱신한다. 각 Phase 마지막은 `완료 기준:` 항목이며 확인 시 `(YYYY-MM-DD 확인)` 을 덧붙인다. 이전 Phase 에서 미룬 항목은 재개 Phase 절에 `(Phase N 이연)` 접미사로 둔다.

## 문서
- 설계 스펙: `docs/superpowers/specs/2026-10-09-trading-agent-design.md`. 아키텍처·도구 계약·체크포인트 파일명·리스크 규칙·Phase 완료 기준의 원천이다. 새 모듈을 만들 때 해당 절을 먼저 읽는다. 절 색인: §0.1 요구사항 R1~R18, §0.2 확정 사항, §0.3 외부 제약(Claude CLI 플래그·KIS 한도·커뮤니티 소스 정책), §1 아키텍처(두 층·도구 4개·명령·디렉터리), §2 브로커, §3 수집(3.3 룩어헤드), §4 에이전트(4.1 오케스트레이터 세션, 4.5 체크포인트), §5 scan/monitor, §6 리스크·executor·원장·알림·메모리, §7 세션 복구, §8 웹 UI, §9 ML, §10 스케줄러·크로스플랫폼, §11 Phase 계획, §12 논문 반영표, §13 검증.
- 구현 플랜: `docs/superpowers/plans/<날짜>-phase<N>-<이름>.md`. 새 Phase 는 `superpowers:writing-plans` 로 플랜을 먼저 쓴다. 플랜의 `범위 조정` 표(제외 항목·이유·재개 Phase)는 TODO.md 이연 항목의 출처다.
- 이 파일은 Phase 완료 시점에 코드와 대조해 어긋난 곳만 고친다. 함수 시그니처·상수·기본값은 적지 않는다(코드가 출처). "왜 그렇게 정했는지" 와 "여러 파일에 걸친 규칙" 만 적는다.

## 실행 명령 (Phase 가 진행되며 늘어난다)
- `uv sync` : 의존성 설치 (Python 3.12)
- `uv run pytest` : 전체 테스트 (네트워크 없이 전부 돈다)
- `uv run ruff check src tests` : 린트, 경고 0건 유지
- `uv run trader status` : `state/STATUS.md` 재생성 (`uv run python -m trader status` 와 동일)
- `uv run trader kis ping [--account ID]` : KIS 시세·휴장일·계좌 현금 점검 (실패 > 0 이면 exit 1, 구독·앱키 필요)
- `uv run trader notify-test` : 텔레그램 알림 테스트 (미설정이면 NullNotifier 로 성공)
- `uv run trader collect --ticker 005930 [--market US --exchange NAS] [--asof ISO] [--force]` : `00_inputs.json` 생성. 절 일부 실패는 정상(exit 0, "오류 절 N건"), 설정·인자 오류만 exit 1
- `uv run trader features backfill [--years 3] [--market KR|US]` / `features snapshot [--market] [--asof ISO]` : 피처 스토어 적재 (실전 20/s 로 20종목 수 분)
- `uv run trader analyze --ticker 005930 [--market US --exchange NAS] [--asof ISO] [--dry-run] [--skip-collect]` : 오케스트레이터 세션 1회 → `30_decision.json` + 계좌별 `40_risk_check_<id>`·`50_order_<id>`. `--dry-run` 은 판정·기록까지만(주문·승인 요청 없음). `FAKE_CLAUDE=1` 이면 claude 없이 고정 응답(HOLD) + 가짜 브로커. 실제 실행은 구독 호출을 쓰므로 `.claude/settings.json` 허용 목록에 넣지 않는다
- `uv run trader approvals list|approve <id>|reject <id>` : approve 모드 계좌의 승인 요청 (30분 만료, 1회만 실행)
- `uv run trader telegram poll` : getUpdates 1회로 `/approve <id>`·`/reject <id>`·`/status` 처리, 오프셋은 control `telegram_offset`
- `uv run trader control show|mode <계좌> <auto|approve|off> [--confirm LIVE]|pause|resume` : 런타임 제어. 실전 `auto` 는 `--confirm LIVE` 가 있어야 하며 `live_confirmed:<id>` 기록을 남긴다
- `uv run trader kis order-test --account paper-main` : Phase 4 실기 전용. 모의 계좌에 현재가 −10% 1주 지정가 매수 → 즉시 취소 (실전 거부, 원장 미기록, 휴장일엔 KIS 가 40100000 으로 거부)
- `uv run trader tool …` : 오케스트레이터 세션 전용(사람이 직접 쓰지 않음)
- `uv run trader --help` : 등록된 명령 확인
- 브랜치는 Phase 단위 `feat/phase<N>-<이름>` 을 직전 Phase 브랜치 위에 쌓고, PR 도 직전 Phase 브랜치를 대상으로 연다 (Phase 0 은 `main`). `.gitattributes` 가 LF 를 강제한다.

## 아키텍처 핵심 규칙 (여러 파일에 걸친 계약)
- **LLM 은 도구만 쓴다.** 오케스트레이터 세션(`orchestrator/session.py`)은 `claude -p --output-format json --tools "Bash,Agent" --allowedTools "Bash(python -m trader tool *)" --permission-mode dontAsk --setting-sources "" --disable-slash-commands --agents <json> --append-system-prompt <orchestrator.md> --session-id <uuid> --max-budget-usd` 로 띄운다. 외부 세계와는 `python -m trader tool <status|collect|checkpoint list/read/write/summarize|submit-decision>` 로만 접촉하고, `tool` 명령은 stdout 에 JSON 한 줄만 쓴다(오류도 `{"ok": false}` + exit 1). 주문은 `submit-decision` 안의 리스크 게이트를 통과한 수량만 나간다.
- **서브에이전트 정의는 `.claude/agents/*.md`** (프런트매터 `name, description, model, tools`) 이고 `agents/registry.py` 가 읽어 `--agents` JSON 으로 넘긴다(`--setting-sources ""` 가 프로젝트 설정을 끊기 때문). 모델은 `settings.models` 가 우선(`analyst-*` → `analyst`). 공통 규칙 6문장은 md 8개가 같은 문장으로 담고 테스트가 확인한다. md 안의 스키마는 설명용 사본이고 정본은 `agents/schemas.py` 에서 런타임에 생성해 프롬프트에 덧붙인다.
- **순서는 프롬프트가 아니라 도구 전제조건이 강제한다.** `submit-decision` 은 해당 종목의 `10_analyst_*` 체크포인트 2개 이상과 `25_research_summary` 가 파일로 있을 때만 받고, `checkpoint write` 는 LLM 쓰기 가능 단계(`10_analyst_<role>`, `20_debate_r<n>_<side>`)만 받으며 pydantic 스키마 + 파일명↔페이로드 일치(role·ticker·round·side, `n ≤ debate_rounds`)를 검증한다. `30_decision` 은 `submit-decision` 전용. `ticker`·`market`·`stage` 는 형식 검증(경로 탈출 방지)을 거치고 `run_store` 도 `state/runs` 밖 경로를 거부한다. 도구는 저장 전에 문자열의 HTML 엔티티(`&lt;` 등, Claude Code 가 서브에이전트 출력을 이스케이프)를 되돌린다. 토론 턴을 새로 쓰면 `25_research_summary` 를 다시 만든다.
- **체크포인트가 상태의 원본이다.** `state/runs/<날짜>/<시장>/<종목>/<단계>.json` 에 `{schema_version, data, call}` 로 저장하고, 존재하는 단계는 건너뛴다. 단계 이름: `00_inputs, 10_analyst_<role>, 20_debate_r<n>_<side>, 25_research_summary, 30_decision, 40_risk_check_<account>`, `50_order_<account>`. 읽기·쓰기는 `orchestrator/run_store` 의 함수로만 한다.
- **수렴 감시**(`orchestrator/summary.py`): 부호 있는 확신 `conviction × {BULLISH:+1, NEUTRAL:0, BEARISH:-1}` 의 양측 차이가 라운드 0 에서 0.2 초과였다가 최종에서 0.2 이하로 붕괴하면 `sycophantic: true, preferred: round0`. 트레이더 md 가 이를 읽어 라운드 0 을 우선한다.
- **세션·비용**: 세션 1회 = `agent_calls` 1행(`agent='orchestrator'`, 시작 시 `running` 으로 먼저 쓰고 종료 시 갱신, `duration_ms` 는 파이썬 측 측정) + `runs` 1행. FAKE 세션(`model='fake'`)은 상한·비용 집계에서 제외. `settings.llm.daily_session_cap` 초과 시 `PipelineError`. `settings.anonymize=true` 는 Phase 6 전까지 `ConfigError`. stdin 에 `asof` 를 넘기고 오케스트레이터는 `tool collect --asof` 로 전달한다. 세션 cwd 는 빈 `state/claude_cwd/`, env 에 `CLAUDE_CODE_RETRY_WATCHDOG=1`·`TRADER_HOME`·venv bin 을 앞세운 `PATH`. `FAKE_CLAUDE=1` 이면 `agents/fake.py` 가 같은 도구 함수를 파이썬으로 호출해 고정 JSON 체크포인트를 쓴다(claude 미설치 환경·테스트).
- **계좌 모드.** yaml 값이 기본, SQLite `control` 테이블 값이 우선. 모의는 `auto` 고정, 실전은 `off` 로 시작하며 `auto` 전환은 확인 문자열 없이는 거부된다. 결정은 종목당 1회, 리스크 판정·주문은 계좌마다.
- **주문은 `orchestrator/executor.py` 안에서만 나간다.** `submit-decision` 이 `30_decision` 저장 → 리포트·토론·결정을 원장에 멱등 적재 → `executor.execute` 로 계좌마다 `rules.evaluate` → `40_risk_check_<id>`(RiskVerdict + mode·stance·dry_run) → 모드 분기: `off` 기록만, `approve` 는 `approvals` 행 + 텔레그램 요청, `auto` 는 `broker.place_order` → `50_order_<id>`({request, result, mode, status}) + `orders` 행 + 알림. 브로커 예외(`KisApiError`·`ValueError`)는 `50_order` 에 실패로 남기고 다음 계좌로 간다. 브로커가 없는 계좌는 잔고를 모르므로 판정 거부로 기록한다. 현재가는 `00_inputs.technical.quote` 에서 읽는다. **멱등**: 같은 날 `30_decision` 이 이미 있으면 `submit-decision` 은 `already: true` 로 기존 결정을 돌려주고 실행하지 않으며, 같은 결정·계좌에 살아 있는 주문(실패·거부·취소 제외)이나 pending 승인이 있으면 `executor` 가 다시 내지 않는다(`repo.has_open_order_or_approval`). 원장 집계(직전 NAV·당일 주문금액·실현손익)는 통화가 달라 **시장별**로 거른다.
- **dry_run 은 LLM 인자가 아니라 env 로 전달한다.** 세션이 `TRADER_DRY_RUN`("1"|"0") 을 넣고 `tool` 서브프로세스가 상속한다. 값이 없으면 dry_run(주문 없음)이 기본이라 LLM 이 플래그를 빠뜨려도 주문이 나가지 않는다. dry_run 에도 브로커로 잔고를 읽어 판정은 하고 주문·승인 요청만 생략한다.
- **실전 `auto` 는 확인 기록이 있어야 실행한다.** `control.set_account_mode(confirm="LIVE")` 가 `live_confirmed:<id>` 를 쓰고(auto 해제 시 지움), `executor.effective_mode` 는 이 기록이 없는 실전 auto 를 `off` 로 강등한다. control 테이블에 직접 `mode:<id>=auto` 를 써도 주문은 나가지 않는다.
- **리스크 규칙(`risk/`)은 순수 함수.** 입력은 `Decision + Portfolio(브로커 잔고 + 원장 집계) + RiskLimits + stance + 현재가`, 출력 `RiskVerdict{approved, adjusted_qty, reasons[], side, order_type, price}`. 거부 사유는 전부 모은다(첫 사유에서 멈추지 않음). 순서: HOLD → 비중 상한(종목당 25%, conservative 15%) → 종목 수(5, conservative 2) → 가능금액(당일 누적 주문 차감, 환전 API 없음) → confidence<0.5 반감 → 거부 규칙(일일 손실 −5% 는 BUY 만, 당일 중복 매수, 기대수익<왕복비용×2(KR 30bp·US 50bp, take_profit 없으면 통과), 최소 보유 3거래일(SELL·TRIM, 손절 제외), 주 2회). US 시장가는 현재가 지정가로 바꾼다(모의 서버 제약). stance 는 최근 5거래일 실현손익이 음수면 conservative. 보유 거래일은 평일 수(휴장일은 Phase 6).
- **승인(`approvals`)은 30분 만료, 1회만 실행.** `executor.approve` 는 pending·미만료만 받아 조건부 UPDATE(`decide_approval` 가 `WHERE status='pending'`, rowcount 0 이면 다른 프로세스가 처리한 것)로 잠근 뒤 결정일 run 디렉터리에 `50_order` 를 쓴다. 주문이 실패하면 승인 상태는 `failed`. 만료분은 `expire_approvals` 가 expired 로 바꾼다. 웹 승인(Phase 5)도 같은 행과 함수를 쓴다. 승인 시점에 게이트를 다시 돌리지는 않는다(30분 안의 변화는 감수).
- **원장 v2·v3.** `MIGRATIONS` 에 버전을 추가만 한다(기존 버전 수정 금지, 컬럼 추가는 새 버전의 `ALTER TABLE`). 주문 행은 전부 `decision_id + account_id` 이고 `amount`(가능금액 차감용, 시장가는 판정 때 현재가 × 수량)·`org_no`(KR 취소용)를 가진다. 금액은 TEXT(Decimal 문자열). `positions_snapshot` 은 executor 가 판정 때마다 계좌·날짜·시장·종목 단위로 upsert 하며(포지션이 없으면 `_CASH_<market>` 자리) 일일 손익은 같은 시장의 직전 스냅샷 NAV 대비다. `fills` 는 체결 동기화(Phase 6) 전까지 비어 있어 `realized_pnl_5d` 는 0(성향은 항상 normal)이고 `held_since` 는 접수된 매수 주문일로 대신한다.
- **브로커 주문.** KR 은 `order-cash`(지정가 00·시장가 01), US 는 `order`(지정가만, 거래소 코드는 시세 EXCD 와 다른 `NASD|NYSE|AMEX`), 취소는 `order-rvsecncl`(KR 은 접수 응답의 `KRX_FWDG_ORD_ORGNO` 가 `OrderResult.org_no` 로 필요). `rt_cd≠0` 은 `KisApiError`. `FakeBroker` 는 `orders`·`cancelled` 에 기록한다.
- **시세·수급은 실전 env 클라이언트만, 주문·잔고는 계좌의 env.** 모의 서버는 시세류를 주지 않는다. `broker/tr_ids.py` 가 tr_id·경로의 유일한 출처이고(공식 `open-trading-api` 예제에서 확인한 값), 모의 미지원 API 는 paper 값이 None 이다. 브로커 계층은 세 겹: `KisAuth(env)` → `KisClient(env, auth)` → `KisMarket(client)`(실전만) / `KisBroker(account, client)`(env 일치 요구). CLI 는 `_build_market/_build_brokers` 로 조립하고 테스트는 이를 monkeypatch 한다.
- **KIS 실패는 전부 `KisApiError(msg_cd, msg1, tr_id)`.** KIS 응답이 아닌 실패는 합성 코드 `AUTH/NETWORK/HTTP<status>/INVALID_JSON/UNSUPPORTED`. 토큰은 `state/kis_token_<env>.json` 캐시(만료 1시간 전 갱신, 실패 후 60초 재발급 금지). `EGW00201`(초당 한도)은 HTTP 200 뿐 아니라 **HTTP 500 본문**으로도 오므로 상태코드와 무관하게 0.5초 뒤 1회 재시도. 모의 서버는 2/s 라 한 점검에서 같은 API 를 두 번 부르지 않는다. US 예수금은 `inquire-present-balance`(통화별 행) 로 읽는다. `inquire-psamount` 는 종목코드 필수라 현금 조회에 못 쓴다(실기 확인). 일봉·수급 목록은 **항상 날짜 오름차순**(마지막이 최신). 금액은 `Decimal`. `OrderRequest.exchange` 는 기본값이 없어 KR 도 `None` 을 명시한다.
- 미확인: `KisBroker` 의 US 잔고·예수금은 `OVRS_EXCG_CD=NASD` 고정이라 NYS/AMS 보유분이 같이 조회되는지 실기에서 확인한다.
- **룩어헤드 방지.** 모든 수집 항목에 `received_at`, `asof` 이후 발행분 제외(뉴스·커뮤니티·공시, EDGAR 는 `filed <= asof`), 재무비율은 사업연도 Y 값을 Y+1 년 4월 1일부터 적용. 과거 `asof` 재수집·스냅샷은 24시간 넘으면 `ConfigError`(백테스트는 Phase 7 에서 피처 스토어로만).
- **수집 계층(`sources/`).** 모든 수집기는 `base.run_section` 을 거쳐 `Section{data, received_at, error}` 를 돌려주고 예외를 던지지 않는다. 호출 없이 건너뛸 때는 `skipped_section(reason)`. `SKIP_REASONS = {disabled, phase6, not available for US}` 는 `00_inputs.errors` 에 집계하지 않고, 그 밖의 error(`no api key`, `no market client`, 예외 문자열)는 `<역할>.<키>: <오류>` 로 모인다. 뉴스·커뮤니티처럼 절 안에 소스별 결과(`data.sources`)가 있으면 그 하위 실패도 `<역할>.<키>.<소스>: <오류>` 로 덧붙인다. 종목명은 KR 이 DART → KIS(`get_name_kr`) 순, US 는 EDGAR 티커 맵 순으로 폴백한다. `00_inputs.json` 키 배치는 `technical{quote, daily, minute, orderbook, indicators}`, `fundamental{ratios, history(3개년+최근 분기, v2), disclosures}`, `news_macro{news, macro(항목별 Section dict)}`, `sentiment{investor_flow, short_interest, community, trends, manual_inputs}` + `meta` + `errors`. 저장 전 `Decimal` → 문자열, 날짜 → ISO 로 바꾸므로 읽는 쪽은 `Decimal(str)` 로 되돌린다. 구조를 바꾸면 `inputs.SCHEMA_VERSION` 을 올린다. DART·EDGAR 비율은 같은 한글 키(`매출성장률·영업이익률·ROE·부채비율·PER·PBR·PSR`)로 `dart.basic_ratios()` 를 공유한다. DART 는 오류를 zip 이 아닌 XML(`status` 800 점검 등)로도 주므로 `DartError` 로 통일한다.
- **커뮤니티·뉴스 소스 정책은 `config/sources.yaml` 기본값이 출처다**(스펙 §0.3): Reddit·클리앙 on, DC·에펨코리아 구현하되 off, 토스·네이버 토론실은 Phase 6 조사, 세이브티커는 Cloudflare 차단으로 `disabled_sites` 기록만. `sources.kis.daily_days` 는 `high_52w_pos`(252봉) 때문에 260 이상.
- **체크포인트는 `orchestrator/run_store` 함수로만**: `run_dir(date, market, ticker)` → `state/runs/<date>/<market>/<ticker>/`, `write_checkpoint`(원자적), `read_checkpoint`(없으면 None), `list_checkpoints`. `collect_and_save` 는 `00_inputs` 가 있으면 건너뛴다(`--force` 로 덮어씀). 디렉터리 날짜는 `asof` 의 KST 날짜.
- **피처 스토어(`ml/features.py`).** `state/features/<date>.parquet`, 키 `(date, ticker)`, 쓰기는 멱등. 비율·수급은 KR 만 채우고 US 행은 NaN(US 비율은 Phase 7). `snapshot` 행의 date 는 asof 가 아니라 마지막 일봉 날짜. 지표 워밍업 구간도 NaN 인 채로 저장한다. 백필은 KIS 일봉 상한 1,000봉에서 자르고 DART 실패 시 가격 지표만 저장한다. `state/cache/` 7일 캐시는 DART 기업코드 맵·EDGAR 티커 맵·KIS 휴장일만 대상이다.
- **`claude -p` 플래그는 설치된 `claude --help` 에 있는 것만 쓴다.** `--bare` 는 구독 인증을 끄므로 금지. cwd 는 `state/claude_cwd/`(빈 디렉터리), `--setting-sources ""` 로 훅·플러그인 유입을 막는다. `FAKE_CLAUDE=1` 이면 subprocess 없이 고정 응답.
- **오류 타입.** 설정 오류는 `ConfigError`, KIS 오류는 `KisApiError`. 키는 URL·로그·예외 메시지에 남기지 않는다(`httpx` 로거는 WARNING).

## 금지사항
- 실전 주문을 수동으로 실행하지 않는다. `.claude/settings.json` 이 `kis order *`·`kis order-test`·`approvals approve`·`telegram poll` 을 거부한다(승인·폴링은 실전 approve 계좌에 주문을 낼 수 있는 경로라 사용자만 실행한다).
- `secrets/` 를 커밋하지 않는다. 읽지도 않는다 (settings.json 의 deny 규칙. Bash 로 읽는 명령도 거부되므로 다른 명령과 묶지 않는다). `config/accounts.yaml` 에는 실제 계좌번호가 있으므로 값을 출력·인용하지 않는다.
- 계좌 모드(control 테이블)를 임의로 변경하지 않는다 (`trader control mode` 는 사용자가 쓴다).
- `uv run trader analyze` 를 `FAKE_CLAUDE=1` 없이 돌리면 구독 호출이 발생한다. 사용자 요청 없이 실제 세션을 띄우지 않는다.
- 셸 스크립트(.sh/.bat/.ps1)를 추가하지 않는다. 훅·설치·유틸은 전부 파이썬.
- `state/` 파일을 직접 편집하지 않는다 (STATUS.md 는 `uv run trader status` 로만 갱신).
- 커뮤니티·뉴스 수집기의 기본 on/off 는 스펙 §0.3 정책을 따른다. 사용자 결정 없이 off 소스를 켜지 않는다.

## 코딩 규칙
- 경로는 전부 `pathlib`, 하드코딩 절대경로 금지, 루트는 `platform/paths` 로만 얻는다(`TRADER_HOME` → 없으면 cwd 에서 위로 `pyproject.toml` 탐색). OS 분기는 `platform/` 만.
- 파일명에 `:` 금지 (타임스탬프는 `YYYYMMDD_HHMMSS`). 저장 시각은 UTC ISO8601, 표시만 KST/ET. 타임존은 `zoneinfo` + `tzdata`. 콘솔 출력 UTF-8 강제.
- subprocess 는 `shell=False, encoding="utf-8", errors="replace"` 고정 (cp949 의존 금지). SessionStart 훅은 표준 라이브러리만.
- SQLite 는 WAL 모드, 쓰기 타임아웃 30초, 스키마 버전 + 순서 마이그레이션. 패키지 관리는 `uv`. 의존성은 Windows 휠이 있는 라이브러리만.
- 금액은 `Decimal`. pydantic 모델은 `extra="forbid"` (yaml 키를 추가하면 모델도 같이 바꾼다).
- 사용자 대면 메시지·로그·docstring 은 한국어. 로거 이름은 `trader.<계층>`.
- TDD: 실패 테스트 먼저. 테스트는 `tmp_path` 만 쓰고 네트워크는 `respx` 로 막는다. Phase 마다 커밋하고 `docs/TODO.md` 를 갱신한다.
- 커밋 트레일러: `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`
