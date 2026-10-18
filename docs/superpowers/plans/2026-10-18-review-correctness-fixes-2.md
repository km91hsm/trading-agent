# 전체 코드 검토 정확성 수정 (2차) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 2026-10-18 검토의 정확성 잔여 12건(`docs/TODO.md` "검토 잔여(정확성)")을 고친다. 1차 플랜(`2026-10-18-review-correctness-fixes.md`, PR #16) 위에 쌓는다.

**Architecture:** 기존 모듈 안의 최소 수정. 스키마 변경 없음. 세 묶음(A 게이트·표시, B 수집·피처·웹, C 원장·인증·데몬·세션)으로 커밋한다.

**Tech Stack:** Python 3.12, pytest, respx, SQLite.

**Spec:** `docs/superpowers/specs/2026-10-09-trading-agent-design.md` §6.1(리스크), §8(웹), §10.1(데몬), §13(준비도).

## Global Constraints

- 금액 `Decimal`, 날짜 창 시장 현지, 저장 시각 UTC ISO. `uv run pytest`·`uv run ruff check src tests` 0건.
- 어떤 경로도 실전 모드를 올리지 않는다. 표시만 바꾼다.

## Review Focus

- `min_cash_pct` 가 0 이면 기존 수량과 동일한가 (A1).
- 실전 `auto` + 확인 기록 없음이 웹·STATUS·CLI 에서 `off` 로 보이는가 (A3).
- 과거 `asof` 수집에서 마지막 일봉이 `asof` 날짜를 넘지 않는가 (B1).
- 전량 매도 뒤 재매수한 종목의 실현손익이 현재 묶음 평균가 기준인가 (C1).
- 두 프로세스가 같은 갱신 창에서 토큰을 발급할 때 실패한 쪽이 새 토큰을 지우지 않는가 (C2).

---

### Task A: 게이트·준비도·모드 표시·웹 호스트

**Files:** `src/trader/risk/rules.py`, `src/trader/readiness.py`, `src/trader/web/routers/accounts.py`, `src/trader/status.py`, `src/trader/cli.py`(control show·web), `src/trader/web/app.py`
**Tests:** `tests/risk/test_rules.py`, `tests/test_readiness.py`, `tests/web/test_accounts_portfolio_orders.py`, `tests/test_status.py`, `tests/test_cli_control.py`, `tests/web/test_app.py`

- [x] A1 `rules.evaluate`: `available = cash_available − today_order_amount − min_cash_pct × NAV`, 줄었으면 사유 `"현금 보유 하한 … 유보"`. 테스트: `RiskLimits(min_cash_pct=0.5)`, NAV 1천만·현금 1천만·목표 0.1(100만) → 가능 500만이라 수량 불변; 목표 0.25(250만)·현금 200만·하한 0.1(100만) → 100만/7만 = 14주.
- [x] A2 `readiness._rate`: interval 잡 제외를 실제 `runs.kind` 로 (`%_monitor`·`telegram_poll`·`fills_sync%`). 재평가 세션 `monitor` 는 분모에 남긴다. 테스트: `kind="kr_monitor"` 오류 5건은 제외, `kind="monitor"` ok 10 은 포함.
- [x] A3 `executor.effective_mode` 를 `/api/accounts`(`mode`+`mode_note`)·STATUS 계좌 표(`off (강등)`)·`control show` 에서 쓴다. 테스트: control 에 `mode:real-main=auto`, `live_confirmed` 없음 → 세 곳 모두 `off`.
- [x] A4 `cli.web`: 비루프백 바인드면 `extra_hosts=["*"]`(`_hosts_for_bind(bind)`), 경고 유지. 테스트: `_hosts_for_bind("0.0.0.0") == ["*"]`, `_hosts_for_bind("127.0.0.1") == []`; `create_app(extra_hosts=["*"])` 가 `Host: 192.168.0.10` 을 200.
- [x] 커밋 `fix(risk, readiness, web): min_cash_pct 적용, interval 잡 종류 정합, 실효 모드 표시, --host 허용`

### Task B: 수집 asof·웹 refresh·피처 스토어·빈 일봉 행

**Files:** `src/trader/sources/inputs.py`, `src/trader/web/routers/portfolio.py`, `src/trader/ml/features.py`, `src/trader/broker/kis_market.py`
**Tests:** `tests/sources/test_inputs.py`, `tests/web/test_accounts_portfolio_orders.py`, `tests/ml/test_features.py`, `tests/broker/test_kis_market.py`

- [x] B1 `_technical(…, asof)`: 일봉·분봉은 `date <= asof` 의 시장 현지 날짜만, `_sentiment` 수급도 같은 필터. `meta.last_candle_date` 기록. 테스트: `asof=T0−1일` 이면 `technical.daily[-1].date <= asof 날짜`, `meta.last_candle_date` 일치.
- [x] B2 `portfolio/refresh`: `build_portfolio(…, memory_settings=deps.settings.memory)`. 테스트: monkeypatch 로 kwargs 확인.
- [x] B3 `features.write_rows`: `old = _coerce(pd.read_parquet(path).reindex(columns=FEATURE_COLUMNS))`. 테스트: 열 5개짜리 옛 파일에 `write_rows` → 성공, 옛 행 유지(결측 NaN).
- [x] B4 `kis_market.get_daily_kr/us`: 날짜가 빈 행은 건너뛴다. 테스트: output2 끝에 `stck_bsop_date=""` 행 → 예외 없이 나머지만.
- [x] 커밋 `fix(sources, features, broker): asof 룩어헤드, refresh 서킷브레이커 설정, 구스키마 parquet, 빈 일봉 행`

### Task C: 실현손익 묶음 평균·토큰 캐시 경쟁·데몬 락·세션 사전 단계

**Files:** `src/trader/ledger/repo.py`, `src/trader/broker/kis_auth.py`, `src/trader/daemon.py`, `src/trader/orchestrator/session.py`
**Tests:** `tests/ledger/test_repo.py`, `tests/broker/test_kis_auth.py`, `tests/test_daemon.py`, `tests/orchestrator/test_session.py`

- [x] C1 `realized_pnl_5d`: 창 안에 SELL 체결이 있는 종목만, 그 종목의 전체 체결을 시간순으로 걸어 **현재 묶음 평균가**(Decimal, 보유 0 이면 리셋)로 SELL 손익을 더한다. 테스트: 10@100 매수→10@150 매도(창 밖), 10@200 매수→10@190 매도(창 안) → −100 (기존 +400).
- [x] C2 `KisAuth._mark_failure`: 발급 전 스냅샷이 아니라 **다시 읽은** 캐시에 `failed_at` 만 더해 쓴다. 테스트: A 가 NEW 를 쓴 뒤 B 가 OLD 스냅샷으로 `_issue` 실패 → 파일 토큰은 NEW.
- [x] C3 `daemon.Lock.acquire`: `os.open(O_CREAT|O_EXCL)` 로 원자적 생성, `FileExistsError` 면 pid 생존 확인 뒤 stale 만 인계(1회 재시도). 시그널 핸들러는 `shutdown(wait=True)` 로 실행 중 잡을 기다린 뒤 종료(잠금 해제가 세션보다 늦도록). 테스트: 기존 락 테스트 + 핸들러 `calls == [True]`.
- [x] C4 `run_session`: `find_claude`~`_start_call` 사이 예외는 `runs` 행을 error 로 닫고 임시 cwd 를 지운 뒤 다시 올린다. 테스트: `claude_bin` 없음(`find_claude` 실패) → 예외, `runs.status == 'error'`, `trader-claude-*` 디렉터리 없음.
- [x] 커밋 `fix(ledger, broker, daemon, session): 묶음 평균 실현손익, 토큰 캐시 경쟁, 락 원자성·종료 순서, 세션 사전 단계 정리`

### Task D: 마무리
- [x] `uv run pytest`, `uv run ruff check src tests`, `docs/TODO.md` 체크, CLAUDE.md 어긋난 문장 갱신, PR (`fix/review-correctness` 대상).
