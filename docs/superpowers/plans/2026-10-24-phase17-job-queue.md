# Phase 17 작업 대기열 + 하락장 대응 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 터미널 없이 운영한다 — 유지보수 작업(스캔→분석·종목 분석·피처 백필·퀀트/RL 학습·복기·체결 동기화)을 데몬이 자동으로 또는 웹 버튼으로 돌리고, 하락장에는 파이썬 규칙으로 신규 매수를 막고 인버스 ETF 로 헤지한다. 거시 지표 3종·네이버 블로그 RSS·시장 공통 수동 입력·섹터 비중 UI 를 함께 넣는다.

**Architecture:** 요청은 원장 `job_requests`(v11) 한 곳, 실행 주체는 데몬 `job_queue` interval 잡 하나(`heavy_lock` 으로 cron 과 겹침 방지, `run_job` 으로 runs·하트비트 정책 불변). 국면은 `risk/regime.py` 순수 함수 + `control.regime:<market>`; 헤지는 `orchestrator/hedge.rebalance` 가 파이썬 `Decision`(`decisions.source='hedge'`, v12)을 `executor.execute(is_hedge=True)` 로 보낸다(계좌 모드·LIVE 확인·paused·dry_run 불변). LLM 은 `context.regime` 으로 상태만 받는다.

**Tech Stack:** Python 3.12 / FastAPI / SQLite / APScheduler, React 19 + Vite + Vitest.

**Spec:** 사용자 승인 플랜(세션 2026-10-24), 스펙 §5·§6.1·§8·§10.1. 디자인: `docs/design/2026-10-24-phase17-ui-patterns.md`.

## Global Constraints
- `ledger/db.MIGRATIONS` 는 버전 추가만(v11 `job_requests`, v12 `decisions.source`). pydantic `extra="forbid"`.
- 실행 주체는 데몬뿐: 웹은 enqueue 만, 데몬이 꺼져 있으면 409. 큐 요청도 세션 상한·계좌 모드·paused 를 그대로 지킨다.
- 헤지는 LLM 비관여. 공매도는 KIS 개인 API 미지원이라 제외. 헤지 ETF 는 스크리너·세션·메모리·ML·monitor 대상이 아니다.
- 색은 `tokens.css` 변수만, 새 UI 는 `Table.tsx` 열 문법. design:design-system(확장 문서) → 구현 → design-critique/accessibility-review.
- 사용자 대면 문구 한국어, 로거 `trader.<계층>`.

## Review Focus
1. 큐 요청과 cron scan_analyze 가 겹칠 때 세션이 두 번 뜨지 않는가 → `heavy_lock` 테스트(`test_daemon_queue.py`).
2. 데몬 SIGKILL 뒤 running 잔류가 재실행되지 않는가 → `_bootstrap_queue` 가 error 로 닫는다.
3. 국면이 같은 운영일에 두 번 계산돼도 히스테리시스 streak 이 겹치지 않는가 → `regime.update` 테스트.
4. 헤지 BUY 가 종목 수·섹터·일일손실·비용 규칙에 걸려 들어가지 못하는 일이 없는가 → `is_hedge` 면제 테스트.
5. 헤지 ETF 가 monitor 손절·트리거에 잡혀 국면과 무관하게 팔리지 않는가 → monitor 제외.

---

### Task 1~5: 작업 대기열 (완료)
**Files:** `src/trader/jobs.py`, `src/trader/ledger/db.py`(v11), `src/trader/daemon.py`, `src/trader/cli.py`, `src/trader/web/routers/jobs.py`, `src/trader/web/sse.py`, `web/src/jobs.ts`, `web/src/components/{RunJob,JobRequests,Jobs}.tsx`, `web/src/pages/{Dashboard,Today,Data,Ml}.tsx`, `docs/design/2026-10-24-phase17-ui-patterns.md`.
- [x] 큐 모듈·마이그레이션·테스트
- [x] 데몬 `job_queue`·heavy 락·`_analyze_targets`·RL 주간 학습·부트스트랩
- [x] CLI `features_fn`
- [x] 웹 API·SSE
- [x] 프런트 버튼·대기열 블록·빈 화면 액션

### A·C·M: 수집 (완료)
- [x] FRED `dxy`·`wti`·`nasdaq`
- [x] `sources/naver_blog.py` + `ChannelsCfg.naver_blog`
- [x] `manual_inputs.append_market/read_market` + `POST /api/control/manual-inputs` + 데이터 페이지 폼

### H1~H5: 하락장 대응 (완료)
**Files:** `src/trader/config.py`(`HedgeCfg`, `RiskLimits.max_sector_pct`), `src/trader/risk/{regime,rules,stance}.py`, `src/trader/orchestrator/{hedge,executor,monitor,tools}.py`, `src/trader/ledger/{db(v12),repo,metrics,views}.py`, `src/trader/daemon.py`(`_regime_body`·`_hedge_body`), `src/trader/{status,cli}.py`, `src/trader/notify/commands.py`, `src/trader/web/routers/system.py`, `web/src/sectors.ts`, `web/src/pages/Dashboard.tsx`, `.claude/agents/trader.md`.
- [x] H1 설정·국면 판정
- [x] H2 게이트
- [x] H3 원장·executor
- [x] H4 헤지 실행·데몬 연결
- [x] H5 가시성·CLI

### S: 섹터 비중 (완료)
- [x] `views.sector_weights`·`/api/portfolio.sectors`·`components/Sectors.tsx`

### 실기 (사용자)
- [ ] `docs/TODO.md` Phase 17 의 '실기(사용자 확인 대기)' 항목 4건.

## 범위 조정
| 제외 항목 | 이유 | 재개 Phase |
|---|---|---|
| 텔레그램 `/run <kind>` | 큐 enqueue 10줄, 웹 버튼으로 충분 | 18 |
| CLI `trader jobs list|run` | 터미널 사용자용 보조 | 18 |
| 헤지 상품 교체(−1x↔−2x) | 변동성 끌림·회전 비용, 진입 상품 유지로 단순화 | 18 |
| 세이브티커 자동 수집 | Cloudflare 403 + robots AI 금지 → 수동 붙여 넣기로 대체 | 없음 |
