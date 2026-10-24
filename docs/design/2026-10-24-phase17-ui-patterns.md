# Phase 17 UI 패턴 확장 — 디자인 시스템 점검·문서 (design:design-system extend)

출처 계약: `DESIGN.md`(토큰·타이포·버튼), `web/src/styles/tokens.css`(CSS 변수만), `components/Table.tsx`(2px 머리선·1px 괘선·우측정렬 tabular),
`.impeccable/surfaces/web-src-app-tsx.md`(시세표 열 리듬, 적청은 데이터 의미에만, 황은 대기·주의에만, 기호는 `Glyph.tsx` SVG).

## 점검 요약
- 기존 버튼 변형: `.btn`(기본), `.btn-primary`(먹 바탕), `.btn-danger`(적 — 거부·위험), `.btn-ghost`, `.btn-sm`, `.btn.sel`. 상태: hover(sel)·active(먹 반전)·disabled(opacity .45, `title` 로 사유).
- 빈 상태 `EmptyState{title,hint,action}` 에 `action` 슬롯이 이미 있고 `.empty-action` 간격 토큰(`--s-3`)이 정의돼 있다 → 새 CSS 없이 버튼을 넣을 수 있다.
- 확인 대화상자 `ConfirmDialog{open,title,body,confirmLabel,danger}` 는 `<dialog>` 기반, body 는 ReactNode 라 체크박스 폼을 담을 수 있다.
- 토큰 밖 색 사용 0건 유지. 새 패턴은 전부 기존 변수로 만든다.

## 새 패턴 1 — 행 안의 '지금 실행' 버튼 (`RunJobButton`)
| 항목 | 결정 |
|---|---|
| 자리 | 잡 표 마지막 열 `실행`(머리 글자 우측정렬 아님, 좌측), 셀 안 `.btn.btn-sm` |
| 변형 | 기본 `.btn.btn-sm`; 세션 비용·주문 가능 작업(scan_analyze·analyze_ticker dry-run 해제)은 확인창 `danger` — 버튼 자체는 적색을 쓰지 않는다(적은 데이터 의미) |
| 상태 | 비활성(토큰 없음·데몬 꺼짐) → `disabled` + `title`=사유; 요청 중 → `disabled` + 라벨 "요청 중…"; 완료 → 토스트 `ok` |
| 타이포 | `--t-xs`(btn-sm), 라벨은 동사 "지금 실행" 고정, 종류는 행의 잡 라벨이 말해 준다 |
| 접근성 | `type=button`, `aria-label="<잡 라벨> 지금 실행"`, 비활성 사유는 `title` + 표 아래 `.note` 1줄(스크린리더가 title 을 못 읽는 경우) |

## 새 패턴 2 — 요청 대기열 표 (`JobRequestsTable`)
열: 작업(라벨 + `<small>` 종류 id) · 상태 · 요청/시작 시각(우측정렬 tabular) · 진행/오류(좌측, 생략 `…`) · 동작(취소).
- 상태 표식은 색이 아니라 기존 `.gate ok|no` 와 `Badge{kind}` 재사용: 대기 `neutral`, 실행 중 `pending`(황 — 대기·주의 의미에 부합), 완료 `ok`, 오류 `danger`, 건너뜀 `neutral`.
- 진행 문구는 `--ink-2`, `--t-xs`, 한 줄 말줄임(`text-overflow: ellipsis`) — 새 클래스 `.progress-text` 하나(토큰만 사용).
- 폴링 중임을 알리는 애니메이션은 두지 않는다(상태 글자가 바뀌는 것으로 충분; 신문 지면은 움직이지 않는다).
- 비어 있으면 `EmptyState("대기·실행 중인 작업이 없어요")`, 데몬 꺼짐이면 `.note` 1줄.

## 새 패턴 3 — 빈 화면 액션 (오늘·데이터·ML)
- `EmptyState.action` 에 `RunJobButton` 1~2개 + 소형 폼(`.row` + `.field`, Settings 와 같은 폼 문법). 힌트 문구는 터미널 명령을 지우고 "무엇이 일어나는지" 한 문장.
- 소형 폼(종목 분석): 입력 `.field` 종목 코드 · US 거래소 `select` · `label><input type=checkbox>` 모의 실행. 제출 버튼은 `.btn.btn-primary`.
- 확인창 본문 체크박스("끝나면 퀀트 학습도", "모의 실행")는 `label.check` (기존 `.field` 와 같은 간격 토큰 `--s-2`).

## 새 패턴 4 — 섹터 비중 표 + 상한선 (Phase 17 S)
열: 섹터 · 종목수(num) · 평가금액(num) · 비중(num) · 막대. 막대는 `<svg role="img" aria-label="비중 n%, 상한 40%">` 폭 100% 높이 8px: 바탕 `--rule`, 채움 `--ink-2`, 상한선 `--ink` 1px 세로선(40% 위치), 초과 섹터 채움은 `--pending`(주의). 적청은 쓰지 않는다(등락 아님). 현금 행은 `--ink-3` 채움.

## 우선 조치
1. CSS 추가는 `.progress-text`, `.bar-svg`(막대 컨테이너), `label.check` 세 가지만 — 전부 토큰 참조.
2. 모든 새 라벨은 `glossary.ts`(`job_queue`, `sector_weight`)와 `jobs.ts` 라벨 맵에 두고 컴포넌트에 문자열을 흩뿌리지 않는다.
3. 구현 뒤 `design:design-critique`·`design:accessibility-review` 로 위계·대비·키보드 점검.

## 구현 뒤 비평 (design:design-critique, 2026-10-24)
- **첫 인상**: 대시보드 상태줄에 '국면' 칩이 더해져도 한 줄 리듬(라벨 + 굵은 값)이 유지된다. 잡 표의 마지막 열 버튼은 `.btn-sm` 이라 숫자 열을 밀지 않는다.
- **위계**: 오늘 빈 화면에서 '지금 스캔 → 분석'(기본 버튼, 위험 확인창)과 '분석 요청'(primary) 의 강조가 뒤바뀌어 보일 수 있으나, 종목 1건 분석이 더 싼 작업이라 primary 가 맞다. 유지.
- **일관성**: 열 머리 '실행' 이 '결과' 열과 혼동돼 '지금 실행' 으로 바꿨다. 상태 배지는 기존 `Badge` kind 만 쓴다(대기 neutral·실행 중 pending·완료 ok).
- **접근성**: 비활성 사유가 `title` 에만 있어 터치·스크린리더에서 보이지 않던 것을 `RunJobButton showReason` 으로 버튼 옆 글자(`--ink-2`)로도 보이게 했다(빈 화면 버튼 전부). 섹터 막대 SVG 는 `role=img` + `aria-label="<섹터> 비중 n%, 상한 40%"`, 상한선은 색이 아니라 세로선이라 대비와 무관하다. 초과는 황색 글자 + '상한 초과' 문구로 색에만 의존하지 않는다.
- **카피**: 터미널 명령을 빈 화면에서 전부 지웠고, "무엇이 일어나는지" 한 문장 + 데몬이 꺼졌을 때의 사유만 남겼다.
