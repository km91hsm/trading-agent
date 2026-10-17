# 시스템 상태 (생성: 2026-10-09 06:16 KST)

## 실행 제어
- 일시정지: 아니오
- 토론 라운드: 1
- 하루 세션 상한: 12

## 계좌
| id | env | 모드 | 시장 |
|---|---|---|---|
| paper-main | paper | auto | KR,US |
| real-main | real | off | KR,US |

## 마지막 실행
아직 실행 기록 없음

## 오늘 LLM 호출
- 0건, $0.00

## 구현 진행도
- Phase 0 골격: 8/9
- Phase 1 브로커: 9/9
- Phase 2 수집: 8/10
- Phase 3 에이전트: 0/10
- Phase 4 리스크·계좌별 실행: 0/7
- Phase 5 웹 UI: 0/6
- Phase 6 scan·monitor·데몬: 0/11
- Phase 7 ML 1단계: 0/8
- Phase 8 학습·평가·보정: 0/7
- Phase 9 RL 사이징(실험): 0/5

## 다음 할 일
- 완료 기준: Windows 에서 `uv sync` 후 `uv run trader status` 동일 동작 (사용자 확인 대기)
- 사용자 확인 대기: 네이버 검색 API 키가 401 — 애플리케이션에 '검색' API 권한이 있는지, 키 값이 맞는지 확인 후 `uv run trader collect --ticker 005930 --force` 재실행
- 사용자 확인 대기: DART 점검 종료 후 `collect`·`features backfill --years 3` 재실행으로 재무비율 적재
- `tool` CLI 4개 (collect·checkpoint·submit-decision·status) + 전제조건 검증
- 출력 스키마 (AnalystReport·DebateTurn·Decision·CallMeta)
