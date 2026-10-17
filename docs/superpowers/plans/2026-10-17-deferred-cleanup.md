# 이연 항목 회수 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Phase 3~9 가 `docs/TODO.md` 에 미뤄 둔 리뷰 Minor·이연 항목 가운데 키·외부 데이터·사용자 결정 없이 끝낼 수 있는 것을 전부 회수하고, 나머지는 이유와 함께 명시적으로 남긴다.

**Architecture:** 기존 모듈 안에서 작은 수정을 모은다. 새 모듈은 둘뿐이다: NYSE 휴장일 규칙 계산(`daemon` 안 함수)과 피처 스토어 "최신 행" 공용 읽기(`ml/features.latest_rows`). 모든 변경은 기존 테스트 파일에 테스트를 덧붙이고 각 태스크가 독립 커밋이다.

**Tech Stack:** 기존 그대로(Python 3.12, pandas, SQLite, FastAPI, React).

**Spec:** `docs/superpowers/specs/2026-10-09-trading-agent-design.md` — §5.1(scan 정량 필터), §6.3(원장 집계), §6.5(메모리), §8(웹 제어 API 에 관심종목 포함), §10.1(휴장일 건너뜀), §13(성과·준비도). 원천은 `docs/TODO.md` 의 이연 줄이다.

## Global Constraints

- CLAUDE.md 코딩 규칙 전부(pathlib, UTC 저장·KST 표시, `extra="forbid"`, 마이그레이션 추가만, 테스트 `tmp_path`·`respx`, 한국어, ruff 0건).
- 실전 주문·계좌 모드를 건드리는 변경 없음. 제어 API 는 Bearer 토큰 필수(§8).
- 커뮤니티·뉴스 소스 on/off 변경 없음(§0.3).

## Review Focus

1. 휴장일을 규칙으로 계산하면 "1월 1일이 토요일인 해"(2028)에 NYSE 는 12/31 을 쉬지 않는다 → Task 2 `test_us_holidays_rules_match_static_and_2028`.
2. 피처 스토어 최신 행을 파일 일부만 읽으면 유니버스에서 빠진 종목(옛 파일에만 있음)도 찾아야 한다 → Task 3 `test_latest_rows_falls_back_to_older_files`.
3. 관심종목을 시장별로 거를 때 숫자 6자리는 KR, 그 외는 US 로 보되 `KR:005930` 처럼 명시한 값은 그대로 따른다 → Task 4 `test_watchlist_market_filter`.
4. 웹 관심종목 POST 는 토큰 없으면 401, 잘못된 티커는 400 이며 GET 은 토큰 없이 된다 → Task 6 `test_watchlist_api_token_and_validation`.
5. `rl_shadow.applied` 는 게이트가 거부한 제안을 "적용" 으로 남기지 않는다 → Task 5 `test_apply_mode_rejected_by_gate_not_marked_applied`.

## 범위 조정

| 제외 | 이유 | 재개 |
|---|---|---|
| KRX 공매도·대차잔고·검색트렌드 수집 | KRX 데이터는 API 약관·키, 네이버 데이터랩은 키 필요 → 사용자 결정 | 사용자 확인 대기 |
| US 연도별 재무비율·동종업 중앙값 비율 | EDGAR 과거 연도·업종 맵 데이터 설계가 따로 필요 | 별도 Phase |
| 하이퍼파라미터 탐색, 멀티 종목 RL 환경, RL 전용 페이지, 분봉 차트, 피처 행 탐색 UI | 실험·UI 확장(가치 대비 범위 큼) | 사용자 결정 |
| 승인 시점 게이트 재평가 | 설계상 "30분 안의 변화는 감수"(CLAUDE.md) | 변경 없음 |
| `approvals.decision_id` 0 기록 | 결정 없는 승인 요청은 손절 경로뿐이고 거기엔 decision_id 가 있음 | 변경 없음 |
| `pid_alive` 가 PermissionError 를 생존으로 봄 | POSIX 의미상 EPERM = 프로세스 존재, 올바름 | 변경 없음 |
| `uv.lock` torch 항목 | extra 전용, 기본 설치 영향 없음 | 변경 없음 |
| 학습/서빙 drawdown 정의 차이 | Phase 9 메타에 명시, 멀티 종목 환경과 함께 | 사용자 결정 |
| 이미 해결됨(TODO 만 정리): 세션 cwd tempfile, `checkpoint write --model`, `/watch` 사용법, telegram_offset 전진, metrics KST 창, ML 페이지 실데이터, 성과 페이지, 텔레그램 관심종목 제어, `_validate_ids` 점 티커, KR 호가단위(`kis order-test`) | 후속 Phase 에서 구현됨 | Task 7 에서 TODO 체크 |

---

### Task 1: 원장 집계 정확성

**Files:**
- Modify: `src/trader/ledger/repo.py:498`(`nav_history`), `src/trader/ledger/performance.py:133`(`_orders_between`), `src/trader/calibration/model.py:103`(`build_calibration`), `src/trader/memory/store.py:38`(`record_decision`), `src/trader/readiness.py:46`(`_rate`)
- Test: `tests/ledger/test_repo.py`(있으면, 없으면 `tests/ledger/test_performance.py`), `tests/ledger/test_performance.py`, `tests/calibration/test_model.py`, `tests/memory/test_store.py`, `tests/test_readiness.py`

**Interfaces:**
- Produces: `repo.nav_history(conn, account_id, market, *, days=30, today: date | None = None)` — `today`(없으면 마지막 스냅샷 날짜) 기준 `date ≥ today − days` 창(스냅샷 개수가 아니라 날짜). `performance._orders_between` 은 KST 자정 경계(`start 00:00 KST` ~ `end+1 00:00 KST`)를 UTC 로 바꿔 비교. `build_calibration` 은 같은 KST 날짜에 행이 있으면 UPDATE(하루 1행). `record_decision(decision_id=None)` 은 `ValueError`. readiness `_rate` 는 `runs` 에 한해 `kind NOT IN ('monitor','telegram_poll','fills_sync')`(interval 잡은 오류일 때만 기록되므로 분모에서 제외).

- [ ] **Step 1: Write the failing tests**

```python
# test_performance.py
def test_nav_history_is_date_window_not_count(conn):
    for d, v in [("2026-09-01", 100), ("2026-10-10", 101), ("2026-10-15", 102)]:
        repo.snapshot_positions(conn, account_id="paper-main", d=date.fromisoformat(d), market="KR",
                                nav=Decimal(v), cash=Decimal(v), positions=[])
    assert [d for d, _ in repo.nav_history(conn, "paper-main", "KR", days=10,
            today=date(2026, 10, 17))] == ["2026-10-10", "2026-10-15"]

def test_orders_window_uses_kst_day(conn):
    # 2026-10-17T23:30Z = 10-18 08:30 KST → 10-17 창에 포함되지 않는다
    _insert_order(conn, created_at="2026-10-17T23:30:00+00:00", amount="1000")
    _insert_order(conn, created_at="2026-10-17T02:00:00+00:00", amount="500")
    rows = performance._orders_between(conn, "paper-main", "KR", date(2026, 10, 17), date(2026, 10, 17))
    assert [r["amount"] for r in rows] == ["500"]

# test_model.py
def test_build_calibration_one_row_per_day(conn):
    seed_matured(conn, 3)
    for _ in range(2):
        model.build_calibration(conn, min_decisions=1, query_vector=None, asof="2026-10-17T00:00:00+00:00")
    assert conn.execute("SELECT COUNT(*) FROM calibration_runs").fetchone()[0] == 1

# test_store.py
def test_record_decision_requires_decision_id(conn):
    with pytest.raises(ValueError):
        store.record_decision(conn, decision_id=None, ticker="T", market="KR", account_id="p",
                              decided_at="2026-10-17T00:00:00+00:00", decision={}, row=None, horizon_days=5)

# test_readiness.py
def test_run_error_rate_ignores_interval_jobs(conn, ...):
    # cron 잡 ok 9 + error 1, monitor error 5 → 0.1 (monitor 제외)
    ...
    assert item("run_error_rate")["value"] == pytest.approx(0.1)
```

- [ ] **Step 2: Run** → FAIL (키워드 인자 없음 / 2행 / 예외 없음 / 비율 0.4).
- [ ] **Step 3: Implement** 위 Interfaces 대로. `_orders_between` 은 `ZoneInfo("Asia/Seoul")` 로 경계를 만들고 `.astimezone(UTC).isoformat()` 로 비교.
- [ ] **Step 4: Run** 해당 테스트 파일 전부 → PASS.
- [ ] **Step 5: Commit** — `fix(ledger): NAV 날짜 창·주문 KST 창·보정 하루 1행·decision_id 필수·오류율 분모`

---

### Task 2: 데몬 견고성 — stale running 정리·reflect 시장별 격리·NYSE 휴장일 규칙

**Files:**
- Modify: `src/trader/daemon.py:100`(`US_HOLIDAYS` → 규칙 함수), `:126`(`should_skip`), `:494`(`_reflect_body`), `:636`(`run` 시작부)
- Modify: `src/trader/ledger/repo.py`(`close_stale_running`)
- Test: `tests/test_daemon.py`, `tests/ledger/test_repo.py`(또는 `test_db.py`)

**Interfaces:**
- Produces: `daemon.us_holidays(year: int) -> frozenset[date]` — NYSE 규칙: 신정(1/1, 일요일이면 월요일 대체, **토요일이면 대체 없음**), MLK(1월 셋째 월), 대통령의 날(2월 셋째 월), 성금요일(부활절 −2일, 부활절은 Anonymous Gregorian 알고리즘), 현충일(5월 마지막 월), 준틴스(6/19), 독립기념일(7/4), 노동절(9월 첫 월), 추수감사절(11월 넷째 목), 성탄절(12/25). 토→금, 일→월 대체(신정 토요일 제외). `is_us_holiday(d: date) -> bool`. 기존 `US_HOLIDAYS` 상수는 제거하고 `should_skip` 은 `is_us_holiday`.
- `repo.close_stale_running(conn, *, older_than_iso: str) -> int` — `agent_calls.status='running' AND created_at < older_than_iso` 를 `status='error', error='stale: 데몬 재기동으로 정리'` 로. `daemon.run` 이 잠금 직후 `now − 1h` 로 호출하고 로그.
- `_reflect_body`: 시장 루프의 `run_reflect` 를 `try/except Exception` 으로 감싸 통보 후 다음 시장(`errors` 목록을 반환 dict 에 추가).

- [ ] **Step 1: Write the failing tests**

```python
def test_us_holidays_rules_match_static_and_2028():
    assert d.us_holidays(2026) == {date(2026,1,1), date(2026,1,19), date(2026,2,16), date(2026,4,3),
        date(2026,5,25), date(2026,6,19), date(2026,7,3), date(2026,9,7), date(2026,11,26), date(2026,12,25)}
    assert d.us_holidays(2027) == {date(2027,1,1), date(2027,1,18), date(2027,2,15), date(2027,3,26),
        date(2027,5,31), date(2027,6,18), date(2027,7,5), date(2027,9,6), date(2027,11,25), date(2027,12,24)}
    h28 = d.us_holidays(2028)
    assert date(2028,4,14) in h28 and date(2028,11,23) in h28 and date(2027,12,31) not in h28
    assert date(2028,1,1) not in h28  # 토요일 신정: 대체 없음
    assert d.is_us_holiday(date(2028, 7, 4)) and not d.is_us_holiday(date(2028, 7, 5))

def test_run_closes_stale_running_calls(conn, ...):
    conn.execute("INSERT INTO agent_calls(agent, status, created_at) VALUES ('orchestrator','running',?)",
                 ("2026-10-09T20:00:00+00:00",))
    conn.execute("INSERT INTO agent_calls(agent, status, created_at) VALUES ('orchestrator','running',?)",
                 ("2026-10-10T00:50:00+00:00",))   # 10분 전: 유지
    conn.commit()
    d.run(deps, scheduler_factory=FakeScheduler, web=False, lock_path=tmp_path / "l.lock")
    rows = conn.execute("SELECT status FROM agent_calls ORDER BY id").fetchall()
    assert [r["status"] for r in rows] == ["error", "running"]

def test_reflect_body_continues_after_market_error(conn, monkeypatch):
    # KR run_reflect 가 PipelineError → US 도 호출되고 errors 에 KR 1건
    ...
    assert calls == ["KR", "US"] and r["errors"] == ["KR: ..."]
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.** 부활절: `a=y%19; b=y//100; c=y%100; d=b//4; e=b%4; f=(b+8)//25; g=(b-f+1)//3; h=(19a+b-d-g+15)%30; i=c//4; k=c%4; l=(32+2e+2i-h-k)%7; m=(a+11h+22l)//451; month=(h+l-7m+114)//31; day=((h+l-7m+114)%31)+1`.
- [ ] **Step 4: Run** `uv run pytest tests/test_daemon.py tests/ledger -q` → PASS.
- [ ] **Step 5: Commit** — `fix(daemon): NYSE 휴장일 규칙 계산·stale running 정리·reflect 시장별 격리`

---

### Task 3: 피처 스토어 최신 행 공용화 + 도구·메모리 정합성

**Files:**
- Modify: `src/trader/ml/features.py`(`latest_rows`), `src/trader/scan/quant.py:79`(`latest_features`), `src/trader/ml/quant_report.py:25`(`latest_row`), `src/trader/orchestrator/tools.py:795-815`(`_record_memory`), `src/trader/orchestrator/tools.py:863,905`(mkdir → run_store), `src/trader/orchestrator/run_store.py`(`market_dir`), `src/trader/memory/outcomes.py:60`(`mature(today)`), `src/trader/memory/vector.py:14`(`sma_5_rel` 부호), `src/trader/backtest.py:75`(HOLD 적중)
- Test: `tests/ml/test_features.py`, `tests/scan/test_quant.py`, `tests/orchestrator/test_tools.py`, `tests/memory/test_outcomes.py`, `tests/memory/test_vector.py`, `tests/test_backtest.py`

**Interfaces:**
- Produces: `features.latest_rows(state, market: str | None, *, tickers: list[str] | None = None, asof: date | None = None, max_files: int = 60) -> pd.DataFrame` — 파일명(`<date>.parquet`) 내림차순으로 읽되 `asof` 이후 파일은 건너뛰고, 종목별 첫 등장 행을 모아 `tickers` 가 전부 모이면 멈춘다(`tickers=None` 이면 `max_files` 까지). `quant.latest_features(market, state=)` 와 `quant_report.latest_row(ticker, market, state, *, asof=None)` 는 이를 쓴다.
- `_record_memory`: `row = quant_report.latest_row(..., asof=date)`; `decided_at` 은 `ctx.now()` 의 KST 날짜가 `date` 와 같으면 `ctx.now()`, 아니면 `datetime.combine(date, time(15, 30), KST)` 의 UTC. `ensure_quant` 도 `asof=date`.
- `run_store.market_dir(date, market, state=None, *, create=False) -> Path`; tools 의 두 `mkdir` 를 `create=True` 호출로.
- `outcomes.mature(..., today)`: 결정일 + horizon 거래일의 종가 날짜가 `today` 이하일 때만 채운다(`window_return` 이 쓰는 마지막 종가 날짜 ≤ today).
- `vector.situation_vector`: `sma_5_rel`·`sma_20_rel` 을 `close/sma − 1` 로(지표 열과 같은 부호), DIMS 주석 수정.
- `backtest`: `HOLD_BAND = 0.02`, HOLD 의 `hit = abs(price_return) <= HOLD_BAND`(`price_return` 은 결과 dict 에 추가).

- [ ] **Step 1: Write the failing tests**

```python
def test_latest_rows_falls_back_to_older_files(project):
    features.write_rows(_rows("2026-10-01", ["A", "B"])); features.write_rows(_rows("2026-10-02", ["A"]))
    df = features.latest_rows(None, "KR", tickers=["A", "B"])
    assert dict(zip(df["ticker"], df["date"].astype(str))) == {"A": "2026-10-02", "B": "2026-10-01"}
    assert features.latest_rows(None, "KR", asof=date(2026, 10, 1))["date"].astype(str).eq("2026-10-01").all()

def test_latest_row_respects_asof(project): ...  # quant_report.latest_row(..., asof=date(2026,10,1)) → 10-01 행

def test_record_memory_decided_at_follows_run_date(...):  # now=2026-10-17T06:00Z, date=2026-10-09 → decided_at 2026-10-09T06:30:00+00:00

def test_mature_waits_for_today(conn, project):  # today 가 결정일+2 → 미만기, 결정일+7 → 만기

def test_sma_rel_sign_matches_indicator():  # close 110, sma_5 100 → +0.1

def test_backtest_hold_hit_is_small_move(conn, project):  # HOLD, price_return 0.01 → hit True, 0.05 → False
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.** `latest_rows` 는 `pd.read_parquet` 를 파일마다 호출하되 필요한 종목이 다 모이면 즉시 중단.
- [ ] **Step 4: Run** 해당 테스트 파일 + `tests/orchestrator` → PASS.
- [ ] **Step 5: Commit** — `fix(ml,memory): 피처 최신 행 부분 읽기·asof 캡·decided_at 운영일·mature today·sma 부호·HOLD 적중`

---

### Task 4: scan — NaN 피처 행 제외·관심종목 시장 구분

**Files:**
- Modify: `src/trader/scan/quant.py:32`(`score`), `src/trader/control/control.py`(`get_watchlist(conn, market=None)`), `src/trader/scan/quant.py`(`candidates`)·`src/trader/daemon.py`·`src/trader/cli.py` scan 호출부(`get_watchlist(conn, market=market)`)
- Test: `tests/scan/test_quant.py`, `tests/control/test_control.py`(있는 파일)

**Interfaces:**
- `score`: `roc_10`·`high_52w_pos`·`turnover` 중 NaN 인 행은 z 계산 전에 제외하고 `reasons` 대신 반환 프레임 속성 `dropped_nan: int`(혹은 로그)로 남긴다. 점수 상위 결과에 NaN 행이 들어가지 않는다.
- `control.get_watchlist(conn, market: str | None = None) -> list[str]` — 항목은 `TICKER` 또는 `KR:TICKER`/`US:TICKER`. market 지정 시 접두사가 있으면 접두사로, 없으면 6자리 숫자 = KR, 그 외 = US. 반환은 접두사 없는 티커. `set_watchlist` 는 `KR:005930` 형식을 허용(`_TICKER_RE` 에 선택적 접두사).

- [ ] **Step 1: Write the failing tests**

```python
def test_score_drops_rows_with_nan_signal():
    df = _frame(...)  # 5행, 1행의 roc_10 NaN
    out = quant.score(df)
    assert "NANROW" not in set(out["ticker"]) and len(out) == 4

def test_watchlist_market_filter(conn):
    control.set_watchlist(conn, ["005930", "AAPL", "US:BRK.B", "KR:000660"])
    assert control.get_watchlist(conn, market="KR") == ["005930", "000660"]
    assert control.get_watchlist(conn, market="US") == ["AAPL", "BRK.B"]
    assert control.get_watchlist(conn) == ["005930", "AAPL", "US:BRK.B", "KR:000660"]
```

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** `tests/scan tests/control tests/test_daemon.py tests/test_cli_scan.py` → PASS.
- [ ] **Step 5: Commit** — `fix(scan): NaN 신호 행 제외·관심종목 시장 구분`

---

### Task 5: Phase 9 Minor 회수

**Files:**
- Modify: `src/trader/cli.py`(`rl train --seeds` `min=1, max=10`), `src/trader/orchestrator/executor.py`(`_rl_size` 기록을 게이트 뒤로), `src/trader/ml/rl/dataset.py`(메모리 스텝 BUY/ADD 만), `src/trader/web/routers/rl.py`(`market` 검증), `src/trader/ml/models/__init__.py`(docstring), `config/ml.yaml`(주석)
- Test: `tests/orchestrator/test_executor.py`, `tests/ml/test_rl_dataset.py`, `tests/web/test_rl_api.py`, `tests/test_cli_rl.py`

**Interfaces:**
- executor: `_rl_size` 는 (sized decision, note, base, proposal dict) 를 돌려주고, `rules.evaluate` 뒤에 `shadow.record(applied = mode=="apply" and verdict.approved)` 를 부른다.
- dataset `_memory_steps`: `decision.action in ("BUY","ADD")` 행만.
- rl 라우터: `market` 이 있으면 `require_market(market)`.

- [ ] **Step 1: Write the failing tests**

```python
def test_apply_mode_rejected_by_gate_not_marked_applied(ectx):
    ectx.rl_sizer = _sizer("apply", weight=0.05)
    # 당일 중복 매수 거부를 만든 뒤 실행 → rl_shadow 1행, applied == 0
    ...
def test_memory_steps_only_buy_add(project, conn):
    seed_matured(conn, 6, action="HOLD"); seed_matured(conn, 6, action="BUY", start=date(2026, 3, 1))
    df = rl_dataset.build_steps(conn, None, market="KR", horizon_days=5, min_memory_steps=1)
    assert len(df) == 6
def test_rl_report_rejects_bad_market(client): assert client.get("/api/rl/report?account_id=paper-main&market=XX").status_code == 400
def test_rl_train_seeds_range(project): assert runner.invoke(app, ["rl", "train", "--seeds", "0"]).exit_code == 2
```

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** — `fix(rl): applied 는 게이트 승인 뒤·메모리 스텝 BUY/ADD·market 검증·seeds 범위`

---

### Task 6: 웹 — 관심종목 제어·SSE 트리거 카운터·favicon·차트 SMA 오버레이

**Files:**
- Modify: `src/trader/web/routers/control.py`(`GET /control/watchlist`, `POST /control/watchlist`), `src/trader/web/sse.py:27`(`snapshot` 에 `triggers`·`watchlist`), `web/index.html`(favicon data URL), `web/src/pages/Settings.tsx`(관심종목 절), `web/src/pages/Chart.tsx`(SMA 20/60 라인)
- Test: `tests/web/test_control_sse.py`

**Interfaces:**
- `GET /api/control/watchlist` → `{tickers: [...]}` 토큰 없음. `POST /api/control/watchlist` body `{action: "add"|"remove", ticker}` → 토큰 필수(401), `ValueError` → 400, 반환 `{tickers}`.
- `sse.snapshot` 에 `"triggers": _max("triggers")`, `"watchlist": control.get_flag(conn, "watchlist") or ""` 추가(값이 바뀌면 `change`).
- Chart: 일봉 `close` 로 SMA20·SMA60 을 클라이언트에서 계산해 `addLineSeries` 2개(체크박스로 on/off, 기본 on).

- [ ] **Step 1: Write the failing tests**

```python
def test_watchlist_api_token_and_validation(client, deps):
    assert client.get("/api/control/watchlist").json() == {"tickers": []}
    assert client.post("/api/control/watchlist", json={"action": "add", "ticker": "005930"}).status_code == 401
    h = {"Authorization": f"Bearer {deps.token}"}
    assert client.post("/api/control/watchlist", json={"action": "add", "ticker": "005930"}, headers=h).json() == {"tickers": ["005930"]}
    assert client.post("/api/control/watchlist", json={"action": "add", "ticker": "bad ticker!"}, headers=h).status_code == 400
    assert client.post("/api/control/watchlist", json={"action": "remove", "ticker": "005930"}, headers=h).json() == {"tickers": []}

def test_sse_snapshot_includes_triggers_and_watchlist(conn):
    s0 = sse.snapshot(conn); control.set_watchlist(conn, ["AAPL"]); s1 = sse.snapshot(conn)
    assert "triggers" in s0 and s1 != s0
```

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement**(+ `npm run typecheck && npm test && npm run build`). **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** — `feat(web): 관심종목 제어 API·설정 페이지·SSE 트리거/관심종목·favicon·차트 SMA`

---

### Task 7: 검증·문서·PR

- [ ] **Step 1:** `uv run pytest -q` 전체·`uv run ruff check src tests`·웹 빌드. `uv run trader status`. 실기(FAKE): `FAKE_CLAUDE=1 uv run trader analyze --ticker 005930 --dry-run`, `uv run trader scan --market KR --dry-run`, `uv run trader readiness`, 웹 `/api/control/watchlist` 200.
- [ ] **Step 2:** `docs/TODO.md` — 이연 줄들을 "회수됨(2026-10-17, 이 플랜)" 으로 체크하고 범위 조정 표의 제외 항목만 남긴 새 줄 1개("이연 유지: …이유…")로 정리. `CLAUDE.md` — US 휴장일 문장(정적 달력 → 규칙 계산), 관심종목 시장 접두사, `nav_history` 날짜 창, `latest_rows` 부분 읽기, `decided_at` 운영일 규칙, 웹 제어 API 목록에 watchlist.
- [ ] **Step 3:** 커밋 → 최종 리뷰(Fable) → Critical/Important 수정 → PR `feat/deferred-cleanup` → `feat/phase9-rl-sizing`.

---

## Self-Review

1. **Spec coverage:** §5.1 NaN 행(Task 4), §6.3 집계(Task 1), §6.5 메모리 정합(Task 3), §8 관심종목 제어(Task 6), §10.1 휴장일(Task 2), §13 준비도 분모(Task 1). TODO 의 나머지 항목은 범위 조정 표에 이유와 함께 있다.
2. **Step scan:** 각 테스트는 단언이 있고 구현은 Interfaces 가 결정한다. Task 1 readiness 테스트의 "..." 은 기존 `_runs` 헬퍼로 cron 잡 10건·monitor 5건을 넣는 것으로 실행 시 채운다.
3. **Type consistency:** `latest_rows(state, market, *, tickers, asof, max_files)` 를 Task 3 의 두 호출자가 같은 이름으로 쓴다. `get_watchlist(conn, market=None)` 는 Task 4 정의를 Task 6 라우터가 그대로 쓴다.
4. **Review Focus:** 5개 모두 소유 Task 에 테스트가 있다.
5. **Proportion:** 7 태스크, 코드 블록은 테스트 단언과 규칙뿐.
