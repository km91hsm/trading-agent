# Phase 26 선물옵션 방향성 레인 — 단계별 교차 리뷰 (2026-10-27)

플랜 `docs/superpowers/plans/2026-10-27-phase26-fo.md` 「Codex 교차 리뷰 절차」A. 단계마다 head 를 커밋으로 고정하고 base/head SHA 를 적는다. 리뷰어는 `codex exec --sandbox read-only --ephemeral`(초점 프롬프트) · `codex review --base <base>` · Claude `/code-review`. P0·P1 은 PR 전 수정 + 테스트.

## 단계 1 — 계좌·브로커·마스터·설정·가격모형·CLI (`feat/phase26-1-fo-broker`)

- base `d112633`(main) / head `90bcf92`. 참고: 단계 1 의 앞 6개 커밋(`a492f51`~`4df85df`)은 다른 세션의 PR #56(`docs/review-2-cross-check`, `a39e102`)에 squash 로 섞여 main 에 먼저 들어갔다. 리뷰는 전체 diff(`d112633..90bcf92`) 기준이고, PR 은 main(`a39e102`) 위로 재배치해 Task 1.7 + 리뷰 반영만 담는다.
- Codex exec 결과 원문: 세션 scratchpad(`codex_s1_exec.md`), `codex review` 결과: `codex_s1_review.log`(저장소에는 표만 둔다).

| # | 심각도 | 출처 | 위치 | 문제 | 조치 |
|---|---|---|---|---|---|
| S1-1 | P0 | exec | `broker/fo.py` `get_balance` | `cblc_qty` 가 `"?"`·`"1.5"`·`""` 이면 `_int` 가 0 으로 바꿔 보유 행이 **조용히 빠져** 미확정 보유 없음으로 보이고 신규 진입을 허용 | `_qty_of` 엄격 해석: 음이 아닌 정수 문자열만 수량, 그 외는 `UnverifiedHolding`(사유 "수량 해석 불가"), 명시적 `0` 만 제외. 테스트 `test_balance_qty_unparseable_is_unverified` |
| S1-2 | P0 | exec·review | `broker/fo.py` `get_order_state` | 원주문 행이 `{"odno"}` 뿐이면 전부 0 → `filled, terminal=True`; 취소 행 없이 `(3,1,0,0)`·`(3,3,1,0)` 항등식 불일치가 예외 없이 통과 | 필수 수량 `_req_int`(결측·비정수 → `INVALID_JSON` "누락"), 항등식 `filled+rejected+cancelled+remaining == order_qty` 아니면 "모순" 예외. 테스트 `test_order_state_missing_or_inconsistent_quantities_raise` |
| S1-3 | P0 | exec·review(P1) | `kis_market.py:643` ↔ `fo/pricing.py` | 시세 IV 는 퍼센트(18.4), Black-76 은 소수 → 연결하면 1840% 로 스트레스 손익 왜곡 | `FoQuote.iv/hist_vol` 은 KIS 퍼센트로 두고 `pricing.reprice` 가 /100 한 곳에서 변환(모델 주석·architecture 명시). 통합 테스트 `test_option_quote_iv_is_percent_and_reprice_converts`(fixture → reprice == price(…, 0.184, …)) |
| S1-4 | P1 | exec | `fo/cli.py` `verify_cmd` | 마스터·시세·주문가능이 실패해도 잔고만 있으면 `fo_verified` 플래그를 적고 exit 1 → 실패한 검증이 영속 성공 상태를 남김 | `errors` 가 비었을 때만 플래그. 테스트 `test_fo_verify_no_flag_when_a_step_fails` |
| S1-5 | P1 | exec·review(P2) | `cli.py` `status`·`_control_conn` | 계좌를 가리지 않는 소비자(STATUS·`control mode/show`)가 현물 목록을 받아 `paper-fo` 가 보이지 않고 `control mode paper-fo off` 가 실패 | 둘 다 `all_accounts`. `fills sync`·`balance_refresh`·데몬·웹은 플랜대로 단계 2(asset 디스패치)·4. 테스트 `test_control_and_status_see_fo_account` |
| S1-6 | P2 | exec | `config.py` `FoProductCfg.option_ticks` | tick 0·음수, 상한 역순, 중간 `inf` 가 설정을 통과 | 검증 추가(양수 tick, 엄격 오름차순, `inf` 는 마지막만). 테스트 `test_option_ticks_validation` |

확인함(두 리뷰어 공통): 방향·종류 분리 검증과 `UnverifiedHolding`, NAV None·연속조회 완주, 모의 야간 TR 전부 `UNSUPPORTED`, 호가단위 경계(3.00→0.02, 10.00→0.05), 플래그 키·`--vehicle` 필터, **현물 기본 경로 회귀 없음**(`boot.accounts` 소비자 전부 현물만, `paper-fo` 가 현물 브로커·잔고·데몬·웹에 섞이는 경로 없음 — 단계 1 의 영향은 혼입이 아니라 파생 계좌의 감시·동기화·웹 표시 **제외**), fixture 합성·비밀정보 없음.

Claude `/code-review`: 아래 「단계 1 Claude 리뷰」.
