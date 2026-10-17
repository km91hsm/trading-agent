# 시스템 상태 (생성: 2026-10-09 04:01 KST)

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
- Phase 1 브로커: 0/9
- Phase 2 수집: 0/7
- Phase 3 에이전트: 0/10
- Phase 4 리스크·계좌별 실행: 0/7
- Phase 5 웹 UI: 0/6
- Phase 6 scan·monitor·데몬: 0/8
- Phase 7 ML 1단계: 0/8
- Phase 8 학습·평가·보정: 0/7
- Phase 9 RL 사이징(실험): 0/5

## 다음 할 일
- 완료 기준: Windows 에서 `uv sync` 후 `uv run trader status` 동일 동작 (사용자 확인 대기)
- KisAuth (env 별 토큰 캐시, 1분 1회 발급 보호)
- KisClient 레이트리밋(실전 20/s, 모의 2/s)·EGW00201 재시도·KisApiError
- tr_ids 단일 파일, accounts.py
- KisMarket (실전 env 만): 시세·일봉·분봉·호가·투자자동향·지수·휴장일
