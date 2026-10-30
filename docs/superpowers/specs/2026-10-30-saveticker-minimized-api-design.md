# SaveTicker 숨김·최소화 수집 설계 초안

상태: 정상 UI 요청 관찰을 반영한 구현 전 설계. 2026-10-30 일반 Chrome에서 정상 목록과 JSON/PDF 200 응답을 확인했다. 숨김 상태의 실제 API 수집 성공을 주장하지 않는다.

## 요구사항과 선택 방향
사용자는 전용 Chrome 창을 허용하지만 뒤로 보내거나 최소화해도 기존 전체20개 범위를 AI 없이 수집하길 원한다. Chrome은 실행 중이고 컴퓨터는 깨어 있어야 한다. 현재 DOM 클릭/화면 갱신 방식의 표시 제약을 해결한 것으로 취급하지 않는다.

기존 일반 Chrome의 인증 상태를 유지하면서 확장 worker가 일정에 따라 content script의 동일 출처 데이터 요청을 실행하는 방향을 우선 검증한다. Chrome 실험 옵션으로 화면을 강제로 활성화하는 방식은 버전 의존성과 사용자 작업 간섭 때문에 운영 해법으로 삼지 않는다.

## 확인된 근거
- 기존 jsonfeed에는 뉴스/list·detail, calendar/events, tickers/popular, market-panel, community/feed/ticker-rankings 응답 파서가 있다. 아래 관찰 표의 GET 요청을 확인했다. 정규장 요청·뉴스 detail은 미확인이며 프런트엔드 URL로 요청 계약을 추정하지 않는다.
- 격리된 headful Chromium에서 자동화 도구의 기본 focus/timer 가정을 제거한 뒤 창 minimized·document.visibilityState hidden을 확인하며 worker 예약 same-origin fetch3회 및 합성 쿠키 전달이 성공했다. phase=capability_only_not_actual_saveticker, elapsed9.28s. 실제 SaveTicker 인증/API·장시간·탭 폐기·Chrome 종료는 증명하지 않는다.
- PDF의 클릭/합성 dispatchEvent 및 발급 파일명/다운로드ID/Native ticket 연결은 격리 Chromium에서 검증했다. 실제 계정의 자동 PDF 첨부는 아직 확인되지 않았다.

## 요청·저장 계약
1. 정상 UI가 보내는 허용 경로의 method와 공개 매개변수를 소량·순차 관찰한다. 인증정보·쿠키·헤더 원문은 출력하거나 Native로 내보내지 않는다. 보안 확인 중에는 관찰을 위한 새 요청/새로고침을 하지 않는다.
2. 확인된 읽기 요청만 stream별 고정 계약으로 만들고 Native 발급 job에 묶는다. 기존 DOM selection 값을 꾸며 API 증거로 대체하지 않는다. Native가 endpoint·method·매개변수·요청 기간/기준·최종 출처·응답 크기/형식을 검증한 API snapshot 계약을 별도로 사용한다.
3. worker가 요청을 직렬화하고 준비된 동일 출처 content에서 브라우저 관리 인증으로 fetch한다. 작업마다 탭 활성화/포커스/스크롤/React 화면 갱신에 의존하지 않는다. 운영 요청 주기는 실제 허용 조건 확인 후 정하고, 검증 중에는 요청을 몰아 보내지 않는다.
4. 기존 parser/Store와 SQLite·JSONL·latest·attachment 파일 계약을 유지한다. 원래 전체 뉴스/본문·리포트/PDF·TOP5·지수/상승/하락·증시 거래대금/상승/하락 실시간/정규장·5개월 캘린더를 각각 검증한다. 미확인 요청을 추정하거나 DOM 방식으로 몰래 대체해 완료로 처리하지 않는다.
5. 리포트 목록/상세/PDF 요청 계약도 별도 확인한다. 아래 표의 GET 목록·상세·inline PDF 계약을 확인했다. 다운로드 모드 및 Native 저장까지의 자동 경로는 별도 검증한다.

## 중단·복구
- 보안 확인/인증·접근 거부는 사이트 전체 자동 작업을 중지하고 상태를 저장한다. 자동 새로고침이나 보안 확인 자동 해결을 하지 않는다. 직접 확인 후 명시적으로 재개한다.
- 일반 실패는 정상 주기보다 빨리 재시도하지 않으며 지수 대기를 적용한다. 예약 시각/실패 횟수는 worker 재시작 후에도 검증해 복원한다.429와 Retry-After가 확인되면 서버 대기 조건을 존중하는 별도 host cooldown이 필요하다.
- 실제 탭 동결/폐기 및 Chrome 재시작 복구는 별도 실기로 검증한다. Chrome 종료·컴퓨터 절전 중 수집을 보장하지 않는다.

## 완료 기준
확정된 요청 계약의 회귀 검사, 잘못된 출처/기준/기간/응답 거절, 재시작/보안 중단 상태 복원, 실제 계정20개 항목 및 PDF 저장, 전용 창 숨김·최소화 상태의 여러 주기 갱신과 포커스 미탈취를 확인한다. 그 전에는 PR을 운영 완료로 머지하지 않는다.

## 접근 방법 비교
- 선택 방향: 일반 Chrome content script가 같은 출처의 확인된 읽기 API를 요청한다. 로그인은 브라우저가 관리하고 worker는 예약·직렬화·중단을 맡는다. 최소화해도 화면 렌더링에 의존하지 않지만 API 응답 기준과 폐기 복구를 검증해야 한다.
- 기존 DOM 클릭: 이미 20개 범위를 수집했으나 숨김 상태 선택·갱신 오류가 재발했다. 목표 운영 방식으로 사용하지 않는다.
- 별도 자동 로그인 브라우저: 보안 확인이 반복되어 안정적인 인증을 확보하지 못했다. 새 인증 우회나 보안 기능 해제를 시도하지 않는다.

## 2026-10-30 정상 UI 관찰
쿠키·인증 헤더·원시 요청 기록을 파일로 보관하지 않았다. 아래는 읽기 데이터 경로와 공개 매개변수만 정리한 결과다. 계정별 groupId 값은 기록하지 않고 고정값으로 구현하지 않는다.

| 경로 (모두 GET) | 관찰된 매개변수 | 확인 범위 |
|---|---|---|
| /api/news/list | page=1, page_size=20, sort=created_at_desc, label_group=1, label_name=1 | 전체 뉴스 UI 선택 시 요청. 상세/페이지 2는 미확인 |
| /api/report/list | page=1, page_size=20 | 200 JSON, reports 20개, total_count/page/page_size |
| /api/report/detail | id=<표시된 report ID> | 200 JSON |
| /api/report/download | url=/api/uploads/documents/<서버 제공 ID>, mode=inline | 상세 화면이 표시한 PDF, 200 application/pdf. 임의 주소 생성 금지 |
| /api/stocks/api/v1/market-panel | 없음 | 200 JSON, marketStatus=open, items 7개. regular.price/changePercent 포함 |
| /api/stocks/api/v1/community/feed/ticker-rankings | limit=5 | 200 JSON, rankings 5개, regular/extended 필드 포함 |
| /api/stocks/api/v1/tickers/popular | tab=gainers 또는 losers, groupId=<현재 그룹>, excludeRisky=true | 커뮤니티 카드. 200 JSON, items 20개, basis=live, session=open |
| /api/stocks/api/v1/tickers/popular | tab=turnover, groupId=<현재 그룹> | 증시 거래대금. 200 JSON, items 20개, basis=live, excludeRisky=false |
| /api/calendar/events | start_date=2026-09-30, end_date=2026-11-01 | 10월 UI에서 200 JSON, events 152개. 달 경계에 여유 날짜가 포함됨 |

커뮤니티에서 맵 보기 설정 때문에 첫 로딩 후 지수/상승/하락 버튼이 사라지는 것도 관찰했다. 카드 보기 전환 후 해당 버튼이 표시됐다. 이 변화가 과거 모든 DOM 오류의 원인이라고 단정하지 않는다.

현재 시장 open 상태에서 정규장 기준 버튼은 disabled였다. 이를 강제 활성화하거나 regular 요청 매개변수를 추정하지 않는다. 증시 상승/하락 선택은 이미 읽은 결과를 재사용해 새 popular 요청이 관찰되지 않았으므로 두 화면의 필터가 같다고 단정하지 않는다.

## 구현에 적용할 결정
- 1.0.10 운영 파일과 수집 중지 상태는 유지한다. 새로운 API backend는 테스트 가능한 별도 계약으로 추가하고 실제 전체 범위 검증 전에 기본값으로 전환하지 않는다.
- 데이터 요청은 하나씩 실행하며 기존 정상 수집 간격보다 단축하지 않는다. PDF도 같은 host 직렬화/접근 중단을 따른다. 네트워크 429는 Retry-After와 로컬 backoff 중 더 늦은 시각을 저장해 적용한다.
- Native 발급 job은 stream, endpoint, 허용 query, 기간/시세 기준, 만료 시각을 포함한다. Native는 응답 source/status/content-type/크기와 stream별 필수 필드를 검증한다. DOM selection을 API 근거로 대신 사용하지 않는다.
- groupId는 개인 설정 식별자이며 순위 자체에 필요한지 확인 전 계약에 고정하지 않는다. 필요하다면 브라우저 안에서 정상 UI의 현재 그룹을 확인하는 제한된 초기화 계약으로 처리한다. 인증·개인 설정 전체 응답을 Native에 전달하지 않는다.
- API snapshot에는 허용된 수집 필드만 전달한다. 리포트 author/profile, calendar notification 상태, inGroup 같은 부수 계정 필드는 보관하지 않는다. 본문은 기존 정책과 크기 한도를 적용한다.
- calendar는 관찰한 여유 날짜 범위를 요청한 뒤 월별 저장을 필터링한다. 기존 파서의 날짜 문자열 절단이 사용자 현지 시간 경계와 맞는지 회귀 검사한다. 월이 없는 빈 응답으로 전체 수집을 성공 처리하지 않는다.
- index/인기 순위는 응답 항목 수를 하드코딩해 정상이라 간주하지 않는다. stream별 종목·basis·필터가 증명되지 않으면 미지원/검증 대기로 남기며 다른 기준으로 대체하지 않는다.
- PDF는 서버가 제공한 문서 경로만 허용한다. 다운로드 ID·Native ticket·발급 파일명·실제 PDF 검증은 유지한다. 새 API 경로가 기존 15초 DOM helper의 가시성 조건에 의존하지 않도록 별도 설계/검사한다.

## 구현 계획에 포함할 남은 탐색
1. 뉴스 상세 GET과 상세 응답에서 원문/번역/제목만 읽는 매핑 확인.
2. 정규장 선택이 가능한 정상 시점의 popular 요청 계약 확인. 그 전에는 해당 stream 전환을 막는다.
3. market-panel 7개 항목에서 기존 지수 5개를 고르는 기준, TOP5 regular/extended 값의 정확한 의미 확인.
4. groupId 필요성과 위험주 제외 필터를 UI와 API 응답으로 대조. 증시/커뮤니티 차이를 유지한다.
5. 리포트 상세 응답에서 PDF 문서 경로를 얻는 필드와 다운로드 모드 확인.
6. content worker의 숨김 상태 실제 요청, Native snapshot 저장, 새 PDF 자동 저장을 한 항목부터 검증한 후 범위를 확장한다.
7. 모든 20개 범위와 PDF를 숨김/최소화 여러 주기에서 통과해야 기본 backend 전환 및 draft 해제를 검토한다.
