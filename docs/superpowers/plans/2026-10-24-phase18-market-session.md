# Phase 18 시장 세션 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 하락장 대응(Phase 17 H)에 에이전트를 붙인다 — 매일 시장별 세션 1회로 거시 애널리스트가 리포트를 쓰고 강세/약세 리서처가 토론하고 전략가가 헤지 비중(0~20%)·레버리지 여부를 **제안**한다. 최종 결정은 파이썬 규칙: 국면 지표가 bear 가 아니면 진입하지 않고, bear 면 제안을 `[min_weight_pct, max_weight_pct]` 로 잘라 쓴다.

**Architecture:** `SessionSpec.kind="market"` 하나를 추가하고(`orchestrator.md` `## market 모드`, `registry.MARKET_AGENTS`), 시장 단위 체크포인트 `_market_*` 는 `market_dir` 에만 둔다(종목 경로 `@_guarded` 와 분리). 도구는 `tools.market_bundle/market_write/market_summarize/submit_market_view`, 입력 조립·세션 기동은 `orchestrator/market_view.py`, 규칙 연결은 `hedge.rebalance` 한 곳(`clamp_weight`, `leveraged` 거부). 결과는 `control.market_view:<market>` 플래그(`date` 포함, 오늘 것만 유효).

**Tech Stack:** Python 3.12 / pydantic / SQLite / APScheduler, React 19 + Vitest.

**Spec:** 사용자 선택(2026-10-24) "시장 세션 추가: 거시 애널리스트 + 강세/약세 토론 + 트레이더가 헤지 비중 제안". 스펙 §4(세션·체크포인트)·§6.1(게이트).

## Global Constraints
- 에이전트는 제안만. `hedge.rebalance` 가 bear 가 아니면 제안을 무시하고, bear 진입 비중만 제안으로 조절한다(완전 거부 0% 는 불가 — `min_weight_pct` 로 하한, 바꾸려면 설정 0).
- 공통 규칙 6문장은 신규 md 2개도 같은 접두로 담고 `tests/agents/test_registry.py` 가 12개를 비교한다.
- 시장 리포트의 `ticker` 는 종목 코드가 아닌 proxy 라벨(`KOSPI200`|`SPX`)이라 `_market_*` 파일만 쓴다.
- 세션 비용: 시장당 하루 +1 세션(약 $1~2). `daily_session_cap` 안에서 analyze 와 경쟁하며 상한이면 세션 없이 규칙 기본값.

## Review Focus
1. bear 가 아닌데 RISK_OFF 제안으로 헤지가 들어가는 경로가 없는가 → `test_proposal_can_refuse_leverage_but_not_force_entry`.
2. 어제 제안이 오늘 쓰이지 않는가 → `market_view_today` 의 `date` 비교, `test_stale_proposal_is_ignored`.
3. 세션 실패·상한·비활성이 Phase 17 동작(max 비중)으로 떨어지는가 → `run_market_view` skipped/error, `clamp_weight(None)`.
4. 시장 세션이 같은 날 두 번 뜨지 않는가 → `_market_view_body` 의 "오늘 이미 제안 있음".

---

### M1 스키마·레지스트리·md
**Files:** `src/trader/agents/schemas.py`, `src/trader/agents/registry.py`, `.claude/agents/{analyst-market,strategist,orchestrator,researcher-bull,researcher-bear,trader}.md`, `tests/agents/test_registry.py`
- [x] `HedgeProposal{market, stance RISK_ON|NEUTRAL|RISK_OFF, hedge_weight 0~0.25, leveraged, confidence, horizon_days, rationale ≤500, invalidation_conditions}`
- [x] `MARKET_AGENTS`, `schema_model_for("strategist")`, 오케스트레이터 프롬프트 스키마 줄
- [x] md 신규 2개 + 기존 4개 보강, 레지스트리 테스트 12개

### M2 도구·CLI
**Files:** `src/trader/orchestrator/tools.py`, `src/trader/cli.py`, `tests/orchestrator/test_market_tools.py`
- [x] `market_bundle(kind analyst|researcher|strategist)`·`market_write(_market_analyst|_market_debate_r<n>_<side>)`·`market_summarize`·`submit_market_view`·`market_view_today`
- [x] `tool market bundle|write|summarize`, `tool submit-market-view`

### M3 입력 조립·세션
**Files:** `src/trader/orchestrator/market_view.py`, `src/trader/orchestrator/session.py`, `src/trader/agents/fake.py`, `tests/orchestrator/test_market_view.py`
- [x] `build_inputs`(regime·index·macro·macro_news·market_notes·hedge·sector_weights·errors)·`save_inputs`·`run_market_view`·`load_view`·`clamp_weight`
- [x] `kind="market"` → run kind `market`, `MARKET_AGENTS`, `fake.run_fake_market`

### M4 규칙 연결
**Files:** `src/trader/config.py`, `config/settings.yaml`, `templates/config/settings.yaml`, `src/trader/orchestrator/hedge.py`, `tests/orchestrator/test_hedge.py`
- [x] `HedgeCfg.market_session=true`, `min_weight_pct=0.05`(≤ max 검증)
- [x] `rebalance`: 제안 비중 clamp·레버리지 거부·bear 아니면 무시·rationale 에 제안 요약

### M5 데몬·대기열·가시성
**Files:** `src/trader/daemon.py`, `src/trader/jobs.py`, `src/trader/risk/regime.py`, `src/trader/status.py`, `src/trader/web/routers/system.py`, `web/src/{sectors.ts,jobs.ts,how.ts}`, `web/src/pages/Dashboard.tsx`, `tests/test_daemon_hedge.py`
- [x] `_market_view_body`(regime 뒤·운영일 1회·알림), 대기열 kind `market_view`
- [x] `regime.summary(today=).view`·`line` '제안 n%', STATUS 줄, 칩 꼬리 "(KR 제안 12%)"

### M6 문서
- [x] CLAUDE.md 시장 세션 단락, `docs/TODO.md` Phase 18, 이 플랜, how.ts

## 범위 조정
| 제외 항목 | 이유 | 재개 Phase |
|---|---|---|
| 오늘의 실행 페이지에 `_market_*` 표시 | 종목 단계 표가 종목 디렉터리 기준 | 다음 Phase |
| 시장 세션 결과 → 스크리너 입력 | 스크리너는 정량 후보 요약만 받음, 프롬프트 크기 | 다음 Phase |
| 제안 적중률 복기 | reflect 가 종목 결정 단위 | 다음 Phase |
