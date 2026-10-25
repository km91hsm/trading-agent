# Phase 21 보완 묶음 — 이연 항목 회수 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Phase 17~20 이 "다음 Phase" 로 넘긴 보완 항목을 전부 회수한다 — 발굴 가시성, 우선순위 큐·`/run`·CLI `jobs`, 제출 시 재시세, 야간 KIS 대조, 헤지 상품 교체·외인 수급 지표(기본 끔), SSE 끊김 표시·liveness, 세이브티커 캘린더 → `events.upcoming`, `MAX_PAST`·브레이커 정리, 스크리너 `context`, 오늘 페이지 시장 단계 파일.

**Architecture:** 새 모듈은 `orchestrator/reconcile.py` 하나. 나머지는 기존 계약 위의 보강 — 큐는 `jobs.PRIORITY`+원장 v15, 재시세는 `tools._requote`+`executor.execute(extra_note=)`, 헤지는 `HedgeCfg` 플래그 두 개(기본 끔), 웹은 `events.ts` 의 onStatus 중계. 상세는 `docs/architecture.md` 「보완 묶음(Phase 21)」.

**Tech Stack:** Python 3.12 / pydantic / SQLite / APScheduler, React 19 + Vitest.

**Spec:** 사용자 요청(2026-10-25) "보완 항목 전부 진행하고 머지도 자동으로".

## Global Constraints
- 브로커가 진실: 대조는 스냅샷만 덧쓰고 주문·체결 행은 건드리지 않는다.
- 재시세는 게이트 입력(수량·손절 검사)만 바꾼다. 재평가(트리거 가격)는 제외.
- 새 국면 지표·상품 교체는 기본 끔. 켜는 것은 사용자 결정(2~4주 데이터 뒤).
- 요청 경로(웹·텔레그램·CLI)는 전부 `jobs.enqueue` + 데몬 생존 확인.

## Review Focus
1. 재시세로 손절선이 현재가 이상이 되면 저장 전에 거부되는가 → `test_submit_decision_requotes_when_inputs_are_stale`.
2. 우선순위가 같은 요청은 FIFO 인가 → `test_claim_next_prefers_priority_then_fifo`.
3. 대조가 주문 행을 바꾸지 않는가 → `test_reconcile_mismatch_notifies_and_fixes_snapshot_but_keeps_orders`.
4. 상품 교체가 같은 날 매수를 내지 않는가 → `test_switch_instruments_sells_after_streak_then_reenters_next_day`.

---

### C1 발굴 가시성 + 유니버스 밖 피처 스냅샷
**Files:** `src/trader/web/routers/runs.py`, `web/src/pages/Today.tsx`, `web/src/pipeline.ts`, `src/trader/daemon.py`, `src/trader/status.py`, `src/trader/notify/commands.py`, `src/trader/scan/discovery.py`, `src/trader/scan/screener.py`, `src/trader/ml/features.py`
- [x] `/api/today.scan` + '오늘 스캔' 블록, `scan_line` 알림, STATUS·`/status`
- [x] `blog_posts`, `discovery: "listed"` → `_snapshot_listed`

### C2 우선순위 큐 + `/run` + CLI `jobs`
**Files:** `src/trader/ledger/db.py`(v15), `src/trader/jobs.py`, `src/trader/notify/telegram.py`·`commands.py`, `src/trader/cli.py`, `web/src/jobs.ts`·`components/JobRequests.tsx`
- [x] priority 열·`PRIORITY`·claim 순서·웹 배지, `/run`, `trader jobs list|run|kinds`

### C3 재시세
**Files:** `src/trader/orchestrator/tools.py`, `src/trader/orchestrator/executor.py`
- [x] `_requote`·`REQUOTE_AFTER_S`·`extra_note`

### C4 야간 KIS 대조
**Files:** `src/trader/orchestrator/reconcile.py`, `src/trader/config.py`(`ReconcileSchedule`), `config/schedule.yaml`(+templates), `src/trader/daemon.py`, `src/trader/jobs.py`
- [x] `reconcile_account/all`, cron `<시장>_reconcile`, 큐 kind

### C5 헤지 교체 + 외인 수급
**Files:** `src/trader/config.py`(`HedgeCfg`), `config/settings.yaml`(+templates), `src/trader/risk/regime.py`, `src/trader/orchestrator/hedge.py`, `src/trader/daemon.py`, `src/trader/broker/fake.py`
- [x] `flow_weak`·`_flow_weak`, `switch_instruments`·`_switch_due`

### C6 SSE 끊김 + liveness
**Files:** `web/src/events.ts`, `web/src/api.ts`, `web/src/hooks.ts`, `web/src/components/Shell.tsx`
- [x] onStatus 중계·`connected`·배지·60초 폴링

### C7 세이브티커 후속 + 정리 + 문서
**Files:** `src/trader/sources/saveticker.py`, `src/trader/sources/inputs.py`, `src/trader/sources/base.py`, `src/trader/ml/features.py`, `src/trader/orchestrator/executor.py`, `src/trader/scan/screener.py`, `src/trader/orchestrator/session.py`, `.claude/agents/{screener,orchestrator,analyst-news-macro}.md`, `CLAUDE.md`, `docs/architecture.md`, `docs/TODO.md`, `README.md`
- [x] `upcoming`·schema 경고, `MAX_PAST`, `_paper_nav_series`, `screen_context`, 시장 단계 파일, 문서

### C8 검증·머지
- [x] `uv run ruff check src tests` · `uv run pytest` · `cd web && npm run typecheck && npx vitest run && npm run build`
- [x] 푸시 → CI 녹색 → PR #20 Ready → main 머지 (태그 `v0.1.6` 은 사용자가 최종 HEAD 에)
