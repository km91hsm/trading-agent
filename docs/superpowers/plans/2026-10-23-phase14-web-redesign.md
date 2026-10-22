# Phase 14 웹 UI 재설계 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** impeccable 절차로 고른 시각 세계 "신문 증권면 시세표" 로 웹 UI 10페이지를 전부 다시 입히고(토큰·공통 컴포넌트·셸·반응형·다크/라이트·로딩/빈 상태·쉬운 용어), API·기능은 그대로 둔다.

**Architecture:** 스타일은 `web/src/styles/tokens.css` 의 CSS 변수 한 벌(라이트 = 미색 신문 용지, 다크 = 먹 바탕)만 쓰고 컴포넌트 CSS 는 변수만 참조한다. 모든 표는 한 가지 열 문법(`Table` 원시요소: 이름 셀 + 기호, 우측정렬 tabular 숫자, ▲▼ 적청, 2px 머리선)을 쓴다. 페이지는 라우트·API 호출을 유지하고 묶음·배치만 바꾼다. 데이터 → 화면 행 변환은 순수 함수(`web/src/tape.ts` 등)로 떼어 vitest 로 고정한다.

**Tech Stack:** React 19 + Vite + TypeScript, react-router-dom 7, lightweight-charts 5, Vitest(jsdom, `react-dom/server` 로 스모크 렌더), `pretendard` npm(자체 호스팅 가변 서체). CSS 프레임워크 없음.

**Spec:** `PRODUCT.md`(제품 진실), `.impeccable/surfaces/web-src-app-tsx.md`(방향 계약 — THESIS·OWN-WORLD·STORY·FIRST VIEWPORT·FORM·FINISH), 선택된 스케치 `.impeccable/mocks/decision/assigned-r1.html`(첫 화면 구성의 기준), 플랜 모드 플랜 `~/.claude/plans/clever-pondering-ritchie.md` Phase 14 절, `docs/superpowers/specs/2026-10-09-trading-agent-design.md` §8.

## Global Constraints

- CLAUDE.md 규칙 전부. 문구 한국어, 라인 100자(TS 는 prettier 기본 100).
- **보존**: FastAPI 라우터·응답 형태 불변. `web/dist` 는 `index.html + assets/`. 라우트 10개 경로 불변(`/ /account /compare /today /orders /chart /data /ml /performance /settings`). localStorage 키 `trader.webToken`·`trader.accountId` 유지. `api.ts`·`stance.ts`·`pages/features.ts` export 이름 유지.
- **색은 토큰만**: `web/src/**/*.{css,tsx}` 에 hex 리터럴 금지(예외: `tokens.css`). 적(`--up`/`--danger`)·청(`--dn`)은 데이터 의미에만, 황(`--pending`)은 대기·주의에만, 실전 계좌 표식은 `--real`.
- **외부 폰트·CDN 금지**(오프라인·번들). 서체는 `pretendard` 패키지의 dynamic subset 을 Vite 로 번들.
- 아이콘은 `Glyph.tsx` 의 인라인 SVG(stroke 1.5, 16px 격자)만. 이모지·유니코드 기호 금지 — ▲▼●○☆ 도 SVG 로 그린다.
- 모든 인터랙티브 요소: hover/focus-visible/active/disabled. 포커스 링 토큰(`--focus`). `prefers-reduced-motion` 이면 전환 0.
- 390px 에서 가로 스크롤 없이 동작(표는 컨테이너 안 가로 스크롤).

## Review Focus

1. 토큰이 없는 조회 전용 상태에서 제어 버튼이 눌리지 않아야 하고, 401 이 오면 "설정/제어에서 토큰 입력" 안내가 떠야 한다 → Task 7 `approve 버튼 disabled`·Task 2 `ErrorBox(AuthError)` 스모크.
2. 실전 계좌(real)를 고르면 상단 바·계좌 칩·승인 행이 전부 `--real` 표식을 보여야 하고 승인은 확인 대화상자를 거쳐야 한다 → Task 3 `accountChip(real)`·Task 7 `ConfirmDialog` 스모크.
3. 데이터가 없는 날(결정 0·스냅샷 없음·피처 없음)에 모든 페이지가 빈 상태 안내("무엇을 하면 채워지는지")를 보여야 한다 → Task 5 `buildTape([])`, Task 6·8 EmptyState 스모크.
4. 다크 모드에서도 적청·황·실전 표식 대비가 4.5:1 이상이어야 한다 → Task 1 `tokens.test.ts` 가 두 테마의 전경/배경 쌍 대비를 계산.
5. 390px 에서 2열 레이아웃이 1열로 접히고 표는 컨테이너 안에서만 스크롤해야 한다 → Task 9 모바일 스크린샷 점검(자동 테스트 없음, 리뷰 항목).

---

## 파일 구조

```
web/package.json                 + pretendard
web/index.html                   title·파비콘(새 마크 SVG)·<html data-theme>
web/public/favicon.svg           새 마크(시세표 세계: 미색 바탕에 먹 ▲ 블록) — icons.svg 삭제
web/src/styles/tokens.css        :root 라이트 / [data-theme=dark] / prefers-color-scheme 다크
web/src/styles/base.css          reset·서체·숫자 tnum·selection·caret·scrollbar·focus·표 괘선 리듬
web/src/styles/shell.css         상단 바·사이드 내비·서랍·페이지 헤더·그리드
web/src/styles/components.css    Badge·Section·Table·EmptyState·Skeleton·Toast·Dialog·Term·버튼·폼
web/src/theme.ts                 themeStore(get/set/resolve) + applyTheme(document)
web/src/glossary.ts              용어 사전 {key: {label, help}}
web/src/format.ts                fmtMoney(market)·fmtPct·fmtDelta·fmtTime(tz) (기존 pages/features.fmt 는 유지)
web/src/tape.ts                  buildTape(...)·marketPhase(now) 순수 함수 (대시보드 시세표 행)
web/src/chart.ts                 sma·markerStyle·formatSessionTime·chartPalette (pages/Chart 에서 이동)
web/src/components/Glyph.tsx     SVG 아이콘 집합
web/src/components/Table.tsx     Table·Th·Td·Num·Delta·Mark(●○☆)
web/src/components/Section.tsx   h2 + 2px 괘선 + 보조문
web/src/components/Badge.tsx     env·mode·status·action 배지
web/src/components/EmptyState.tsx / Skeleton.tsx / Toast.tsx(ToastProvider, useToast) / ConfirmDialog.tsx(<dialog>) / Term.tsx
web/src/components/Shell.tsx     TopBar+SideNav+서랍(기존 Nav.tsx·AccountPicker.tsx 대체)
web/src/components/Tables.tsx    기존 표 4개를 Table 원시요소로 재작성(ErrorBox 유지)
web/src/components/JsonView.tsx  토큰 색으로 재스타일(구조 유지)
web/src/pages/*.tsx              10 페이지 재구성
web/src/*.test.ts                tokens·theme·tape·format·glossary·chart(이동)·components 스모크
```

---

### Task 1: 토큰·베이스·서체·테마

**Files:**
- Create: `web/src/styles/tokens.css`, `web/src/styles/base.css`, `web/src/theme.ts`
- Modify: `web/package.json`(`pretendard`), `web/src/main.tsx`(css import 순서: pretendard → tokens → base), `web/index.html`(`<html lang="ko" data-theme="">`)
- Delete 내용: `web/src/index.css` 는 빈 파일로 두지 말고 삭제하고 import 제거
- Test: `web/src/tokens.test.ts`, `web/src/theme.test.ts`

**Interfaces:**
- Produces: `theme.ts` — `type Theme = "light" | "dark" | "system"`, `themeStore.get(): Theme`(localStorage `trader.theme`, 기본 `"system"`), `themeStore.set(t)`, `resolveTheme(t, prefersDark: boolean): "light" | "dark"`, `applyTheme(doc: Document, t: Theme)`(`<html data-theme>` 에 resolve 값).
- Produces: 토큰 이름(둘 다 테마가 정의): `--paper --panel --ink --ink-2 --ink-3 --rule --rule-strong --up --dn --pending --danger --ok --real --paper-env --sel --focus`, 간격 `--s-1..--s-8`(4·8·12·16·24·32·48·64px), 타입 `--t-xs 12 --t-sm 13 --t-md 14 --t-lg 16 --t-xl 21 --t-2xl 28`(rem 고정), `--radius 4px`, `--shadow`(offset+blur).
- Produces: `base.css` 의 유틸 클래스 `.num`(tnum·우측정렬) `.up .dn .muted .sr-only`.

- [ ] **Step 1: 테스트 작성** — `tokens.test.ts`: `tokens.css` 를 텍스트로 읽어 `:root` 와 `[data-theme="dark"]` 블록의 `--ink`/`--paper`, `--up`/`--paper`, `--dn`/`--paper`, `--pending`/`--paper`, `--real`/`--paper` 쌍의 WCAG 대비(간단 상대휘도 함수 테스트 안에 구현)가 **4.5 이상**임을 assert. `theme.test.ts`: `resolveTheme("system", true) === "dark"`, `resolveTheme("light", true) === "light"`, `themeStore` set/get 왕복, `applyTheme` 가 `data-theme` 를 쓴다.
- [ ] **Step 2: 실패 확인** `cd web && npx vitest run src/tokens.test.ts src/theme.test.ts` → 모듈 없음으로 FAIL
- [ ] **Step 3: 구현** — `npm i pretendard`; `tokens.css`(라이트 미색 `#F6F4EE`/먹 `#17181A`/적 `#C8322B`/청 `#2F6FD1`/황 `#B7791F`/실전 `#C8322B`… 다크는 먹 `#141516` 바탕에 미색 글자, 적청은 밝기 올려 대비 확보); `base.css`(reset, `font-family: "Pretendard Variable", Pretendard, -apple-system, "Apple SD Gothic Neo", "Malgun Gothic", sans-serif`, `font-variant-numeric: tabular-nums`, `::selection`·`caret-color`·`scrollbar-color`·`:focus-visible` 토큰, `@media (prefers-reduced-motion)`); `theme.ts`.
- [ ] **Step 4: 통과 확인** 같은 명령 → PASS. `npm run typecheck`.
- [ ] **Step 5: Commit** `feat(web): 디자인 토큰·베이스 스타일·Pretendard·테마 저장소`

### Task 2: 아이콘·표 원시요소·공통 컴포넌트

**Files:**
- Create: `web/src/components/{Glyph,Table,Section,Badge,EmptyState,Skeleton,Toast,ConfirmDialog,Term}.tsx`, `web/src/styles/components.css`, `web/src/glossary.ts`, `web/src/format.ts`
- Test: `web/src/components.test.tsx`(`renderToStaticMarkup` 스모크), `web/src/format.test.ts`, `web/src/glossary.test.ts`

**Interfaces:**
- `Glyph({name, size?, title?})` — name ∈ `up|down|held|pick|watch|menu|close|check|x|sun|moon|system|refresh|alert|info|chevron`.
- `Table({children, dense?, sticky?})`, `Th({num?, children})`, `Td({num?, children})`, `Num({value: string|number|null, market?: "KR"|"US"})`(fmtMoney), `Delta({value: number|null, pct?: boolean})`(부호·▲▼ Glyph·`.up/.dn`), `Mark({kind: "held"|"pick"|"watch"})`.
- `Section({title, aside?, children})`, `Badge({kind: "env"|"mode"|"status"|"action"|"pending", value})`, `EmptyState({title, hint, action?})`, `Skeleton({rows?})`, `ToastProvider`·`useToast(): (msg, kind?) => void`, `ConfirmDialog({open, title, body, confirmLabel, danger?, onConfirm, onClose})`(`<dialog>` + `showModal`), `Term({k})`(glossary 라벨 + `title`/포커스 시 help).
- `format.ts`: `fmtMoney(v, market)`(KR 정수 천단위 + "원", US `$` 소수 2), `fmtPct(v, digits=2)`, `fmtDelta(cur, prev) -> {abs, pct}|null`, `fmtTime(iso, market)`(KR KST·US ET `HH:MM`), `fmtDate(iso)`.
- `glossary.ts`: `GLOSSARY: Record<string, {label: string; help: string}>` 키 `action.BUY|SELL|HOLD|ADD|TRIM, target_weight, stop_loss, take_profit, confidence, stance, mode.auto|approve|off, dry_run, trigger, sycophantic, stage.00_inputs|10_analyst|20_debate|25_research_summary|30_decision|40_risk_check|50_order|45_stop_loss|31_decision|41_risk_check|51_order`. `termLabel(k)`·`termHelp(k)`(없으면 k 그대로).

- [ ] **Step 1: 테스트** — `format.test.ts`: `fmtMoney("2580000","KR") === "2,580,000원"`, `fmtMoney("231.8","US") === "$231.80"`, `fmtDelta(232500, 229000)` → `{abs: 3500, pct: 1.53}`(소수 2자리 반올림), `fmtTime("2026-10-23T00:31:00+00:00","KR") === "09:31"`, `"US"` → `"20:31"`. `glossary.test.ts`: 위 키 전부 존재, `termLabel("stop_loss") === "손절선"`. `components.test.tsx`: `Delta value=1.53 pct` 마크업에 `class="up"` 과 `<svg`; `EmptyState` 에 hint 텍스트; `Badge kind="env" value="real"` 에 `data-env="real"`; `Table` 머리행 `th` 에 `num` 클래스.
- [ ] **Step 2: 실패 확인** `npx vitest run` → FAIL
- [ ] **Step 3: 구현** (components.css 는 토큰만 참조)
- [ ] **Step 4: 통과** `npx vitest run && npm run typecheck`
- [ ] **Step 5: Commit** `feat(web): 아이콘·표 원시요소·배지·빈 상태·토스트·대화상자·용어 사전`

### Task 3: 셸(상단 바·사이드 내비·서랍)·App·api 정리

**Files:**
- Create: `web/src/components/Shell.tsx`, `web/src/styles/shell.css`, `web/src/tape.ts`(`marketPhase` 만 이 Task)
- Modify: `web/src/App.tsx`(Shell 로 감싸고 ToastProvider), `web/src/api.ts`(`EventSnapshot` 을 서버 `sse.py` 스냅샷과 일치: `orders approvals_pending approvals runs decisions triggers watchlist paused`), `web/src/hooks.ts`(테마 상태 추가 `theme/setTheme`), `web/index.html`, `web/public/favicon.svg`
- Delete: `web/src/components/Nav.tsx`, `web/src/components/AccountPicker.tsx`, `web/public/icons.svg`, `web/src/index.css`
- Test: `web/src/tape.test.ts`(`marketPhase`), `web/src/api.test.ts`(기존 유지 + EventSnapshot 키 테스트는 타입이라 생략), `components.test.tsx` 에 `Shell` 스모크 추가(계좌 real 이면 `data-env="real"` 칩, 미승인 n 배지)

**Interfaces:**
- `marketPhase(now: Date): {kr: "pre"|"open"|"closed", us: "pre"|"open"|"closed", label: string}` — KR 09:00~15:30 KST, US 09:30~16:00 ET(주말은 closed). 라벨 예 `"KR 정규장 마감 · US 개장 전"`.
- `Shell({children})` — 상단 바 54px: 브랜드 `trader`(새 마크 Glyph), 계좌 `<select>`(옵션 라벨 `id · 모의|실전 · mode`, 실전이면 `data-env="real"`), `marketPhase` 라벨, 우측 배지(일시정지 → `pending`, 미승인 n → `pending`, 실행 상태 ok, 토큰 유무), 테마 버튼(sun/moon/system 순환). 사이드 내비 188px(≥1024px), 그 아래는 상단 서랍(`menu` Glyph, `<nav aria-expanded>`). 본문 `<main>` 최대폭 없음, 패딩 `--s-5`.
- `AppState` 에 `theme: Theme; setTheme(t)`.

- [ ] **Step 1: 테스트** `marketPhase(new Date("2026-10-23T13:41:00+09:00"))` → `kr:"open"`, `us:"closed"`; `"2026-10-23T22:41:00+09:00"` → `kr:"closed", us:"pre"`; 토요일 → 둘 다 closed. Shell 스모크(AppContext 가짜 값으로 `renderToStaticMarkup` — `MemoryRouter` 사용).
- [ ] **Step 2: 실패 확인**
- [ ] **Step 3: 구현** — 스케치 `assigned-r1.html` 의 상단 바·내비 치수·괘선 그대로. 파비콘은 미색 바탕 원 + 먹 ▲ 의 16/32px SVG 한 파일.
- [ ] **Step 4: 통과** `npx vitest run && npm run typecheck && npm run build`
- [ ] **Step 5: Commit** `feat(web): 셸(상단 바·사이드 내비·서랍·테마)·App 재구성·SSE 타입 일치·템플릿 잔재 삭제`

### Task 4: 차트 헬퍼 이동·차트/성과 페이지

**Files:**
- Create: `web/src/chart.ts`
- Modify: `web/src/pages/Chart.tsx`, `web/src/pages/Performance.tsx`, `web/src/chart.test.ts`·`sma.test.ts`(import 를 `../chart` 로)
- Test: `chart.test.ts` 에 `chartPalette` 폴백 테스트 추가

**Interfaces:**
- `chart.ts`: `sma`, `formatSessionTime` 그대로 이동. `markerStyle(kind, side, palette = DEFAULT_PALETTE)` → 색은 palette 에서(`order: palette.ink`, `decision: palette.dn`, `trigger: palette.pending`, 손절·SELL 은 `palette.up`). `chartPalette(el: Element): Palette` 는 `getComputedStyle(el)` 로 `--ink --paper --panel --rule --up --dn --pending` 을 읽고 비면 `DEFAULT_PALETTE`(라이트 토큰 값과 동일). `type Palette = {ink, paper, panel, rule, up, dn, pending}`.
- 차트 생성 옵션(배경·격자·글자색·크로스헤어)은 palette 에서, 테마가 바뀌면 `applyOptions`. 마커 지름 3단계: 확신 <0.5 작게, <0.7 중간, 이상 크게(`markerSize(conf)` export, 테스트).

- [ ] **Step 1: 테스트** 기존 `chart.test.ts` 의 hex 비교를 `DEFAULT_PALETTE` 참조로 바꾸고, `markerStyle("order","BUY",{...custom})` 가 custom 색을 쓰는지, `markerSize(0.72) === 2`(0/1/2), `chartPalette(빈 element)` → DEFAULT.
- [ ] **Step 2: 실패 확인** (import 경로 변경으로 FAIL)
- [ ] **Step 3: 구현** — Chart 페이지: 조회 폼(시장·종목·계좌·간격·SMA 토글)을 Section 안 한 줄 폼으로, 차트 아래 마커 범례를 Glyph+텍스트로. Performance: 지표를 **시세표 괘선 표**로(카드 타일 아님: Section "성과" 안 2열 표 — 지표명 | 값), NAV 차트는 palette, 비용 민감도 표는 Table, 결정 분포·수렴율은 같은 표.
- [ ] **Step 4: 통과** `npx vitest run && npm run typecheck`
- [ ] **Step 5: Commit** `refactor(web): 차트 헬퍼를 chart.ts 로, 팔레트 토큰화·차트/성과 페이지 재구성`

### Task 5: 대시보드(운영 상태 줄·오늘의 시세표·주문/승인·성과 블록)

**Files:**
- Modify: `web/src/pages/Dashboard.tsx`, `web/src/tape.ts`
- Test: `web/src/tape.test.ts`

**Interfaces:**
- `buildTape(input: {positions: Position[]; decisions: Decision[]; riskChecks: RiskCheck[]; orders: Order[]; watch: string[]; market?: Market}) : TapeRow[]` — `TapeRow = {key, ticker, market, mark: "held"|"pick"|"watch"|null, qty, price: string|null, weight: number|null(평가금액/NAV 는 호출자가 nav 를 넘겨 계산: 인자 `nav?: string`), action: string|null, confidence: number|null, decisionId: number|null, gate: {approved: boolean|null, text: string}, order: {status: string|null, text: string}}`. 정렬: 보유 → 선정(결정 있음) → 관심, 같은 묶음 안은 ticker. 결정이 없고 보유도 아닌 관심종목은 `action:null, gate.text:"분석 없음"`. 게이트 text: 승인이면 `"<qty>주 <상태>"`(주문 있으면 주문 상태·금액), 거부면 `"거부 · <첫 사유>"`, HOLD 통과는 `"통과 · 주문 없음"`.
- 대시보드 API: 계좌 context + `/api/portfolio?market&account_id`(시장별로 2회: KR·US), `/api/decisions?days=1`, `/api/risk-checks`·`/api/orders`·`/api/approvals`(withAccount), `/api/calls?days=1`, `/api/runs?days=1`(마지막 잡), `/api/performance?market&days=30&account_id`(성과 블록 — 응답 형태는 `Performance.tsx` 의 `Report` 타입을 `types.ts` 로 옮겨 공유), `GET /api/control/watchlist`. 선택 행의 근거는 `/api/checkpoint?stage=30_decision` 의 `data.rationale`(없으면 "근거 없음").
- 구성(스케치 그대로): 제목+날짜/장 상태 → 운영 상태 한 줄(2px 선 사이: 데몬/마지막 잡/세션 n/12·비용/미승인 n(만료 분)/오류 n/일시정지) → 2열(1fr | 330px): 왼쪽 Section "오늘의 시세표" Table(열: 종목·현재가·보유·비중·결정·확신·손절·게이트·실전 — 등락은 전일 종가가 API 에 없으므로 **열에서 뺀다**, 발명 금지) + 선택 행 근거 한 줄 + Section "오늘의 주문·승인"(시각순, 만료 분 표시), 오른쪽 Section "성과 30일"·"모델" 표. 390px: 1열.
- 로딩: Skeleton, 결정 0건: EmptyState("오늘 결정이 아직 없어요", "scan → analyze 가 돌면 채워져요. 데몬 상태를 확인하세요").

- [ ] **Step 1: 테스트** `buildTape` 정렬·mark·gate.text 3케이스(승인+체결, 거부, HOLD), 빈 입력 → `[]`, 관심종목만 → `gate.text === "분석 없음"`.
- [ ] **Step 2: 실패 확인** → **Step 3: 구현** → **Step 4: 통과** `npx vitest run && npm run typecheck`
- [ ] **Step 5: Commit** `feat(web): 대시보드 — 운영 상태 줄·오늘의 시세표·주문/승인·성과 블록`

### Task 6: 오늘의 실행(파이프라인 판)·데이터 페이지

**Files:**
- Modify: `web/src/pages/Today.tsx`, `web/src/pages/Data.tsx`, `web/src/pages/features.ts`(유지)
- Create: `web/src/pipeline.ts`
- Test: `web/src/pipeline.test.ts`

**Interfaces:**
- `pipelineStages(stages: string[]): Stage[]` — 체크포인트 파일명 목록을 순서 `00_inputs → 05_quant → 10_analyst_* → 20_debate_* → 25_research_summary → 30_decision → 31_decision_r* → 35_calibration → 40_risk_check_* / 41_* → 45_stop_loss_* → 50_order_* / 51_* / 55_*` 로 묶은 `Stage = {key, label(termLabel), done: boolean, files: string[], summary: string|null}` 배열(모든 단계가 **같은 크기 판**으로 한 줄에; 없는 단계는 빈 판). `stageSummary(stage, data)` 는 단계마다 한 줄("보탠 것"): 00 → `오류 절 n건`, 10 → `강세 n·중립 n·약세 n`, 20 → `r<n> bull/bear`, 25 → `sycophantic 이면 "붕괴 · r0 우선"` 아니면 `우세 측`, 30 → `action · 비중 · 확신`, 40 → `승인 n주 | 거부 · 사유`, 50 → `status · 금액`.
- Today 페이지: 시장·날짜 폼 → 종목 탭(버튼 묶음, 보유/선정 Mark) → 파이프라인 판 띠(판 클릭 = 그 단계 체크포인트 열기) → 뷰어(토론은 DebateView, 그 밖은 JsonView; `call` 은 details). Data 페이지: 00_inputs 뷰어(TodayPage inputsOnly 유지) + 피처 최신 행 Table(정렬 헤더 Glyph, 인라인 style 제거) + 이력 Table(스크롤 컨테이너).

- [ ] **Step 1: 테스트** `pipelineStages(["00_inputs","10_analyst_technical","10_analyst_fundamental","30_decision","40_risk_check_paper-main"])` → 순서·done 플래그·`files` 묶음; `stageSummary("25_research_summary",{sycophantic:true})` → `"붕괴 · r0 우선"`.
- [ ] **Step 2~4** 실패 → 구현 → 통과
- [ ] **Step 5: Commit** `feat(web): 오늘의 실행 파이프라인 판·데이터 페이지 재구성`

### Task 7: 주문/승인·계좌·계좌 비교

**Files:**
- Modify: `web/src/pages/Orders.tsx`, `web/src/pages/Account.tsx`, `web/src/pages/Compare.tsx`, `web/src/components/Tables.tsx`
- Test: `components.test.tsx` 에 ApprovalsTable 스모크(토큰 없음 → 버튼 없음·안내; real 계좌 → `data-env="real"` 행)

- 주문/승인: Section "승인 대기"(만료까지 분, 승인/거부 버튼 — 실전이면 `ConfirmDialog(danger)`; `window.confirm` 제거; 결과는 Toast), "주문"(시각순 Table, 상태 Badge, `unknown` 은 `pending` 색 + "상태 미확인"), "리스크 판정"(사유 전부 표시). 계좌: 요약 표(NAV·현금·스냅샷 날짜·성향 Term) + 포지션 Table(평단·현재가·평가금액·손익은 평단 대비 Delta — 둘 다 있을 때만) + 미승인·최근 주문; refresh 버튼은 토큰 없으면 disabled + Term help. 비교: 왼쪽 결정 Table(선택 행 `.sel`), 오른쪽 상세(rationale·계좌별 결과 Table, 실전 행 표식). 모바일: 비교는 1열(선택 시 상세가 아래).
- [ ] 테스트 → 실패 → 구현 → 통과 → Commit `feat(web): 주문/승인·계좌·계좌 비교 재구성(확인 대화상자·토스트)`

### Task 8: ML·설정/제어

**Files:**
- Modify: `web/src/pages/Ml.tsx`, `web/src/pages/Settings.tsx`
- Test: `components.test.tsx` 에 Settings 폼 스모크(토큰 없으면 제어 버튼 disabled)

- ML: 활성 모델 요약 줄 → 2열(모델 Table | 상세: 폴드 Table·중요도 막대(토큰 색, 값 Num)·메타 JsonView) → RL 실험·섀도 Table. 빈 상태: "학습된 모델이 없어요 · `trader ml train`". 설정/제어: Section 묶음 — 토큰(입력·저장·지우기, 상태 Badge), 거래(일시정지/재개 — 현재 상태 Badge), 계좌 모드(계좌별 행: 현재 effective mode Badge + select + 실전 auto 는 LIVE 입력, 변경은 ConfirmDialog), 관심종목(칩 + 추가 입력, KR:/US: 접두 help), 테마(라이트/다크/시스템), 설정 JSON(JsonView), STATUS.md(`<pre>` 토큰 색).
- [ ] 테스트 → 실패 → 구현 → 통과 → Commit `feat(web): ML·설정/제어 페이지 재구성`

### Task 9: 검증·리뷰·문서화 (impeccable §7 + 프로젝트 문서)

**Files:**
- Create: `DESIGN.md`, `.impeccable/design.json`(documenter 산출), `.impeccable/review/{desktop,mobile}.png`
- Modify: `.gitignore`(`.impeccable/mocks/`, `.impeccable/review/`, `.impeccable/build/`, `.impeccable/config.local.json`), `CLAUDE.md`(웹 UI 절 한 줄), `README.md`·`docs/bundle-usage.md`(웹 UI 설명·스크린샷 문구), `docs/TODO.md`(Phase 14 체크)

- [ ] **Step 1** `cd web && npm run typecheck && npm test && npm run build && cd .. && uv run pytest tests/web && uv run ruff check src tests` 전부 통과.
- [ ] **Step 2** `uv run trader web` 띄우고 Chrome 도구로 1440·390 × 라이트/다크 × 10페이지 + 상태(빈·오류·토큰 없음·대화상자 열림)를 한 번에 캡처, 어긋난 것 고치고 한 번 더(상한 2회). `desktop.png`·`mobile.png` 를 `.impeccable/review/` 에 저장.
- [ ] **Step 3** `impeccable detect --json web/src web/index.html` 1회 → 기계적 지적 수정.
- [ ] **Step 4** `impeccable-finish-reviewer` 서브에이전트(원 요청·답변·방향 계약 경로·스케치 경로·스크린샷·craft-floor 경로·detect 결과) → disposition 대로 fix 1배치·재캡처·판정.
- [ ] **Step 5** `impeccable-documenter` 서브에이전트 → `DESIGN.md` + `.impeccable/design.json` 확인.
- [ ] **Step 6** 문서·gitignore·CLAUDE.md·TODO 갱신, `uv run trader status`, Commit `docs(web): DESIGN.md·리뷰 산출물·문서 갱신`.
