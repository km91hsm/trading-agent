# Stock 교훈 1차 — 안전장치·판단 근거 (2026-10-27)

출처: 중단한 Stock 프로젝트의 DECISIONS·운영 문서 조사(사용자 결정 2026-10-27, 웹 수동 주문 제외 전부, 2차례 PR).
1차는 매매를 늘리지 않고 막거나 재는 것만. 전부 파이썬 규칙, LLM 경로 불변.

| # | 항목 | 구현 | Stock 근거 |
|---|---|---|---|
| 1 | 원장 백업 | `ledger/backup.py` SQLite backup API, 데몬 cron 매일 장 마감 뒤 + 일별 14개·주별 8개 보존 | D-075 |
| 2a | 누적 손실 킬스위치 | `risk/killswitch.py`: (계좌·시장) NAV 가 기준(첫 스냅샷 또는 해제 시점) 대비 −15% 면 `control.killswitch` 켬 + 알림, `rules.evaluate(killswitch=)` 가 일반 BUY/ADD·단타 진입 거부(매도·손절·헤지는 통과). 해제는 사람만(`trader control killswitch off --note`), 텔레그램 `/kill` 은 켜기만 | D-049·D-059 |
| 2b | 실전 자본 상한 | `accounts.yaml capital_cap: {KR: 원, US: 달러}` → `build_portfolio` 가 NAV·가능금액을 상한으로 자름 | D-074 |
| 3 | 매매 불가 종목 차단 | 현재가 응답 종목상태(관리·투자위험·투자경고·거래정지·단기과열)·시장경고·임시정지를 `Quote` 에 싣고 `risk/tradability.block_reason` → `rules.evaluate(trade_block=)` BUY 거부 | D-030 |
| 4 | 지수 섀도 기준선 | `ledger/shadow` 전략 `index_hold`·`index_trend`(지수 proxy 100일선 위면 보유, 아래면 현금), 종가는 KIS 일봉(피처 스토어에 proxy 없음) | D-037~D-039 |
| 5 | 모의 체결 대 가정 | `ledger/paper_review.py`: LLM 주문 체결률·지정가 대비 슬리피지(bp)·비용 가정 비교, 주간 텔레그램 + CLI `trader paper-review` | docs/07 4-7 |
| 6a | 수정주가 감지 | 스냅샷 때 새 일봉과 저장 종가가 0.5% 넘게 다르면 그 종목만 재백필 + 알림 | D-034·D-064·D-070 |
| 6b | 생존 편향 경고 | `ml train` 결과·ML 페이지에 "현재 구성 종목으로 과거 학습" 주의 | D-037 |
| 6c | 단타 켜기 조건 | TODO·architecture 에 "수개월 분봉 재생·비용·보류 구간 1회" 명시(코드 없음) | D-120 |

검증: TDD(pytest·vitest), ruff, Codex 로컬·GitHub 리뷰, CI.
