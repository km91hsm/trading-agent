# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

- **운용자 본인**(개발자 겸 운용자). 데몬이 도는 로컬 PC 의 브라우저에서 하루 몇 번 — 장 전(스캔·분석 결과), 장중(주문·손절·트리거), 장 후(성과) — 상태를 확인하고, 가끔 승인·거부·일시정지·모드 전환을 누른다. 데스크톱 브라우저가 주 장면이고, 폰 브라우저로 같은 페이지를 열어 보는 경우도 있다(모바일은 텔레그램 봇이 1차 채널이지만 UI 도 동작해야 한다).
- **번들을 받아 쓰는 비개발자**(지인·가족). Python·터미널 없이 zip 을 풀고 더블클릭해 쓴다. 용어(손절선·stance·dry_run·체크포인트)를 모른다고 전제하고 짧은 설명이 UI 안에 있어야 한다.
- 확인됨(2026-10-23 인터뷰): "다른 사람도 본다". 동시 접속·권한 분리는 없다(한 PC, 토큰 하나).

## Product Purpose

한국투자증권(KIS) Open API 로 모의·실전 계좌를 운용하는 LLM 멀티 에이전트 + ML 주식 자동매매 시스템의 **운영 콘솔**. 자동매매는 데몬이 스케줄대로 돌리고, 웹 UI 는 (1) 오늘 어떤 종목에 왜 그 결정이 났는지 추적하고, (2) 시스템이 안전하게 돌고 있는지(데몬·오류·미승인·손절·일시정지·모의/실전 분리) 확인하고, (3) 성과와 실전 전환 준비도를 읽는 자리다. 세 축은 **동등한 우선순위**다(2026-10-23 확인).

성공 = 운용자가 하루 몇 번 1~2분 안에 "오늘 무슨 일이 있었고, 지금 안전하며, 내가 눌러야 할 것이 있는가" 를 알 수 있고, 비개발자도 같은 화면에서 길을 잃지 않는 것. 목표 운영은 모의 4주 이상 무사고 + 벤치마크 우위 확인 뒤 사용자가 실전 전환.

## Positioning

- **결정의 근거가 전부 파일로 남는다.** 종목당 `00_inputs → 10_analyst_<4역할> → 20_debate_r<n>_<bull|bear> → 25_research_summary → 30_decision → 40_risk_check_<계좌> → 50_order_<계좌>` 체크포인트가 상태의 원본이라, UI 는 "왜" 를 단계 순서대로 되짚어 보여 줄 수 있다. 토론 수렴 감시(`sycophantic`)·보정·메모리 결과도 같은 묶음에 있다.
- **LLM 은 제안만 하고, 리스크 한도·주문은 파이썬이 강제한다.** 계좌마다 리스크 판정(`RiskVerdict`: 승인/조정 수량/거부 사유 전부)이 기록되므로 UI 는 "LLM 이 원한 것 vs 실제 나간 것" 을 계좌별로 대조한다.
- **모의/실전이 같은 결정을 계좌별로 다르게 집행**(auto/approve/off, 실전 auto 는 LIVE 확인 기록 필요)하고, 모든 조회가 `account_id` 로 분리된다.

## Operating Context

- 데몬(`trader daemon`/번들 더블클릭)이 `config/schedule.yaml` 대로 KR(07:30 scan→analyze, 09:00~15:30 monitor)·US(뉴욕 시간) 잡을 돌리고 웹 서버(기본 `127.0.0.1:8787`)를 같이 띄운다. UI 는 원장(SQLite)·체크포인트(`state/runs/<날짜>/<시장>/<종목>/`)·피처 스토어(parquet)·KIS 시세(차트·refresh 만)를 읽는다.
- 실시간은 SSE `/api/events`(원장 카운터 변화 시 `change`). 데이터 갱신은 분 단위 — 틱 차트 터미널이 아니다.
- 제어는 텔레그램 봇(승인·일시정지·모드·관심종목)과 웹 UI 가 같은 함수를 부른다. 웹 제어는 `WEB_TOKEN`(Bearer) 이 있어야 하며 토큰이 없으면 조회 전용이다.
- 시간은 저장 UTC, 표시 KST(KR)/ET(US). 금액은 Decimal 문자열(KRW 는 정수, USD 는 소수 2자리). 운영일(run date)은 KR 은 KST 날짜, US 는 뉴욕 현지 날짜.
- 한국어 UI. 종목은 KR 6자리 코드(예: 005930)·US 티커(예: AAPL) + 이름.

## Capabilities and Constraints

페이지(현재 10개, 라우트·API 는 보존 대상; 묶음·배치는 바꿔도 됨):
- 대시보드(계좌 카드·결정 분포·오늘 LLM 세션/비용), 계좌 상세(포지션·미승인·최근 주문·refresh), 계좌 비교(결정 1건의 계좌별 판정·주문), 오늘의 실행(시장·날짜·종목·단계 체크포인트 뷰어 + 토론 뷰어), 주문/승인(승인 요청·주문·리스크 판정, 승인/거부), 차트(일봉·분봉·SMA·결정/주문/트리거 마커), 데이터(00_inputs·피처 최신 행·이력), ML(퀀트 모델 버전·폴드·피처 중요도·RL 실험·섀도), 성과(NAV·MDD·Sortino·벤치마크 1/N·B&H·quant·비용 민감도·결정 분포·수렴율), 설정/제어(토큰·일시정지·계좌 모드·관심종목·설정 JSON·STATUS.md).
- 제어 동작: 승인/거부(실전은 확인 단계), 계좌 모드 auto/approve/off(실전 auto 는 `confirm: LIVE`), 일시정지/재개, 관심종목 추가/제거, 포트폴리오 refresh. 그 밖의 쓰기는 없다(주문을 직접 내는 UI 는 없다).

기술 제약:
- React 19 + Vite + TypeScript, react-router-dom 7, lightweight-charts 5. CSS 프레임워크 없음(추가 의존성은 번들 크기·오프라인 동작을 고려해 최소). `web/dist` 는 `index.html + assets/` 구조여야 하고 FastAPI 가 SPA 폴백으로 서빙한다.
- 외부 폰트·CDN 에 의존하지 않는다(로컬·오프라인에서도 떠야 하고 번들에 들어간다). 시스템 한글 폰트 스택.
- API 응답 형태·라우터·토큰 규칙·`account_id` 필수 규칙은 불변. vitest 테스트가 import 하는 헬퍼(`sma`·`markerStyle`·`formatSessionTime`·`api.ts`·`stance.ts`·`pages/features.ts`)는 경로를 옮기면 테스트도 같이 옮긴다.
- localStorage 키 `trader.webToken`·`trader.accountId` 유지.

용어(UI 가 설명해야 하는 것): action(BUY/SELL/HOLD/ADD/TRIM), target_weight(주문 뒤 총 비중), stop_loss(손절선)·take_profit(익절선), confidence(확신), stance(normal/conservative: 최근 5거래일 실현손익이 음수면 보수), mode(auto/approve/off), dry_run(주문 없이 판정만), 체크포인트 단계 이름, 트리거(익절·±7%·무효화 조건 도달 → 재평가), 수렴(sycophantic: 토론이 한쪽으로 붕괴해 라운드 0 을 우선).

미결(발명하지 않는다): 다크/라이트 기본값, 내비 형태, 페이지 묶음 변경 여부 — 방향 선택(new-work)에서 정한다.

## Brand Commitments

- 제품명 `trader`(소문자, CLI 이름과 동일). 로고·마크는 아직 없다(현재 파비콘은 Vite 템플릿 잔재) — 재설계에서 마크를 정하면 앱 아이콘(Phase 15 번들)도 같은 마크를 쓴다.
- 모의(paper)와 실전(real) 은 **항상** 시각적으로 구분돼야 한다(실전은 전 화면에서 다른 표식).
- 한국어 문구, 존댓말 없이 간결한 명사형/서술형(기존 CLI·텔레그램 문구와 같은 톤: "일시정지", "미승인 2건", "손절 보류").

## Evidence on Hand

- 실데이터: `state/trader.db`(원장: decisions·orders·approvals·risk_checks·positions_snapshot·agent_calls·runs·triggers·memory), `state/runs/<date>/<market>/<ticker>/*.json`(체크포인트), `state/features/*.parquet`, `state/models/`. 현재 모의 결정은 HOLD 4건뿐이라 화면 점검용 데이터는 `tests/web/`·`FAKE_CLAUDE=1` 로 만든 가짜 run 을 쓴다.
- 없는 것(만들지 않는다): 사용자 후기·고객 로고·수익률 주장·벤치마크 우위 주장. 성과 숫자는 원장에서 계산한 값만 보여 준다.

## Product Principles

1. **먼저 안전, 그다음 근거, 그다음 성과를 같은 화면 깊이로.** 일시정지·미승인·오류·실전 모드는 어느 페이지에서도 보여야 한다.
2. **"왜" 는 파이프라인 순서로 읽힌다.** 입력 → 애널리스트 → 토론 → 요약 → 결정 → 게이트 → 주문. JSON 덤프는 보조이지 1차 화면이 아니다.
3. **모의와 실전은 섞이지 않는다.** 계좌 선택이 전역이고, 실전은 색·라벨이 다르며, 실전 제어는 한 번 더 묻는다.
4. **비개발자가 읽을 수 있다.** 용어에는 한 줄 설명, 빈 화면에는 "무엇을 하면 채워지는지".
5. **운영 콘솔이지 트레이딩 터미널이 아니다.** 분 단위 데이터, 밀도는 있되 과장된 실시간 연출은 없다.

## Accessibility & Inclusion

- 상승/하락·승인/거부·모의/실전을 색만으로 구분하지 않는다(라벨·아이콘 병행). 한국 관행(상승 빨강·하락 파랑)을 따르되 색각 이상에서도 구분되는 명도 차를 둔다.
- 키보드로 승인/거부·페이지 이동이 가능해야 한다. 포커스 링 유지.
