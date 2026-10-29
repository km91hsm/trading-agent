# 일반 Chrome 기반 AI 독립 SaveTicker 수집

사용자 승인 범위: Codex/Claude 없이 상시 동작하며, 기존 전체 범위(뉴스·본문·SAVE 리포트/PDF, 커뮤니티 TOP5·지수/상승/하락, 증시 거래대금/상승/하락, 캘린더 지난달~3개월 뒤)를 유지한다. 사용자가 일반 Chrome에서 로그인·보안 확인을 완료한다. Chrome과 컴퓨터가 켜져 있고 로그인 세션이 유효한 동안 수집한다. 절전·Chrome 종료·인증 만료 중에는 수집할 수 없으며 이를 상태로 표시한다.

## 접근과 선택
1. Playwright 브라우저: 사람 확인 반복 실기로 제외.
2. Chrome 디버깅 포트: 별도 권한과 재확인 가능성이 있어 이번 운영 경로에서 제외.
3. 선택: 일반 Chrome MV3 확장 → Native Messaging Python 호스트 → 기존 Store. 확장 설치 1회 후 재기동·30초 알람으로 복구하며, AI·구독·네트워크 수신 포트·브라우저 쿠키 내보내기가 필요 없다.

## 구성
- `extensions/saveticker/`: manifest, service worker, content script, popup, 순수 계획/직렬화 테스트. 네 전용 탭(news/community/market/background)만 조작한다. 사용자 기존 탭을 가져가거나 닫지 않는다. 다운로드 리포트는 `~/Downloads/SaveTicker/`만 사용한다.
- `src/trader/saveticker/native.py`: 길이 접두 JSON 프로토콜, 허용된 수집 메시지 검증, 기존 dom/jsonfeed·reports.save_pdf·Store, service/runner 상태. 브라우저 네트워크/LLM/브로커 호출 없음.
- `src/trader/saveticker/bridge_install.py`: 확장 패키지 복사, 고정 공개키로 확장 ID 산출, Native Messaging manifest/소스 런처 준비·설치·제거. CLI `saveticker extension prepare|install-host|uninstall-host|status`.
- `automation.py`: extension backend면 Playwright 자동 실행 억제, Native 호스트 연결·heartbeat 기반 상태. 계정 env 파일 없이도 구성됨으로 판단.
- 번들 CLI의 첫 인자가 정확한 `chrome-extension://<id>/`일 때만 Native 호스트 경로로 분기(Chrome가 origin을 argv로 전달). 일반 CLI는 불변. 소스 환경은 절대 python 경로·TRADER_HOME을 넣은 전용 shell 런처(macOS/Linux); Windows source는 설치 미지원 안내, Windows 번들은 기존 trader.exe로 동작.

## Chrome 권한과 설치 경계
- `storage`, `alarms`, `nativeMessaging`, `downloads`; host permission은 `https://www.saveticker.com/*`만.
- cookies/debugger/프록시/자동화 탐지 은폐 권한 없음. MAIN-world JSON 관찰이 필요하면 허용된 응답 필드만 전달한다. 로그인 엔드포인트·쿠키·요청 헤더·프로필·비밀번호는 읽거나 저장하지 않는다.
- 확장 설치·Native host 등록·운영 backend 전환은 준비된 패키지/권한표/검증 결과를 보여준 뒤 사용자 최종 승인 시에만 실행한다. 테스트는 합성 페이지/임시 홈에서 수행한다.
- 기존 앱이 Playwright를 재기동하지 않도록 모드 전환 시 기존 수집만 중지한다. 구버전 앱은 backend 설정을 읽지 않으므로 설치 절차에서 계정 env 파일을 값 출력 없이 `.extension-backup`으로 이동하는 호환 보호를 제공하고 제거 시 복원한다. 원장·계좌·거래 데몬은 재시작하거나 실행하지 않는다.

## 수집 흐름
- 사용자가 확장 popup에서 시작하면 enabled를 저장한다. onStartup/재로딩/알람에서 enabled 확인, Native 연결, 계획 갱신, 누락된 전용 탭만 재생성한다.
- Native 연결은 MV3 worker 수명을 유지하며, alarm 30초로 재확인한다. 기기 절전은 깨우지 않으며 기상 후 다음 tick부터 재개한다. 사용자 정지는 enabled=false, 알람/연결 종료, 생성한 전용 탭만 선택적으로 정리한다.
- Native plan은 기존 build_views와 뉴스/리포트 View를 재사용, 달이 바뀌면 갱신한다. 각 lane은 동시 1작업, 같은 탭에서 지수/상승/하락·정규장 기준·월을 화면 UI로 선택 후 검증한다. UI 선택 실패를 빈 성공으로 저장하지 않는다.
- 기본 30초 tick, 뉴스·실시간 순위 30초, 정규장 순위/캘린더 300초, 리포트 900초. cold 작업이 굶지 않도록 8 hot 뒤 cold 우선. 각 동작 최소 2초 간격, 스크롤 2회(미완결은 partial), 뉴스 상세 최대 2건/회·리포트 다운로드 최대 2건/회 순환.
- Capture의 observed_at은 호스트 수신 시각. 화면의 상대 시간만으로 발행 시각을 만들지 않는다. normal requests의 allowlisted JSON이 있으면 jsonfeed로, 없으면 DOM으로 파싱하고 근거/partial을 보존한다. 처음부터 전량 완료로 표시하지 않는다.
- 인증/재확인/401·403·429는 해당 lane을 blocked로 유지하며 자동 로그인·새로고침·재시도하지 않는다. 사용자가 직접 확인한 후 popup의 재개로 다시 점검한다. 배포 준비·tab 생성에서 계정 env를 읽지 않는다.

## Native Messaging v1 계약
`uint32 native-endian length + UTF-8 JSON`; 입력 최대 64MiB, 일반 snapshot HTML/JSON 총합 4MiB, 응답 최대 1MiB. EOF 정상 종료, 깨진 프레임/잘못된 origin/데이터는 값 출력 없이 에러 코드만. 요청 처리 중 예외가 발생해도 안전한 다음 메시지는 처리한다(프레임 자체가 깨졌으면 종료). Native host는 단일 `runtime/bridge-host.lock`, 활성 동안 기존 `runtime/service.lock`도 잡아 중복 수집을 막는다. 잠금 실패 시 저장하지 않고 종료한다.

모든 요청에는 `type`과 정수 `id`가 있고 응답은 같은 id, `ok`, `type`, 실패 시 고정 `error` 코드만 반환한다.

- `hello {type:'hello',id,protocol:1}` → `{ok:true,type:'plan',id,protocol:1,views:[{stream,kind,url,tab,source,category,month,regular,interval_s}],limits:{max_scrolls:2,max_details:2,max_reports:2}}`. plan의 interval은 최소30초. 달 범위는 호스트 KST 기준. plan 갱신은 hello 재사용.
- `snapshot {type:'snapshot',id,stream,page_url,html,tab?,regular?,month?,details?:[{title,html}],json_responses?:[{url,status,data}],status?:'ok'|'partial'|'error'|'auth_required',reason?:str}` → Store save 요약 `{ok:true,type:'saved',id,stream,count,changed,status,report_items?:[{id,title,attachment_ok}]}`. stream·URL·선택(tab/regular/month)이 현재 plan과 일치해야 한다. auth/error는 items=[]로 저장하되 과거 last-good 데이터는 유지. JSON payload는 canonical Item으로 변환한 뒤만 저장하고 원문 HTML/JSON은 기록하지 않는다. 제목·본문·수치만 저장. 댓글/커뮤니티 게시물·트렌드 제외.
- `pdf {type:'pdf',id,item_id,download_name}` → `{ok:true,type:'pdf_saved',id,item_id,attachment}`. download_name은 확장이 방금 생성한 `<32hex>-<13digit>.pdf`만. Native 호스트가 `~/Downloads/SaveTicker/` 안의 실제 파일인지 resolve·symlink 검사 후 기존 save_pdf로 최대50MB·200쪽 검증. 현재 news_reports의 항목에만 연결하고 현재 수신시각으로 새 Capture 저장. 성공 PDF의 재다운로드는 24시간 캐시.
- `state {type:'state',id,lane,state,reason?}` → `{ok:true,type:'state_saved',id}`. lane= news/community/market/background, state=connecting/collecting/waiting_verification/auth_required/paused/error. 임의 error 메시지/stack/HTML은 상태 파일에 저장하지 않고 정해진 reason 코드만.
- `ping {type:'ping',id}` → `{ok:true,type:'pong',id}`. 전체 service heartbeat 갱신. 탭별 상태는 state/snapshot 때 갱신. EOF는 extension_disconnected로 표시하며 자동 Playwright로 폴백하지 않는다.

## 전체 범위 검증
1. Native framing·origin pinning·권한·메시지 크기·경로 탈출·오류 비노출·중복 저장·asof를 Python 테스트.
2. 모든 stream plan과 탭 전환·regular toggle·월 전환·상세·PDF 연결·auth 정지·시작/정지/재기동·alarm 중복을 JS Node 테스트(기본 node:test, 추가 의존성 없음).
3. 합성 HTML/JSON→Native→Store→기존 reader 실제 왕복; 네 lane이 빠지거나 월이 한 달이라도 누락되면 불완전으로 기록.
4. ruff/전체 pytest/웹 타입 검사·vitest·build/독립 리뷰. 기존 PR #78의 브라우저 미설치 skip 시 Playwright loop 누출도 회귀 수정.
5. 최종 승인 후 사용자 Chrome에 설치, AI 호출 없이 여러 tick 수집 파일 갱신 확인. 재부팅/절전/Windows 실기는 별도 체크로 남긴다. 승인 전에는 설치 완료·상시 실기 완료로 보고하지 않는다.

## Builder check로 확정한 보완(앞 계약보다 우선)
- 호스트 상태는 `NativeSession(root).handle(message)` 객체가 가진다. 모든 실패도 같은 id로 응답하고 임의 payload/예외 메시지를 로그에 쓰지 않는다.
- `begin {type:'begin',id,stream}` → `{ok:true,type:'job',id,job_id,view,expires_at}`. lane당 활성 작업 1개, job TTL120초, snapshot은 유효한 job_id 필수·한 번만 소비. 호스트가 stream/view를 선택하고 응답 증거 `selection:{source?,tab?,regular?,month?,loaded:bool}`를 비교한다. 요청한 값을 복사한 selection은 금지. 실제 aria-selected·aria-checked/pressed·월 레이블에서 읽는다. 확인할 수 없으면 error/partial이고 다른 stream으로 저장하지 않는다.
- JSON 관찰은 UI 선택 완료 후 같은 job generation에서 완료된 응답만 연결한다. 불확실하면 DOM fallback + partial. 정규장 JSON에는 모든 quote에 명시적인 regular 객체가 있어야 하며 없으면 live를 regular로 사용하지 않는다. 빈 월은 items=[] partial이고 정상 완료로 단정하지 않는다.
- MAIN JSON projector(허용 경로는 jsonfeed.ROUTES 그대로): news list/detail은 id/title/source(name)/channel(name)/created_at/is_headline_only/content/번역 ko_KR title·content/tickers(symbol)/content_labels(name,label_name)만; calendar는 events(id,title,event_date,event_date_only,content)만; ranking은 rank/symbol/nameKo/labelKo/price/changePercent/turnover/ticker(symbol,nameKo)/regular(price,changePercent,turnover)만. 재귀 본문은 text/children/content/body 깊이8, 단일 문자열200k, 배열500, 총 snapshot4MiB. 그 외 키는 브라우저에서 버리고 호스트도 다시 허용 키만 처리한다. source DOM fallback은 script/style/iframe/form/input/textarea/header/nav/menus를 제거하고 구조·class·수집용 aria/data 속성만 허용한다. raw HTML/JSON은 파일·오류에 남기지 않는다.
- `pdf_begin {type:'pdf_begin',id,item_id}` → `{ok:true,type:'pdf_job',id,ticket,download_name}`. 현재 news_reports 미완료 항목에만 TTL120초 일회 티켓·파일명(32hex-13digit.pdf)을 발급한다. 확장은 PDF 클릭 전에 전역 단일 pending에 ticket/name/item_id를 넣고, 생성된 PDF downloadId(뉴스 referrer/정해진 시간창/mime 또는 확장자)를 한 번만 묶는다. onDeterminingFilename에서 SaveTicker/<발급명>만 지정, 완료 이벤트의 실제 파일명이 정확히 일치할 때 `pdf {id,type,item_id,ticket,download_name}` 전송. 호스트는 ticket/ID/name 일치·mtime·경로·파일 내용 확인 후 티켓 소비. 기존 다운로드·동시 일반 다운로드·재연결 전 티켓·미완료 파일은 연결하지 않는다. Chrome가 파일명을 바꾸면 partial로 남긴다. 24시간 캐시는 파일 hash/size·저장 attachment 실재까지 확인한다.
- 전용 탭은 생성 시 `#trader-saveticker=<설치별 무작위 token>:<lane>` 마커를 붙인다. 확장 storage.local의 소유 token 및 lane에 정확히 일치하는 탭만 복구·재사용한다. 일반 URL만 보고 사용자 탭을 가져가지 않는다. 중복은 동일 마커의 소유 탭에 한해서만 정리한다. snapshot page_url은 검증된 origin/path로만 구성하고 마커는 저장하지 않는다. worker restart 때 단일 tick과 Native 요청 실패 정리·exponential backoff 1/2/4/...60초, 정지는 재접속 금지.
- backend 전환 직전 실행 중 collector만 중지하고 service lock 해제를 확인한다. 백업 env가 있으면 덮어쓰지 않고 설치 거부. 설치 중 실패 시 등록 파일·모드·자격 파일을 rollback한다. 원본 env가 새로 만들어졌으면 uninstall은 백업 복원을 거부한다. 거래 데몬은 건드리지 않는다.

## 고정 확장 식별자
Native host 이름: `com.trader.saveticker`. 확장 ID: `gfchfnjghmjiidifohjglpodaakdphho`. Manifest의 공개 key(서명용 비밀키는 저장하지 않음):

```text
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA3WeKbuemZizg+F+9q1VmyQPIJ639KzMlycxI33pcMvITzLWUNTt4jTI62Wgh2/bMbM6Zgya1J27bmAYn5Wfd5AFc2NQwscNvzx0d6d3zYm3q+S2C19RbOneLa7bW3JOCSn0JPw1cXPJfSDKzXer9whZprisEx0/bmKPjOmveFl1axpXZMIG0CaqC2FBT/fgcSxKsMNP7MOTlelNoQN+JklWrAw4BBvqtyYklWnIYe+xF1ZZShtdeLHEs4HhpvVLekPs44g6FWnoxEFAFnbgkuj3jw4OlTnxjWI1bvCgcIo2cOtGbpkJwxirmMLU77V+lpDUR7ZjAMsxdbE/nRzRJjQIDAQAB
```

### 실기 후 계약 보완 (1.0.1)
- Chrome 새 탭은 `pendingUrl`만 있거나 URL 필드가 없을 수 있다. 초기 탐색은 정본 URL로 진행하되 수집 증거는 실제 content 응답에서만 얻는다. 실패한 발급 작업은 정본 URL의 error snapshot으로 종료한다.
- community 정규장 checkbox가 비활성·checked=true이고 title이 정확히 `지금은 실시간과 정규장 기준이 같아요`인 때만 selection에 `regular_disabled:true`, `regular_equivalent:true`, `regular_equivalence_label`을 함께 보낸다. `regular`는 실제 true를 유지한다. Host는 community kind에만 이를 허용하고 quote.basis에 안내를 남긴다. requested_regular는 요청 기준을 유지한다.
- 커뮤니티 탭이 aria 선택 상태를 제공하지 않는 경우, 지수/상승/하락 세 버튼의 같은 그룹 안에서 유일한 `bg-neutral-800 text-background-secondary text-medium-bold` 선택 스타일을 확인한다.

### 백그라운드 초기화 보완 (1.0.3)
- 실기에서 숨겨진 뉴스/캘린더 탭은 로딩 완료 뒤에도 선택 텍스트가 준비되지 않았고, 표시한 뒤 동일 코드의 선택 검증과 저장이 성공했다. 수집은 전용 Chrome 창에서 정확한 토큰 소유 탭만 이동/복구하고 각 작업 탭을 차례로 활성화한다. 창 전체에는 focused:true를 사용하지 않는다.
- 다른 사용자 탭이 섞인 창은 수집 창으로 재사용하지 않는다. 정확한 토큰의 탭만 새 전용 창으로 옮긴다. 최소화된 수집 창은 임의 복원하지 않고 native paused/page_not_loaded로 기록한다.
- runtime/selection-<lane>.json은 요청 stream과 선택 조건별 일치 여부/loaded/status만 기록한다. 페이지 텍스트·선택값·HTML·인증정보를 기록하지 않는다.

### 1.0.5 worker 대기와 UI 경로 변경
- Content 대기는 고정2초 내부 pace 메시지로만 처리한다. caller 확장 ID·소유 tab/token·enabled·동시 제한을 확인하고 stop/disconnect 때 pending reply를 취소한다. auth/검증 실패 시 저장하지 않는 조건 유지.
- 시작/재개·Chrome 시작·새 전용 창은 소유 창을1회 표시할 수 있다. 기존 사용자 창을 focus하지 않으며 일반 주기/MV3 idle 복원은 focus하지 않는다.
- reports 선택이 /news?label_group=7로 이동하며 hash를 지운다. 발급된 뉴스 작업의 tab/token/epoch/page 증명을 가진 동안에만 exact source query와 빈 hash를 수리한다. 작업 종료 뒤/다른 tab·origin·path·query·hash에는 적용하지 않는다.
