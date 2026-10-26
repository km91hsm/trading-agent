# SaveTicker 브라우저 수집

뉴스 전체를 한 번 수집해 속보/SAVE PICK/로이터 등 출처와 카테고리로 분류하고, SAVE 데일리 리포트 PDF 원본과 추출 텍스트를 별도로 보관한다. 커뮤니티는 지수/상승/하락/종목별 커뮤니티 TOP5만 수집한다. 사용자 게시글·댓글은 수집하지 않는다. 증시 거래대금/상승/하락, 캘린더 월별 일정도 저장한다. 거래·LLM 실행은 하지 않는다.

## 설치와 계정

```text
uv sync
uv run trader saveticker install-browser   # Chromium 1회(서비스 시작 때도 자동)
```

번들(`Trader.app`/`TraderApp.exe`)은 Playwright 드라이버를 포함하고 Chromium 은 첫 실행에 내려받는다.
평소에는 데몬 잡 `saveticker` 가 계정 파일이 있을 때 `trader saveticker service` 를 띄우고 감시·정리한다
(`docs/architecture.md` 「세이브티커 수집기」). 아래 launchd 등록은 데몬 없이 상시 수집할 때만 쓴다.

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

캘린더 기본 범위는 KST 현재 지난달부터 앞으로 3개월(양 끝 월 포함). `--start-month YYYY-MM --end-month YYYY-MM`으로 최대 25개월을 선택할 수 있다. 뉴스는 `--max-scrolls`(기본 20), 뉴스 본문/PDF 배치는 `--max-details`(기본 20)로 한 번의 처리량을 제한한다. 각 스크롤에서 가상 행을 누적한다. 상시 실행은 전체 뉴스의 첫 수집에서 설정한 스크롤 범위를 읽고, 이후 전체 뉴스만 최대 2회로 줄여 최근 목록을 우선한다. PDF 목록의 스크롤 범위는 줄이지 않는다. 상시 본문과 PDF 다운로드는 회차당 각각 최대 2개이며, `--once` 배치는 `--max-details`를 따른다. 과거 전체 본문은 `--once --max-scrolls 500 --max-details 500` 같은 별도 배치로 처리하고, 한도에 닿으면 계속 partial로 남는다. 한도 도달·끝 여부 미확인·본문 대기는 `partial`로 표시한다. 전체 사이트의 과거 뉴스을 다 받았다고 간주하지 않는다.

정규장 기준과 실시간 기준을 별도 스트림으로 수집한다. 위험주 제외 필터는 수집 화면에서 해제한다. 휴장 중 비활성 필터는 사이트에 표시된 실제 기준을 `basis`에 남긴다. 사용자 관심종목·계정 설정·댓글 등록은 변경하지 않는다.

## 파일

기준 폴더는 `config/sources.yaml` 의 `news.saveticker_dir`(기본 `$TRADER_HOME/state/external/saveticker`) 아래 `data/`다 — 리더가 읽는 곳과 같다. 상대 경로는 프로젝트 루트 기준이다. `news.saveticker_retention_days`(30) 지난 변경 이력·화면 캡처(스트림별 마지막 제외)·날짜 폴더·PDF 원본은 데몬이 운영일 1회 지운다(추출 텍스트·manifest 는 남김). 수집 결과는 `data/`, 로그·실행 상태·잠금 파일은 형제 폴더 `runtime/`로 분리한다.

```text
feed.sqlite3                  변경 이력 원본(WAL)
2026-10-25/news.jsonl          UTC 수신일 기준 뉴스 변경분
2026-10-25/community.jsonl     지수/상승/하락/TOP5 변경분
2026-10-25/market.jsonl        순위·시세 변경분
2026-10-25/calendar.jsonl      일정·발표값 변경분
latest/<stream>.json          스트림별 최근 유효 캡처
status.json                   시도·유효 수집 시각, 상태, 처리 소요·목표 주기
../runtime/runner.json        pid, 실행 상태, 현재 처리 스트림, 스케줄 지연
../runtime/collector.lock      감독 프로세스 잠금
../runtime/collector-<lane>.lock 작업별 중복 실행 잠금
../runtime/runner-<lane>.json    작업별 상태·JS 오류 진단
../runtime/<lane>.log          작업별 수집 로그
attachments/<sha256>.pdf       다운로드 PDF 원본
attachments/<sha256>.txt       PDF에서 추출한 텍스트
attachments/report-<id>.json   다운로드 확인 시각과 파일 manifest
```

JSONL은 한 줄이 변경 버전 하나이며 `{revision, stream, received_at, item}`이다. `view`에는 출처·카테고리·탭·조회 월을 남긴다. `item`에는 `source_group, categories, is_breaking, report_date, attachment, id, kind, title, body, tickers, published_at, scheduled_at, scheduled_date, price, change_pct, turnover, rank, forecast, previous, actual, views, likes, comment_count, parent_id, detail_status, basis`가 들어간다. 해당 없는 값은 null/빈 문자열이다.

수집 시각은 UTC, 화면 시간대는 Asia/Seoul로 고정한다. 상대시각만 있는 목록은 `published_at=null`이며 추정한 절대시각을 만들지 않는다. 캘린더 `scheduled_at`은 미래여도 유효한 예정 정보다. 종일·시각 미정 일정은 `scheduled_at=null`과 `scheduled_date`로 보존한다. 발표값의 알려진 시각은 `received_at`이며 일정 시각으로 과거에 알았다고 처리하면 안 된다.

스트림 내 같은 행과 상대시각만 달라진 행은 중복 버전을 만들지 않는다. 뉴스는 조회수·좋아요·댓글 수만 바뀐 행도 새 버전이 아니다(하루 1만 줄 중 대부분이 조회수 변경이었다). 수정·순위 변화와 커뮤니티·순위 화면의 조회수·좋아요 변화는 새 버전이다. 사이트가 고유 ID를 노출하지 않는 화면에서는 제목+출처 의 해시를 사용하므로 동일 제목의 기사가 겹칠 가능성이 있다. 원문 고유 ID가 확인되면 우선하도록 확장할 수 있다.

SQLite 커밋 후 JSONL과 최신 파일을 원자적으로 교체한다. 파일 생성 중 중단돼도 `export`로 원본에서 복구한다. 오류/로그인 만료/빈 응답이 나면 기존 유효 최신 파일을 지우지 않으며 반드시 `status.json`의 시각·상태를 함께 읽는다. `query --asof`는 수신 시각과 발행 시각이 asof 이하인 버전만 반환한다. 다른 탭의 본문 대기 행이 기존 본문을 가리지 않도록 마지막 확인된 본문 버전을 원래 수신 시각과 함께 우선한다. 순위·캘린더는 그 시점 마지막 유효 화면에 실제 있는 항목만 반환한다. 기존 `00_inputs.json`을 자동 변경하지 않는다.

## 주기와 전체 수집의 의미

전체 뉴스·커뮤니티 순위·증시 순위는 20초, 정규장 순위·캘린더는 300초, PDF 리포트는 900초 점검 목표다. PDF는 화면의 다운로드 버튼으로만 받으며 상시 실행에서 한 번에 최대 2개를 처리한다. 미수집 PDF가 남으면 60초 후 이어 받고, 처리 실패가 첫 항목에 몰려도 다음 보고서를 순환 처리한다. 확인된 원본은 최대 24시간 재사용하며 SHA256과 크기를 검증한다. 추출 텍스트가 없는 PDF도 원본을 보관하고 `pdf_only`로 구분한다. 보고서 날짜는 화면에 적힌 일자를 `report_date`로 보존하며 발행 시각을 임의로 만들지 않는다.

상시 실행은 뉴스/커뮤니티/증시/백그라운드(PDF·캘린더) 네 프로세스로 나눈다. PDF 다운로드나 캘린더 탐색이 빠른 목록을 막지 않도록 각각 전용 브라우저를 사용한다. 한 작업이 종료되면 감독 프로세스가 나머지도 정지시키고 원인을 보존한다. 이는 실제 지연 보장이 아니다. `elapsed_s`, `overrun_s`, `runner.lag_s`, `status -- age_s`를 확인한다. 뉴스 목록은 가상 스크롤 범위 안에서 누적하며, 본문 준비·한도 도달·끝 미확인은 `partial`로 표시한다. TOP5에 사이트가 4개만 표시하면 4개만 저장한다. 트렌드는 2026-10-26 사용자 요청으로 수집·장애 감시에서 제외했다. 과거 저장 이력은 유지한다. 사용자 글·댓글의 이전 파서는 과거 파일 호환 테스트용으로 남아 있으나 수집기는 해당 스트림 실행을 거부한다.

사이트 자바스크립트 오류는 `pageerror` 이벤트의 횟수·오류 타입·경로를 기록한다. 메시지/stack/쿼리 URL은 계정이나 토큰이 포함될 수 있어 저장하지 않는다. 뉴스 상세 클릭은 주 목록 안으로 제한해 상단 중복 제목과 혼동하지 않는다. 본문 컨테이너가 준비되기 전에는 번역·원문 버튼을 본문으로 저장하지 않는다.

## 추가로 권하는 데이터

1. 발표 직전 예상치와 발표 후 실제값·수정치: 서프라이즈와 당시 알려진 값을 구분.
2. 순위 진입/이탈과 변화 속도: 상승률 숫자보다 지속성과 거래대금 동반 여부 확인.
3. 동일 기사 묶음·정정 이력·원문 출처: 재전송된 속보를 여러 호재로 세지 않기.
4. 지연·누락·로그인 만료 지표: 데이터가 없다는 것과 수집에 실패했다는 것을 구분.
5. 공식 원천 교차 확인: KIS 시세·호가·체결(기존 수집기), SEC/DART 공시, FRED 경제지표 빈티지. 새로운 유료 소스는 추가하지 않는다.

공식 참고: [Playwright 인증](https://playwright.dev/python/docs/auth), [FRED observations/vintage_dates](https://fred.stlouisfed.org/docs/api/fred/series_observations.html), [SEC API](https://www.sec.gov/search-filings/edgar-application-programming-interfaces).

`--only community_index_live,market_turnover_live,calendar_2026-10`처럼 일부 스트림만 점검할 수 있다. 전체 수집은 이 옵션을 생략한다.

## 2026-10-25 검토 중 실제 화면 검증

- 전체 뉴스 568건에 속보/SAVE PICK/로이터 각각의 표본 20건이 모두 포함됨을 확인했다. 전체 과거의 수학적 완전성 증명은 아니며, 이 관찰에 따라 전체만 수집하고 분류한다.
- 수정 코드로 뉴스 9건 중 처리 한도 내 본문 2건, 트렌드 10개, TOP5 실제 표시 4개를 저장했다. 사용자 글·댓글은 결과에 없다.
- SAVE 리포트 10월 23일/22일 원본을 서로 다른 SHA256 파일로 저장했다. 각각 28/33페이지이며 추출 텍스트는 12,140/16,091자다. 첫 PDF 표지를 렌더링해 SAVE 브리핑 자료임을 확인했다.
- 실제 사이트 점검에서는 pageerror가 재현되지 않았다. 로컬 브라우저에서 TypeError를 발생시켜 오류 기록과 민감 문자열 제외 동작을 검증했다.
- 네 작업으로 분리한 상시 실행에서 20초 목표 관측을 4~6회씩 측정했다. 뉴스 중앙값 19.6초(최대 20.0초), 커뮤니티 21.1~21.4초(최대 25.8초), 증시 20.0~22.0초(최대 25.0초)였다. 짧은 주말 표본이며 실제 사이트의 신규 데이터 도착 지연이나 장중 부하를 보장하지 않는다. 초기 뉴스 범위 수집·빈 달·본문/PDF 대기는 계속 partial로 구분한다. 과거 PDF는 순차 다운로드 중이다.
- 운영 계정은 원래 프로젝트를 TRADER_HOME으로 지정해 읽고, 새 운영 결과는 그 아래 data에 저장한다. 원문 고유 ID 미노출에 따른 해시 충돌 가능성, 전체 과거 뉴스, Windows 라이브 검증은 제한으로 남는다.

## 2026-10-26 JSON 응답 전환

뉴스·커뮤니티 순위·증시·캘린더는 정상 브라우저 UI가 수신한 허용 JSON 응답에서 추출한다. 독립 HTTP 요청이나 사용자 글·댓글 API는 사용하지 않는다. 로그인·탭 선택·스크롤·기사 열기는 UI로 수행하며, SAVE 리포트 목록/PDF 원본 다운로드는 기존 UI 경로를 유지한다.

뉴스 목록 content는 본문으로 저장하지 않는다. 기사를 열 때 도착한 상세 JSON에서 텍스트 블록을 펼치고 한국어 번역이 제공되면 한국어 제목·본문을 선택한다. 한도 내 미처리 기사는 pending으로 남긴다. 응답은 현재 수집 회차에서만 사용하며 조회 실패나 차단을 성공으로 표시하지 않는다. 시세 문자열은 JSON의 숫자를 그대로 보존하며 통화 기호·퍼센트 기호를 덧붙이지 않는다.

## macOS 자동 운영 (AI 호출 없음)

`trader saveticker service`가 기존 수집 감독 프로세스를 실행한다. 일반 종료 오류는 30/60/120초 뒤 최대 3회 재시작한다. 5분 이상 정상 실행 후 발생한 장애는 새 재시작 예산을 사용한다. `AccessDenied`·`LoginRequired`는 반복 시도하지 않고 중단한다. `trader saveticker monitor`는 프로세스 종료를 즉시, 유효 갱신 120초 지연·파일 읽기 실패를 연속 2회 확인 후 macOS 알림으로 알린다. 같은 장애는 중복 알리지 않고 복구도 한 번 알린다. 계정·쿠키·원본 예외는 알림에 넣지 않는다.

설치된 사용자 LaunchAgent:

- `com.user.saveticker.collector`: 로그인 시 `service` 실행, 종료 대기 70초.
- `com.user.saveticker.monitor`: 로그인 시 및 60초마다 `monitor` 실행.

실행 파일·작업 폴더는 설치 당시 Python 환경과 코드 체크아웃을 절대 경로로 참조한다. Codex/Claude 앱을 종료해도 동작하지만 해당 코드 폴더·가상환경을 삭제하면 안 된다. 이 LaunchAgent는 거래 데몬을 실행하지 않는다. macOS 로그인 세션과 깨어 있는 컴퓨터가 필요하며, 수동 재부팅·로그아웃·잠자기 중 연속 수집까지 보장하지 않는다. 재부팅 후 로그인 시 시작 여부는 실제 재부팅 전까지 미검증이다. macOS 알림 허용·집중 모드 설정은 사용자 환경에 따른다.

등록 상태는 `launchctl print gui/$(id -u)/com.user.saveticker.collector` 및 `.monitor`로 확인한다. 의도적으로 수집을 중지할 때는 두 작업을 모두 `launchctl bootout gui/$(id -u)/com.user.saveticker.collector`와 `launchctl bootout gui/$(id -u)/com.user.saveticker.monitor`로 내린다. 다시 시작하려면 `~/Library/LaunchAgents/com.user.saveticker.{collector,monitor}.plist`를 각각 `launchctl bootstrap`으로 등록한다. 실행·재시작 기록은 `runtime/service.json`, 감시 기록은 `runtime/alert-monitor.json`이다.
