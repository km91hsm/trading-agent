# SaveTicker Collection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 전체 SaveTicker 화면의 변경분을 인증 정보 없는 로컬 파일로 수집한다.
**Architecture:** DOM browser adapter → validated capture → SQLite revision store → JSONL/latest/status export. 독립 프로세스이며 거래 실행과 분리한다.
**Tech Stack:** Python 3.12, Playwright optional extra, SQLite, Typer, pytest.
**Spec:** docs/superpowers/specs/2026-10-25-saveticker-design.md

## Global Constraints
- 저장 시각 UTC ISO8601, 파일명에 콜론 금지, pathlib, 루트 platform/paths.
- secrets/saveticker.env만 계정 원본, 출력에 값·쿠키·폼 없음.
- 크로스플랫폼 Python, 테스트 tmp_path와 오프라인 fixtures.
- 모든 요청 동작 간 최소 2초, 실시간은 목표 주기이며 partial·overrun을 보고.

## Review Focus
- 재시작과 수정: 동일 데이터는 중복 생성하지 않고 수정은 revision.
- 로그인 만료·빈 DOM: 기존 성공 데이터를 최신으로 덮지 않는다.
- 가상 목록: 스크롤 중 사라진 행도 누적하며 한도 도달을 partial로 표시.
- 캘린더: 연도 경계·예정 시간·발표값 수신 시간 혼동 금지.
- 분석 조회: asof 이후 수신·발행 데이터 제외, ticker 정확 매칭.

### Task 1: Capture·저장·조회
Files: sources/saveticker/{models,store}.py, tests/saveticker/test_saveticker_store.py
- [x] 실패 테스트: 중복, 수정, 재시작, partial/failure latest 보존, asof 필터.
- [x] SQLite와 원자적 JSONL/latest/status 구현.
- [x] 테스트·린트 검증.

### Task 2: DOM와 브라우저 스케줄러
Files: sources/saveticker/{dom,browser,service}.py, tests/saveticker/test_saveticker_service.py
- [x] 실패 테스트: DOM 파서, 전체 스트림, calendar months, 가상 목록·수집 한도·실패 격리.
- [x] 역할·DOM 선택자 기반 전체 탭/스크롤/상세 수집, 로그인·주기·중단 구현.
- [x] 오프라인 검증 + 실제 브라우저에서 선택자 확인.

### Task 3: CLI·보안 설정·문서·최종 검증
Files: sources/saveticker/cli.py, cli.py, pyproject.toml, uv.lock, docs/saveticker-collection.md, docs/TODO.md
- [x] 실패 테스트: 잘못된 인자, import·status·query, optional dependency·credentials 누락.
- [x] run(자동 로그인)/import/status/query/export 명령과 설치·파일 형식 문서.
- [x] 전체 pytest·ruff, 라이브 수집 가능한 범위 확인, 미검증 범위 명시.

## 실행 결정 기록
- 사용자 추가 승인에 따라 계정 파일을 사용하고 브라우저 세션 파일은 저장하지 않는다.
- headless는 HTTP 403, headed 자동 로그인 성공으로 일반 창을 기본값으로 정했다. 창과 실행 프로세스가 유지돼야 수집된다.
- 핵심 전체 목록은 30초, 중복 출처/카테고리 검증·정규장·캘린더는 300초 목표로 분리했다. 실제 간격은 처리량에 따라 길어지므로 상태 시각을 함께 제공한다.
- 시점 조회는 최신 미완성 기사보다 그 시점 알려진 완성 본문을 우선하며 원래 수신 시각을 반환한다. 잘못 최신이라고 판단하지 않게 하기 위한 선택이다.
- 고유 ID 미노출 화면은 해시 ID를 사용한다. 동일한 작성자·본문이나 같은 출처·제목이 반복되면 충돌할 수 있다는 제한을 문서화했다.

검증 완료(2026-10-25): pytest 1,010 passed / 1 skipped, 새 수집기 테스트 49 passed, ruff 통과. 리뷰의 본문 가림·댓글 예산 기아·종일 일정 누락과 실기의 사이드 패널 혼입·달력 전체 범위/필터 충돌을 회귀 테스트로 수정했다. Windows와 전체 과거 완전 수집은 TODO의 운영 검증 항목으로 남긴다.
