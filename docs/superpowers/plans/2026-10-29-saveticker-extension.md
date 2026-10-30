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

### 1.0.4 초기화/재시도 보완
- 표시된 탭도 컨트롤 준비가 늦는 조건을 회귀 검사로 추가. 준비 대기 최대14초, 선택 시도2회, 달력 이동 총26회, 만료20초 전/90초 공통 제한.
- 일반 오류는 실제 시도 시각을 보존한 별도 retryAt으로30초 후 다시 가능. 아직 읽지 않은 월/화면을 실패한 리포트 재시도가 가리지 않도록 검사. 인증은 자동 재시도하지 않음.
- 확장37개, native/설치40개 통과. 독립 리뷰 추가 P1/P2 없음. 실제 전체 범위·PDF·frozen 복원·다중 주기 확인 진행 중.

### 1.0.5 표시·대기·소유 표시 보완
- 숨김 DOM 타이머 지연을 피하려고 고정2초 대기를 Native 연결로 유지되는 MV3 worker에서 응답한다. 같은 확장·정확한 소유 탭/token·enabled를 검증하고 탭당1개/총4개로 제한하며 중지·연결 해제 때 취소한다.
- 명시적 시작/재개·Chrome 시작·새 전용 창에만 소유 창을1회 표시한다. 일반 tick/MV3 idle 복원은 기존 창의 focus를 바꾸지 않는다. 최소화된 창은 쉰다.
- 실기 리포트 출처 변경이 주소의 hash를 제거함을 확인. 이미 발급한 같은 tab/token/epoch 뉴스 작업과 정확한 source query, 빈 hash일 때만 소유 표시를 복원한다. 전역 재소유·다른 탭/경로/hash/query 수용 없음.
- 확장44 tests 및 독립 리뷰 통과. frozen104 runtime/launcher 복원 완료; 다음 실제 Chrome 연결에서 frozen PID와 전체범위/PDF/여러주기 확인 필수.

### 1.0.6 장시간 실행·PDF 보완 (실기 대기)
- 1.0.5 frozen Native 프로세스가 AI 없이 5시간 이상 유지되고 초기 전체20개 stream·뉴스6회 갱신을 확인했다. 장시간 후 뉴스 전체 DOM 초과와 월 변경 지연 오류가 남았으며, 실제 PDF 첨부는0건이었다. 이를 완료로 간주하지 않는다.
- 뉴스100카드/프레임512KiB·세 프레임, 리포트 카드만, 상세256KiB 제한으로 누적 DOM 전송을 줄였다. 캘린더는 스크롤 없이 실제 월별 행/명시적 빈 상태를 기다리고 parser도 다른 달 행을 거절한다.
- 정적 same-site MAIN DOM 도우미가 nonce로 표시된 정확한 PDF 버튼 클릭 이후 최대15초간 same-origin blob 앵커를 전달한다. Chrome 다운로드 API가 반환한 ID만 ticket과 연결한다. 추가 Chrome 권한·쿠키·요청 관찰은 없다. 취소/중지/늦은 응답은 이전 작업을 저장하지 않아야 한다.
- 1.0.6 후보 Python62개(격리된 실제 Chromium blob 전달 포함), Node69개 통과. 독립 검토에서 발견한 이동 후 취소 조건도 발급된 작업 객체·탭·nonce·token·epoch로 검증하도록 보완하고 회귀 검사를 추가했다. frozen 재빌드·실제 PDF·전체주기 재검증은 아직 미완료다.

### 1.0.6 검증 결과 (2026-10-29)
- 최종 커밋 b8242f2: Node69개, 관련 Python62개, 전체 Python2644 passed/1 skipped(562.22s), ruff/diff check 통과. CI40883931408/test123482472679 통과(4m19s). 독립 리뷰의 PDF 이동 후 취소 P2 수정 후 추가 blocker 없음.
- macOS bundle/selfcheck·격리된 frozen Native20개 작업·합성1페이지PDF·EOF exit0·가짜 등록 복원 통과. 실제 Chromium 격리 프로필의 same-origin blob→extension downloads ID 전달 통과. 합성 데이터는 운영 저장소에 넣지 않았다.
- 설치 패키지에 extension-next-1.0.6, native-runtime/Trader-next-1.0.6.app으로 별도 준비했다. 운영 Trader.app·현재1.0.5 수집 실행 파일은 교체하지 않았다. Mac 잠금으로 중지/교체/사용자 Chrome 재로드·실제 PDF·전체주기 확인이 필요하여 PR80 draft를 유지한다.

### 1.0.6 실제 Chrome 검증 (2026-10-30 08:18~08:24 KST)
- 사용자 잠금 해제 후1.0.5 정상 중지·Native 종료를 확인하고1.0.6 frozen/확장으로 교체했다. 이전 버전은 별도 rollback으로 보존. 사용자 재로드·시작 후 popup 확장/수집기1.0.6 및 Chrome 자식 frozen PID34066 확인.
- 시작 이후20/20 stream이 error/auth 없이 partial로 갱신됐다. 뉴스4회 갱신, 리포트20, TOP5/지수5, 나머지순위20, 캘린더9월133/10월136/11월96/12월0/1월0. 빈 월은 실제 명시적 빈 화면을 확인했다.
- 기존 reader로 지수/순위·34개 근접 일정·뉴스를 읽었으나 최신 report excerpt 없음. 새 PDF0건이며 popup native_failed. 일반 수집 성공과 PDF 실패를 분리해 기록한다.
- 실제 isolated content→MAIN→candidate 경로를 추가 Chromium 합성 검사로 확인:3회 인증된 pace·정확한 버튼1회·page-origin/sourceWindow/nonce 일치·blob candidate 성공(11.01s). 실제 PDF 첫 실패를 다음 pdf_begin busy가 덮을 수 있는 진단 문제 보완 중이다.

### 1.0.7 PDF 첫 실패 진단 보완
- 첫 hook/candidate 실패 후 같은 ticket 유효 시간에 다음 pdf_begin을 요청하여 기존 원인을 busy로 덮던 동작을 RED→GREEN으로 수정했다. 첫 실패에서 멈추고 원인을 유지한다. Native begin/save 오류는 실제 고정 코드만 매핑하며 미확인 문자열은 고정 fallback으로 처리한다.
- 확장79개, 실제 content→MAIN Chromium 검사1개, ruff/diff 통과. 독립 리뷰 P1/P2 없음. Native Python은 변경하지 않았다.
- 수집 중지·Native 종료 후 확장 파일만1.0.7로 교체했다. 기존 frozen Native1.0.6 코드/권한은 유지. 사용자 Chrome 재로드 후 실제 최초 PDF 실패 코드를 확인해야 한다.

### 1.0.7 실제 PDF 진단 및1.0.8 후보
- 실제 popup 최초 실패는 url_unverified. 별도 일반 탭에서 실제 화면 클릭은 같은 사이트 /api/report/download의200 application/pdf와6.1MB 파일 완료를 확인했다. 쿠키/요청 헤더/응답 본문은 수집하지 않았고 페이지 한정 Network 진단은 종료했다. 수동 파일을 자동 수집 성공으로 세지 않는다.
- 격리 재현에서 anchor.dispatchEvent(MouseEvent) 경로가 click 전용 도우미를 우회함을 확인했다. 숨김/반응형 중복 버튼도 잘못 선택되거나 매칭이 실패했다. 실제 사이트 원인으로 단정하지 않고 재현된 지원 누락만 수정했다.
-1.0.8은 HTMLAnchorElement.prototype에만 dispatchEvent wrapper를 제한하고, 정확한 버튼 클릭 이후 same-origin blob만 전달한다. EventTarget 전역은 바꾸지 않으며 원래 상속 descriptor를 복원한다. 후보/버튼 불일치도 고정 진단 코드로 구분한다.
- 확장93개와 실제 isolated Chromium click/dispatch 두 경로(19.71s), ruff/diff 통과. 독립 리뷰 P1/P2 없음. frozen Native 코드는 변경하지 않았다. 빌드/실제 자동 PDF는 검증 대기.
- 진단 창이 수집 창을 완전히 가린 뒤 visible=false와 선택 오류 재발을 확인했다. 표시된 전용 창을 유지해야 하는 기존 제한이 남아 있으며, 일반 데이터 초기20개 성공만으로 가려진 창의 무인 동작을 보장하지 않는다.

### 창 가림 제약에 대한 사용자 질문 (2026-10-30)
- 현재 DOM 방식은 완전히 가림/최소화에서 화면 선택 오류가 재발했고1.0.8도 community 선택 오류가 남았다. 표시된 화면을 유지하는 임시 제한을 없앤 것으로 주장하지 않는다.
- 일반 Chrome content의 same-origin API 요청을 worker가 예약하는 방향을 읽기 검토했다. jsonfeed에 뉴스/list·detail, calendar/events, tickers/popular, market-panel, community/feed/ticker-rankings 응답 파서는 있다. 요청 method/query/body·page/sort/regular/month 계약과 추가 인증 헤더 필요 여부는 미확인이다.
- 구현 전 정상 UI 요청의 허용 경로·method·비밀이 아닌 매개변수를 확인해야 한다. Native에도 DOM 선택값을 꾸미는 대신 발급 작업별 API 요청 계약 검증을 별도로 추가해야 한다. API 방식도 Chrome 종료/동결/폐기/시스템 절전 동작을 실기로 검증하기 전 보장하지 않는다. 실험적 Chrome flag만으로 해결됐다고 주장하지 않는다.

### 1.0.9 파일명 원인 확정 및 후보 준비
-1.0.8에서 blob 전달/다운로드가 진행됐으나 파일명이 blob UUID로 저장돼 filename_unverified가 발생했다. 정상 Chrome 동작(default)과 임시 다운로드 디렉터리에서 실제 production worker·fake Native·합성 HTTPS로 A/B했다. 기존 empty onDeterminingFilename 콜백은 발급 파일명을 잃고 실패, 콜백 제거는 정확한 SaveTicker/<발급명>·일치 ticket/item/name Native 전달에 성공했다. 추가 파일명 hint는 최종 코드에 넣지 않았다.
- CDP allow/allowAndName도 정상 파일명 결정 경로를 우회하므로 검증 환경에서 제거했다. 테스트는 temp profile prefs와 default 동작을 사용한다. 사용자 Chrome의 해당 진단 설정 복원 명령은 CUA raw CDP에서 지원되지 않았으며, 실제 계정 검증 시 도구 연결 없는 정상 Chrome 재시작을 검토한다. 경로/ID/ticket/PDF 검증은 완화하지 않았다.
- 최종109 Node93개, 실제 isolated Chromium3개(27.57s), ruff/diff 및 macOS bundle/selfcheck 통과. 독립 검토 P1/P2 없음. Native Python 변경 없음. Chrome 다운로드 API 문서의 onDeterminingFilename 계약과도 확인했다: https://developer.chrome.com/docs/extensions/reference/api/downloads#event-onDeterminingFilename
- Mac이 다시 잠겨 사용자 해제를 요청했다. extension-next-1.0.9 및 native-runtime/Trader-next-1.0.9.app으로 후보만 준비하며 현재 설치본은 교체하지 않는다. 실제 계정의 자동 PDF 저장은 여전히 미검증이다.

### 사용자 요구 확정 및109 확장 적용
- 사용자 요구: 전용 창은 허용하되 뒤로 숨기거나 최소화해도 수집을 계속하고 싶다. 현재 DOM 방식의 가림 제약을 해결한 것으로 주장하지 않으며 API 요청 계약 검증을 다음 단계로 진행한다.
- 잠금 해제 후 사용자의 Chrome 조작과 자동 클릭이 충돌했다.109는 Native Python 변경이 없으므로 실행 중 Native를 교체하지 않고 외부 unpacked extension 디렉터리만 원자적으로 교체했다. extension-before-1.0.9를 rollback으로 보존하고 Native109 app 후보는 별도로 남겼다. Chrome 확장 재로드/시작 및 실제 PDF 검증 대기.

### 1.0.10 재시도 안전 보완과 현재 차단 상태
- 사용자 경고에 따라 실패한900초 리포트가30초 뒤 반복되던 경로와 정상 주기 조건이 backoff를 무시하던 조건을 RED→GREEN으로 수정했다. 정상 주기를 유지하고 실패 대기는 max(정상 주기,60초)부터 max(정상 주기,15분)까지 지수 증가한다. 현재plan의128개 이하 stream·유한 시각·유효 범위/실패횟수를 검증해 Chrome 로컬 저장·재시작 복원한다.
- content/Native의 인증/보안 확인은 Native snapshot 실패 전에도 전체4lane를 먼저 중지·저장한다. 이후 재시작/일반 시작에서도 탭 접근/후속수집/PDF를 건너뛰고 명시적 재개만 허용한다. 실제 관측한 managed challenge title/표시h1을 감지하되 일반 Cloudflare 기사에는 반응하지 않는다.
- Node103개 및 독립 검토 통과. 부모의 최종 격리 Chromium3개22.08s 통과. bridge-only mock worker는 Native 연결이 없는 타이머 동작 때문에 bounded MAIN lease가 만료됐고, 해당 transport 검사는 빠른 비동기 mock ack로 한정했다. 별도 production-worker 검사는 실제 pacing·파일명·Native ticket 전달을 계속 검증한다. 운영 MAIN TTL/worker pacing은 완화하지 않았다.
- 격리된 headful Chromium을 CDP no_defaults로 연결해 실제 minimized+document hidden 상태의 same-origin fetch3회/합성 쿠키 전달 성공을 확인했다(9.28s). 자동화 기본 focus emulation을 제거한 시험이며 실제 사이트 인증/API를 증명하지 않는다. 설계 초안: docs/superpowers/specs/2026-10-30-saveticker-minimized-api-design.md.
- 사용자가 보안 확인 반복을 재확인했다. 이후 실제 사이트 요청/새로고침을 멈추고 임시 검증 탭만 닫았다. 실제 요청 계약·실제 자동PDF·전체 범위의 숨김 운영은 미완료다. 수집기는 중지한 채110 파일만 적용하며 운영 거래 앱은 변경하지 않는다.
