---
name: trader
description: 신문 증권면 시세표 — 미색 용지 위 먹 잉크, 적청은 데이터 의미에만, 모든 표가 같은 열 문법.
colors:
  paper: "#F6F4EE"
  panel: "#FCFBF8"
  sel: "#EDEAE0"
  ink: "#17181A"
  ink-2: "#585D64"
  ink-3: "#7A7F86"
  rule: "#DAD6CC"
  rule-strong: "#17181A"
  up: "#C8322B"
  dn: "#2860BF"
  pending: "#8A5A00"
  pending-bg: "#FFF3D1"
  danger: "#C8322B"
  danger-bg: "#FBE4E2"
  ok: "#1E6B41"
  ok-bg: "#E3F3EA"
  real: "#B3261E"
  real-bg: "#FBE9E7"
  paper-env: "#1F4E9B"
  paper-env-bg: "#E8F0FB"
  focus: "#2860BF"
  dark-paper: "#141516"
  dark-panel: "#1C1D1F"
  dark-sel: "#2A2B2E"
  dark-ink: "#EDE9DF"
  dark-ink-2: "#B3AFA6"
  dark-ink-3: "#8A8780"
  dark-rule: "#34353A"
  dark-up: "#F06A5E"
  dark-dn: "#6FA3F5"
  dark-pending: "#E6B35A"
  dark-pending-bg: "#3A2E12"
  dark-danger-bg: "#3E1F1C"
  dark-ok: "#6CCB8F"
  dark-ok-bg: "#16311F"
  dark-real: "#FF7B70"
  dark-paper-env: "#8FB4F0"
  dark-paper-env-bg: "#1B2A44"
typography:
  display:
    fontFamily: "Pretendard Variable, Pretendard, -apple-system, Apple SD Gothic Neo, Malgun Gothic, Noto Sans KR, sans-serif"
    fontSize: "1.3125rem"
    fontWeight: 900
    lineHeight: 1.2
    letterSpacing: "-0.01em"
  headline:
    fontFamily: "Pretendard Variable, Pretendard, -apple-system, Apple SD Gothic Neo, Malgun Gothic, Noto Sans KR, sans-serif"
    fontSize: "1.3125rem"
    fontWeight: 800
    lineHeight: 1.2
    letterSpacing: "-0.01em"
  title:
    fontFamily: "Pretendard Variable, Pretendard, -apple-system, Apple SD Gothic Neo, Malgun Gothic, Noto Sans KR, sans-serif"
    fontSize: "0.875rem"
    fontWeight: 800
    lineHeight: 1.45
  body:
    fontFamily: "Pretendard Variable, Pretendard, -apple-system, Apple SD Gothic Neo, Malgun Gothic, Noto Sans KR, sans-serif"
    fontSize: "0.875rem"
    fontWeight: 400
    lineHeight: 1.45
    fontFeature: "\"tnum\" 1, \"ss05\" 1"
  tape:
    fontFamily: "Pretendard Variable, Pretendard, -apple-system, Apple SD Gothic Neo, Malgun Gothic, Noto Sans KR, sans-serif"
    fontSize: "0.8125rem"
    fontWeight: 400
    lineHeight: 1.45
    fontFeature: "\"tnum\" 1, \"ss05\" 1"
  label:
    fontFamily: "Pretendard Variable, Pretendard, -apple-system, Apple SD Gothic Neo, Malgun Gothic, Noto Sans KR, sans-serif"
    fontSize: "0.75rem"
    fontWeight: 600
    lineHeight: 1.3
  mono:
    fontFamily: "ui-monospace, SF Mono, Menlo, Consolas, monospace"
    fontSize: "0.92em"
rounded:
  base: "4px"
  skeleton: "2px"
  round: "50%"
spacing:
  s-1: "4px"
  s-2: "8px"
  s-3: "12px"
  s-4: "16px"
  s-5: "24px"
  s-6: "32px"
  s-7: "48px"
  s-8: "64px"
components:
  button:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink}"
    typography: "{typography.tape}"
    rounded: "{rounded.base}"
    padding: "5px 12px"
  button-hover:
    backgroundColor: "{colors.sel}"
    textColor: "{colors.ink}"
  button-active:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.paper}"
  button-primary:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.paper}"
    typography: "{typography.tape}"
    rounded: "{rounded.base}"
    padding: "5px 12px"
  button-primary-hover:
    backgroundColor: "{colors.ink-2}"
    textColor: "{colors.paper}"
  button-danger:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.danger}"
    typography: "{typography.tape}"
    rounded: "{rounded.base}"
    padding: "5px 12px"
  button-danger-hover:
    backgroundColor: "{colors.danger-bg}"
    textColor: "{colors.danger}"
  button-danger-active:
    backgroundColor: "{colors.danger}"
    textColor: "{colors.paper}"
  button-ghost:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink}"
    typography: "{typography.tape}"
    rounded: "{rounded.base}"
    padding: "5px 12px"
  button-sm:
    typography: "{typography.label}"
    padding: "2px 8px"
  input:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink}"
    typography: "{typography.tape}"
    rounded: "{rounded.base}"
    padding: "5px 8px"
    height: "30px"
  badge:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink-2}"
    typography: "{typography.label}"
    rounded: "{rounded.base}"
    padding: "1px 8px"
  badge-pending:
    backgroundColor: "{colors.pending-bg}"
    textColor: "{colors.pending}"
  badge-ok:
    backgroundColor: "{colors.ok-bg}"
    textColor: "{colors.ok}"
  badge-danger:
    backgroundColor: "{colors.danger-bg}"
    textColor: "{colors.danger}"
  badge-env-paper:
    backgroundColor: "{colors.paper-env-bg}"
    textColor: "{colors.paper-env}"
  badge-env-real:
    backgroundColor: "{colors.real-bg}"
    textColor: "{colors.real}"
  chip:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink}"
    typography: "{typography.label}"
    rounded: "{rounded.base}"
    padding: "2px 8px"
  tape-cell:
    typography: "{typography.tape}"
    padding: "8px 8px"
  tape-cell-dense:
    typography: "{typography.tape}"
    padding: "5px 8px"
  tape-row-selected:
    backgroundColor: "{colors.sel}"
  toast:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.paper}"
    typography: "{typography.tape}"
    rounded: "{rounded.base}"
    padding: "8px 12px"
  dialog:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink}"
    rounded: "{rounded.base}"
    padding: "24px"
    width: "440px"
  plate:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink-2}"
    rounded: "{rounded.base}"
    padding: "8px 12px"
    height: "96px"
  sidenav-link:
    textColor: "{colors.ink-2}"
    typography: "{typography.body}"
    padding: "8px 24px"
  sidenav-link-active:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ink}"
---

# Design System: trader

## Overview

**Creative North Star: "아침 식탁의 증권면"**

trader 웹 UI 는 아침 신문의 증권면 시세표다. 미색 용지(`paper`) 위에 먹 잉크(`ink`)로 찍힌 한 줄 한 종목의 표가 화면의 몸통이고, 포지션·결정·주문·성과가 전부 같은 열 리듬(이름 셀 + 기호, 우측정렬 tabular 숫자, 2px 머리선 + 1px 행 괘선)으로 읽힌다. 색은 장식이 아니라 데이터다. 적(`up`)은 상승·매수, 청(`dn`)은 하락·매도, 황(`pending`)은 눌러야 할 것(미승인·대기), 실전 계좌는 적색 테두리 표식으로 어느 화면에서도 모의와 구분된다. 밤에는 같은 지면을 먹 바탕(`dark-paper`)에 미색 글자로 뒤집되 적청과 괘선 리듬은 그대로 둔다.

밀도는 운영 콘솔의 밀도다: 분 단위 데이터를 하루 몇 번 1~2분에 읽는 자리라 숫자는 촘촘하고 설명은 한 줄이다. 위계는 서체 하나(Pretendard)의 크기·굵기 대비 하나로만 만들고, 그림자·카드 격자·큰 KPI 타일은 쓰지 않는다. 선택한 행만 미색 음영(`sel`)으로 들리고 나머지는 지면에 가라앉는다. 상태는 색이 아니라 기호(● 보유, ○ 선정, ☆ 관심, ▲▼ 등락)로 먼저 말한다.

거부한 것(방향 계약으로 확인): 같은 크기 카드 격자 + 큰 숫자 KPI 타일 + 파란 강조의 범주 표준, 터미널·블룸버그 코스튬, 이모지·유니코드 기호 아이콘.

**Key Characteristics:**
- 모든 표가 같은 열 문법 — 2px 먹 머리선, 1px 미색 행 괘선, 우측정렬 `tnum` 숫자, 이름 셀 굵게 + 코드 작게
- 적청황은 데이터 의미 전용(등락·결정 방향·대기·실전), 브랜드 강조색 없음
- 서체 하나(Pretendard Variable, 자체 호스팅), rem 고정 스케일 6단, 굵기 400~900 으로만 위계
- 평면. 그림자는 떠 있는 층(서랍·토스트·대화상자)에만, 반경 4px 하나
- 라이트/다크가 같은 변수 이름을 공유하고 `tokens.test.ts` 가 두 테마 모두 4.5:1 대비를 고정

## Colors

미색 종이와 먹의 2톤 중립 위에, 신문 시세표의 적청과 교정지의 황·녹을 의미색으로 얹은 팔레트다.

### Primary
- **먹 잉크** (`ink`): 본문·제목·표 숫자·기본 버튼 배경·선택된 버튼·토스트 배경. 머리선(`rule-strong`)도 같은 값이다. 이 세계의 "강조"는 색이 아니라 먹의 굵기다.
- **회색 잉크 2단** (`ink-2`, `ink-3`): `ink-2` 는 보조문·표 머리글·내비 비활성·`muted`, `ink-3` 는 정렬 아이콘·용어 점선 밑줄·입력 hover 테두리처럼 가장 뒤로 물러나는 획.

### Secondary (데이터 의미색)
- **상승 적** (`up`): 등락 ▲ 숫자, BUY/ADD 결정 라벨, JSON 뷰어의 boolean. `danger` 와 같은 값이지만 토큰은 나눠 둔다(의미가 다르다).
- **하락 청** (`dn`): 등락 ▼ 숫자, SELL/TRIM 결정 라벨, 차트의 결정 마커 원, JSON 뷰어의 number, 포커스 링(`focus`).
- **대기 황** (`pending`, `pending-bg`): 미승인·일시정지·accepted/unknown/running 상태 배지, approve 모드 배지, 차트 트리거 마커, 범례 사각형. 화면에서 "눌러야 할 것"은 이 색으로만 켜진다.
- **승인 녹** (`ok`, `ok-bg`): filled/approved/ok 상태 배지, auto 모드 배지, 게이트 통과, 완료 단계 체크, 제어 가능 배지.
- **거부 적** (`danger`, `danger-bg`): rejected/failed/error 배지, 게이트 거부, 오류 박스, 위험 버튼, 오류 토스트.

### Tertiary (계좌 환경 표식)
- **실전 적** (`real`, `real-bg`): 실전 계좌 배지(적색 테두리 + 연적 바탕), 실전일 때 상단 바 밑선과 계좌 select 테두리. 모의와 섞이지 않게 전 화면에서 한 가지 표식으로만 쓴다.
- **모의 청** (`paper-env`, `paper-env-bg`): 모의 계좌 배지(테두리 없음). `dn` 과 다른 값이라 등락과 혼동되지 않는다.

### Neutral
- **미색 용지** (`paper`): body 바탕, 활성 내비 항목 바탕, 먹 배경 위 글자색(버튼 primary·토스트·선택 텍스트).
- **판** (`panel`): 상단 바·내비·버튼·입력·배지·플레이트·뷰어·대화상자 바탕. 용지보다 한 단 밝은 종이.
- **선택 음영** (`sel`): 선택 행·근거 박스(`why-box`)·hover 바탕(행 hover 는 50% 섞음)·스켈레톤 반짝임.
- **괘선** (`rule`): 표 행 밑선, 입력·배지·칩·플레이트·뷰어 테두리, 내비 우측선, 차트 격자, 스크롤바.
- **먹선** (`rule-strong`): 표 머리 밑선, 절 제목 밑선, 운영 상태 줄 위선, 상단 바 밑선, 대화상자 테두리.
- **다크 테마** (`dark-*`): 같은 역할의 뒤집힌 값. `dark-rule-strong`·`dark-danger`·`dark-real-bg` 는 각각 `dark-ink`·`dark-up`·`dark-danger-bg` 와 같다.

### Named Rules
**The Ink-Only Emphasis Rule.** 강조는 먹의 굵기(2px 선, 굵기 800~900, 먹 바탕 반전)로만 한다. 적·청·황·녹은 등락·결정 방향·상태·환경이라는 데이터 의미가 있을 때만 쓰고, 브랜드나 장식으로 쓰지 않는다.

**The Hex-In-One-File Rule.** hex 는 `web/src/styles/tokens.css` 에만 적고 나머지 CSS·TSX 는 `var(--…)` 만 쓴다. 차트(`chart.ts`)는 런타임에 같은 변수를 `getComputedStyle` 로 읽는다.

**The Red-Is-Up Rule.** 한국 관행대로 상승이 적, 하락이 청이다. 색과 함께 반드시 ▲▼ 기호가 붙어 색각 이상에서도 읽힌다.

## Typography

**Display Font:** Pretendard Variable (자체 호스팅 npm `pretendard`, 폴백 -apple-system / Apple SD Gothic Neo / Malgun Gothic / Noto Sans KR)
**Body Font:** 같은 Pretendard Variable
**Label/Mono Font:** ui-monospace / SF Mono / Menlo / Consolas (JSON 뷰어·STATUS.md 의 `code`·`pre` 만)

**Character:** 서체 하나로 신문 지면을 짠다. 위계는 크기(6단 rem 스케일)와 굵기(400·500·600·700·800·900) 대비 하나뿐이고, 모든 숫자는 `tnum`(+ `ss05`)으로 열이 맞는다. 자간은 큰 제목에서만 −0.01em 으로 조인다.

### Hierarchy
- **Display** (900, 1.3125rem, 1.2, −0.01em): 성과 블록의 NAV 같은 "큰 숫자" 한 개(`big`). 한 절에 하나.
- **Headline** (800, 1.3125rem, 1.2, −0.01em): 페이지 제목 `h1`. 그 아래 `sub`(0.8125rem, `ink-2`)가 날짜·장 상태를 한 줄로 받는다. 브랜드 `trader` 워드마크는 900·1rem.
- **Title** (800, 0.875rem): 절 제목 `h2`(`sec-h`, 2px 먹 밑줄) · 플레이트 제목(0.8125rem) · 운영 상태 줄의 값(`statusline b`). `h3` 는 700·0.875rem.
- **Body** (400, 0.875rem, 1.45): 기본 본문·내비 항목. 행 길이 제한은 없다(표가 몸통이라).
- **Tape** (400, 0.8125rem, 1.45): 표 셀·버튼·입력·운영 상태 줄·대화상자 본문·근거 박스. 이 제품의 실제 본문 크기다. 이름 셀 `nm` 은 700, 코드는 `small` 500 `ink-2`.
- **Label** (600, 0.75rem, 1.3): 표 머리글(`ink-2`)·배지(700)·칩·필드 라벨·절 보조문(500)·근거 한 줄(`why`)·주석(`note`). 대문자 변환·자간 확장은 쓰지 않는다(결정 라벨 `badge[data-action]` 만 800 + 0.02em).
- **Mono** (0.92em): JSON 뷰어와 STATUS 원문만.

### Named Rules
**The One Face Rule.** 서체는 Pretendard 하나. 외부 폰트·CDN 금지(오프라인 번들). 위계는 크기·굵기 대비로만 만들고 색·자간·대문자로 만들지 않는다.

**The Tabular Rule.** 숫자가 들어가는 모든 셀은 `.num`(우측정렬 + `tabular-nums` + `nowrap`)이다. body 가 이미 `tnum` 이라 숫자 열은 어디서든 맞는다.

**The One Big Number Rule.** 1.3125rem 보다 큰 글자는 없다(`--t-2xl` 1.75rem 은 정의만 있고 쓰이지 않는다). 성과의 NAV 처럼 절당 한 숫자만 `big` 을 쓴다.

## Layout

셸은 CSS grid 2×2: 상단 바 54px(`--shell-top`) 전폭 + 좌측 내비 188px(`--shell-nav`) + 본문. 상단 바와 내비는 sticky, 본문 패딩은 16px 32px 64px. 본문은 `.cols`(1fr | 330px, 간격 24px — 시세표 + 성과 블록)와 `.cols-2`(1fr | 1fr)로 나뉘고 열은 `minmax(0, …)` 으로 표 넘침을 막는다.

세로 리듬은 절(`sec`) 단위: 절 사이 24px, 절 제목 아래 2px 먹선 + 8px, 표 행은 8px 8px 패딩(`dense` 는 5px 8px), 운영 상태 줄은 2px 위선 + 1px 아래선 사이 8px. 간격 토큰 8단(4/8/12/16/24/32/48/64) 중 4~24 가 거의 전부고 32·48·64 는 본문 패딩에만 나온다.

반응형 분기는 둘뿐이다.
- **1023px 이하**: 내비가 상단 바 아래 고정 서랍(폭 min(280px, 80vw), 왼쪽에서 160ms 슬라이드, 그림자)으로 바뀌고 햄버거 버튼이 나타난다. `.cols`·`.cols-2` 가 1열이 되고 본문 패딩은 16px 16px 48px. 장 상태 문구는 숨긴다.
- **599px 이하**: 상단 바에서 브랜드 글자·계좌 배지·`ok`/`neutral` 상태 배지를 숨긴다(미승인·일시정지 황색 배지는 남긴다).

표는 `.table-wrap` 으로 항상 가로 스크롤이 가능하고 셀은 `nowrap`(긴 글 셀만 `wrap`, 최소 200px). 모바일에서 표는 줄바꿈이 아니라 가로 스크롤이다.

## Elevation & Depth

평면이다. 깊이는 종이의 두 단(`paper` 바탕 위 `panel` 판)과 괘선 굵기(1px 괘선 / 2px 먹선), 그리고 선택 음영(`sel`)으로만 낸다. 표·절·플레이트는 그림자가 없고, 플레이트는 윗선만 2px 로 두꺼운 "판" 이며 완료되면 윗선이 먹색이 된다.

### Shadow Vocabulary
- **떠 있는 층** (`box-shadow: 0 2px 8px rgba(23, 24, 26, 0.12)`, 다크 `0 2px 10px rgba(0, 0, 0, 0.5)`): 모바일 내비 서랍·토스트·확인 대화상자. 지면 위로 실제로 올라온 요소에만.

### Named Rules
**The Flat-Page Rule.** 지면 안의 요소(표·절·배지·버튼·플레이트)는 그림자가 없다. 그림자 하나(`--shadow`)는 지면 위에 떠 있는 층(서랍·토스트·대화상자)에만 쓴다.

**The Rule-Weight Rule.** 구분은 괘선 굵기로 한다 — 머리·절 제목·상태 줄 위선·대화상자 테두리는 2px 먹선, 행·입력·판 테두리는 1px 미색 괘선. 실전 계좌면 상단 바 밑선이 2px 적선이 된다.

## Shapes

직각에 가까운 인쇄물의 형태다. 반경은 `--radius` 4px 하나(버튼·입력·배지·칩·플레이트·뷰어·토스트·대화상자·차트 틀)이고 예외는 스켈레톤 셀 2px, 플레이트 점·브랜드 마크 안쪽 2px, 플레이트 상태 점 50% 뿐이다. 테두리는 1px `rule` 이 기본이고 기본 버튼만 1px 먹 테두리, 실전 배지만 적 테두리, 선택 사항인 플레이트는 점선이다. 활성 내비 항목은 오른쪽 3px 먹 세로선으로 표시한다.

기호는 전부 16px 격자 SVG(`Glyph`, stroke 1.5, `currentColor`): 상승 ▲·하락 ▼는 채운 삼각형(10px), 보유 ● 채운 원, 선정 ○ 빈 원, 관심 ☆ 빈 별(12px). 브랜드 마크는 먹 사각(반경 2px) 안의 미색 ▲ — 시세표의 상승 블록이다.

## Components

### Buttons
문구가 붙은 작은 인쇄 버튼. 먹 테두리, 판 바탕, 누르면 먹으로 반전된다.
- **Shape:** 살짝 깎인 모서리(4px), 1px 테두리
- **기본(`btn`):** 판 바탕 + 먹 글자 + 먹 테두리, 5px 12px, 0.8125rem 700. hover 는 `sel` 바탕, active 는 먹 바탕 + 미색 글자. 선택된 토글(`btn.sel`)도 먹 반전.
- **Primary(`btn-primary`):** 먹 바탕 + 미색 글자, hover 는 `ink-2`.
- **Danger(`btn-danger`):** 적 테두리 + 적 글자, hover `danger-bg`, active 적 바탕 반전. 거부·실전 제어에.
- **Ghost(`btn-ghost`):** 테두리가 `rule` 로 물러나고 600. 보조 동작.
- **Small(`btn-sm`):** 2px 8px, 0.75rem. 표 안·칩 옆.
- **Disabled:** 불투명도 0.45, `not-allowed`.
- **Transition:** 배경·글자색 160ms(`--motion`, reduced-motion 이면 0).

### Badges
의미 배지. 글자 0.75rem 700, 1px 8px, 4px 반경. 기본은 판 바탕 + `rule` 테두리 + `ink-2` 글자(조회 전용·중립).
- **환경:** 모의 = `paper-env` 글자 + `paper-env-bg`(테두리 없음), 실전 = `real` 글자 + `real-bg` + **적 테두리**.
- **모드:** auto = 녹, approve = 황, off = 기본.
- **상태:** filled/approved/ok 녹, pending/accepted/unknown/running 황, rejected/failed/error 적. 의미색 배지는 테두리가 투명이다.
- **결정(`data-action`):** 배경·테두리 없이 글자만 — BUY/ADD 적, SELL/TRIM 청, HOLD `ink-2`, 800 + 0.02em.

### Chips
관심종목 같은 제거 가능한 항목. 판 바탕, 1px `rule`, 2px 8px, 0.75rem, 오른쪽에 `x` 글리프 버튼(`ink-2`).

### Inputs / Fields
- **Style:** 판 바탕, 1px `rule` 테두리, 4px 반경, 5px 8px, 최소 높이 30px, 0.8125rem. `select`·text·date·password·search 전부 같다.
- **Hover:** 테두리 `ink-3`.
- **Focus:** 전역 `:focus-visible` — 2px `focus`(청) 외곽선, 오프셋 2px. 캐럿은 먹.
- **Label:** `field` 가 라벨(0.75rem 600 `ink-2`)을 위에 두고 2px 간격. `row` 가 필드들을 12px 간격으로 바닥 정렬한다.
- **실전 계좌 select:** 테두리가 `real` 로 바뀐다.

### Navigation
- **상단 바(`topbar`):** 54px, 판 바탕, 2px 먹 밑선(실전이면 적선). 왼쪽부터 마크 + `trader`(900·1rem) · 계좌 select + 환경 배지 · 장 상태(0.75rem muted) · 오른쪽 상태 배지(일시정지·미승인 황, 제어 가능 녹 / 조회 전용 중립) · 테마 버튼(32px 정사각, 1px `rule`).
- **좌측 내비(`sidenav`):** 188px, 판 바탕, 오른쪽 1px 괘선, 항목은 8px 24px·0.875rem·`ink-2`. hover 는 `sel` + 먹 글자, 활성은 `paper` 바탕 + 800 + 오른쪽 3px 먹선.
- **모바일:** 1023px 이하에서 고정 서랍(위 Layout).

### Tape (시그니처: 시세표)
제품의 모든 표가 쓰는 원시요소(`Table`·`Th`·`Td`·`Num`·`Delta`·`Mark`).
- **머리:** 0.75rem 600 `ink-2`, 2px 먹 밑선. 정렬 가능 머리는 글자 뒤 12px 정렬 글리프(`ink-3`).
- **행:** 1px `rule` 밑선, 셀 8px 8px(`dense` 5px 8px), 0.8125rem, `nowrap`, 위 정렬. hover 는 `sel` 50%, 선택(`sel`/`aria-selected`)은 `sel` 전부. 클릭 가능 행은 포인터.
- **이름 셀(`nm`):** 앞에 16px 폭 기호 자리(`Mark`: ● 보유 먹, ○ 선정, ☆ 관심 `ink-2`, 없으면 빈 16px) + 종목명 700 + 코드·시장 `small` 500 `ink-2`.
- **숫자 셀(`num`):** 우측정렬 tabular. `Delta` 는 ▲▼— 10px 글리프 + 부호 없는 숫자, 적/청/`ink-2`.
- **근거 행(`why-box`):** 선택 행 아래 `sel` 바탕 한 줄, 0.8125rem, 1px 괘선.
- **게이트:** 통과 녹, 거부 적 700.

### Section (절)
`sec` 24px 위 간격, 제목 `h2` 0.875rem 800 + 오른쪽 보조문(0.75rem 500 `ink-2`) 을 베이스라인 정렬, 아래 2px 먹선. 표·플레이트·빈 상태가 그대로 이어진다.

### Plates (파이프라인 판)
모든 단계가 같은 크기(최소 96px, `minmax(150px, 1fr)` 자동 채움, 8px 간격). 판 바탕, 1px `rule` 테두리에 윗선만 2px; 완료(`done`)면 윗선·글자가 먹이 되고 제목 옆 체크 글리프가 녹. 선택 단계(`data-optional`)는 점선. 제목 0.8125rem 800 + 요약 한 줄 0.75rem + 파일 칩들.

### Empty / Skeleton
- **빈 상태:** 24px 12px 패딩, 1px 괘선 밑선, 제목 700 + 힌트 0.8125rem `ink-2`("무엇을 하면 채워지는지") + 선택적 동작 버튼.
- **스켈레톤:** 표 행 모양(12px 간격, 1px 괘선), 셀 높이 12px·반경 2px, `sel`→`panel`→`sel` 1.4s 반짝임.

### Toast / Dialog
- **토스트:** 우하단 고정, 먹 바탕 + 미색 글자, 8px 12px, 최대 360px, 그림자. 오류는 `danger`, 성공은 `ok` 바탕.
- **확인 대화상자(`dialog`):** 2px 먹 테두리, 판 바탕, 24px 패딩, 최대 440px, 그림자, backdrop 45% 검정. 제목 1rem, 본문 0.8125rem `ink-2`, 동작 버튼 오른쪽 정렬.

### Term (용어)
설명이 붙는 용어는 `ink-3` 점선 밑줄(오프셋 3px) + `help` 커서. 비개발자용 한 줄 설명을 `title` 로 단다.

### Chart
`lightweight-charts` 를 토큰 팔레트로 그린다: 판 바탕, `rule` 격자·축선, `ink-2` 글자·크로스헤어(라벨 바탕 먹). 마커는 주문 = 먹 화살표(매도·축소 위), 결정 = 청 원, 트리거 = 황 사각형; 지름은 확신 3단계(<0.5 / <0.7 / ≥0.7) 고정 램프. 틀은 1px `rule` + 4px 반경, 최소 460px(작은 차트 320px).

## Do's and Don'ts

### Do:
- **Do** 모든 표를 `Table`/`Th`/`Td` 로 만들고 숫자 열은 `num`, 등락은 `Delta`, 종목 이름 셀은 `Mark` + `nm` 으로 — 열 문법 하나.
- **Do** 새 색이 필요하면 `tokens.css` 의 `:root` 와 `[data-theme="dark"]` 에 같은 이름으로 함께 추가하고 `tokens.test.ts` 의 4.5:1 대비 검사를 통과시킨다.
- **Do** 상태를 기호·라벨과 색으로 병행한다(▲▼, ●○☆, 배지 문구). 색만으로 구분하지 않는다.
- **Do** 실전 계좌는 `real` 배지(적 테두리)·상단 바 적선·select 적 테두리 세 가지로 전 화면에서 표시한다.
- **Do** 아이콘은 `Glyph` 16px 격자 SVG 에 추가한다(stroke 1.5, `currentColor`).
- **Do** 절은 `Section` 으로 — 제목 + 2px 먹선 + 보조문 자리. 큰 숫자는 절당 하나(`big`).
- **Do** 빈 화면에는 `EmptyState` 로 "무엇을 하면 채워지는지" 한 줄을 넣는다.

### Don't:
- **Don't** `tokens.css` 밖에 hex 를 적지 않는다(`chart.ts` 의 폴백 사본 제외). 인라인 `style` 은 데이터로 정해지는 폭·flex 비율에만.
- **Don't** 같은 크기 카드 격자, KPI 타일, 파란 브랜드 강조를 들이지 않는다. 강조는 먹의 굵기다.
- **Don't** 지면 안 요소에 그림자를 주지 않는다. `--shadow` 는 서랍·토스트·대화상자 전용.
- **Don't** 적·청을 등락·결정 방향·거부·실전 외의 의미로 쓰지 않는다. 황은 대기·주의 외에 쓰지 않는다.
- **Don't** 두 번째 서체, 대문자 변환, 자간 확장, 1.3125rem 보다 큰 글자, 4px 외의 반경(스켈레톤·점 제외)을 들이지 않는다.
- **Don't** 이모지·유니코드 기호를 아이콘으로 쓰지 않는다. 전부 `Glyph`.
- **Don't** 실시간 틱 연출(깜빡임·카운터 애니메이션)을 넣지 않는다. 움직임은 160ms 배경·색 전환과 서랍 슬라이드, 스켈레톤 반짝임뿐이다.
