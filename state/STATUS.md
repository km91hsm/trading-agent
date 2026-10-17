# 시스템 상태 (생성: 2026-10-18 05:53 KST)

## 실행 제어
- 일시정지: 아니오
- 토론 라운드: 1
- 하루 세션 상한: 12

## 계좌
| id | env | 모드 | 시장 | 성향 |
|---|---|---|---|---|
| paper-main | paper | auto | KR,US | KR normal, US normal |
| real-main | real | off | KR,US | KR normal, US normal |

## 마지막 실행
- analyze KR 2026-10-17T07:16:55.300801+00:00 → ok
- analyze KR 2026-10-17T06:29:17.941831+00:00 → ok
- analyze KR 2026-10-17T00:53:21.290488+00:00 → ok
- scan KR 2026-10-09T03:27:14.318886+00:00 → ok
- analyze KR 2026-10-09T02:06:43.334830+00:00 → ok

## 오늘 LLM 호출
- 0건, $0.00

## 최근 결정
- 2026-10-17 KR 005930: HOLD w=0.0 conf=0.5
- 2026-10-17 KR 000660: HOLD w=0.0 conf=0.5
- 2026-10-09 KR 000660: HOLD w=0.0 conf=0.5

## 성과(30일)
- paper-main KR:  MDD  (1일)
- real-main KR:  MDD  (1일)

## ML 모델
- quant 20261016_225858: 정확도 0.3151 / 기준선 0.4646 / 알파 -0.003313 · 미승격
- rl_sizer 20261017_065953: RL -0.001304 / 고정 0.003331 / LLM 0.0 · 미승격

## 결정 분포(30일)
- HOLD 3
- 토론 수렴율: 0%

## 미승인 주문
- 없음

## 구현 진행도
- Phase 0 골격: 8/9
- Phase 1 브로커: 9/9
- Phase 2 수집: 8/10
- Phase 3 에이전트: 15/18
- Phase 4 리스크·계좌별 실행: 8/11
- Phase 5 웹 UI: 10/12
- Phase 6 scan·monitor·데몬: 19/23
- Phase 7 ML 1단계: 10/14
- Phase 8 학습·평가·보정: 8/10
- Phase 9 RL 사이징(실험): 7/9

## 다음 할 일
- 완료 기준: Windows 에서 `uv sync` 후 `uv run trader status` 동일 동작 (사용자 확인 대기)
- 사용자 확인 대기: 네이버 검색 API 키가 401 — 애플리케이션에 '검색' API 권한이 있는지, 키 값이 맞는지 확인 후 `uv run trader collect --ticker 005930 --force` 재실행
- 사용자 확인 대기: DART 점검 종료 후 `collect`·`features backfill --years 3` 재실행으로 재무비율 적재
- 실기(사용자 확인 대기): 처음부터 끝까지 bundle 경로로 1회 실행해 리서처 라운드 0·1 묶음이 Agent 프롬프트에 전문으로 들어가는지 세션 기록으로 확인 (`uv run trader analyze --ticker 005930 --dry-run`, 체크포인트 비운 뒤, 약 $2.5)
- 완료 기준: Windows 에서 FAKE_CLAUDE dry-run 동일 동작 (사용자 확인 대기)
