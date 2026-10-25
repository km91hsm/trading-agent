# Phase 19 뉴스 저장소 + 종목 발굴 + 전략가 국면 지표 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** (1) 맨 처음 종목 선정을 정량 필터만이 아니라 **에이전트가 뉴스로** 한다 — 유니버스 안 발굴 우선, 그 다음 상장 종목 전체(≤2). (2) 그러려면 종목이 정해진 뒤 종목별로만 받던 뉴스를 **시장 전체**로 상시 저장해야 한다. (3) 하락장 대응도 에이전트 판단을 더 쓴다 — 전략가 RISK_OFF 를 4번째 국면 지표로(지표 0개면 진입 불가), RISK_ON 은 경계에서 하루 보류.

**Architecture:** 원장 v14 `news_items` + `sources/news_store`(데몬 `news_collect`) → 읽는 곳 셋(발굴·시장 세션·종목 세션). 발굴은 screen 세션 안 두 단계(`scout` → `screener`, 세션 수 불변) 이고 코드 확정·거래소·제외·병합은 파이썬(`scan/discovery`). 전략가 지표는 `RegimeSignals.agent_risk_off` + `regime.reapply` + 데몬 `_regime_apply_view`, 보류는 `hedge._risk_on_deferral`.

**Tech Stack:** Python 3.12 / pydantic / SQLite / APScheduler, React 19 + Vitest.

**Spec:** 사용자 답변(2026-10-25) "유니버스 안 뉴스 발굴 우선 → 상장 종목 전체", "전략가 RISK_OFF 를 4번째 지표로", "뉴스는 전체 뉴스를 수집", "세이브티커는 나중에".

## Global Constraints
- LLM 은 이름·촉매만 낸다. 코드·거래소·제외·상한·병합은 파이썬.
- 룩어헤드: 저장은 published·received 둘 다, 읽기는 둘 다 asof 이하.
- 전략가만으로 bear 가 되지 않는다(정량 지표 0개 → neutral 까지). 해제 규칙은 바꾸지 않는다.
- 공통 규칙 6문장은 md 13개가 같은 접두(테스트).

## Review Focus
1. 유니버스 밖 발굴이 시세 없는 코드로 analyze 에 들어가지 않는가 → `resolve` 의 unresolved·`test_resolve_us_listed_needs_exchange_from_quotes`.
2. 저장소 뉴스가 asof 뒤 것을 보여 주지 않는가 → `test_recent_hides_items_published_or_received_after_asof`.
3. 전략가 신호가 다음 날로 새지 않는가 → `_regime_apply_view` 가 RISK_OFF 가 아니면 끈 채 재판정, `reapply` 는 오늘 판정에만.
4. RISK_ON 보류가 3/3 이나 확신 낮음에 걸리지 않는가 → `test_risk_on_defers_entry_only_at_boundary`.

---

### N 뉴스 저장소
**Files:** `src/trader/sources/news_store.py`, `src/trader/ledger/db.py`(v14), `src/trader/daemon.py`(`_news_body`·`news_collect`), `src/trader/jobs.py`, `src/trader/orchestrator/market_view.py`, `src/trader/sources/inputs.py`(`InputDeps.conn_factory`·`_with_store_news`), `src/trader/web/routers/news.py`, `web/src/pages/Data.tsx`, `config/sources.yaml`·`config/schedule.yaml`(+templates)
- [x] `collect/insert_items/tag/recent/prune/count/universe_names`
- [x] 데몬 interval 잡 + 큐 kind + 보존
- [x] 시장 세션 `macro_news` 저장소 우선, 종목 세션 `news.stock` 합침
- [x] `GET /api/news` + 데이터 페이지 '시장 뉴스'

### S scout 발굴
**Files:** `.claude/agents/scout.md`, `.claude/agents/screener.md`, `.claude/agents/orchestrator.md`, `src/trader/agents/schemas.py`(Discovered), `src/trader/agents/registry.py`, `src/trader/scan/discovery.py`, `src/trader/orchestrator/tools.py`(`discovery_inputs`·`submit_discovery`), `src/trader/cli.py`(`tool scan discovery-inputs`·`tool submit-discovery`), `src/trader/scan/screener.py`(`discovery=`), `src/trader/agents/fake.py`
- [x] 입력 조립 → scout → 제출(코드 확정·제외·상한) → 병합 → screener
- [x] 데몬·CLI scan 이 `discovery=True`, `_scan.json.discovery`
- [x] FAKE 경로

### R 전략가 지표
**Files:** `src/trader/risk/regime.py`, `src/trader/orchestrator/hedge.py`, `src/trader/daemon.py`(`_regime_apply_view`), `src/trader/config.py`(`HedgeCfg.agent_signal/agent_min_confidence`), `web/src/sectors.ts`
- [x] `agent_risk_off`·`reapply`·`count_text`
- [x] 시장 세션 뒤 재판정 + 전환 알림
- [x] RISK_ON 경계 보류

## 범위 조정
| 제외 항목 | 이유 | 재개 Phase |
|---|---|---|
| 세이브티커 로컬 파일 리더 | 예시 파일(형식·경로) 미확보 | 다음 Phase(TODO 이연) |
| 발굴 입력의 네이버 블로그 글 | 저장소 뉴스로 충분한지 먼저 관찰 | 다음 Phase |
| 오늘 페이지 '뉴스 발굴' 사유 표시·STATUS 한 줄 | `_scan.json.discovery` 로 확인 가능 | 다음 Phase |
