# 전체 코드 검토 정확성 수정 (1차) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 2026-10-18 전체 코드 검토에서 확인된 주문·자금 직접 영향 결함 8건을 고친다.

**Architecture:** 기존 모듈 안에서 최소 수정. 새 개념은 "운영일(run date)" 하나만 추가한다(`run_store.run_date_for`). 리스크 규칙은 순수 함수 그대로, executor 는 기록 실패 보호만 덧붙인다.

**Tech Stack:** Python 3.12, pytest, SQLite.

**Spec:** `docs/superpowers/specs/2026-10-09-trading-agent-design.md` §5.2(monitor), §6.1(리스크), §6.3(승인). 검토 결과는 이 세션 보고(2026-10-18).

## Global Constraints

- 금액은 `Decimal`, 날짜 창은 시장 현지(`MARKET_TZ`), 저장 시각 UTC ISO.
- 마이그레이션은 추가만(이 플랜은 스키마 변경 없음).
- 테스트는 `tmp_path`·`respx` 만. `uv run pytest`, `uv run ruff check src tests` 0건.
- 사용자 대면 문구·docstring 한국어.

## Review Focus

- US 모니터가 KST 자정 이후 재평가·손절을 낼 때 운영일이 전날 KST 날짜로 유지되는가 (Task 1).
- ADD 가 이미 목표 비중 이상 보유일 때 0주로 거부되는가 (Task 2).
- 지정가가 현재가보다 높을 때 주문금액이 가능금액을 넘지 않는가 (Task 2).
- `submit-decision` 재시도가 미실행 결정을 실행까지 마치는가 (Task 3).
- 다른 채팅 업데이트만 100건 와도 오프셋이 전진하는가 (Task 4).

---

### Task 1: 운영일(run date) 도입과 시장 현지 날짜 창

**Files:**
- Modify: `src/trader/orchestrator/run_store.py` (함수 추가)
- Modify: `src/trader/ledger/repo.py:379-423` (`today_buy_tickers`, `today_order_amount`, `trades_this_week` 에 `market`)
- Modify: `src/trader/orchestrator/executor.py:146-150` (market 전달)
- Modify: `src/trader/daemon.py:399,478`, `src/trader/cli.py:1272`
- Test: `tests/orchestrator/test_run_store.py`, `tests/ledger/test_repo.py`, `tests/test_daemon.py`

**Interfaces:**
- Produces: `run_store.run_date_for(market: str, now: datetime) -> date`. KR 은 KST 날짜. US 는 뉴욕 현지 날짜의 09:30 ET 를 KST 로 바꾼 날짜(= 뉴욕 현지 날짜). 데몬·CLI monitor 와 US analyze 가 쓴다.
- Produces: `repo.today_buy_tickers(conn, account_id, d, market=None)`, `repo.trades_this_week(conn, account_id, d, market=None)`; `today_order_amount` 는 이미 `market` 을 받으므로 창만 현지로. `market` 이 `MARKET_TZ` 에 있으면 `_day_bounds(d, MARKET_TZ[market])`, 없으면 KST.

- [x] 테스트: `run_date_for("US", 2026-10-20 00:30 KST)` == 2026-10-19, `run_date_for("US", 2026-10-19 22:00 KST)` == 2026-10-19, `run_date_for("KR", 2026-10-20 00:30 KST)` == 2026-10-20.
- [x] 테스트: `today_buy_tickers(..., market="US")` 가 D 의 ET 창(= D 05:00 UTC ~ D+1 05:00 UTC) 안 주문만 센다. 2026-10-19 15:00 UTC(= 11:00 ET) 주문이 d=2026-10-19 US 창에 들어가고, KST 창(d=2026-10-20)으로는 안 잡히는 것을 확인.
- [x] 구현 후 `build_portfolio` 가 세 함수에 `market=market` 을 넘긴다. 데몬 `_analyze_body`·`_monitor_body` 와 CLI `monitor_cmd` 는 `run_store.run_date_for(market, now)` 를 `today` 로 쓴다.
- [x] 데몬 테스트: `_monitor_body` 가 US 00:30 KST 에 `monitor_once(today=전날)` 을 받는지(`monitor_once` monkeypatch 로 인자 캡처).
- [x] 커밋: `fix(monitor): US 운영일을 시장 현지 기준으로 통일 (KST 자정 이후 재평가 실패)`

### Task 2: 리스크 규칙 — 목표 비중은 주문 뒤 총 비중, 지정가 수량, 손절선 검증

**Files:**
- Modify: `src/trader/risk/rules.py:115-135`
- Modify: `.claude/agents/trader.md` 규칙 4 문장
- Test: `tests/risk/test_rules.py`

**Interfaces:**
- `target_weight` 의 의미를 BUY/ADD/TRIM 모두 "주문 뒤 총 비중" 으로 고정한다. ADD 매수금액 = `target_weight × NAV − 현재 평가금액`, 0 이하면 거부 사유 `"이미 목표 비중 이상 보유"`.
- 수량 산출 가격 `sizing_price` = 지정가면 `order_price`, 아니면 `price`.
- BUY/ADD 에 `stop_loss` 가 있고 `stop_loss >= price` 면 거부 사유 `"손절선이 현재가 이상"` (즉시 손절 매도 방지).

- [x] 테스트 수정: `test_qty_from_weight_and_position_cap` 의 ADD 절반 → ADD 0.25 에 20% 보유면 5%(7주), ADD 0.1 에 20% 보유면 0주 거부.
- [x] 테스트: `limit_price=73500`, `target_weight=0.1`, NAV 1천만 → qty = floor(1,000,000 / 73,500) = 13 (현재가 70000 이면 14 가 됐을 것).
- [x] 테스트: `stop_loss=72000`, price 70000 BUY → approved False, 사유에 "손절선".
- [x] trader.md 규칙 4 에 "ADD/TRIM 도 주문 뒤 총 비중이다" 를 덧붙인다(공통 6문장 불변).
- [x] 커밋: `fix(risk): 목표 비중을 총 비중으로 통일, 지정가 수량·손절선 검증`

### Task 3: `submit-decision` 멱등 분기가 미실행 결정을 실행까지 마친다

**Files:**
- Modify: `src/trader/orchestrator/tools.py:696-770`
- Test: `tests/orchestrator/test_tools.py`

**Interfaces:**
- `30_decision` 이 있고 `40_risk_check_*` 가 하나도 없으면 기존 결정으로 `executor.execute` + `_record_memory` 를 돌리고 `{"ok": True, "already": True, "resumed_execution": True, "executions": [...]}` 를 돌려준다. `ctx.conn is None` 검사는 `write_checkpoint` 앞으로 옮긴다.

- [x] 테스트: 보고서·요약·00_inputs 를 쓰고 `30_decision` 만 직접 저장(40_ 없음) → `submit_decision` 호출 → `already` True, `resumed_execution` True, `40_risk_check_paper-main` 파일과 `risk_checks` 행 존재. 두 번째 호출은 `resumed_execution` 없음.
- [x] 테스트: `ctx.conn=None` 이면 `30_decision` 파일이 생기지 않는다.
- [x] 커밋: `fix(tools): 30_decision 만 있고 판정이 없으면 재제출 때 실행을 재개`

### Task 4: 텔레그램 오프셋이 걸러진 업데이트도 인정한다

**Files:**
- Modify: `src/trader/notify/telegram.py:118-150`, `src/trader/notify/commands.py:66-104`
- Test: `tests/notify/test_telegram_receiver.py`, `tests/notify/test_commands.py`

**Interfaces:**
- `TelegramReceiver.last_update_id: int | None` — 마지막 `poll` 의 원본 결과 전체 중 최대 `update_id`(없으면 None).
- `poll_once` 는 `getattr(receiver, "last_update_id", None)` 과 유지된 업데이트 중 큰 값 + 1 을 오프셋으로 저장한다.

- [x] 테스트: 다른 chat 업데이트만 두 건(10, 11) → `poll()` 은 `[]`, `last_update_id == 11`.
- [x] 테스트: `Recv` 가짜에 `last_update_id=50` 을 두고 `texts=[]` → `poll_once` 뒤 `telegram_offset == 51`.
- [x] 커밋: `fix(telegram): 걸러진 업데이트도 오프셋에 반영 (명령 유실 방지)`

### Task 5: 체결 매칭에 주문 날짜 조건

**Files:**
- Modify: `src/trader/orchestrator/fills.py:21-29`
- Modify: `src/trader/ledger/repo.py` (`day_bounds(d, market)` 공개)
- Test: `tests/orchestrator/test_fills.py`

**Interfaces:**
- `repo.day_bounds(d: date, market: str | None) -> tuple[str, str]` — 시장 현지 하루의 UTC ISO 경계(market 이 없거나 모르면 KST).
- `_order_id_for` 는 `created_at` 이 체결일(`f.filled_at` 의 시장 현지 날짜) 창 안인 주문만 매칭한다.

- [x] 테스트: D 에 `order_no="12"` 주문 + 체결, D+8 에 같은 `order_no`·종목·방향 체결 → `orders` 가 2행(두 번째는 `mode='external'`), 첫 주문의 fills 가 그대로.
- [x] 커밋: `fix(fills): 체결↔주문 매칭을 같은 현지 날짜 주문으로 제한`

### Task 6: 주문 접수 뒤 기록 실패 보호

**Files:**
- Modify: `src/trader/orchestrator/executor.py:409-470, 518-527`
- Test: `tests/orchestrator/test_executor.py`

**Interfaces:**
- `_place`: `insert_order` 실패는 CRITICAL 로그 + 접수번호 포함 알림 뒤 재발생. `write_checkpoint`·`notifier.send` 실패는 경고만 남기고 결과를 돌려준다.
- `approve`: `_place` 가 예외를 내면 승인을 `failed`(expect="approved") 로 바꾸고 `{"ok": False, "error": ...}` 를 돌려준다.

- [x] 테스트: notifier 가 예외를 던져도 `_place` 결과 `accepted` True 이고 `orders` 행이 있다.
- [x] 테스트: `repo.insert_order` 를 예외로 monkeypatch 하고 `approve` → `ok` False, 승인 상태 `failed`, 알림에 접수번호.
- [x] 커밋: `fix(executor): 주문 접수 뒤 장부 기록 실패를 보호`

### Task 7: 같은 종류 트리거는 운영일에 1회만 재평가

**Files:**
- Modify: `src/trader/orchestrator/monitor.py:186-200`, `src/trader/ledger/repo.py` (`trigger_handled_between`)
- Test: `tests/orchestrator/test_monitor.py`

**Interfaces:**
- `repo.handled_trigger_today(conn, market, ticker, kind, lo, hi) -> bool`: `handled_at IS NOT NULL` 이고 `created_at` 이 창 안인 같은 종류 트리거 존재 여부.
- `monitor_once`: 운영일 현지 창에 처리된 같은 종류 트리거가 있으면 새 트리거를 쌓지 않고 재평가도 띄우지 않는다(실패로 처리된 것은 `session_id` 가 `실패:` 로 시작하므로 제외하지 않는다 — 실패는 30분 뒤 재시도 허용).

- [x] 테스트 수정: `test_trigger_reevaluation_rate_limited` 의 r3(31분 뒤 2번째 세션) → 같은 날 같은 종류라 세션 없음(`len(ctx.calls) == 1`). 새 테스트: 다음 날(D+1)에는 다시 1회.
- [x] 커밋: `fix(monitor): 지속 조건이 30분마다 세션을 재생성하지 않도록 운영일 1회 제한`

### Task 8: 마무리

- [x] `uv run pytest`, `uv run ruff check src tests`.
- [x] `docs/TODO.md` 에 이 플랜 항목 추가·체크, CLAUDE.md 의 어긋난 규칙 문장(운영일·목표 비중·오프셋) 갱신.
- [ ] 커밋 후 PR (`main` 대상).
