# Phase 6 scan·monitor·데몬 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 사람이 명령을 치지 않아도 평일에 scan → analyze → monitor(손절·트리거 재평가) → 계좌별 주문이 스케줄대로 돌고, 로그온 시 자동 기동되는 데몬을 만든다. 이전 Phase 이연 항목(체결 동기화·텔레그램 상시 수신·커뮤니티 재구축·수집기 보강)을 함께 거둔다.

**Architecture:** `scan` 은 피처 스토어 기반 정량 필터 → (ML 랭킹 자리) → screener 세션(haiku)으로 5개를 고르고, `analyze` 는 picks + 보유종목을 분석한다. `monitor` 는 60초 루프에서 보유종목 시세를 읽어 손절은 파이썬이 즉시 매도하고 트리거는 `triggers` 에 기록한 뒤 trader 재평가 세션(`--resume`)을 띄워 같은 게이트·executor 로 보낸다. `daemon` 은 APScheduler 로 `schedule.yaml` 을 실행하며 web·텔레그램 수신 루프를 같은 프로세스에 두고 `state/daemon.lock` 으로 단일 인스턴스를 보장한다. `install` 은 OS 별 로그온 시작 등록을 파이썬 템플릿으로 만든다.

**Tech Stack:** APScheduler 3.x(Windows 휠), uvicorn(서버 스레드), KIS 체결 조회 tr_id, youtube-transcript-api(순수 파이썬), 텔레그램 공개채널 웹 미리보기(`t.me/s/<채널>`), Reddit OAuth(`httpx`), 기존 FAKE_CLAUDE 스텁 확장.

**Spec:** `docs/superpowers/specs/2026-10-09-trading-agent-design.md` §5(scan·monitor), §10(데몬·설치·크로스플랫폼), §1.2(명령 표), §3(수집 이연), §6.2(손절은 §5.2), §11 Phase 6 행, §0.3(커뮤니티 정책).

## Global Constraints

- Phase 0~5 규칙 전부(CLAUDE.md). 주문은 `orchestrator/executor.py` 안에서만. `paused` 면 데몬은 세션·monitor 주문을 내지 않는다.
- **손절 매도는 LLM 없이 파이썬이 즉시** 낸다(`auto`·`approve` 모두 즉시, 사후 통보; `off` 는 기록만). 손절선은 결정의 `stop_loss`, 없으면 `avg_price × (1 + settings.risk.stop_loss_pct)`(-15%).
- 트리거: ±7% 급등락(결정 시점 가격 대비), `take_profit` 도달, 신규 공시(DART/EDGAR, 마지막 확인 이후), `invalidation_conditions` 의 가격 조건(`"<"`·`">"` 숫자가 있는 문장만 파싱, 나머지는 무시). **같은 종목 재평가는 30분에 1회**, 하루 세션 상한(`llm.daily_session_cap`)에 걸리면 트리거만 기록.
- 구독 한도 도달(세션 상한) 시 screener picks 를 `5 → 3 → 1` 로 축소한다.
- 데몬은 `state/daemon.lock`(pid + 시작 시각 JSON) 단일 인스턴스, 죽은 pid 는 stale 로 덮어쓴다. 휴장일(`KisMarket.is_holiday`, 캐시)·주말은 scan/analyze/monitor 를 건너뛰고 로그에 남긴다.
- 모든 스케줄 시각은 `schedule.yaml`(KST) 에서 읽고 `config.load_schedule()`(pydantic, `extra="forbid"`)로 검증한다. US monitor 창은 자정을 넘는다(23:30~06:00).
- 셸 스크립트 금지: launchd plist·Windows 작업 스케줄러 XML 은 파이썬 템플릿 문자열로 만들고 `subprocess(shell=False)` 로 `launchctl`/`schtasks` 를 부른다. OS 분기는 `platform/installer.py` 만.
- 수집기는 Phase 2 규칙(`run_section`, `asof` 필터, 예외 없음). 커뮤니티 on/off 기본값은 스펙 §0.3(사용자 결정 없이 off 소스를 켜지 않는다 — 조사 결과만 기록).
- 원장 마이그레이션은 v4 로 **추가만**(`triggers`, `fills` 적재용 컬럼은 기존 사용).
- 테스트: 시각은 `clock=`/`now=` 주입, 스케줄러는 실제로 띄우지 않고 잡 등록 결과를 검사, 네트워크는 respx, LLM 은 FAKE_CLAUDE.

## Review Focus

1. 손절선 도달 종목이 `approve` 계좌에 있어도 승인 없이 즉시 매도돼야 하고 `off` 계좌는 기록만 → Task 4 `test_stop_loss_sells_immediately_even_in_approve_mode`, `test_stop_loss_off_account_records_only`.
2. 같은 종목에 30분 안에 트리거가 두 번 와도 재평가 세션은 한 번 → Task 4 `test_trigger_reevaluation_rate_limited`.
3. 데몬 두 번째 인스턴스는 lock 때문에 즉시 종료하고, 죽은 pid 의 lock 은 넘겨받아야 한다 → Task 6 `test_daemon_lock_rejects_second_instance`, `test_stale_lock_is_taken_over`.
4. 휴장일·주말에는 scan/analyze/monitor 잡이 실행되지 않고 로그만 남아야 한다 → Task 6 `test_jobs_skip_holiday`.
5. 체결 동기화가 같은 체결을 두 번 적재하면 안 된다(재실행 멱등) → Task 2 `test_sync_fills_idempotent`.

## 범위 조정

| 항목 | 이유 | 재개 Phase |
|---|---|---|
| ML 랭킹(정량 점수 대신 모델 점수) | registry 는 Phase 7. `scan` 은 `rank_fn` 훅만 둔다 | 7 |
| reflect 잡·ml train 잡의 실제 동작 | Phase 8·7. 데몬에는 자리(로그만) | 7·8 |
| 토스증권·네이버 종목토론실 수집기 구현 | 이 Phase 는 조사·결정(문서화)까지. 공개 API 가 없고 약관·robots 차단이면 영구 off | — |
| 검색트렌드(네이버 데이터랩) | 키 없음·우선순위 낮음 | 8 |
| 웹 UI 실시간(SSE 를 트리거 push 로) | `triggers`·`runs` 행이 SSE 카운터에 잡히므로 폴링으로 충분. push 는 보류 | 8 |
| 텔레그램 채널 수집에 Telethon(API id 필요) | 사용자 키 없음 → 공개 미리보기(`t.me/s/`) HTML 만 | — |
| 차트 지표 오버레이·분봉 | UI 범위. 데이터는 있음 | 8 |

---

## 파일 구조

```
src/trader/config.py                 ScheduleConfig + load_schedule(), SourcesConfig.channels 모델 확정
src/trader/ledger/db.py              MIGRATIONS v4: triggers
src/trader/ledger/repo.py            insert_trigger / last_trigger_at / sync 용 fills upsert(uniq key)
src/trader/broker/tr_ids.py          DOM_CCLD(일별 체결), OVS_CCLD_US
src/trader/broker/accounts.py        KisBroker.get_fills(market, date) -> list[Fill]
src/trader/broker/models.py          Fill
src/trader/broker/fake.py            FakeBroker.fills
src/trader/orchestrator/fills.py     sync_fills(conn, broker, account, date) (멱등)
src/trader/scan/quant.py             정량 필터·점수 (피처 스토어)
src/trader/scan/screener.py          screener 세션 입력 조립·결과 파싱, FAKE
src/trader/orchestrator/session.py   SessionSpec.kind ("analyze"|"screen"|"resume"), stdin payload 확장
src/trader/agents/schemas.py         Screened, TriggerContext
.claude/agents/screener.md           (Phase 3 보류분)
.claude/agents/orchestrator.md       screen/resume 모드 절 추가
src/trader/orchestrator/monitor.py   손절·트리거 판정(순수 함수) + monitor_once(I/O)
src/trader/orchestrator/resume.py    트리거 → 재평가 세션 → submit (executor 재사용)
src/trader/daemon.py                 APScheduler + web 스레드 + telegram 루프 + lock + 휴장일
src/trader/platform/installer.py     launchd plist / schtasks XML 템플릿 + 등록·해제
src/trader/sources/youtube.py        자막 수집 (채널 최신 영상 N개)
src/trader/sources/telegram_channel.py  공개채널 미리보기 수집
src/trader/sources/manual_inputs.py  state/manual_inputs/<ticker>.md|json
src/trader/sources/community.py      Reddit OAuth, 키워드 단어 경계
src/trader/sources/inputs.py         channels·manual_inputs·events 절 연결, 공시 종료일 KST, 미래 asof 거부, 건너뜀 표시
src/trader/cli.py                    scan / monitor [--once] / daemon / install / fills sync / telegram loop
tests/scan/, tests/orchestrator/test_{fills,monitor,resume}.py, tests/test_daemon.py, tests/platform/test_installer.py, tests/sources/test_{youtube,telegram_channel,manual_inputs}.py, tests/test_config_schedule.py
```

---

### Task 1: `schedule.yaml` 로더·채널 설정·원장 v4(`triggers`)

**Files:** Modify `src/trader/config.py`, `config/sources.yaml`(키 없음, 모델만), `src/trader/ledger/db.py`, `src/trader/ledger/repo.py`. Test `tests/test_config_schedule.py`, `tests/ledger/test_repo.py`(추가).

**Interfaces:**
- `config.MonitorWindow{start: str("HH:MM"), end: str, interval_s: int=60}`, `config.MarketSchedule{scan: str, analyze: str, monitor: MonitorWindow}`, `config.ScheduleConfig{kr: MarketSchedule, us: MarketSchedule, reflect: str, ml_train: {weekday: str, time: str}}`(전부 `extra="forbid"`), `config.load_schedule(path=None) -> ScheduleConfig`(없으면 `ConfigError`, 시각 형식 검증).
- `SourcesConfig.channels: ChannelsConfig{youtube: list[str], telegram: list[str], max_videos: int = 3, max_posts: int = 30}`(기존 yaml 키와 일치).
- v4: `triggers(id, account_id NULL, market, ticker, kind TEXT, detail TEXT(json), price TEXT, decision_id INTEGER NULL, created_at, handled_at NULL, session_id NULL)`; `fills` 에 `UNIQUE(account_id, order_no, filled_at, qty, price)` 는 v2 테이블 변경이 필요하므로 **새 유니크 인덱스** `CREATE UNIQUE INDEX IF NOT EXISTS ux_fills_dedupe ON fills(account_id, order_id, filled_at, qty, price)`.
- `repo.insert_trigger(conn, *, market, ticker, kind, detail: dict, price: Decimal, decision_id: int|None, account_id: str|None=None) -> int`, `repo.last_trigger_handled_at(conn, market, ticker) -> datetime|None`, `repo.mark_trigger_handled(conn, trigger_id, session_id: str|None)`, `repo.insert_fill(...)` 는 중복(인덱스 충돌)이면 `None` 을 돌려준다(`INSERT OR IGNORE`).

- [ ] Step 1 실패 테스트: `test_load_schedule_parses_and_validates`(기본 yaml → kr.monitor.interval_s == 60, us.monitor.end == "06:00"; `"25:00"` 은 ConfigError; 모르는 키 ConfigError), `test_channels_config_defaults`, `test_v4_triggers_and_fill_dedupe`(SCHEMA_VERSION ≥ 4; 같은 fill 두 번 → 1행·두 번째 None; insert_trigger → last_trigger_handled_at None → mark → datetime).
- [ ] Step 2~4 RED → 구현 → GREEN(전체). Step 5 Commit `feat(config,ledger): schedule 로더·채널 설정·v4 triggers·fills 중복 방지`

### Task 2: 체결 동기화 (`fills`)

**Files:** Modify `src/trader/broker/tr_ids.py`, `models.py`(`Fill{order_no, ticker, market, side, qty, price: Decimal, filled_at: datetime}`), `accounts.py`, `fake.py`. Create `src/trader/orchestrator/fills.py`. CLI `trader fills sync [--account] [--date]`. Test `tests/broker/test_fills.py`(fixtures `ccld_kr.json`, `ccld_us.json` 를 공식 예제 응답 형태로 작성), `tests/orchestrator/test_fills.py`.

**Interfaces:**
- tr_ids: `DOM_CCLD = ("/uapi/domestic-stock/v1/trading/inquire-daily-ccld", "TTTC0081R", "VTTC0081R")`(3개월 이내), `OVS_CCLD_US = ("/uapi/overseas-stock/v1/trading/inquire-ccnl", "TTTS3035R", "VTTS3035R")`.
- `KisBroker.get_fills(market, d: date) -> list[Fill]`(KR: `INQR_STRT_DT=INQR_END_DT=YYYYMMDD, SLL_BUY_DVSN_CD=00, CCLD_DVSN=01(체결), INQR_DVSN=00`; 체결수량 0 행 제외. US: `ORD_STRT_DT/ORD_END_DT, OVRS_EXCG_CD=NASD, SLL_BUY_DVSN=00, CCLD_NCCS_DVSN=00`).
- `fills.sync_fills(conn, broker, account, d) -> dict{inserted: int, orders_updated: int}`: 체결을 `orders`(order_no 로 매칭, 없으면 `decision_id NULL`·`mode='external'` 행 생성) 에 연결해 `repo.insert_fill` 멱등 적재, 체결 합계가 주문 수량 이상이면 `orders.status='filled'`, 일부면 `'partial'`.
- `FakeBroker.fills: dict[(market, date) → list[Fill]]`.

- [ ] Step 1 실패 테스트: `test_get_fills_kr_params_and_parse`, `test_get_fills_us_parse`, `test_sync_fills_idempotent`(두 번 sync → inserted 1·0, fills 1행), `test_sync_marks_filled_and_partial`, `test_external_fill_creates_order_row`, `test_cli_fills_sync`.
- [ ] Step 2~4. Step 5 Commit `feat(broker,orchestrator): 체결 조회·fills 동기화`

### Task 3: `scan` — 정량 필터 + screener 세션

**Files:** Create `src/trader/scan/__init__.py`, `quant.py`, `screener.py`, `.claude/agents/screener.md`. Modify `src/trader/agents/schemas.py`(`Screened`), `src/trader/agents/registry.py`(screener 를 `--agents` 에 포함하는 `screen` 모드), `src/trader/orchestrator/session.py`(`SessionSpec.kind: Literal["analyze","screen","resume"]="analyze"`, stdin 에 `kind`·`candidates`), `.claude/agents/orchestrator.md`(screen 모드: 후보 요약을 screener 에게 주고 `tool submit-screen` 으로 제출), `src/trader/orchestrator/tools.py`(`submit_screen` → `state/runs/<date>/<market>/_screen.json`), `src/trader/agents/fake.py`(screen 모드: 상위 5개 그대로), CLI `trader scan --market KR [--asof] [--dry-run]`. Test `tests/scan/test_quant.py`, `tests/scan/test_screener.py`, `tests/test_cli_scan.py`.

**Interfaces:**
- `quant.score(df: pd.DataFrame) -> pd.DataFrame`(입력: 피처 스토어 최신 행(`ml/features` 열: `turnover_20`·`atr_pct`·`roc_20`·`high_52w_pos` 등 기존 열 이름을 그대로 씀), 출력: `ticker, market, score, reasons[list]`; 규칙: 거래대금 하위 30% 제외, 변동성(atr_pct) 상위 10% 제외, 점수 = z(roc_20)·0.5 + z(high_52w_pos)·0.3 + z(turnover_20)·0.2, 상한 30), `quant.candidates(universe: Universe, market, *, state=None, rank_fn: Callable[[pd.DataFrame], pd.DataFrame] | None = None, exclude: set[str]) -> list[dict{ticker, exchange, score, reasons}]`(피처 행이 없는 종목은 점수 없이 끝에 둔다).
- `schemas.Screened{picks: list[Pick{ticker: str, reason: str(max 200)}], rejected: list[str]}`(picks 1~5).
- `screener.run_screen(settings, conn, accounts, *, market, date, candidates, cap_hit: bool, state=None, now=...) -> list[dict]`: 세션(`kind="screen"`, 모델 `models.screener`=haiku)을 띄워 `_screen.json` 을 읽고 picks 를 돌려준다. `cap_hit` 면 세션 없이 정량 상위 3개(상한에 더 가까우면 1개). 반환은 `analyze` 가 쓰는 `[{"ticker","exchange"}]` + 보유종목(브로커 `get_positions`) 합집합(중복 제거, watch 목록 항상 포함).
- CLI `scan` 은 `{"market","date","candidates":[…],"picks":[…],"analyze_targets":[…]}` 를 stdout JSON 과 `state/runs/<date>/<market>/_scan.json` 에 남긴다.

- [ ] Step 1 실패 테스트: `test_quant_filters_and_scores`(합성 피처 df 8종목 → 거래대금 하위 제외·상위 30 상한·점수 내림차순), `test_candidates_merges_universe_and_watch`, `test_screened_schema_limits`(picks 6개 거부), `test_fake_screen_session_writes_screen_json`, `test_cap_hit_reduces_picks`(세션 상한 도달 시 3개, 세션 없음), `test_cli_scan_fake`(FAKE_CLAUDE + 피처 parquet 픽스처 → `_scan.json`).
- [ ] Step 2~4. Step 5 Commit `feat(scan): 정량 필터·screener 세션·scan 명령`

### Task 4: `monitor` — 손절 즉시 매도·트리거·재평가 세션

**Files:** Create `src/trader/orchestrator/monitor.py`, `src/trader/orchestrator/resume.py`. Modify `schemas.py`(`TriggerContext`), `session.py`(`kind="resume"`: stdin 에 `trigger`·`decision`·`quote`), `orchestrator.md`(resume 모드: 수집 없이 trader 만 재호출해 `submit-decision`), `fake.py`(resume: HOLD 유지), `executor.py`(`execute(..., is_stop_loss=True)` 전달·`mode="approve"` 라도 손절이면 즉시 `_place`), `.claude/agents/trader.md`(트리거 입력 절). CLI `trader monitor --market KR [--once] [--interval 60]`. Test `tests/orchestrator/test_monitor.py`, `test_resume.py`, `tests/test_cli_monitor.py`.

**Interfaces:**
- 순수 함수 `monitor.check_position(pos: Position, decision: dict | None, quote: Decimal, *, stop_loss_pct: float, now: datetime) -> list[Signal]`, `Signal{kind: Literal["stop_loss","take_profit","move_7pct","invalidation"], price: Decimal, detail: dict}`; `monitor.parse_price_conditions(conditions: list[str]) -> list[tuple[str, Decimal]]`(`"<"`/`">"`·`"이하"`/`"이상"` + 숫자).
- `monitor.monitor_once(ctx: MonitorContext{settings, accounts, conn, brokers, market_client, notifier, state, now, session_runner}, *, market, today) -> dict{checked, stop_loss_orders, triggers, reevaluated}`: 계좌별 포지션(브로커) → 종목별 최신 결정(`decisions`+`30_decision`) → 시세 1회(종목당) → 손절이면 `executor.execute(SELL 결정 합성, is_stop_loss=True, dry_run=False)` → 그 밖의 신호는 `repo.insert_trigger` 후 `resume.reevaluate(...)`(30분 내 중복·세션 상한·paused 면 기록만).
- `resume.reevaluate(ctx, *, market, date, ticker, exchange, trigger_id) -> SessionResult`: `SessionSpec(kind="resume", tickers=[…], trigger=…)` 로 `run_session` → 새 `30_decision`(`run_dir` 하위 `resume_<HHMMSS>/`… 대신 **같은 디렉터리에 `30_decision` 을 덮어쓰지 않고 `31_decision_r<n>`** 로 저장, `submit-decision --stage 31_decision_r<n>` 허용) → `executor.execute`. `mark_trigger_handled`.
- 손절 합성 결정: `Decision(action="SELL", target_weight=0, order_type="MARKET"(KR)/"LIMIT"(US, 현재가), confidence=1.0, rationale="손절선 도달")`.

- [ ] Step 1 실패 테스트: `test_check_position_stop_loss_from_decision_and_default`, `test_parse_price_conditions`, `test_take_profit_and_move_7pct_signals`, `test_stop_loss_sells_immediately_even_in_approve_mode`(FakeBroker.orders 1, approvals 0, 텔레그램 통보), `test_stop_loss_off_account_records_only`, `test_trigger_reevaluation_rate_limited`(두 번째 신호는 triggers 행만, session_runner 호출 1회), `test_reevaluation_blocked_when_paused_or_cap`, `test_resume_session_writes_31_decision_and_executes`(FAKE), `test_cli_monitor_once`.
- [ ] Step 2~4. Step 5 Commit `feat(monitor): 손절 즉시 매도·트리거·재평가 세션`

### Task 5: 수집 보강 — YouTube·텔레그램 채널·manual_inputs·Reddit OAuth·이연 항목

**Files:** Create `src/trader/sources/youtube.py`, `telegram_channel.py`, `manual_inputs.py`. Modify `community.py`(Reddit OAuth `client_credentials`, UA `macos:trader:0.1 (by /u/<REDDIT_USER>)`, 키워드 단어 경계), `inputs.py`(`sentiment.community.sources.youtube/telegram`, `sentiment.manual_inputs`, `news_macro.events`(DART·EDGAR 예정 공시 + 결정의 horizon 내 실적일은 "미확정" 표기), 공시 종료일 KST, 미래 `asof` 거부, 기존 파일 건너뜀 시 `{"skipped": true}` 반환), `config.py`(Secrets `REDDIT_CLIENT_ID/SECRET/USER`), `secrets/.env.example`(키 이름만), `docs/superpowers/specs/…`(§0.3 표에 토스·네이버 조사 결과 추가). Test `tests/sources/test_youtube.py`, `test_telegram_channel.py`, `test_manual_inputs.py`, `test_community.py`(추가), `tests/sources/test_inputs.py`(추가).

**Interfaces:**
- `youtube.YouTubeSource(channels: list[str], *, http, max_videos=3, transcript_fn=None).fetch(keywords, asof) -> dict{sources: {channel: [{video_id, title, published, transcript_excerpt(≤2000자)}]}}`: 채널 RSS(`https://www.youtube.com/feeds/videos.xml?channel_id=`)로 최신 영상 → `youtube_transcript_api`(의존성 추가; 실패는 항목별 error) → 키워드 매칭 영상만.
- `telegram_channel.TelegramChannelSource(channels, *, http, max_posts=30).fetch(keywords, asof)`: `https://t.me/s/<channel>` HTML(`tgme_widget_message_text`, `time datetime=`) 파싱, asof 필터, 키워드 매칭.
- `manual_inputs.read(ticker, *, state=None) -> dict|None`: `state/manual_inputs/<ticker>.md`(본문) 또는 `.json`, `mtime` 을 `received_at` 로.
- Reddit: `REDDIT_CLIENT_ID/SECRET` 있으면 OAuth(`https://www.reddit.com/api/v1/access_token`, grant `client_credentials`) + `https://oauth.reddit.com/r/<sub>/new.json`, 없으면 기존 RSS 유지(429 는 error 로 기록).

- [ ] Step 1 실패 테스트: `test_youtube_rss_and_transcript_filtered_by_keyword_and_asof`, `test_telegram_preview_parses_and_filters`, `test_manual_inputs_md_and_json`, `test_reddit_oauth_when_keys_present`, `test_keyword_ascii_word_boundary`(`"F"` 가 `"OFF"` 에 안 걸림), `test_collect_skips_existing_and_reports`, `test_future_asof_rejected`, `test_disclosure_end_date_kst`, `test_events_section_present`.
- [ ] Step 2~4. Step 5 Commit `feat(sources): YouTube·텔레그램 채널·manual_inputs·Reddit OAuth·이연 보강`

### Task 6: `daemon` — APScheduler·web·텔레그램 루프·lock·휴장일

**Files:** Create `src/trader/daemon.py`. Modify `cli.py`(`trader daemon [--no-web]`, `trader telegram loop [--interval 30]`), `pyproject.toml`(`apscheduler>=3.10,<4`). Test `tests/test_daemon.py`, `tests/test_cli_telegram_loop.py`.

**Interfaces:**
- `daemon.Lock(path).acquire() -> bool`(JSON `{pid, started_at}`; pid 살아 있으면 False, 죽었으면 덮어쓰고 True), `.release()`.
- `daemon.build_jobs(schedule: ScheduleConfig, settings, *, markets) -> list[Job{id, trigger: CronTrigger|IntervalTrigger, func_name, kwargs}]`: `kr_scan`(cron 07:30 KST, 월~금), `kr_analyze`, `kr_monitor`(interval 60s, 09:00~15:30 안에서만 실행하도록 `func` 안에서 창 검사), `us_*`(자정 넘김: `in_window(now, "23:30", "06:00")`), `reflect`(자리), `ml_train`(자리), `telegram_poll`(interval 30s), `fills_sync`(장 마감 후 1회). 잡 함수는 전부 `daemon.run_job(name, fn)` 로 감싸 예외를 로그·`runs` 테이블에 남기고 데몬을 죽이지 않는다.
- `daemon.should_skip(market, today, market_client) -> str|None`(주말/휴장/paused 사유 문자열).
- `daemon.run(settings, schedule, accounts, secrets, *, web: bool = True, scheduler_factory=BlockingScheduler, now=...)`: lock → web 스레드(`uvicorn.Server` + `threading.Thread(daemon=True)`) → 잡 등록 → `scheduler.start()`; SIGINT/SIGTERM 에 release.
- `telegram loop`: `telegram poll` 을 `interval` 초마다 반복(데몬 밖에서도 쓸 수 있게).

- [ ] Step 1 실패 테스트: `test_daemon_lock_rejects_second_instance`, `test_stale_lock_is_taken_over`(죽은 pid 파일), `test_build_jobs_registers_expected_ids_and_times`(cron 시각·요일, interval 60), `test_in_window_handles_midnight_crossing`, `test_jobs_skip_holiday`(FakeMarket holidays → `should_skip` 사유·run_job 미호출), `test_run_job_logs_exception_without_raising`, `test_daemon_run_registers_jobs_with_fake_scheduler`(scheduler_factory 가짜, web=False), `test_cli_telegram_loop_runs_n_times`(interval 0, `--max-iterations 2` 테스트 전용 옵션).
- [ ] Step 2~4. Step 5 Commit `feat(daemon): APScheduler 데몬·단일 인스턴스·휴장일 건너뜀·텔레그램 루프`

### Task 7: `install` — launchd / Windows 작업 스케줄러

**Files:** Create `src/trader/platform/installer.py`. Modify `cli.py`(`trader install [--dry-run] [--uninstall]`). Test `tests/platform/test_installer.py`.

**Interfaces:**
- `installer.render(platform: Literal["darwin","win32"], *, python: Path, project_root: Path, label="com.trader.daemon") -> str`: darwin 은 `~/Library/LaunchAgents/<label>.plist` 내용(`ProgramArguments=[<venv python>, "-m", "trader", "daemon"]`, `WorkingDirectory`, `RunAtLoad`, `KeepAlive`, `StandardOutPath=state/logs/daemon.out`), win32 는 작업 스케줄러 XML(로그온 트리거, `Exec Command=<python>`, `Arguments=-m trader daemon`, `WorkingDirectory`).
- `installer.install(*, dry_run: bool, run=subprocess.run, platform=sys.platform, home=Path.home()) -> dict{path, command: list[str], written: bool}`: 파일 쓰기 + `launchctl load -w <plist>` / `schtasks /Create /TN trader-daemon /XML <xml> /F`. `uninstall` 은 `launchctl unload` + 삭제 / `schtasks /Delete /TN trader-daemon /F`. 그 밖의 플랫폼은 `ConfigError`.

- [ ] Step 1 실패 테스트: `test_render_plist_contains_python_and_cwd`, `test_render_schtasks_xml_logon_trigger`, `test_install_dry_run_writes_nothing_and_prints_command`, `test_install_darwin_writes_and_calls_launchctl`(run 가짜), `test_uninstall_win32_calls_schtasks_delete`, `test_unsupported_platform_rejected`.
- [ ] Step 2~4. Step 5 Commit `feat(platform): install 명령 (launchd·작업 스케줄러 템플릿)`

### Task 8: 이연 소항목 — 트레이더 묶음 포트폴리오·휴장일 보유일·Phase 3/5 Minor

**Files:** Modify `src/trader/orchestrator/tools.py`(`checkpoint bundle --kind trader` 의 `context.portfolio` 를 계좌별 `latest_positions` 로; `checkpoint write --model` 옵션으로 `call.model` 기록; `_validate_ids` 가 `.`·`..` 거부), `src/trader/risk/rules.py`(`_trading_days_between(start, end, holidays: set[date])`), `executor.py`(휴장일 집합 전달: `KisMarket.is_holiday` 캐시 기반 `holidays_between`), `session.py`(세션 cwd 를 `tempfile.mkdtemp(prefix="trader-claude-")` 로, 종료 시 삭제), `ledger/metrics.py`·`views.py`(30일 창 KST). Test 기존 파일에 추가.

- [ ] Step 1 실패 테스트: `test_trader_bundle_includes_portfolio_per_account`, `test_checkpoint_write_records_model`, `test_validate_ids_rejects_dots`, `test_min_hold_skips_holidays`, `test_session_cwd_is_temp_and_removed`, `test_metrics_window_uses_kst_date`.
- [ ] Step 2~4. Step 5 Commit `fix: Phase 3~5 이연 소항목 (묶음 포트폴리오·휴장일 보유일·모델 기록·임시 cwd·KST 창)`

### Task 9: 실기·문서·PR

- [ ] Step 1: ruff·pytest 전부, `cd web && npm test && npm run build`(SSE 카운터에 `triggers` 추가 시).
- [ ] Step 2: 실기 — `FAKE_CLAUDE=1 uv run trader scan --market KR` → `_scan.json`; `FAKE_CLAUDE=1 uv run trader monitor --market KR --once`(보유 없음이면 checked 0); `uv run trader daemon --no-web` 를 10초 띄웠다 종료(lock 생성·해제 확인, 휴장일 건너뜀 로그); `uv run trader install --dry-run` 출력; `uv run trader fills sync --account paper-main`(휴장일이면 0건); YouTube·텔레그램 채널은 `config/sources.yaml` 에 채널이 비어 있으므로 "사용자 확인 대기"(채널 ID 입력 후 `collect --force`).
- [ ] Step 3: TODO Phase 6 체크(이연 항목 포함), CLAUDE.md(scan·monitor·daemon·install·수집 보강 규칙), `uv run trader status`, 커밋, PR → `feat/phase5-web`. 완료 기준 "평일 자동 1주 무사고"·"Windows install 후 기동" 은 사용자 확인 대기.

## Self-Review 메모
- §5.1 scan → Task 3(ML 랭킹은 `rank_fn` 자리), §5.2 monitor 손절·트리거·30분·세션 상한 → Task 4, §10.1 데몬·lock·휴장일·install → Task 6·7, §3 이연 수집(YouTube·텔레그램·manual_inputs·events·Reddit) → Task 5, Phase 4 이연(체결 동기화 → Task 2, 텔레그램 루프 → Task 6, 손절 → Task 4, 묶음 포트폴리오·휴장일 보유일 → Task 8), Phase 2 이연(공시 KST·건너뜀 표시·미래 asof·키워드 경계 → Task 5), Phase 3 이연(call.model·임시 cwd → Task 8), Phase 5 이연(SSE push·관심종목·지표 오버레이는 범위 조정, `_validate_ids`·KST 창 → Task 8). 토스·네이버·검색트렌드는 범위 조정에 사유 명시.
- 타입 일치: `SessionSpec.kind` 값 `"screen"|"resume"` 을 Task 3·4·6 이 같이 쓴다. `repo.insert_trigger` 시그니처를 Task 4 가 그대로 쓴다. `MonitorContext.session_runner` 는 `run_session` 과 같은 시그니처.
