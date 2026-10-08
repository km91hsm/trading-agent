# Phase 4 리스크·계좌별 실행 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `submit-decision` 이 받은 `Decision` 하나를 계좌마다 파이썬 리스크 게이트에 통과시켜(`40_risk_check_<계좌>`), 계좌 모드에 따라 모의 계좌는 즉시 주문하고(`50_order_<계좌>`), 실전 `off` 계좌는 기록만 하며, `approve` 계좌는 텔레그램 승인 뒤 주문한다. 모든 결정·판정·주문·체결이 원장 테이블에 남는다.

**Architecture:** `risk/rules.py`(순수 함수) ← `risk/stance.py`(최근 5거래일 실현손익으로 성향) ← `risk/costs.py`(왕복 비용). `orchestrator/executor.py` 가 계좌별로 브로커 잔고를 읽어 `Portfolio` 를 만들고 `rules.evaluate` → 모드 분기 → `KisBroker.place_order`(Phase 1 의 `NotImplementedError` 를 구현) → `ledger/repo.py` 기록. `ledger/db.py` 는 v2 마이그레이션으로 테이블 8개를 추가한다. 승인은 `approvals` 테이블 + 텔레그램 `getUpdates` 폴링 명령(`trader telegram poll`, 데몬 루프는 Phase 6).

**Tech Stack:** 기존 + 없음(텔레그램 수신도 httpx 로 `getUpdates`).

**Spec:** `docs/superpowers/specs/2026-10-09-trading-agent-design.md` — §0.2 #5·#8, §2 브로커(주문), §6.1~6.4, §4.5 체크포인트, §11 Phase 4, §13.

## Global Constraints

- Phase 0~3 규칙 전부. 금액 `Decimal`, 시각 UTC 저장.
- **주문은 `executor` 안에서만** 일어난다. CLI 에 수동 주문 명령을 두지 않는다(`.claude/settings.json` 거부 규칙 유지). 실전 계좌 `auto` 는 `control.set_account_mode(confirm="LIVE")` 로만.
- 리스크 규칙은 순수 함수(I/O 없음)이고 입력은 `Decision + Portfolio + Stance + RiskLimits + 비용`. 판정 이유는 전부 `reasons[]` 에 한국어 문장으로 남긴다.
- 모의 서버 제약: US 주문은 지정가만(`order_type=LIMIT` 강제, 시장가 요청은 거부 사유 기록). 환전 API 없음 → 당일 누적 주문금액을 원장에서 차감해 가능금액 계산.
- `ledger/db.MIGRATIONS` 에 v2 를 **추가만** 한다(v1 수정 금지). 테이블: `analyst_reports, debate_turns, decisions, risk_checks, orders, fills, positions_snapshot, approvals`. 모든 주문 행은 `decision_id + account_id`.
- 체크포인트 `40_risk_check_<account>` 는 `RiskVerdict` dump, `50_order_<account>` 는 `{request, result, mode}`.
- 실기 주문은 **모의 계좌 1주(또는 최소 금액)** 로만, 실전 계좌는 `off` 로 기록만 확인한다. 실전 주문은 어떤 경우에도 이 Phase 에서 내지 않는다.
- 텔레그램 토큰은 로그·예외에 남기지 않는다(기존 패턴).

## 범위 조정

| 항목 | 이유 | 재개 Phase |
|---|---|---|
| 손절 자동 매도·트리거 재평가 | monitor 루프와 함께 | 6 |
| 웹 승인 UI | 같은 `approvals` 레코드를 쓰는 라우터는 Phase 5 | 5 |
| 텔레그램 수신 상시 루프·`/pause` 등 제어 명령 전체 | Phase 4 는 `/approve`·`/reject`·`/status` 만 + 1회 폴링 명령. 상시 루프는 데몬 | 6 |
| RL 사이저 수량 제안 | Phase 9 | 9 |
| 주문 체결 조회(`fills` 자동 적재) | Phase 4 는 주문 응답(접수번호)만 기록, 체결 동기화는 monitor 와 함께 | 6 |

## Review Focus

1. 실전 계좌가 yaml 에서 `off` 인데 control 테이블에 누군가 `auto` 를 써 두면(확인 문자열 없이) executor 가 주문하면 안 된다 → Task 4 `test_real_auto_requires_control_confirmation_record`.
2. 같은 종목에 당일 이미 매수 주문이 있으면 두 번째 `BUY` 는 거부돼야 한다(재실행·트리거 중복) → Task 2 `test_duplicate_buy_same_day_rejected`.
3. 일일 손실이 -5% 를 넘긴 계좌에서 `SELL` 은 허용되고 `BUY` 만 막혀야 한다 → Task 2 `test_daily_loss_stop_blocks_buy_not_sell`.
4. 브로커 주문 호출이 예외를 던져도 `50_order_<계좌>` 에 실패가 기록되고 다른 계좌 처리는 계속돼야 한다 → Task 4 `test_broker_failure_recorded_and_continues`.
5. 승인 요청이 30분을 넘기면 승인해도 주문하지 않아야 한다 → Task 5 `test_expired_approval_not_executed`.

---

## 파일 구조

```
src/trader/ledger/db.py            MIGRATIONS v2
src/trader/ledger/repo.py          insert_decision/report/turn, insert_risk_check, insert_order, approvals CRUD, today_orders, realized_pnl_5d, snapshot_positions
src/trader/risk/__init__.py  costs.py  stance.py  rules.py
src/trader/broker/accounts.py      place_order/cancel_order 구현
src/trader/broker/fake.py          FakeBroker 주문 기록
src/trader/orchestrator/executor.py
src/trader/orchestrator/tools.py   submit_decision → executor 연결 (dry_run 이면 판정까지만)
src/trader/notify/telegram.py      TelegramReceiver.poll(), 명령 파서
src/trader/cli.py                  approvals list|approve|reject, telegram poll, analyze(--dry-run 해제 시 실행)
src/trader/status.py               결정 분포·미승인 주문 절
tests/ledger/test_repo.py  tests/risk/test_{costs,stance,rules}.py  tests/broker/test_orders.py
tests/orchestrator/test_executor.py  tests/notify/test_receiver.py  tests/test_cli_approvals.py
```

---

### Task 1: 원장 v2 마이그레이션과 `repo.py`

**Interfaces:** `MIGRATIONS` 에 `(2, [...])`: `analyst_reports(id, date, market, ticker, role, signal, score, confidence, json, created_at)`, `debate_turns(id, date, market, ticker, side, round, stance, conviction, json, created_at)`, `decisions(id, date, market, ticker, action, target_weight, confidence, session_id, json, created_at)`, `risk_checks(id, decision_id, account_id, approved, adjusted_qty, reasons, created_at)`, `orders(id, decision_id, account_id, ticker, market, side, qty, order_type, price, status, order_no, message, mode, created_at)`, `fills(id, order_id, account_id, qty, price, filled_at)`, `positions_snapshot(id, account_id, date, ticker, qty, avg_price, current_price, eval_amount, nav, cash, created_at)`, `approvals(id, decision_id, account_id, order_json, status, requested_at, expires_at, decided_at)`. `repo.py` 함수는 전부 `(conn, …) -> id|rows`. `ingest_checkpoint(conn, date, market, ticker, stage, data)` 가 `10_*`·`20_*`·`30_*` 를 각 테이블에 멱등 저장(`tools.checkpoint_write`/`submit_decision` 이 호출).

- [ ] Step 1: `test_v2_migration_adds_tables`, `test_ingest_checkpoint_idempotent`, `test_today_orders_and_realized_pnl`. Step 2~4. Step 5: Commit `feat(ledger): 원장 v2 테이블과 repo`

### Task 2: `risk/costs.py`·`stance.py`·`rules.py` (TDD)

**Interfaces:**
- `costs.round_trip_bp(market) -> int`(KR 30, US 50), `costs.expected_return(decision, price) -> float|None`(`take_profit` 또는 `limit_price` 대비).
- `stance.Stance = Literal["normal","conservative"]`, `stance.decide(realized_pnl_5d: Decimal) -> Stance`, `stance.limits_for(stance, limits: RiskLimits) -> RiskLimits`(conservative: 종목당 0.15, 신규 2종목).
- `rules.Portfolio{account_id, market, nav: Decimal, cash_available: Decimal, positions: dict[ticker, Position], today_pnl_pct: float, today_buy_tickers: set[str], today_order_amount: Decimal, held_since: dict[ticker, date], trades_this_week: dict[ticker, int]}`, `rules.RiskVerdict{account_id, approved: bool, adjusted_qty: int, reasons: list[str], side, order_type, price}`.
- `rules.evaluate(decision, portfolio, *, limits, stance, market, price: Decimal, today: date, is_stop_loss: bool=False) -> RiskVerdict`. 순서: (1) HOLD → 승인 아님·수량 0·"HOLD". (2) 수량 = `target_weight × nav / price` 내림, 종목당 상한·신규 종목 수·가능금액(당일 누적 차감)·일일 손실(-5% 면 BUY/ADD 거부)·당일 중복 매수·최소 보유 3거래일(SELL/TRIM, 손절 제외)·주 2회·기대수익 < 비용×2 거부·confidence<0.5 반감·US LIMIT 강제. 거부 이유는 전부 모아 돌려준다(첫 사유에서 멈추지 않음).

- [ ] Step 1: `test_hold_is_noop`, `test_qty_from_weight_and_position_cap`, `test_max_positions_blocks_new`, `test_daily_loss_stop_blocks_buy_not_sell`, `test_duplicate_buy_same_day_rejected`, `test_min_hold_days_except_stop_loss`, `test_weekly_trade_cap`, `test_expected_return_below_cost_rejected`, `test_low_confidence_halves`, `test_us_market_order_forced_limit`, `test_conservative_stance_limits`. Step 2~4. Step 5: Commit `feat(risk): 리스크 규칙·성향·비용 (순수 함수)`

### Task 3: 브로커 주문 (`KisBroker.place_order/cancel_order`, `FakeBroker`)

**Interfaces:** `place_order(OrderRequest) -> OrderResult`: KR `order-cash`(`PDNO, ORD_DVSN 00 지정가|01 시장가, ORD_QTY, ORD_UNPR`), US `order`(`OVRS_EXCG_CD NASD|NYSE|AMEX, PDNO, ORD_QTY, OVRS_ORD_UNPR, ORD_DVSN 00`), 모의는 `V` tr_id. 응답 `output.ODNO` → `order_no`, `rt_cd≠0` 은 `KisApiError`. `cancel_order(order_no)` 는 KR `order-rvsecncl`(tr_id 확인 필요). `FakeBroker.place_order` 는 `orders: list[OrderRequest]` 에 기록하고 `OrderResult(accepted=True, order_no="FAKE-n")`.

- [ ] Step 1: `test_kr_limit_order_params_and_tr_id`(모의 VTTC0012U), `test_us_order_forces_limit_and_exchange_code`(NAS→NASD), `test_order_rejected_is_error`, `test_fake_broker_records`. Step 2~4. Step 5: Commit `feat(broker): 주문·취소`

### Task 4: `executor.py` 와 `submit-decision` 연결

**Interfaces:** `executor.build_portfolio(conn, broker, account, market, today) -> Portfolio`(브로커 잔고·현금 + 원장의 당일 주문·보유 시작일·주간 거래수·5일 실현손익). `executor.execute(ctx, decision, *, date, market, price, dry_run) -> list[dict]`: 계좌마다 `verdict = rules.evaluate(...)` → `40_risk_check_<id>` 저장 + `risk_checks` 기록 → 모드(`control.get_account_mode`; 실전은 `auto` 라도 control 값이 아니라면 `off` 로 취급): `off` → 기록만, `approve` → `approvals` 행(30분) + 텔레그램 승인 요청, `auto` → `broker.place_order` → `50_order_<id>` + `orders` 행 + 알림. `dry_run=True` 면 판정·기록까지만 하고 주문·승인 요청을 내지 않는다. 브로커 예외는 `50_order` 에 `accepted False` 로 기록하고 다음 계좌로. `tools.submit_decision` 은 `40_risk_check` 자리 대신 `executor.execute` 를 부른다(세션 spec 의 `dry_run` 을 체크포인트 `00_inputs.meta` 또는 도구 옵션 `--dry-run` 으로 전달).

- [ ] Step 1: `test_paper_auto_places_order_and_writes_checkpoints`, `test_real_off_records_only`, `test_real_auto_requires_control_confirmation_record`, `test_approve_mode_creates_approval_and_notifies`, `test_dry_run_no_orders`, `test_broker_failure_recorded_and_continues`, `test_submit_decision_runs_executor`. Step 2~4. Step 5: Commit `feat(orchestrator): 계좌별 executor 와 submit-decision 연결`

### Task 5: 승인 흐름 (텔레그램 수신·CLI)

**Interfaces:** `telegram.TelegramReceiver(token, chat_id, http)`: `poll(offset) -> list[Update{update_id, text, chat_id}]`(`getUpdates`, chat_id 불일치 무시), `parse_command(text) -> ("approve"|"reject"|"status", arg)`. `executor.approve(ctx, approval_id) -> dict`(만료 확인 → 주문 → `orders`·`50_order`), `executor.reject(ctx, approval_id)`. CLI `trader approvals list`, `trader approvals approve <id>`, `trader approvals reject <id>`, `trader telegram poll`(오프셋은 `control` 플래그 `telegram_offset` 에 저장, 1회 처리). 승인 요청 메시지에 `/approve <id>` 안내.

- [ ] Step 1: `test_poll_filters_chat_and_parses`, `test_approve_executes_order_once`, `test_expired_approval_not_executed`, `test_cli_approvals_flow`. Step 2~4. Step 5: Commit `feat(notify,cli): 텔레그램 승인 흐름`

### Task 6: 결정 분포 메트릭과 STATUS

**Interfaces:** `ledger/metrics.py`: `decision_distribution(conn, days=30) -> dict[action, int]`, `convergence_rate(conn, days) -> float|None`, `pending_approvals(conn)`. `status.build_status` 에 "결정 분포(30일)", "미승인 주문", 계좌별 성향(`stance`) 추가.

- [ ] Step 1: `test_decision_distribution`, `test_status_shows_pending_and_distribution`. Step 2~4. Step 5: Commit `feat(ledger,status): 결정 분포·미승인 주문`

### Task 7: 실기·문서·PR

- [ ] Step 1: ruff·pytest 전부.
- [ ] Step 2: FAKE 로 `uv run trader analyze --ticker 005930`(dry-run 해제) → `40_risk_check`·`50_order_paper-main`(FAKE 결정이 HOLD 라 주문 없음) 확인. 모의 주문 실기는 `FakeBroker` 가 아닌 실제 모의 계좌로 **1주 지정가 매수 → 즉시 취소** 를 `tests` 가 아닌 수동 스크립트(`uv run trader kis order-test --account paper-main`, 모의 전용·실전 거부)로 1회 확인하고 결과를 TODO 에 기록. 실전 계좌는 `off` 기록만 확인.
- [ ] Step 3: 텔레그램 승인 실기: `approve` 모드로 모의 계좌를 임시 전환(control) → 승인 요청 수신 → `/approve` → 주문 → 원복.
- [ ] Step 4: TODO Phase 4 체크, CLAUDE.md(리스크·executor·원장 v2·승인 규칙), `uv run trader status`, 커밋, PR → `feat/phase3-agents`.

## Self-Review 메모
- §6.1 규칙 전부 Task 2, §6.2 모드 분기 Task 4, §6.3 테이블 Task 1, §6.4 발신·수신(approve/reject/status) Task 5. 손절·트리거·웹 승인·체결 동기화는 범위 조정.
- `kis order-test` 는 Phase 4 실기 전용 명령이며 모의 env 만 허용하고 `.claude/settings.json` 거부 규칙(`kis order *`)에 걸리도록 이름을 `kis order-test` 로 둔다.
