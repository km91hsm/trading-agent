# SaveTicker session recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 재확인 때 창을 유지하고 성공한 인증을 작업 간·재시작 간 재사용한다.
**Architecture:** session.py가 공유 로그인 락·state 파일·수동 대기를 소유한다. CLI는 생성된 Browser 어댑터로 기존 수집 루프를 실행한다.
**Tech Stack:** Python 3.12, Playwright storage_state, pytest.
**Spec:** docs/superpowers/specs/2026-10-29-saveticker-session-design.md

## Global Constraints
- 외부 사이트 보안 확인은 사람이 완료한다. 계정 값·토큰은 출력하지 않는다.
- 주문 및 실행 중 분석은 수정하지 않는다. Windows에서도 작동해야 한다.

## Review Focus
- 동시 작업이 최신 state를 읽기 전에 로그인 락을 잡는가.
- headless·시간 초과·종료 신호에서 대기를 마치는가.
- 인증 실패와 저장 실패에서 context·락을 정리하는가.
- 인증 파일이 원자 저장되고 0600인가.
- 기존 재로그인 경로도 인증 성공 후 state를 저장하는가.

### Task 1: 로그인 상태와 재확인 대기
**Files:** src/trader/saveticker/session.py, src/trader/saveticker/cli.py, tests/saveticker/test_saveticker_session.py
**Interfaces:** session_context(browser, runtime, account, *, headed, stop, on_wait, **adapter_options) yields Browser; serialized_login(adapter, runtime, *, headed=False, stop=None, on_wait=None).
- [x] 재사용·직렬화·수동 완료·headless·시간 초과·종료·정리 테스트 작성 후 실패 확인.
- [x] 최소 구현 후 수집기 전체 테스트 확인. 리뷰 지적 재인증 우회 경로·잠금 대기 종료를 추가 RED/GREEN으로 수정.
- [ ] 문서/TODO 갱신, 전체 Python/웹 검증, 독립 리뷰, PR/CI.
- [ ] 실행 중 앱 배포 가능 여부 확인; 실제 로그인 성공 여부는 별도로 기록.

## 실행 기록
- 독립 리뷰: 장중 재인증이 session 경로를 우회하는 문제와 로그인 락 대기의 종료 신호 처리를 수정. 재리뷰 주요 결함 없음.
- 로그인 단계 Playwright TimeoutError도 수동 대기로 전환(실제 사이트에서 login_submit 단계에서 발견).
- 웹 타입 검사·270 tests·빌드 통과. macOS 번들 빌드·selfcheck 등 스모크 통과. 전체 Python 게이트 실행 중.
- 실제 사이트: 사용자가 수집 Chromium에서 사람 확인을 시도했으나 반복. 일반 사용자 Chrome 로그인은 성공. 지원되는 정식 Chrome 자동 실행을 1회 비교했으나 로그인 진행이 막혀 그 변경은 폐기. 두 수집 시도 모두 종료; 운영 앱은 변경하지 않음.
- 실기 정상화는 미완료. 사이트 인증 지원 여부가 선행 조건이며 테스트 통과를 실제 수집 복구로 간주하지 않는다.
