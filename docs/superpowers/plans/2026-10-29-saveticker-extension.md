# SaveTicker AI-independent Chrome Extension Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** 기존 전체 수집 범위를 일반 Chrome+Native Messaging으로 AI 없이 실행한다.
**Architecture:** 확장 4개 전용 탭 → v1 Native JSON framing → 기존 parser/Store. 인증은 사용자 Chrome이 유지한다.
**Tech Stack:** Chrome MV3, nativeMessaging, Python 3.12, node:test, pytest.
**Spec:** docs/superpowers/specs/2026-10-29-saveticker-extension-design.md

## Global Constraints
- 인증정보·브로커·LLM 호출 없음. 설치 전 사용자 승인. source/env/원장 보존.
- 뉴스·리포트/PDF·TOP5·지수/상승/하락·거래대금·5개월 캘린더 전부 계획·테스트 포함.
- 인증·접근 거부는 멈춤. 가상 목록·빈 월·본문 미수집은 partial로 기록.

## Review Focus
- 잘못된 extension origin과 프레임/경로를 거절하고 payload를 에러에 노출하지 않는가.
- Native EOF/Chrome 재시작/worker 재시작에 중복 탭·중복 작업이 없는가.
- regular·tab·month가 맞지 않으면 이전 응답을 다른 stream에 저장하지 않는가.
- 새 PDF만 Downloads/SaveTicker에서 읽고 원본/본문 상태를 구분하는가.
- 구버전 앱의 Playwright 재기동과 설치/제거 복원이 안전한가.

### Task 1: Native host protocol and persistence
Files: src/trader/saveticker/native.py, tests/saveticker/test_saveticker_native.py.
Interface: serve(root: Path, extension_id: str, origin: str, stdin=None, stdout=None); handle_message(root: Path, message: dict) -> dict; build_plan(now=None) -> dict.
- [x] Framing·origin·스키마·월/regular·HTML/JSON/본문/PDF·멱등/asof 실패 테스트 → 최소 구현 → 통과.
- [x] 독립 리뷰·지적 수정.

### Task 2: Chrome extension scheduler and UI collectors
Files: extensions/saveticker/** (only).
Interface: Native Messaging host com.trader.saveticker; v1 protocol exactly spec. Exports pure/testable planner/collector functions; worker/background wires Chrome APIs.
- [x] 모든 stream/lifecycle/security/선택 확인/scroll·detail·PDF 제한의 실패 테스트 → 최소 구현 → node --test 통과.
- [x] 독립 리뷰·지적 수정.

### Task 3: Installer, CLI, backend integration and packaging
Files: src/trader/saveticker/bridge_install.py, cli.py, automation.py, src/trader/__main__.py, packaging/trader.spec, web/src/saveticker*, docs/bundle-usage.md, docs/TODO.md, tests for integration; existing Playwright test cleanup.
- [x] 준비/설치계획·등록·uninstall복원·기존app억제·네이티브진입·번들리소스·UI 상태 실패 테스트 → 구현 → 통과.
- [x] 실제 홈을 건드리지 않은 임시 경로 설치/왕복/종료 검증(source·frozen 프로세스, 20개 작업·PDF).
- [ ] 전체 Python·웹·JS·bundle gate·전체 독립 리뷰·PR/CI.
- [x] 패키지/권한/변경 내용을 제시하고 사용자 설치 승인. Native host 등록·기존 수집기 전환 완료; chrome:// 제어 정책 거부로 확장 로드만 사용자 직접 진행.
- [ ] 승인 후 Chrome 실제 전체 범위·AI 없이 여러 tick·파일 갱신 확인(승인 전은 미완료 유지).

## 검증 기록
- Python 전체: 2,639 passed, 1 skipped (569.69s); ruff·diff check 통과.
- 웹: 39 files/270 tests, typecheck/build 통과. 오류 배지 후속 수정은 해당 4개 재검증 통과.
- 확장: 23 Node tests. 독립 리뷰 후 뉴스 상단 초기화·실제 새 뉴스 버튼·오류 표시 보완.
- macOS bundle build/selfcheck 완료. frozen host를 임시 HOME·공백 경로에서 별도 실행: 20개 stream 요청/상태 저장, 합성 뉴스·시장·리포트/PDF, EOF exit 0, 등록 복원. 빈 합성 community/calendar는 항목 수 0으로 남겼으며 실제 사이트 결과로 주장하지 않음.
- 설치 패키지: 운영 root의 runtime/chrome-extension-20261029 (자체 runtime 포함). 실제 전체 사이트 수집·Chrome 재시작·Windows 실기는 아직 미확인.

### 실기 후 보완
- Chrome 등록/사용자 확장 로드·시작 완료. 실제 수집: 뉴스 7, TOP5 5, 시장 6개 기준 각20, 캘린더 9월133/10월137/11월96. 오류: 커뮤니티 기준 선택·리포트·빈 12월/1월. 원래 코드 CI 통과(6m51s).
- 새 탭 URL 없는 시작 조건과 미종료 작업을 회귀 테스트로 수정. 커뮤니티 실제 DOM CSS 선택·비활성 동등성 표시 지원, 실행 버전 진단 추가(확장1.0.1). 후속39 native/install·28 Node 통과.
- 후속 bundle/CI 및 업데이트 후 실제 전체 주기 재검증 진행 중. 완전 수집/운영 완료는 아직 주장하지 않음.

### 1.0.3 검증 진행
- 숨겨진 탭의 실제 초기화 부족을 비교 진단으로 확인. 전용 unfocused 창·소유 탭만 이동·순차 활성화·최소화 대기 구현. 독립 리뷰 후 초기 Tab metadata fallback을 실제 URL이 둘 다 없을 때만 사용하도록 수정.
- Node 32 tests, Native 26 tests 통과. runtime 비교 진단은 본문/선택값 없이 boolean만 저장하는 회귀 검사 포함.
- 로컬 원인 확인에는 임시 standalone Python launcher를 사용했다. 완료 전 frozen runtime으로 복원하고 두 주기 이상 갱신 확인 필수.
