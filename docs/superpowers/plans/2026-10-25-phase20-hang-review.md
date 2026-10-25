# Phase 20 응답없음 수정 + 전체 코드 리뷰 수정 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 사용자 보고 "프로그램이 가끔 응답없음" 의 원인을 고치고, 메인 포함 전체 코드 리뷰(프로세스·성능·버그) 49건 중 high·medium 전부와 저비용 low 를 수정한다. 공개 저장소 전환(Actions 한도 회피)에 맞춰 계좌번호를 secrets 로 옮기고 PR CI 를 둔다.

**Architecture:** 트레이는 `TrayController`(캐시 상태·워커 스레드·`on_main`), 세션 종료는 `run_claude(cancel=)`, 웹 SSE 는 리더 탭(`events.ts`). 리뷰 수정은 상세를 `docs/architecture.md` 「응답없음 대응(Phase 20)」·「전체 리뷰 수정(Phase 20)」 에 둔다.

**Tech Stack:** Python 3.12 / pystray·PyObjC / SQLite / APScheduler, React 19 + Vitest.

## Review Focus
1. 메인 스레드에서 SQLite·`shutdown(wait=True)` 를 다시 부르는 경로가 없는가 → `tests/platform/test_tray.py`.
2. 종료 요청 뒤 `trader tool` 고아 프로세스가 남지 않는가 → 프로세스 그룹 SIGTERM/SIGKILL 테스트.
3. 탭 N 개에서 EventSource 가 1개인가 → `web/src/events.test.ts`.
4. NAV 정의 변경이 게이트 수치를 바꾼다 → 모의에서 하루 관찰 뒤 실전.

---

### G 공개 저장소 대비
- [x] `load_accounts` 자리표시자 → `KIS_PAPER_CANO/KIS_REAL_CANO`, `secrets import` 매핑, `.github/workflows/ci.yml`

### H 응답없음
- [x] 트레이 종료 비동기·캐시 상태·AppKit 메인 스레드·데몬 사망 감지
- [x] 세션 종료 유예(`SessionCancelled`), 데몬 루프 `stop_event`
- [x] SSE 리더 탭, 웹 KIS 대기 상한 503, YouTube·수집 절 타임아웃, 풀 16

### F 리뷰 수정 (high 7 → medium 22 → low 저비용)
- [x] high: NAV·손절 전 취소/매도가능수량·dry-run 40_·US 3거래소·재평가 큐·missed cron·잠금 pid
- [x] medium 묶음1(executor/주문) · 묶음2(데몬/큐/피처/ML) · 묶음3(웹/수집/텔레그램)
- [x] low: 섹터 상한·세션 상한 트랜잭션·시장 창·토큰/모델/DART 메모·캐시 락·중복 상수

## 범위 조정
| 제외 항목 | 이유 | 재개 Phase |
|---|---|---|
| 야간 KIS 대조(reconciliation) 잡 | 설계 범위 밖(새 잡) | 다음 Phase |
| 제출 시 재시세(re-quote) | 게이트 수치 변화 관찰 뒤 | 다음 Phase |
| SSE 끊김 표시·liveness 폴링 | 리더 탭으로 1차 해결 | 다음 Phase |
| 메모리 브레이커 다계좌 OR | 모의 계좌 1개 | 다음 Phase |
