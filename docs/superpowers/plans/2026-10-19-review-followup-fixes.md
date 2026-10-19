# 전체 코드 검토 후속 수정 (main 포함 전수 리뷰) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 2026-10-18 검토 체인(정확성 1·2차·성능·구조) 위에서 `main` 을 포함한 코드베이스 전수 리뷰(스킬 리뷰 2회 + 계층별 리뷰 4개)가 찾은 35건을 고친다. `refactor/review-structure` 위에 쌓는다.

**Architecture:** 기존 모듈 안의 최소 수정 + 신규 `risk/ticks.py`(호가단위). 스키마 변경 없음(`orders.status` 에 `unknown` 값 추가는 데이터 값). 네 묶음으로 커밋한다: A 손절·주문 안전, B 데몬, C 운영일·재평가·거래소, D 데이터 품질·호가단위·기타.

**Tech Stack:** Python 3.12, pytest, respx, SQLite.

**Spec:** `docs/superpowers/specs/2026-10-09-trading-agent-design.md` §5.2(monitor), §6(리스크·executor), §9.1(워크포워드), §10.1(데몬).

## Global Constraints
- 금액 `Decimal`, 날짜 창 시장 현지, 저장 시각 UTC ISO. `uv run pytest`·`uv run ruff check src tests` 0건.
- 어떤 경로도 실전 모드를 올리지 않는다. 주문은 executor 안에서만.

## Review Focus
- 손절선 ≥ 현재가 결정이 어떤 action 이어도 저장되지 않는가 (A1).
- `unknown` 주문이 중복 방지 조건에 살아 있는 주문으로 잡히고 fills sync 가 확정하는가 (A2).
- `Lock` 파일이 내용 없이 존재하는 순간이 없는가 (B1).
- US `collect`·`analyze`·`scan`·`tool collect` 가 데몬과 같은 `runs/<date>` 를 보는가 (C1).
- `fwd_date` 필터가 계약을 바꾸지 않는가: 결측 없는 데이터에서 split 결과 동일 (D5).

---

### Task A: 손절·주문 안전 (높음 1~6, 낮음 8)
- [x] A1 `tools.submit_decision`: `stop_loss ≥ 현재가` 면 action 과 무관하게 거부 (저장 전).
- [x] A2 `KisApiError("NETWORK_UNKNOWN")`(응답 미수신) → `executor._place` 가 `orders.status='unknown'` + "[주문 상태 미확인]" 알림; `fills._order_id_for` 가 같은 날·종목·방향 체결로 `accepted` 확정.
- [x] A3 `monitor._sell_now`: `OrderBookkeepingError` 격리 + `sl_unrecorded:<계좌>:<시장>:<종목>:<날짜>` 플래그로 당일 재발주 차단.
- [x] A4 `has_open_order_today(exclude_filled=True)` 로 체결된 TRIM 이 손절을 막지 않음; 손절은 결정 단위 `has_open_order_or_approval` 을 보지 않음.
- [x] A5 `executor.approve`: 실효 모드가 `approve` 가 아니면 미실행. 브로커가 거부한 주문의 기록 실패는 승격 없이 계속.
- [x] 커밋 `fix(monitor, executor, broker): 손절·주문 안전 보강`

### Task B: 데몬 (높음 4, 중간 17·18)
- [x] B1 `Lock`: 임시 파일 + `os.link` 원자 생성(하드링크 미지원이면 O_EXCL 폴백), pid 없는 잠금은 10초 안이면 살아 있는 것으로.
- [x] B2 시그널 핸들러 재진입 무시, plist `ExitTimeOut`(`llm.timeout_s + 60`).
- [x] B3 `_analyze_body`: `_scan.json` 없음 → 알림 + `{"error": …}`; `run_job` 은 `error` 키 결과를 error 행으로.
- [x] B4 `logging_setup.SharedTimedRotatingFileHandler`: 다른 프로세스가 회전했으면 스트림 재오픈.
- [x] 커밋 `fix(daemon, installer, logging): 데몬 이중 기동·종료·로그 공유 보강`

### Task C: 운영일·재평가·거래소·보유일 (중간 7·9·10·11, 낮음)
- [x] C1 `collect_and_save`·CLI collect/analyze/scan·`tool collect`·`counts_from_inputs` → `run_date_for`.
- [x] C2 `resume.handled_marker`: 실패 세션(`ok=False`)도 `실패:` 표시. `_can_reevaluate` 는 오늘 결정 없으면 세션 생략(트리거는 미처리 유지).
- [x] C3 `Position.exchange`(잔고 `ovrs_excg_cd` → EXCD), monitor 는 잔고 거래소 우선·없으면 universe·둘 다 없으면 건너뜀.
- [x] C4 재평가 체크포인트 `41_risk_check_r<n>`·`51_order_r<n>`, `parse_price_conditions` 비가격 문장 제외·최대값, `held_since` 시장 현지 날짜.
- [x] 커밋 `fix(monitor, cli, sources, ledger): 운영일 통일·재평가 전제·US 거래소·보유일`

### Task D: 데이터 품질·호가단위·기타 (중간 8·12~16·19·20, 낮음)
- [x] D1 `make_labels` 의 `fwd_date` + `walkforward.split` 필터. D2 `quant_report` 캐시 키 mtime·크기. D3 `backfill` flush 보류·재시도.
- [x] D4 `snapshot_positions` 사라진 종목 삭제, `record_decision` 만기 전 갱신, `realized_pnl_5d` 매수 기록 없는 SELL 제외·market 필터.
- [x] D5 `risk/ticks.quantize_price`(KRX 표·US 0.01) 를 `rules.evaluate` 가 적용, `_price_str(market)` 검증.
- [x] D6 유보 축소 사유 1건, macro `asof`·공용 `utc_now`, KR 분봉 원시 행 수 판단, RL 스텝 KST 운영일, `TRADER_HOME` 절대경로, 차트 US 마커 현지 날짜, `/status` 실효 모드, `_config_errors` 적용 확대.
- [x] 커밋 `fix(ml, ledger, risk, sources, cli): 데이터 품질·호가단위·기타 검토 잔여`

### Task E: 마무리
- [x] `uv run pytest`(781 passed) · `ruff` 0건, TODO.md, CLAUDE.md 갱신, 푸시.

### Task F: 사용자 결정 반영 (2026-10-20)
- [x] F1 `fills.sync_fills(now=)`: 시장별 조회 성공 + 그날 정규장 종료(`MARKET_CLOSE` 현지)면 `expire_unfilled` 로 체결 없는 `accepted`·`unknown` → `expired`. 장중·조회 실패는 불변. `repo.DEAD_STATUSES`/`dead_status_sql()` 로 죽은 상태 목록을 한 곳에.
- [x] F2 `build_jobs`: `<시장>_scan_analyze` 단일 cron(scan 시각). `_scan_analyze_body` 는 stale running 정리 → scan(실패 시 통보) → analyze. `market_job` 이 본체 결과를 돌려줘 `error` 키가 runs 에 닿는다.
- [x] F3 `quant.candidates(asof=)` ← CLI scan 운영일·데몬 `today`. 신호 결측·필터 제외 종목은 "피처는 있으나 필터·신호 결측으로 제외".
- [x] 커밋 `fix(fills, daemon, scan): 미체결 만료·scan→analyze 단일 잡·stale 정리·asof 전달`

### Task G: 후속 수정 독립 리뷰 + 리뷰 공백(웹·프롬프트·설정) 반영 (2026-10-20, 18건)
- [x] G1 손절 dedupe 가 장중 체결을 못 보는 문제: 막힐 때 그 계좌 체결을 1회 동기화(`monitor._blocked_by_open_sell`).
- [x] G2 `DaemonDeps.stop_event`: 시그널 핸들러가 세우고 `_analyze_body` 가 남은 종목 세션을 띄우지 않음(`stopped`). scan 실패는 `_analyze_body` 가 원인과 함께 알림 1건·error 1건.
- [x] G3 `MarketSchedule.analyze` 선택(yaml 에서 제거), `_tick_size_kr` 제거(`risk/ticks`), unknown 주문의 "[주문 실패]" 덧알림 제거.
- [x] G4 재평가의 현재가 = 그날 최신 트리거 가격(`repo.latest_trigger_price`; 손절선 검사·게이트), 트레이더 묶음에 `previous_decision(_stage)`, 섹션 읽기(fundamental·sentiment)에 `quote` 동봉.
- [x] G5 `registry.agents_json` 이 md 에 스키마가 없으면 정본 스키마를 덧붙임(screener·reflector). 공통 규칙 6문장 접두 비교를 10개 md 전부로 확장.
- [x] G6 md 갱신: trader(계좌별 portfolio·memory·memory_ids_used·trigger.price), analyst-fundamental/sentiment(quote 사본·manual_inputs), analyst-news-macro(events), orchestrator(previous_decision).
- [x] G7 웹: `/api/today`·`/api/checkpoint` 기본 날짜와 `portfolio/refresh` 스냅샷 날짜를 `run_date_for`, Performance 페이지 시장 초기화.
- [x] 커밋 `fix: 후속 수정 독립 리뷰·웹/프롬프트/설정 리뷰 반영`

### Task H: 남은 보완 3건 (2026-10-20)
- [x] H1 `session.run_claude`: 새 프로세스 그룹(`start_new_session`)으로 띄우고 타임아웃이면 `os.killpg` (고아 `trader tool` 서브프로세스 방지; Windows 는 자식만).
- [x] H2 `performance.report` 퀀트 벤치마크가 `universe.yaml` 종목을 `latest_rows(tickers=)` 로 넘긴다.
- [x] H3 `logging_setup.configure(name=)`: `trader web`→`trader-web.log`, `telegram loop`→`trader-telegram.log`, `monitor`→`trader-monitor.log`. 데몬·짧은 명령은 `trader.log`.
- [x] 커밋 `fix: 세션 프로세스 그룹 kill·퀀트 벤치마크 유니버스·장기 명령 로그 분리`

### Task I: 순수 정리 3건 (2026-10-20)
- [x] I1 `cli._boot(settings|accounts|secrets|conn, log_name)`: 로깅·설정·계좌·시크릿·스키마 적용 원장 연결을 한 곳에. status·kis ping·order-test·tool ctx·exec ctx·daemon·fills·control·web·scan·analyze·monitor·rl report·reflect 가 쓴다.
- [x] I2 `agent_calls.num_turns`(마이그레이션 v9): 세션 결과의 턴 수를 기록 (`/api/calls` 에 그대로 노출).
- [x] I3 `repo._hydrate_decision`: `get_decision(hydrate=True)`·`latest_decision`·`views.get_decision` 이 같은 복원을 쓴다.
- [x] 커밋 `refactor: CLI 부트스트랩 헬퍼·num_turns 기록·결정 hydrate 공유`

## 범위 조정
| 제외 항목 | 이유 | 재개 |
|---|---|---|
| Phase 7 별도 Phase 항목(US 재무비율·동종업 중앙값·피처 탐색 UI·하이퍼파라미터 탐색) | 데이터 설계 필요 | 사용자 결정 |
