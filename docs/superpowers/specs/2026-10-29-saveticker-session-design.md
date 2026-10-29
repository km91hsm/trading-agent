# SaveTicker 로그인 복구

사용자가 승인한 수정: 수집용 로그인 세션을 재사용하고, 재확인이 필요하면 창을 유지해 직접 완료한다.

## 접근
- 매번 새 로그인: 현재 장애를 반복하므로 제외.
- 작업별 영구 Chromium 프로필: 프로필 잠금과 작업별 재로그인이 남아 제외.
- 선택: 공유 Playwright storage state를 로그인 잠금 안에서 읽고, 인증 성공 후 원자 저장한다. 쿠키·localStorage·IndexedDB를 재사용한다. 각 작업의 페이지·수집 처리는 계속 분리한다.

## 동작과 제약
headed 모드에서 VerificationRequired·LoginRequired·로그인 단계 Playwright TimeoutError를 최대 600초 기다린다. 사용자가 새로고침·로그인·재확인을 마쳐 프로필 메뉴가 보이면 이어서 수집한다. 자동 새로고침이나 인증 제출은 반복하지 않는다. 대기 상태를 runner에 기록하고 종료 신호를 확인한다. headless·기한 초과는 기존 verification 차단을 유지한다.

인증 상태는 수집 폴더 runtime/auth/storage-state.json에 0600으로 저장하며 로그·데이터 export에 넣지 않는다. 인증 실패 시 기존 상태를 덮지 않는다. 기존 거래 데몬·주문·계좌 모드는 변경하지 않는다. 앱 배포 전 실행 중 세션과 런타임 데이터를 보존한다.

실기 발견(2026-10-29): 재확인 문구 대신 login_submit TimeoutError가 나기도 하므로 로그인 단계 시간 초과도 같은 수동 대기로 전환한다. 접근 거부(AccessDenied)는 대기·우회하지 않는다.

실기 결과: Chrome for Testing에서 사람이 직접 확인해도 Cloudflare 확인이 반복됐다. 사용자 일반 Chrome에서는 로그인 성공. 설치된 Chrome을 자동 수집용으로 한 번 실행해도 로그인 수동 대기로 진입하여 호환성 전환은 채택하지 않았다. 자동 수집 정상화는 아직 확인되지 않았다. 사이트 측 자동화 브라우저 인증 지원 여부 확인이 필요하다.
