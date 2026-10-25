# Phase 23 플랜 — stock-autotrading 이식 + 장중 뉴스 발굴

## Context
사용자 요청(2026-10-25): 리뷰에서 고른 이식 항목을 작업하고, 종목 발굴을 "하루 1회"가 아니라 **뉴스가 갱신될 때마다** 하도록 바꾼다. 이전 PR #20 은 main 에 머지됐고 작업 브랜치 `claude/youthful-ramanujan-jq4w0u` 는 main 위에 liveness 경로 수정 1커밋(`6757524`)만 있다. 이 플랜은 그 위에 쌓고 새 PR(main 대상, 드래프트 → 검증 녹색이면 Ready·머지)로 낸다.

**발굴 빈도에 대한 답**: 맞다. 사용자 결정(2026-10-25) "세션 20개 넘어도 상관없다" 에 따라 **뉴스 수집(30분)마다 새 뉴스가 있으면 scout 세션을 띄운다**. 세션 비용 절약을 위한 2층 분리는 하지 않고, 대신 쓸모없는 호출만 막는다: 새 뉴스가 0건이면 세션 없음, 이미 오늘 분석한(또는 큐에 있는) 종목은 다시 넣지 않음, 장중 창 밖(야간·주말)은 아침 scan 이 48h 뉴스를 한 번에 보므로 띄우지 않음. `llm.daily_session_cap` 기본값을 14 → 40 으로 올린다(종목 12 + 시장 2 + 장중 발굴 ≤ 2시장×13회). 유니버스 종목에 뉴스가 몰리는 경우는 scout 결과를 기다리지 않고 파이썬 트리거가 바로 분석 큐에 넣어 더 빠르게 간다(둘이 겹치면 큐 중복 가드가 잡는다). 실제 구독 여유는 이 세션에서 알 수 없다 — `llm.usage_file` 관측값(대시보드 '사용량')이 유일한 근거라 사용자가 보고 `discovery.intraday.discover_cap` 으로 조절한다.

## 작업 (커밋 단위)

### D 장중 뉴스 발굴 (핵심 요청)
**원칙(사용자 결정 2026-10-25)**: 새 뉴스가 있으면 **같은 종목이라도 다시 분석**한다. 단 새 뉴스가 이미 본 뉴스와 **같은 내용(유사 기사)** 이면 세션을 띄우지 않는다. 종목당 하루 1회 제한은 두지 않는다.
- **유사 뉴스 판정(파이썬, LLM 없음)** `sources/news_store.novel_items(conn, market, *, since, lookback_days=3, threshold=0.6)`: `since` 이후 받은 항목 중 (같은 시장, 최근 3일) 앞선 항목과 제목이 유사하면 뺀다 — 제목 정규화(소문자, 괄호·기호·언론사 접미 제거, 숫자 유지) 뒤 토큰 집합 Jaccard ≥ 0.6 **또는** `difflib.SequenceMatcher` 비율 ≥ 0.8 이면 중복. 같은 종목 태그끼리만 비교하고, 태그 없는 뉴스는 태그 없는 풀끼리 비교. 표준 라이브러리만. 판정 결과는 `news_items.novel`(원장 v16, INTEGER NULL = 미판정) 에 남겨 다시 계산하지 않는다. 테스트: 같은 사건의 매체 다른 기사 3건 → 1건만 novel, 숫자(가격·분기)가 다른 제목은 novel.
- `scan/discovery.py`: `news_burst(conn, market, *, since, min_items=1)` → novel 항목만 태그 종목별로 세어 ≥ min_items 인 유니버스/보유 종목 목록(+ 태그 없는 novel 수). `_discovery_inputs` 조립 재사용(`build_inputs(..., intraday=True, since=)` → 파일명 `_discovery_inputs_r<n>`, 뉴스는 novel 만).
- **같은 종목 재분석 경로**: 오늘 `30_decision` 이 이미 있으면 `analyze_ticker` 가 아니라 **재평가**로 간다 — `triggers` 행 kind `news`(detail 에 novel 뉴스 제목·id), 큐 `reevaluate{market, ticker, exchange, trigger_id}`(기존 `resume.reevaluate` → `31_decision_r<n>`, 종목당 30분 1회 규칙은 그대로 적용). 오늘 결정이 없으면 `analyze_ticker`. 큐 중복 가드가 대기 중 요청을 합친다.
- `daemon._news_body` 끝: 시장별 `_news_trigger(deps, market)` — 먼저 `novel_items` 로 새 뉴스를 판정·저장. 장중 창(`monitor` 창 안)·paused 아님·`settings.discovery.intraday.enabled` 일 때 (a) novel 뉴스가 붙은 유니버스·보유 종목마다 재분석(위 경로: 오늘 결정 있으면 `reevaluate`, 없으면 `analyze_ticker`, `source="news"`, priority 5), (b) **novel 뉴스가 1건 이상이면** `jobs.enqueue("discover", {market})`(유니버스 밖 발굴; 중복 가드로 앞 요청이 대기 중이면 하나만, `discover_cap` 기본 20). novel 이 0건이면 아무 세션도 띄우지 않는다. 직전 처리 시각은 `control.news_trigger_at:<market>`.
- 큐 kind `discover`(heavy): `_discover_body` — `build_inputs(intraday=True, since=직전 처리 시각)`(novel 뉴스만 싣고 `recent_picks` 에 오늘 분석·발굴 종목을 넣어 scout 가 같은 촉매를 반복하지 않게) → `SessionSpec(kind="discover")`(orchestrator.md `## discover 모드`: scout → `submit-discovery` 만, screener 없음) → `_discovery_r<n>` 후보를 재분석 경로로 enqueue(유니버스 밖은 피처 스냅샷 C1 로직 재사용). scout.md 에 "입력 뉴스는 이미 유사 기사를 걸러낸 것이고, `recent_picks` 에 있는 종목은 **새 촉매**가 있을 때만 다시 낸다" 를 명시. FAKE 경로는 기존 fake scout 흉내.
- 설정 `settings.yaml discovery: {intraday: {enabled: true, min_items: 1, discover_cap: 20, similarity: {jaccard: 0.6, ratio: 0.8, lookback_days: 3}}}`(+templates, `DiscoveryCfg`), `llm.daily_session_cap` 14 → 40(+templates·테스트·CLAUDE.md 숫자). 큐 실행 시점의 상한 검사는 기존 `run_session`.
- 재평가 세션의 트레이더 입력: `resume` 모드 `trigger.kind == "news"` 면 트리거 detail 의 novel 뉴스 제목 목록이 들어가고 `00_inputs` 는 세션 앞에서 다시 수집한다(기존 재평가와 같은 `collect` 경로, `--force`).
- 알림 `[뉴스 발굴] KR 005930: 새 뉴스 3건 → 분석 요청 #id`. 오늘 페이지 '오늘 스캔' 아래 '장중 발굴' 행(`_scan.json` 이 아니라 `job_requests.source='news'` 조회).
- 테스트: `tests/sources/test_news_store.py`(novel_items 유사 판정·저장), `tests/scan/test_discovery.py`(news_burst), `tests/test_daemon_news.py`(트리거 → analyze/reevaluate/discover enqueue, 유사 뉴스만이면 세션 없음, 창 밖·paused 거부), `tests/test_daemon_queue.py`(discover body FAKE), `tests/orchestrator/test_resume.py`(kind=news 트리거 입력).

### T 보유기간별 차등 주기 (사용자 결정 2026-10-25)
판단 주기는 계층별이다 — 초~분: 파이썬 monitor, 분~시간: 트리거·뉴스 재평가 세션, 일: 아침 scan→analyze, 중·장기: horizon_days·만기 복기. 이 층을 **결정의 보유기간(horizon)** 으로 차등한다.
- 프로필 `risk/horizon.py`: `profile_of(horizon_days)` → `short`(≤10일)·`mid`(11~40)·`long`(>40). 설정 `risk.horizon_profiles`(+templates, `HorizonProfile` pydantic): `move_pct`(급등락 트리거: 단기 0.04·중기 0.07·장기 0.10), `reeval_interval_min`(재평가 간격: 15·30·60), `periodic_check_min`(시간 단위 정기 재평가: 단기 60·중기 0=없음·장기 0), `news_min_novel`(뉴스 재분석에 필요한 novel 건수: 1·1·3), `daily_reanalyze_every`(아침 재분석 주기 운영일: 1·2·5 — 그 사이 날은 보유만 monitor, 트리거·뉴스가 있으면 즉시).
- `monitor.check_position(..., profile=)`: `MOVE_PCT` 상수 → 프로필 값; `_can_reevaluate` 의 `REEVAL_INTERVAL` → 프로필 값; 단기 보유는 `periodic_check_min` 마다 kind `periodic` 트리거(가격 변동 ≥ move_pct/2 일 때만, 운영일 상한 6회)로 시간 단위 재평가. 결정의 프로필은 `30_decision.horizon_days` 에서(없으면 mid).
- 데몬 `_analyze_body` 분석 대상 중 **보유 종목**은 프로필의 `daily_reanalyze_every` 를 보고 건너뛸 수 있다(`control.last_analyzed:<market>:<ticker>`; picks·watch·뉴스 재분석은 영향 없음). 뉴스 트리거(D)는 `news_min_novel` 을 프로필별로 본다.
- trader.md: "horizon_days 가 재평가 주기를 정한다 — 단기(≤10일)는 시간 단위 점검·±4% 재평가, 장기(>40일)는 큰 변동·뉴스 묶음 때만" 명시. 웹 보유 표에 프로필 배지(단기·중기·장기), 오늘 페이지 결정 요약에 표시.
- 테스트 `tests/risk/test_horizon.py`, `tests/orchestrator/test_monitor.py`(프로필별 임계·간격·periodic 트리거), `tests/test_daemon.py`(장기 보유 재분석 건너뛰기).

### S 분 단위 단타 층 (사용자 결정 2026-10-25, 모의 전용·기본 끔)
LLM 세션은 1회 수 분이라 분 단위 판단은 **파이썬 신호 엔진**이 감지·집행하고, 에이전트는 "오늘 어떤 종목을 단타 후보로 볼지·단타 허용 여부·예산" 을 정한다(종목 판단은 scout·screener·시장 세션 결과를 재사용, 분 단위 LLM 호출 없음).
- 유니버스: 오늘 `_scan.json` picks + 뉴스 novel 급증 종목(D) + watch, **보유 종목·헤지 ETF 제외**, 시장 국면 bear 면 단타 금지(전략가 RISK_OFF 확신 ≥ 기준이면 neutral 에서도 금지 — Phase 22 기울기 재사용).
- `scalp/signals.py`(순수 함수, 1분봉 입력): 진입 = 최근 5분 거래량 ≥ 직전 20분 평균 × `volume_mult`(3) ∧ 종가가 당일 고가·VWAP 동시 상향 돌파 ∧ 개장 뒤 `warmup_min`(15분) 경과 ∧ 마감 `close_before_min`(20분) 전; 청산 = 익절 `take_profit_pct`(+1.5%) / 손절 `stop_pct`(−1.0%) / 보유 `max_hold_min`(60) / 마감 전 전량. 하루 종목당 1회, 동시 보유 `max_positions`(2), 비중 `max_weight_pct`(0.05, 계좌 NAV), 단타 일일 실현손실 `daily_stop_pct`(−1%) 닿으면 그날 중단. 백테스트 함수 `scalp.backtest(candles)`(분봉 이력으로 승률·손익 — CLI `trader scalp backtest --ticker --days`).
- 집행 `scalp/engine.py`: 데몬 interval 잡 `<시장>_scalp`(60초, monitor 와 별도, 장중 창·paused·dry_run 준수, heavy 밖). 1분봉은 `get_minute_kr/us` 로 종목당 1회/틱(KIS 20/s 한도 안, 유니버스 ≤ 10종목). 결정은 파이썬 `Decision(source='scalp', horizon_days=1, order_type=LIMIT 현재가)` → `executor.execute(is_scalp=True)`: 게이트는 비중·현금·중복만 적용(최소 보유 3거래일·주 2회·기대수익 규칙 면제, 헤지와 같은 방식), 체크포인트 `47_scalp_<id>`·`57_order_scalp_<id>`, `decisions.source='scalp'` 라 분포·메모리·복기 제외. **모의 계좌(`env == "paper"`)만** — 실전 계좌는 설정이 켜져 있어도 기록만(`note`). 분봉·신호·체결은 `state/scalp/<date>/<market>.jsonl` 에 남겨 다음 날 `scalp report` 로 승률·슬리피지를 본다.
- 에이전트 연결: strategist 의 `HedgeProposal` 에 `scalp_ok: bool`(기본 true) 추가 → `market_view.scalp_ok` 가 false(확신 ≥ 기준)면 그날 단타 금지. scout.md·screener.md 에 "단타 후보 표시는 하지 않는다(파이썬이 picks 를 그대로 쓴다)" 명시.
- 설정 `settings.yaml scalp: {enabled: false, max_weight_pct: 0.05, max_positions: 2, volume_mult: 3, take_profit_pct: 0.015, stop_pct: 0.01, max_hold_min: 60, warmup_min: 15, close_before_min: 20, daily_stop_pct: 0.01}`(+templates, `ScalpCfg`). 켜는 것은 사용자가 모의에서 `scalp backtest`·`scalp report` 를 본 뒤.
- 웹: 오늘 페이지 '단타' 표(진입·청산·손익·사유), 대시보드 상태줄 칩 '단타 켬/끔', 설정 페이지 토글은 두지 않음(yaml 만 — 실주문 경로). 텔레그램 `[단타 진입]`·`[단타 청산]` 알림, STATUS 한 줄.
- 테스트 `tests/scalp/test_signals.py`(진입·청산·시간 규칙·백테스트), `tests/scalp/test_engine.py`(모의만 주문·bear 금지·일일 중단·종목당 1회·dry_run), `tests/test_daemon_scalp.py`(잡 창·heavy 밖), 스키마 테스트(`scalp_ok`).

### A 운영 점검 + 특보판
- `src/trader/ops/health.py`(신규): 다섯 영역 판정(순수 함수 + 원장 읽기): `loss`(일일/5일 실현손익 대비 −5%/−10%), `orders`(unknown·마감 뒤 미정리·승인 만료 수), `reconcile`(오늘 `reconcile:*` 불일치·미실행), `jobs`(하트비트 stale, 최근 24h error 잡, missed cron), `llm`(세션 실패율·상한 근접·사용량 창). 단계 `ok|watch|warning|critical`, 각 항목 `{key, level, text, link}`. 최악 단계 = 머리.
- 데몬 interval 잡 `ops_check`(30분, 가벼움): 단계가 직전(`control.ops_level`)보다 나빠질 때만 `[점검 격상] …` 알림. `GET /api/system/ops`. 텔레그램 `/status` 와 STATUS.md 에 머리 한 줄.
- 웹 `components/Advisory.tsx`: 대시보드 맨 위. 정상이면 괘선 한 줄("점검 정상 · 10:30"), 아니면 펼쳐 항목 표(배지는 `Badge kind=pending|neutral`, 위험은 `--danger`). KPI 타일 금지 준수. 그 아래 '조치 필요' 블록(미승인·unknown 주문·대조 불일치·세션 실패 — 각 행이 해당 페이지 링크).
- 테스트 `tests/ops/test_health.py`, `tests/web/test_app.py`, vitest `advisory.test.ts`(행 변환 순수 함수).

### B 주문 안전: UNKNOWN 엄격 종결 + 아침 미체결 재호가
- `fills._order_id_for`: 같은 현지 날짜·종목·방향에 **살아 있는 후보가 정확히 1건**일 때만 unknown 에 붙인다. 2건 이상이면 붙이지 않고 `[체결 매칭 모호]` 알림 1회(`control.fill_ambiguous:<date>`), 사용자 확인 항목으로 특보 `orders` 에 노출. 테스트 `tests/orchestrator/test_fills.py`.
- 재호가 `orchestrator/reprice.py`: monitor 첫 창 뒤 `reprice_after_min`(30) 분에 운영일 1회 — 오늘 `accepted` 지정가 BUY 미체결(체결 0)을 **취소 후 같은 결정으로 재제출**(`executor.execute` 재호출, 현재가 지정가, 게이트 재통과; 취소된 원 주문은 dead 라 중복 가드에 걸리지 않음). SELL/TRIM 지정가는 현재가로 재호가(취소→재제출 동일). 상한: 원 지정가 대비 `max_chase_pct`(1%) 넘게 비싸면 재제출하지 않고 알림. dry_run·off 계좌 제외. 설정 `risk.reprice: {enabled: true, after_min: 30, max_chase_pct: 0.01}`. 체크포인트 `52_reprice_<id>`. 테스트 `tests/orchestrator/test_reprice.py`, `tests/test_daemon_hedge.py` 옆 monitor 연결 테스트.

### C watchdog
- `trader watchdog [--once]`: 데몬 밖 프로세스가 `heartbeat.read_status`(stale 10분)·오늘 `reconcile` 플래그(16:40 현지 +30분 뒤 없으면)·`ops_level` 을 보고 문제면 텔레그램(1시간 간격 재알림, `state/watchdog.json`). `trader install --watchdog` 가 5분 간격 launchd plist / 작업 스케줄러 항목을 추가(기존 install 패턴, 거부 목록 유지). 테스트 `tests/test_cli_watchdog.py`.

### E 결정 추적(trace)
- `GET /api/decisions/{id}/trace`: 그 결정의 체크포인트 묶음을 한 번에(`10_analyst_*` 요약 — 역할·stance·confidence·핵심 근거 2개, `20_debate` 라운드별 stance·conviction, `25_research_summary` 의 sycophantic, `30_decision`, `40_risk_check_*` reasons·note, `50_order_*` 상태). `views.decision_trace`.
- 웹 주문 페이지: 행 펼침으로 trace(애널리스트 4 → 토론 → 결정 → 게이트 → 주문) + UNKNOWN "확인 필요 N건" 경고줄 + 지정가까지 거리 %. 테스트 `tests/ledger/test_views.py`, vitest `orders.test.ts`.

### G 웹 구조
- `web/src/nav.ts`: 묶음(운용·분석·시스템)·`purpose`·`features[since]` 한 곳 → Shell 레일, 모바일 하단 탭 5칸(홈·오늘·주문·계좌·전체), `/how` 의 "최근 추가" 목록.
- `components/CommandPalette.tsx`: ⌘K / Ctrl+K, 이동 전용(화면·종목 검색 → `/chart?ticker=`), `g` 두 키. 쓰기 명령 없음.
- `components/Table.tsx` 에 `core` 열 + 좁은 화면 행 펼침(`<details>`) 옵션(가로 스크롤 대신). 적용: 시세표·주문·보유.
- CSV: 서버 `?format=csv` (`/api/orders`·`/api/decisions`·`/api/portfolio` 보유), 웹 `CsvLink`.
- `hooks.useLoad`: 가려진 탭이면 재조회 쉬고 돌아오면 즉시(`visibilitychange`), 전역 `trader:refresh` 이벤트.
- 테스트 vitest(nav·palette 순수 함수·table), `tests/web/test_app.py` csv.

### H 문서·검증·PR
- CLAUDE.md(장중 발굴·특보·재호가·UNKNOWN 규칙·watchdog), `docs/architecture.md` 「Phase 23」, `docs/TODO.md` Phase 23(+ 실기·결정 대기: 종목 일정 블랙아웃은 DART 기준일 소스 확인 뒤 다음 Phase), 플랜 파일, how.ts, README.
- `uv run ruff check src tests`·`uv run pytest`·`cd web && npm run typecheck && npx vitest run && npm run build` → 커밋·푸시 → 드래프트 PR(main) → CI 녹색 → Ready → 머지(이전과 같은 절차) → 브랜치를 main 으로 재설정.

## 진행 상태 (2026-10-25, 플랜 모드 재진입 시점)

**D·T·S 는 작업 트리에 구현 완료, 미커밋**(변경 파일 50개). 검증: `ruff check src tests` 통과, 웹 `typecheck`·vitest 147·`build` 통과, 전체 pytest 는 백그라운드 실행 중(직전 실행 1179 통과·5 실패 중 4건은 해당 테스트 수정으로 해소, 1건 `test_cli_app` 은 재실행 시 통과하는 시간 의존 플레이크).

구현된 것:
- **D**: 원장 v16 `news_items.novel`, `news_store.novel_items`(Jaccard 0.6/ratio 0.8, 숫자 다르면 새 뉴스), `discovery.news_burst`·`build_inputs(intraday_news=, analyzed_today=)`(`_discovery_inputs_r<n>`), 세션 kind `discover`(`DISCOVER_AGENTS=("scout",)`, FAKE `run_fake_discover`, `submit_discovery` 가 `_discovery_r<n>` 사본), 데몬 `_news_trigger`(장중 창·paused·설정 → 종목 재분석: 오늘 결정 있으면 트리거 kind `news` + `reevaluate`, 없으면 `analyze_ticker` priority 5 / 새 뉴스 ≥1 이면 큐 `discover` ≤ `discover_cap`) · `_discover_body` · `_recollect`(뉴스 재평가 전 `00_inputs` 재수집) · `repo.latest_trigger_price` 가 가격 없는 최신 트리거면 None(재시세 적용), 설정 `discovery.intraday`, `daily_session_cap` 40, `/api/today.news_jobs` + 오늘 페이지 '장중 발굴' 표, orchestrator/scout/trader md.
- **T**: `HorizonProfile`·`RiskLimits.horizon_profiles`(short/mid/long, 셋 필수), `risk/horizon.py`(name_of·of_decision·reanalyze_due), monitor `check_position(move_pct=)`·`periodic_signal`·`_can_reevaluate(interval)`, 데몬 `_skip_held_by_profile`(screener 가 target `source` 표기), 뉴스 트리거의 보유 종목 `news_min_novel`, `/api/portfolio` 보유 행 `horizon`·`horizon_days` + 표 '보유기간' 열, trader.md.
- **S**: `ScalpCfg`(기본 끔), `HedgeProposal.scalp_ok`, `scalp/signals.py`(entry_signal·exit_reason·backtest_day·summarize), `scalp/engine.py`(allowed·candidates·tick·report, 상태 `state/scalp/<date>/<market>.json` + `events.jsonl`), `rules.evaluate(is_scalp=)`(최소 보유·주 2회·기대수익 면제), `executor.execute(scalp_leg=)`(47_/57_, RL 제외), 데몬 `<시장>_scalp` 60초 잡·`_scalp_body`, CLI `trader scalp backtest|report`, `/api/system/scalp`, 대시보드 칩·오늘 페이지 '단타' 표, STATUS 절, strategist/scout/screener md.

**플랜 대비 바뀐 점(기록)**: ① 단타는 `decisions` 행을 만들지 않는다(`source='scalp'` 대신 `orders.decision_id=NULL`) — `decisions` 의 UNIQUE(date, market, ticker) 가 오늘 LLM 결정과 겹쳐 덮어쓰기 때문. 기록은 상태 파일·47_/57_ 체크포인트·orders 행. ② D·T·S 가 공유 파일(config·daemon·executor·rules·cli·md)을 함께 바꿔 **한 커밋**으로 낸다(플랜의 "D·T → S" 두 커밋 대신).

남은 순서: 전체 pytest 녹색 확인 → D·T·S 커밋 → A → B·C·E → G → H(문서·PR·머지).

## 하지 않는 것
- 규칙 전략 엔진·슬리브·빌더·메타 라벨(설계 충돌). 분 단위 **LLM** 판단(세션 지연으로 불가 — 단타는 파이썬 신호 + 에이전트의 후보·허용 판단). 디자인 세계 교체(신문 증권면 유지). 종목 일정 블랙아웃(소스 미확인 → TODO). 단타의 실전 계좌 적용(모의 검증 뒤 사용자 결정, 코드로는 paper 만).

## 위험
- 장중 발굴은 세션을 하루 최대 20여 개 늘린다(사용자가 수용). 상한 40 안에서 돌고 `discovery.intraday.enabled: false`·`discover_cap` 으로 줄일 수 있다. 구독 사용률은 대시보드 '사용량' 으로 사용자가 본다.
- 재호가는 실제 주문을 낸다(취소+재제출). 게이트를 다시 통과하고 추격 상한 1% 를 둔다. dry_run 계좌는 기록만.
- 단타는 분 단위로 실주문을 낸다 — 모의 전용·기본 끔·비중 5%·일일 −1% 중단. 1분봉 신호는 백테스트 함수로 먼저 본다. 보유기간별 단기 프로필은 재평가 세션을 늘린다(시간 단위, 운영일 ≤6회/종목) — 상한 40 안.
- 커밋 순서: D·T → S → A·B·C·E → G → H. 각 묶음마다 전체 검증 뒤 커밋.
