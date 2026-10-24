# SaveTicker 브라우저 수집

뉴스 출처·카테고리, 커뮤니티 지수/상승/하락 및 글·열린 댓글, 증시 거래대금/상승/하락, 캘린더 월별 일정을 로컬에 저장한다. 거래·LLM 실행은 하지 않는다.

## 설치와 계정

```text
uv sync --extra browser
uv run playwright install chromium
```

`secrets/saveticker.env`에 아래 이름으로 계정을 저장한다. 실제 값은 이 문서·명령 인자·로그에 넣지 않는다.

```dotenv
SAVETICKER_EMAIL='이메일'
SAVETICKER_PASSWORD='비밀번호'
```

이 파일은 `.gitignore`의 `secrets/*`로 제외된다. macOS/Linux는 소유자 전용(0600)으로 두고 Windows는 사용자 전용 폴더 접근 권한을 사용한다. 로그인 세션은 프로세스 메모리에만 유지하며 브라우저 프로필·쿠키·trace 파일은 저장하지 않는다. 비밀번호는 dotenv 보간 없이 읽어 `$`가 들어간 값도 그대로 사용한다.

## 실행

```text
uv run trader saveticker run --headed --once
uv run trader saveticker run --headed
uv run trader saveticker status
uv run trader saveticker query --ticker NVDA --limit 50
uv run trader saveticker query --asof 2026-10-25T00:00:00+00:00
uv run trader saveticker export
```

기본은 창이 있는 브라우저(`--headed`)다. 이 환경에서는 headless 접근이 HTTP 403으로 거절되어, 일반 창 모드를 사용한다. `--headless`는 해당 환경에서 접근이 확인된 경우만 선택한다. 일반 브라우저에서 인증·접근이 가능한 경우만 수집하며 보안 확인·CAPTCHA를 우회하지 않는다. 실패 시 `runner.json`의 고정된 단계 이름·오류 타입으로 확인한다. 비밀번호/이메일/쿠키/전체 예외 메시지는 출력하지 않는다.

캘린더 기본 범위는 KST 현재 지난달부터 앞으로 3개월(양 끝 월 포함). `--start-month YYYY-MM --end-month YYYY-MM`으로 최대 25개월을 선택할 수 있다. 뉴스·커뮤니티는 `--max-scrolls`(기본 20), 본문·댓글은 `--max-details`(기본 20)로 한 번의 처리량을 제한한다. 각 스크롤에서 가상 행을 누적한다. 상시 실행은 각 스트림의 첫 수집 뒤에는 스크롤을 최대 2회로 줄여 최근 목록을 우선한다. 과거 전체 본문은 `--once --max-scrolls 500 --max-details 500` 같은 별도 배치로 처리하고, 한도에 닿으면 계속 partial로 남는다. 한도 도달·끝 여부 미확인·본문 대기는 `partial`로 표시한다. 전체 사이트의 과거 글을 다 받았다고 간주하지 않는다.

정규장 기준과 실시간 기준을 별도 스트림으로 수집한다. 위험주 제외 필터는 수집 화면에서 해제한다. 휴장 중 비활성 필터는 사이트에 표시된 실제 기준을 `basis`에 남긴다. 사용자 관심종목·계정 설정·댓글 등록은 변경하지 않는다.

## 파일

기준 폴더는 `$TRADER_HOME/state/external/saveticker/`다. TRADER_HOME이 없으면 현재 프로젝트의 platform/paths 규칙을 쓴다.

```text
feed.sqlite3                  변경 이력 원본(WAL)
2026-10-25/news.jsonl          UTC 수신일 기준 뉴스 변경분
2026-10-25/community.jsonl     글·댓글·지수/상승/하락 변경분
2026-10-25/market.jsonl        순위·시세 변경분
2026-10-25/calendar.jsonl      일정·발표값 변경분
latest/<stream>.json          스트림별 최근 유효 캡처
status.json                   시도·유효 수집 시각, 상태, 처리 소요·목표 주기
runner.json                   pid, 실행 상태, 현재 처리 스트림, 스케줄 지연
collector.lock                단일 수집기 잠금
```

JSONL은 한 줄이 변경 버전 하나이며 `{revision, stream, received_at, item}`이다. `view`에는 출처·카테고리·탭·조회 월을 남긴다. `item`에는 `id, kind, title, body, tickers, published_at, scheduled_at, scheduled_date, price, change_pct, turnover, rank, forecast, previous, actual, views, likes, comment_count, parent_id, detail_status, basis`가 들어간다. 해당 없는 값은 null/빈 문자열이다.

수집 시각은 UTC, 화면 시간대는 Asia/Seoul로 고정한다. 상대시각만 있는 목록은 `published_at=null`이며 추정한 절대시각을 만들지 않는다. 캘린더 `scheduled_at`은 미래여도 유효한 예정 정보다. 종일·시각 미정 일정은 `scheduled_at=null`과 `scheduled_date`로 보존한다. 발표값의 알려진 시각은 `received_at`이며 일정 시각으로 과거에 알았다고 처리하면 안 된다.

스트림 내 같은 행과 상대시각만 달라진 행은 중복 버전을 만들지 않는다. 수정·조회수·좋아요·순위 변화는 새 버전이다. 사이트가 고유 ID를 노출하지 않는 화면에서는 제목+출처 또는 작성자+본문의 해시를 사용하므로 동일 제목/동일 작성자·본문 글이 겹칠 가능성이 있다. 원문 고유 ID가 확인되면 우선하도록 확장할 수 있다.

SQLite 커밋 후 JSONL과 최신 파일을 원자적으로 교체한다. 파일 생성 중 중단돼도 `export`로 원본에서 복구한다. 오류/로그인 만료/빈 응답이 나면 기존 유효 최신 파일을 지우지 않으며 반드시 `status.json`의 시각·상태를 함께 읽는다. `query --asof`는 수신 시각과 발행 시각이 asof 이하인 버전만 반환한다. 다른 탭의 본문 대기 행이 기존 본문을 가리지 않도록 마지막 확인된 본문 버전을 원래 수신 시각과 함께 우선한다. 순위·캘린더는 그 시점 마지막 유효 화면에 실제 있는 항목만 반환한다. 기존 `00_inputs.json`을 자동 변경하지 않는다.

## 주기와 전체 수집의 의미

전체 뉴스·커뮤니티 글·실시간 순위는 30초, 정규장 순위·개별 뉴스 출처/카테고리·캘린더는 300초 재시도 목표다. 핵심 목록 8회마다 전체 탭 한 개를 처리해 어느 쪽도 영구 대기하지 않도록 한다. 단일 브라우저에서 모든 스트림을 순서대로 처리하고 탐색 동작 사이 2초 이상을 두므로 첫 전체 수집·본문 처리에는 더 오래 걸릴 수 있다. 실제 간격을 30초라고 보장하지 않는다. `elapsed_s`, `overrun_s`, `runner.lag_s`, `status -- age_s`를 보고 판단한다. backlog가 크면 부분 수집 한도를 조정하거나 수집 프로세스의 작업 분할이 추가로 필요하다.

댓글·답글은 화면에서 읽을 수 있는 항목만 저장한다. 비노출·삭제·권한 제한 글, 사이트에 없는 일정, 다른 모든 종목의 과거 토론을 임의로 생성하지 않는다. 수집기의 각 스트림 상태가 검증되기 전에는 전체 수집 완료로 보고하지 않는다.

## 추가로 권하는 데이터

1. 발표 직전 예상치와 발표 후 실제값·수정치: 서프라이즈와 당시 알려진 값을 구분.
2. 순위 진입/이탈과 변화 속도: 상승률 숫자보다 지속성과 거래대금 동반 여부 확인.
3. 동일 기사 묶음·정정 이력·원문 출처: 재전송된 속보를 여러 호재로 세지 않기.
4. 지연·누락·로그인 만료 지표: 데이터가 없다는 것과 수집에 실패했다는 것을 구분.
5. 공식 원천 교차 확인: KIS 시세·호가·체결(기존 수집기), SEC/DART 공시, FRED 경제지표 빈티지. 새로운 유료 소스는 추가하지 않는다.

공식 참고: [Playwright 인증](https://playwright.dev/python/docs/auth), [FRED observations/vintage_dates](https://fred.stlouisfed.org/docs/api/fred/series_observations.html), [SEC API](https://www.sec.gov/search-filings/edgar-application-programming-interfaces).

`--only community_index_live,market_turnover_live,calendar_2026-10`처럼 일부 스트림만 점검할 수 있다. 전체 수집은 이 옵션을 생략한다.

## 2026-10-25 로컬 검증 결과

- 커뮤니티 지수 5개·상승/하락 각 20개, 증시 거래대금/상승/하락 각 20개. 사이드 패널 중복 제외 확인.
- 캘린더 2026-09 133개, 2026-10 136개, 2026-11 79개. 2026-12 및 2027-01은 빈 결과라 partial로 기록.
- 전체 뉴스 출처/카테고리는 22개 스트림. 목록과 본문·댓글은 처리 한도 때문에 partial이 있을 수 있다.
- 상시 실행에서 핵심 목록 반복 갱신을 확인했다. 실제 간격은 약 75~85초였으며 목표 30초보다 느리다. 이 환경의 단일 브라우저 전체 탭 동시 30초 수집은 달성하지 않았다.
- 현재 채팅의 코드는 별도 worktree에 있고 운영 파일은 원래 프로젝트 폴더를 TRADER_HOME으로 지정해 공유한다. 다른 checkout에서 실행할 때도 TRADER_HOME을 기존 운영 폴더로 지정해야 같은 계정·파일을 사용한다.
- 원문 고유 ID가 노출되지 않는 화면의 해시 ID, 전체 과거/모든 댓글의 완전 수집, Windows 라이브 검증은 남은 제한이다.
