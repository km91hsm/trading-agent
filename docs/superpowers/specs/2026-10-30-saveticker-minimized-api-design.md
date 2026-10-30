# SaveTicker 숨김·최소화 수집 설계 초안

상태: 요청 계약 확인 전 초안. 실제 사이트의 보안 확인이 반복되어 추가 요청을 멈췄다. 이 문서는 실제 API 수집 성공을 주장하지 않는다.

## 요구사항과 선택 방향
사용자는 전용 Chrome 창을 허용하지만 뒤로 보내거나 최소화해도 기존 전체20개 범위를 AI 없이 수집하길 원한다. Chrome은 실행 중이고 컴퓨터는 깨어 있어야 한다. 현재 DOM 클릭/화면 갱신 방식의 표시 제약을 해결한 것으로 취급하지 않는다.

기존 일반 Chrome의 인증 상태를 유지하면서 확장 worker가 일정에 따라 content script의 동일 출처 데이터 요청을 실행하는 방향을 우선 검증한다. Chrome 실험 옵션으로 화면을 강제로 활성화하는 방식은 버전 의존성과 사용자 작업 간섭 때문에 운영 해법으로 삼지 않는다.

## 확인된 근거
- 기존 jsonfeed에는 뉴스/list·detail, calendar/events, tickers/popular, market-panel, community/feed/ticker-rankings 응답 파서가 있다. 실제 요청 method/query/body와 stream별 조건은 미확인이다. 프런트엔드 label_group=7 주소를 API 매개변수로 추정하지 않는다.
- 격리된 headful Chromium에서 자동화 도구의 기본 focus/timer 가정을 제거한 뒤 창 minimized·document.visibilityState hidden을 확인하며 worker 예약 same-origin fetch3회 및 합성 쿠키 전달이 성공했다. phase=capability_only_not_actual_saveticker, elapsed9.28s. 실제 SaveTicker 인증/API·장시간·탭 폐기·Chrome 종료는 증명하지 않는다.
- PDF의 클릭/합성 dispatchEvent 및 발급 파일명/다운로드ID/Native ticket 연결은 격리 Chromium에서 검증했다. 실제 계정의 자동 PDF 첨부는 아직 확인되지 않았다.

## 요청·저장 계약
1. 정상 UI가 보내는 허용 경로의 method와 공개 매개변수를 소량·순차 관찰한다. 인증정보·쿠키·헤더 원문은 출력하거나 Native로 내보내지 않는다. 보안 확인 중에는 관찰을 위한 새 요청/새로고침을 하지 않는다.
2. 확인된 읽기 요청만 stream별 고정 계약으로 만들고 Native 발급 job에 묶는다. 기존 DOM selection 값을 꾸며 API 증거로 대체하지 않는다. Native가 endpoint·method·매개변수·요청 기간/기준·최종 출처·응답 크기/형식을 검증한 API snapshot 계약을 별도로 사용한다.
3. worker가 요청을 직렬화하고 준비된 동일 출처 content에서 브라우저 관리 인증으로 fetch한다. 작업마다 탭 활성화/포커스/스크롤/React 화면 갱신에 의존하지 않는다. 운영 요청 주기는 실제 허용 조건 확인 후 정하고, 검증 중에는 요청을 몰아 보내지 않는다.
4. 기존 parser/Store와 SQLite·JSONL·latest·attachment 파일 계약을 유지한다. 원래 전체 뉴스/본문·리포트/PDF·TOP5·지수/상승/하락·증시 거래대금/상승/하락 실시간/정규장·5개월 캘린더를 각각 검증한다. 미확인 요청을 추정하거나 DOM 방식으로 몰래 대체해 완료로 처리하지 않는다.
5. 리포트 목록/상세/PDF 요청 계약도 별도 확인한다. 정상 UI에서 /api/report/detail, /api/report/download 경로와 PDF200응답은 보았지만 method/매개변수는 아직 확보하지 않았다.

## 중단·복구
- 보안 확인/인증·접근 거부는 사이트 전체 자동 작업을 중지하고 상태를 저장한다. 자동 새로고침이나 보안 확인 자동 해결을 하지 않는다. 직접 확인 후 명시적으로 재개한다.
- 일반 실패는 정상 주기보다 빨리 재시도하지 않으며 지수 대기를 적용한다. 예약 시각/실패 횟수는 worker 재시작 후에도 검증해 복원한다.429와 Retry-After가 확인되면 서버 대기 조건을 존중하는 별도 host cooldown이 필요하다.
- 실제 탭 동결/폐기 및 Chrome 재시작 복구는 별도 실기로 검증한다. Chrome 종료·컴퓨터 절전 중 수집을 보장하지 않는다.

## 완료 기준
확정된 요청 계약의 회귀 검사, 잘못된 출처/기준/기간/응답 거절, 재시작/보안 중단 상태 복원, 실제 계정20개 항목 및 PDF 저장, 전용 창 숨김·최소화 상태의 여러 주기 갱신과 포커스 미탈취를 확인한다. 그 전에는 PR을 운영 완료로 머지하지 않는다.
