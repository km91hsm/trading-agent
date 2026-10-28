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

### 단계 1 Claude 리뷰 (`/code-review` high, main `a39e102` 위 재배치 뒤 `48dcba2`·`61026ac`)

| # | 분류 | 위치 | 문제 | 조치 |
|---|---|---|---|---|
| C1-1 | 정확성 | `fo/cli.py` `verify_cmd` | 마스터에 roll 창 밖 선물 월물이 없으면 `front=None` 으로 시세·주문가능 단계를 **조용히 건너뛰고** 잔고만으로 플래그를 적음(S1-4 의 빈틈) | 마스터 빈 응답·월물 없음을 `errors` 로 세어 플래그 없이 exit 1. 테스트 `test_fo_verify_no_flag_when_no_front_month`·`_master_empty` |
| C1-2 | 정확성 | `broker/fo.py` `_qty_of`·`_req_int` | `str.isdigit()` 은 `'²'` 같은 비십진 숫자 문자도 True 라 `int()` 가 ValueError 로 터짐(미확정/예외 계약 밖) | `isdecimal()`. 테스트에 `'²'` 사례 추가 |
| C1-3 | 정확성 | `fo/cli.py` `chain_cmd` | 행사가가 전부 None 인 월물이면 `min(range(0))` 트레이스백 | `ConfigError`(exit 1). 테스트 `test_fo_chain_atm_window_and_errors` |
| C1-4 | 테스트 | `fo chain` | 테스트 없음 | 위 테스트(ATM ± n 창·errors·실패 경로) |
| C1-5 | 단순화 | `fo/cli.py` `_mask_cash` | 모델이 강제하는 필드의 '있음' 은 항상 True | NAV(선택 필드)만 보고 |
| C1-6 | 재사용 | `fo/cli.py` ↔ `fo_master` | 비공개 `_of_product`·`_expiries` 에 의존 | 공개 `fo_master.of_product`·`option_front`. 테스트 `test_of_product_and_option_front_public` |
| C1-7 | 운영 | `cli.py` `_build_brokers` | 파생 계좌의 공용 모의 키 폴백(설계상 허용)이 부트마다 '같은 앱키' WARNING | fo 계좌는 INFO, 현물은 WARNING 그대로. 테스트 `test_fo_key_fallback_logs_info_not_warning` |

단계 1 결론: Codex P0 3·P1 2·P2 1 + Claude 7 전부 반영, `uv run ruff check`·`uv run pytest` 통과 뒤 PR(main).

## 단계 2 — 저장 계층·상태기계·게이트·실행·원장 asset 분기 (`feat/phase26-2-store-gate-exec`)

- base `4b713a3`(단계 1 브랜치 head) / head `155a4c6`(리뷰 시점, 문서 커밋까지). 리뷰어 셋이 같은 범위를 봤다: `codex exec`(초점 프롬프트, `codex_s2_exec.md`) · `codex review --base 4b713a3`(`codex_s2_review.log`) · Claude `/code-review` high. 결과 원문은 세션 scratchpad, 저장소에는 표만.
- 세 리뷰가 독립적으로 같은 결함을 셋 잡았다(S2-3=R-2, S2-4=R-3=C2-3, S2-6=R-4=C2-7). 전부 P0·P1 로 보고 PR 전에 고쳤다. 테스트는 `tests/fo/test_review_s2.py`(20건) + `tests/orchestrator/test_fills_fo.py`(4건 추가) + `tests/fo/test_positions.py`(1건 변경).

### Codex exec (S2-1~12)

| # | 심각도 | 위치 | 문제 | 조치 |
|---|---|---|---|---|
| S2-1 | P0 | `fo/fills.py` ↔ `ledger/repo.insert_order/insert_fill` | 체결 트랜잭션 안에서 commit 하는 공용 헬퍼가 중간 확정 → partial 2→3 증가 때 마지막 COMMIT 이 활성 트랜잭션 없이 실패, 롤백 불가 | `repo.insert_order/insert_fill(commit=)` 플래그(기본 True — 현물 호출부 불변), 파생 동기화는 `commit=False` 로 계보 하나 = `store.tx` 하나. `test_repo_insert_helpers_can_skip_commit`·`test_fo_fills_partial_then_more_keeps_transaction` |
| S2-2 | P0 | `fo/executor._transmit` | 예약 commit 뒤 전송 전에 다른 프로세스가 `recall_entries` 로 cancelled 로 바꾼 `prepared` 다리를 상태 확인 없이 `sending` 으로 덮어써 주문 | `store.claim_leg`(`UPDATE … WHERE status='prepared'`, rowcount 1 만 전송 권한) → 실패면 `status: "recalled"` 기록, 주문 없음. `test_claim_leg_only_from_prepared` |
| S2-3 | P0 | `executor.recall_entries/continue_reduce`·`positions._confirm_leg`·`fills._sync_lineage` | 회수·복구가 `record_fill_progress` 로 수량만 올리면 동기화가 "누적 같음" 으로 경제값(평균가·비용·실현손실)을 영구 건너뜀 → 수명 예산 과소 | 수량 진행과 경제값 진행 분리: 동기화는 `fills` 행 누적(`_fill_totals`) 대비 증분으로 경제값을 적고 `record_fill_progress` 는 다리보다 클 때만. 어느 경로가 먼저여도 각 체결의 경제값은 정확히 1회. `test_recall_confirmed_fill_gets_economics_once_in_sync` |
| S2-4 | P0 | `executor.continue_reduce` | 축소 다리가 거부·취소·만료로 끝났는데 잔량이 남으면 `finalize` 로 가고 `closing` 보유가 영구 방치 | `_resume_reduce`: 비활성 다리 + `qty_held > 0`(closing·exit_failed) 또는 resizing `qty_held > qty_target` 이면 같은 의도로 다음 attempt 를 낸다(`exit_retry_max` 그대로). `test_expired_reduce_leg_is_reissued_next_tick` |
| S2-5 | P0 | `executor.run_leg` RESIZE↓ 분기 | 결정의 감액이 진입 회수·잔고 확정 없이 바로 축소 → 미체결 진입이 뒤에 체결되면 보유가 되돌아옴 | 새 목표·손절선·배수를 저장한 뒤 **공통 `reduce(intent="RESIZE_DOWN")`** 로(진입 회수 → 체결 반영 → 보유 − 목표). `test_resize_down_from_decision_recalls_entries_first` |
| S2-6 | P0 | `rules.evaluate` | 감액 RESIZE 에도 진입 거부 조건(enabled·검증·진입 창·시세 노후) 적용 → 스펙 §4.1 위반 | 거부 사유를 **하드**(paused·방향/수단 불일치·대상 포지션 상태·NAV 없음) 와 **진입 전용**(`_entry_rejections`) 으로 나누고 사이징 결과 `n_target < held` 면 감액 다리를 만들며 진입 전용 사유는 note 에 "진입 조건 무시(감액)" 로만. `test_resize_down_ignores_entry_only_rejections` |
| S2-7 | P0 | `sizing.size`·`store.reserve` | 보유·미체결 계획 손실을 이번 주문가 하나로 재평가 → 수명 예산 과소 | 보유는 `entry_premium`(평균가), 미체결은 각 다리 가격으로 계산한 `planned_loss_existing` 을 `SizeResult`·`FoVerdict`·`ReservePlan` 으로 넘겨 예약 재검사가 같은 합계를 쓴다. `test_held_planned_loss_uses_average_price`·`test_reserve_budget_uses_existing_planned_loss` |
| S2-8 | P0 | `sizing.margin_check` | 로컬 예약(미체결 진입 계약)이 브로커 `ord_psbl_qty` 기반 허용 수량에서 빠지지 않음 | `reserved_qty(legs)`(살아 있는 진입 다리 잔량) 을 `floor(q × (1 − buffer)) − reserved` 로 차감. `test_margin_allowance_subtracts_reserved_entry_contracts` |
| S2-9 | P0 | `rules.evaluate` | RESIZE 가 실제 노출(가격 상승으로 승인 배수 초과)을 `leverage_used` 로 저장해 이후 상한이 올라감 | `leverage_used = min(L, 실제)` — 승인 배수는 단조, 실제 노출은 `exposure_after` 로만. `test_stored_leverage_never_exceeds_approved_on_resize` |
| S2-10 | P0 | `executor.run_leg` HOLD | 손절선 강화가 `stop_fut` 를 갱신하지 않아 지수 부재 시간대에 느슨한 선물 보호선 유지 | `_stop_fut_change`: 베이시스 있으면 재환산, 없으면 기존 오프셋 유지, `monotone_stop` 으로 유리한 쪽만(RESIZE↓ 저장에도 같이). `test_hold_tightening_stop_updates_stop_fut` |
| S2-11 | P0 | `executor.reduce` | 잔고 수량을 다리 진행과 별개로 `qty_held` 에 덮어써 뒤의 체결 동기화가 이중 차감 | 덮어쓰기 제거. 불일치면 `_apply_position_fills`(오늘 체결을 이 포지션 다리들의 `record_fill_progress` 로) 뒤 다시 대조, 그래도 다르면 unknown. `test_reduce_applies_fills_instead_of_overwriting_qty` |
| S2-12 | P1 | `continue_reduce`·`positions.recover` | 주문 상태 네트워크 조회가 `BEGIN IMMEDIATE` 안 | `_confirm_leg` 가 조회를 잠금 밖에서 하고 반영만 짧은 `store.tx`; `recover` 도 다리마다 같은 구조. `test_order_state_queried_outside_write_lock`·`test_recover_releases_lock_between_broker_queries` |

### codex review (R-1~4)

| # | 심각도 | 위치 | 문제 | 조치 |
|---|---|---|---|---|
| R-1 | P1 | `fo/fills._leg_for` | 주문번호 매칭이 날짜를 보지 않아 KIS 가 다음 날 재사용한 번호가 옛 다리(다른 계약·방향)에 붙음 | 다리·외부 주문 매칭을 `repo.day_bounds(day, "KR")` 의 접수·전송·생성 시각(`COALESCE(accepted_at, sent_at, created_at)`)으로 한정. 전송·접수 시각은 실행 문맥 시계(`ctx.now`). `test_fo_fills_order_no_reused_next_day_not_attached` |
| R-2 | P1 | = S2-3 | | |
| R-3 | P1 | = S2-4 | | |
| R-4 | P2 | = S2-6 | | |

### Claude `/code-review` (C2-1~10)

| # | 분류 | 위치 | 문제 | 조치 |
|---|---|---|---|---|
| C2-1 | 정확성 | `executor.reduce` | 브로커 보유 None(잔고에서 사라짐) + 원장 잔량이면 체결 반영 없이 unknown | S2-11 경로가 체결을 먼저 반영하고 `held None ∧ qty_held 0` 은 finalize(closed). `test_reduce_applies_fills_instead_of_overwriting_qty` |
| C2-2 | 정확성 | `recall_entries`·`fills.expire_legs` | 취소·만료된 진입 다리가 보유 0 인 `entering` 포지션을 열린 채 남김(자리 점유) | `store.release_if_empty` 하나로(거부·취소·만료·회수·복구 ② 모두). `test_recall_cancel_closes_empty_entering`·`test_cancelled_entry_leg_closes_empty_entering`·`test_fo_expire_closes_empty_entering` |
| C2-3 | 정확성 | = S2-4 | | |
| C2-4 | 정확성 | `rules.evaluate` | OPEN/RESIZE 가 대상 포지션의 closing·exit_failed·awaiting_settlement·unknown 을 보지 않음 | 하드 거부 "포지션 상태 {status} — 진입·증액·감액 결정 불가". `test_entry_rejected_on_target_position_status` |
| C2-5 | 정확성 | `positions._confirm_leg` | 일시 조회 실패를 "확정 불가" 로 보고 포지션 unknown 격상 | 삼상태 `confirmed|unconfirmed|error`: error 는 `RecoveryReport.deferred`(다음 틱 재시도, 신규는 "미확정 다리" 로 막힘). `test_recover_query_failure_defers_instead_of_unknown` |
| C2-6 | 정확성 | `fills.sync` | 계보 하나의 `record_fill_progress` ValueError(체결 > 다리 수량)가 동기화 전체 중단 | 계보별 트랜잭션, 모순 계보는 그 포지션만 unknown + `[파생 체결 모순]` 알림 + `anomalies` 집계. `test_fo_fills_anomalous_lineage_isolated` |
| C2-7 | 정확성 | = S2-6 | | |
| C2-8 | 정확성 | `executor.run_leg` | 결정의 `entry_price` 원값을 `orderable` 조회에 넘김(호가단위 밖이면 조회 실패 → 거부) | `quantize_tick(tick_table_for(product, kind))` 로 맞춘 뒤 조회. `test_orderable_gets_quantized_price` |
| C2-9 | 정확성 | `sizing.local_reserved` | 축소 다리까지 예약에 세어 신규를 과소 허용·과다 거부 | `reserved_qty` 는 살아 있는 **진입** 다리만. `test_margin_allowance_subtracts_reserved_entry_contracts` |
| C2-10 | 정확성 | `store.reserve/transition` | 전이표(`positions.TRANSITIONS`)를 저장 계층이 검증하지 않아 표 밖 전이가 통과 | 표를 `fo/types.TRANSITIONS` 로 옮기고 `transition`·`reserve` 가 표 밖 상태 변경을 ValueError/거부. `test_transition_validates_state_table` |

확인함(리뷰어 공통): 예약 핵심 구간(`BEGIN IMMEDIATE` 안 총량 재계산·`active_reduce_leg_id`), sending/unknown 재전송 금지와 조기 closed 방지, 재시도 상한 → exit_failed, `premium_stop` 부호(매도 min·매수 max)·`approved_mult` 단조, 증거금 전제 5종·IV 퍼센트 변환, `orders.asset` 전파(현물 호출부 `asset="stock"` 명시)와 **현물 집계·스냅샷·점검 회귀 없음**, 실현손실 부호(LONG +1/SHORT −1), v26 추가만·fixture 합성·비밀정보 없음.

단계 2 결론: **보완 뒤 진행** — Codex P0 11·P1 2·P2 1 + Claude 10(중복 3) 전부 반영. `uv run ruff check`·`uv run pytest` 통과 뒤 PR(`--base feat/phase26-1-fo-broker`).

### GitHub Codex PR 리뷰 (G2-1~7, PR #60 head `1bd0172`)

| # | 심각도 | 위치 | 문제 | 조치 |
|---|---|---|---|---|
| G2-1 | P1 | `fo/fills._sync_lineage` | KIS `filled_amount` 는 통화 금액(1계약 350pt = 17,500,000원)인데 `Δ금액 ÷ Δ수량` 을 포인트 단가로 저장 → 평균가 1,750만·비용에 승수 이중 곱 → 수명 예산 붕괴 | 계보 금액을 승수로 나눠 포인트 합으로 바꾼 뒤 `fills.price`(포인트)·증분 평균가·비용·실현손실 전부 포인트 기준(`orders.amount` 만 통화). 테스트 fixture 도 통화 금액으로. `test_fill_amount_currency_converted_to_points` |
| G2-2 | P1 | `executor.recall_entries` | 낡은 `prepared` 스냅샷을 무조건 cancelled 로 덮어 전송기가 `claim_leg` 로 선점한 다리를 취소 처리(빈 포지션 닫힘)하면서 주문은 나감 | `store.cancel_prepared`(`WHERE status='prepared'` 조건부 UPDATE) 가 rowcount 1 일 때만 취소·해제, 아니면 현재 행을 다시 읽어 주문번호 없으면 pending. `test_recall_skips_prepared_leg_claimed_by_transmitter` |
| G2-3 | P2 | `executor._apply_position_fills` | 주문번호만으로 전 다리를 매칭해 어제 다리에 오늘의 같은 번호 체결이 붙음(유령 계약 → unknown) | `repo.day_bounds(today, "KR")` 의 접수·전송·생성 시각 안 다리만(체결 동기화 `_leg_for` 와 같은 범위). `test_apply_position_fills_only_today_legs` |
| G2-4 | P2 | `store.apply_fill_economics` | 복구가 여러 다리 수량을 먼저 올리면 `qty_held − delta` 가중이 미반영 체결까지 옛 평균으로 셈(350.6875 ≠ 350.75) | `accounted_qty` 인자(기본 None 은 기존식) — `fills._accounted_entry_qty` = `qty_held` − Σ(진입 다리 `filled_qty` − 그 주문 `fills` 누적). `test_entry_average_weights_only_accounted_fills` |
| G2-5 | P1 | `rules.evaluate` RESIZE 분기 | 목표 ≥ 보유 ∧ < 보유 + 미체결(`n_leg < 0`)을 "같음" 으로 보고 승인·다리 없음 → 미체결 3이 다 체결되면 목표 2 대비 4 | `FoVerdict.recall_entries`: 다리 없이 회수 요청(진입 전용 사유 무시, note "미체결 진입 회수 뒤 재계산"); `run_leg` 가 `recall_entries` 뒤 `expected_version` 갱신 봉투로 한 번 재계산(`_recalled`), 미확정이면 blocked. `test_resize_below_pending_requests_recall`·`test_resize_recalls_excess_pending_then_resizes` |
| G2-6 | P2 | `positions.recover` ② | `awaiting_settlement` 포지션이 같은 잔고로 두 번째 복구에서 `can_transition` 실패 → unknown 격상 | 브로커에 없음 분기 첫머리에서 정산 대기는 `settling` 으로 보고 그대로. `test_recover_keeps_awaiting_settlement_on_repeat` |
| G2-7 | P1 | `executor.reduce`·`types.TRANSITIONS` | RESIZE_DOWN 재시도 소진 때 `resizing → exit_failed` 가 표에 없어 ValueError, 알림·다음 축소 없음 | 표에 `resizing → exit_failed`·`exit_failed → held`(감액 목표 도달만, `_resize_target_reached`) 추가; `_resume_reduce` 는 감액이면 exit_failed 뒤에도 목표 유지 채 보유 − 목표, 전량 의도는 잔량 전부. `test_resize_down_retry_exhaustion_escalates_to_exit_failed` |

GitHub 7건 전부 반영(`tests/fo/test_review_s2_github.py`), 스레드 답글·해결 처리.

## 단계 3 — 세션·도구·에이전트 (`feat/phase26-3-session`)

base `1bd0172` → (단계 2 GitHub 리뷰 반영 뒤) `c7bf51e`, head `94e4e3f`(리뷰 대상) → 보완 커밋은 이 절 끝. 리뷰 3종: `codex exec --sandbox read-only --ephemeral`(초점 7개: 쓰기 허용 목록·봉투 대조·실행 경로 안전·상한/사용량·입력 룩어헤드·에이전트 계약·현물 회귀), `codex review --base feat/phase26-2-store-gate-exec`, Claude `/code-review` high. 반영 테스트는 `tests/fo/test_review_s3.py`(23건) + 기존 테스트 수정(`test_registry`·`test_schemas_fo`·`test_tools_fo`).

### Codex exec (S3-1~9)

| # | 심각도 | 위치 | 문제 | 조치 |
|---|---|---|---|---|
| S3-1 | P0 | `cli._fo_tool_ctx`·`fo/session.run`·`executor.FoExecContext` | 두 생성 경로 모두 `regime_of`·`view_of` 를 넣지 않아 실행 게이트가 항상 neutral·None(국면 상한·역행 대조·옵션 매도 역행 거부 우회) | `executor.context_sources(conn, now)`(원장 `regime:KR`·오늘 `market_view:KR`)를 두 경로가 같이 넘김. `test_fo_tool_ctx_wires_regime_view_and_spot`·`test_session_run_wires_regime_view` |
| S3-2 | P0 | `submit_fo_decision` | `historical` 은 수집 당시 bool 뿐 — 어제 입력·리포트를 오늘 `--date 어제` 로 제출하면 통과, 120초 지연 제출도 동일 | 입력 `meta.date ≠ --date` 면 기록만(`record_only: input_date`, "입력 날짜 불일치 — 실행 안 함"). **120초는 실행 직전 `run_leg` 의 재시세·재잔고 검사가 본다**(플랜 3차 P1: 아침 결정은 의도, 유효기간은 실행 직전 입력에) — 제출 시각 검사는 두지 않음(세션이 보통 2분을 넘는다). `test_submit_rejects_inputs_from_other_run_date` |
| S3-3 | P0 | `submit_fo_decision` 아침 분기·`fo/session.run` | 결정 파일 저장 뒤 계좌 루프가 끊기면(예외·프로세스 종료) 재제출 `already`·세션 재실행 생략으로 미실행 계좌가 영구 건너뜀. 계좌별 예외 격리 없음 | `_fo_run_accounts`(제출·재개 공용): 계좌 예외 → 그 계좌 `_fo_risk_check` error + 다음 계좌, `OrderBookkeepingError` 는 CRITICAL + 알림; `already` 재제출은 판정 파일·오늘 보류 플래그 없는 계좌만 재개(`resumed_execution`). `test_submit_isolates_account_exception_then_resumes`·`test_submit_bookkeeping_error_is_critical_and_continues` |
| S3-4 | P0 | `cli._tool_ctx`·`fo_master.load` | 현물 도구(`tool checkpoint write` 등)도 파생 브로커 생성 + 최대 30초 마스터 다운로드를 먼저 함 | `_tool_ctx(fo=True)` 는 파생 도구(`_fo_tool_run`)만, 마스터는 `fo_master.load(offline=True)`(캐시만, 없으면 빈 목록 — 다운로드는 데몬·CLI). `test_tool_ctx_builds_fo_only_on_request`·`test_master_load_offline_never_downloads`·`test_fo_tool_ctx_none_without_fo_accounts_and_master_failure_empty` |
| S3-5 | P0 | `registry.orchestrator_prompt`·`session.py` | kind 분기가 없어 현물 세션 프롬프트에도 fo 지침·`FoDecisionEnvelope` 스키마가 들어감("현물 프롬프트 변경 없음" 위반) | `orchestrator_prompt(kind=)` 가 `_body_for_kind` 로 그 종류의 `## <kind> 모드` 절만(파생은 종목 `# 절차` 도 제외)·스키마도 종류별. `run_session` 이 `kind=spec.kind` 전달. `test_orchestrator_prompt_branches_by_kind`·`test_run_session_passes_kind_to_prompt` |
| S3-6 | P1 | `fo/inputs._technical` | 분봉 필터가 날짜뿐(같은 날 `asof` 뒤 봉 포함 — 룩어헤드), cutoff 가 UTC 날짜라 08:20 KST 는 전날 | 컷은 KST 운영일(`run_date_for`) + 분봉 `time ≤ asof(KST)`. 과거 asof 의 현재가·호가는 재현 불가 → `historical` dry-run 강제가 방어(기록). `test_candles_cut_by_kst_run_date_and_minute_time` |
| S3-7 | P1 | `fo/cli.session_cmd`·`fo/session.run`·`inputs._news_macro` | 호출자가 fo 계좌·브로커만 넘겨 `spot=[]` — 현물 헤지 ETF 보유가 항상 빈 목록 | `FoExecContext.spot_accounts/spot_brokers`; CLI 는 `boot.accounts` 현물 브로커, 도구는 `_tool_ctx` 의 현물 브로커, `fo/session.run(spot_accounts=, spot_brokers=)`. `test_news_macro_receives_spot_accounts` |
| S3-8 | P1 | `fo_reeval_request` | 라운드 할당에 잠금이 없어 동시 두 요청이 같은 r1 을 다른 request_id 로 덮음 | 같은 포지션·같은 버전의 미결 요청은 **재사용**(`reused: true`), `fo bundle --kind trader --round <n>` 으로 요청 고정(없으면 거부). 동시 첫 요청 경합은 fail-closed(덮인 봉투의 제출은 request_id 불일치로 거부)이고 운영 요청 주체는 데몬 빠른 레인 한 프로세스(단계 4) — DB 예약은 두지 않음. `test_reeval_request_reused_until_decided_and_bundle_round_pinned` |
| S3-9 | P1 | `tools._shrink`·`fo_bundle` | 문자열만 줄여 일봉 260·분봉 60 묶음이 24KB 를 넘을 수 있음 | `_shrink` 에 긴 dict 목록 꼬리 유지(120/60/30건) 단계 추가(evidence → 목록 → 문자열). `test_bundle_size_capped_with_long_candle_lists` |

### codex review (R3-1)

| # | 심각도 | 위치 | 문제 | 조치 |
|---|---|---|---|---|
| R3-1 | P1 | `cli._fo_tool_ctx` | = S3-1(실행 컨텍스트에 국면·시장 뷰 미연결) | S3-1 과 같음 |

### Claude `/code-review` (C3-1~14)

| # | 분류 | 위치 | 문제 | 조치 |
|---|---|---|---|---|
| C3-1 | 정확성 | = S3-1 | | |
| C3-2 | 정확성 | = S3-3 | | |
| C3-3 | 정확성 | `submit_fo_decision` 보류 플래그 | dry_run 세션도 `fo_pending_entry` 를 남기고(소비자가 실행할 수 있음), 플래그에 dry_run·요청 정보 없음, 낡은 플래그 미정리 | dry_run 은 보류 없이 기록만(`record_only: dry_run`), 플래그에 `request_id·dry_run` 추가, 계좌 루프 첫머리에서 오늘 것이 아니면 삭제, 소비자 `tools.fo_pending_entry` 는 오늘 운영일만. `test_pending_flag_skipped_in_dry_run_and_consumer_requires_today`·`test_pending_flag_records_requested_context` |
| C3-4 | 정확성 | `cli._fo_tool_ctx` env `TRADER_FO_HISTORICAL` | 세션이 설정하지 않는 env 를 읽음(이중 경로, 항상 0) | env 제거 — `_fo_inputs.meta.historical` 하나가 출처(`submit-fo-decision` 이 읽음) |
| C3-5 | 정확성 | = S3-4 | | |
| C3-6 | 정확성 | = S3-5 | | |
| C3-7 | 정확성 | = S3-6 | | |
| C3-8 | 정확성 | = S3-7 | | |
| C3-9 | 정확성 | = S3-8·S3-9 | | |
| C3-10 | 정확성 | `fo/session.run`·`fo/cli.session_cmd` | 운영일이 `_today(None)`(now) — `--asof` 가 어제면 디렉터리가 어긋남 | `date` 생략 시 `run_date_for("KR", asof or now)`, 결과에 `date`. `test_session_run_date_from_asof` |
| C3-11 | 중복 | `_fo_turns`·`fo_write`·`fo_summarize`·`inputs._jsonable` | 현물 `_load_turns`·`market_write`·`market_summarize`·`sources/inputs._jsonable` 복제 | `_load_turns(d, pattern)`·`_validate_payload`·`_summarize_turns` 하나씩, `_jsonable` 은 import |
| C3-12 | 계약 | `fo-trader.md` 입력 ↔ 코드 | budget 키(`planned` 없음·`remaining` 있음)·계좌 positions 키·`sell_stop_mult` 가 코드와 다름; `fo write` 가 리포트 ticker 를 검사하지 않음 | md 를 코드대로 고치고 `fo write` 는 `ticker == meta.proxy` 검사. `test_fo_trader_md_input_contract_matches_bundle`·`test_fo_write_requires_report_ticker_to_be_proxy` |
| C3-13 | 효율·전파 | `submit_fo_decision` 전제 검사·`run_session`·`tool fo --market` | 전제 검사가 토론 턴 JSON 을 전부 읽음; 세션 dry_run 이 `fo.dry_run` 에 전파되지 않음; `--market US` 가 무시됨 | 전제는 단계 이름만, `run_session` 이 `fo.dry_run=True`, `_fo_tool_run` 이 `KR` 외 거부. `test_run_session_dry_run_forces_fo_dry_run`·`test_fo_tool_commands_reject_non_kr_market` |
| C3-14 | 문서 | `CLAUDE.md` | 에이전트 md 수(12)·세션 종류 목록이 낡음 | 16개·kinds 갱신 + kind 별 프롬프트 규칙. `test_claude_md_lists_fo_kinds_and_agent_count` |

확인함(리뷰어 공통): 쓰기 허용 목록·경로 탈출 거부, 봉투 대조(아침 계좌·포지션 비움·request_id, 재평가 네 값 + 현재 version), FLAT HOLD·historical·창 밖에서 주문 없음, 두 상한 한 트랜잭션·`RESUME_KINDS`, 옵션 체인 ≤18, fixture 합성·비밀정보 없음.

단계 3 결론: **보완 뒤 진행** — P0 5·P1 4 + Claude 14(중복 8) 전부 반영(S3-2 의 제출 시각 검사와 S3-8 의 DB 예약은 설계대로 부분 반영, 사유는 표). `uv run ruff check`·`uv run pytest` 통과 뒤 PR(`--base feat/phase26-2-store-gate-exec`).

### GitHub Codex PR 리뷰 (G3-1~4, PR #61 head `800ca63`)

| # | 심각도 | 위치 | 문제 | 조치 |
|---|---|---|---|---|
| G3-1 | P1 | `submit_fo_decision` 제출·재개 | 입력 신선도를 세션 시작 때만 봐서 늦은 제출이나 몇 시간 뒤 재개가 낡은 결정으로 바로 주문을 냄 | `_fo_submit_stale`: `meta.asof` 가 `llm.timeout_s + fo.max_input_age_s` 보다 오래면 `record_only: stale_submit`(판정만, 주문·보류 없음). 제출과 재개 둘 다 적용. 진입 창 밖 보류는 `run_leg` 가 실행 직전 재조회·재계산하므로 그대로. `test_late_submit_records_only`·`test_resume_hours_later_records_only` |
| G3-2 | P2 | 재개 분기 계좌 선별 | 판정 파일(`_fo_risk_check_<계좌>`)은 다리 예약 **전에** 쓰이므로, 그 사이에 죽으면 재개가 그 계좌를 끝난 것으로 보고 건너뜀 | `_fo_account_done`: 보류 플래그·`_fo_order_<계좌>`·결정 파일 저장 뒤 생긴 라운드 0 원장 다리(OPEN·RESIZE_UP·RESIZE_DOWN·CLOSE)가 있거나, 판정이 실행할 것이 없을 때(거부·기록만·오류·건너뜀·다리 없음)만 끝난 것. 승인·다리 있는 판정만 남았으면 다시 실행. `test_resume_reruns_account_whose_risk_check_has_no_leg` |
| G3-3 | P2 | `fo/inputs._news_macro` | `market_view.build_inputs` 에 시크릿을 넘기지 않아 FRED·ECOS 거시 지표가 늘 빠짐 | `FoExecContext.secrets` 추가, `cli._fo_tool_ctx`·`fo/session.run(secrets=)`·`fo session` CLI 가 `boot.secrets` 를 넘김. `test_news_macro_forwards_secrets`·`test_session_run_puts_secrets_on_context`·`test_fo_tool_ctx_carries_boot_secrets` |
| G3-4 | P2 | `_fo_run_accounts` 창 판정 | RESIZE·REPLACE 를 진입으로 보고 진입 창(cutoff 15:30) 밖이면 보류해, 장중(15:45 까지) 축소·청산이 막힘 | 진입 창은 OPEN 과 보유 없는 RESIZE·REPLACE 만. 보유가 있으면 장중이면 바로 `run_leg`(증액 다리는 게이트가 진입 창 밖이면 거부). `test_replace_with_position_closes_after_entry_cutoff`·`test_resize_with_position_runs_open_without_position_pends`·`test_open_after_entry_cutoff_still_pends` |

## 단계 4 — 데몬·모니터·재평가·웹 (`feat/phase26-4-daemon-web`)

리뷰 base `800ca63`(단계 3 head), head `9362175`. 세 리뷰를 같은 범위에서 돌렸다: Codex exec(초점 프롬프트 11항목) P0 7·P1 6, `codex review --base 800ca63` P1 1·P2 3, Claude `/code-review` high 10. 겹치는 지적이 많아 한 표로 묶는다(출처 X=exec, R=review, C=Claude).

| # | 심각도 | 위치 | 문제 | 조치 |
|---|---|---|---|---|
| S4-1 | P0 | `fo/order_test.py` 청산 재시도 (X1·R1) | 청산 지정가 취소 중 체결돼도 취소 결과를 버리고 시장가 1계약을 다시 내 반대 포지션이 생길 수 있음 | `_close`: 취소가 **terminal 로 확정**될 때만 확정 잔량만큼 시장가 재주문, 미확정이면 재주문 없이 `unresolved`. `test_option_close_filled_during_cancel_no_extra_order`·`test_close_cancel_unconfirmed_does_not_resubmit` |
| S4-2 | P0 | `order_test.run` 진입 취소 (X2·R2) | 진입 취소 미확정·조회 실패를 '보유 없음' 으로 끝냄 | terminal 취소일 때만 무보유 종료, 아니면 주문번호와 함께 `unresolved`. `test_entry_cancel_unconfirmed_is_unresolved` |
| S4-3 | P0 | `order_test.run` 확인 단계 (X3·R3·C2) | 진입 체결 뒤 잔고 조회·시세·청산 주문 예외가 정리(청산)를 건너뛰고 traceback 으로 끝남 | 확인 단계 예외는 기록 후 청산 진행, 청산 시세 실패는 시장가, 주문 예외는 `_place` 가 '살아 있을 수 있음' 으로 기록·`unresolved`. `test_balance_failure_still_closes`·`test_close_order_exception_reported_unresolved` |
| S4-4 | P0 | `fo/engine.maintain` (X4) | 유지 검사가 `held` 만 보고 진입 중 보유·미체결 진입 예약을 빼서 노출·예산·스트레스가 과소 계산 | `entering` 포함, 살아 있는 진입 다리 잔량을 노출·수명 예산·스트레스에 합산, 위반이면 `_risk_reduce` 가 **진입 회수부터**(다음 틱에 보유만으로 재계산). `test_maintain_counts_pending_entry_and_recalls_first` |
| S4-5 | P0 | 데몬·CLI `engine.tick` 호출 (X5) | 휴장일 함수를 넘기지 않아 강제 청산일·만기 의견일이 평일 휴장을 운영일로 셈 | `engine.tick` 이 `holiday_fn` 이 없으면 `market_client.is_holiday`(KIS, 틱 안 캐시, 실패는 개장)를 쓴다. `test_force_close_day_uses_market_holidays` |
| S4-6 | P0 | `positions.adopt`·`fo adopt` (X6) | 다른 계좌 잔고로 unknown 포지션을 승계할 수 있음 | `adopt(account_id=)` 소유 계좌 일치 필수, CLI 가 넘긴다. `test_adopt_refuses_other_account` |
| S4-7 | P0 | `reeval.apply` HOLD (X7)·`engine` 기록만 모드 (C1·C4) | dry_run·paused·off 에서도 HOLD 익절선 변경, 보류 진입 플래그 소비(실제 데몬이 아침 결정을 못 냄), REPLACE 플래그 소비, 트리거 기록·재평가 대기열 등록, 정산 전이, 유지 축소의 `qty_target` 하향이 일어남 | `engine._hold`(=`executor._record_only`)가 있으면 보류 진입·REPLACE 를 소비하지 않고 트리거·재평가를 띄우지 않으며 유지 축소는 목표를 건드리지 않고 기록만, dry_run 은 정산·halted 를 쓰지 않음, HOLD 의 `tp_pt` 는 주문이 나가는 모드일 때만. `test_dry_run_tick_keeps_pending_flag_and_records_no_trigger`·`test_paused_risk_reduce_does_not_lower_target`·`test_reeval_hold_tp_unchanged_when_paused` |
| S4-8 | P1 | `engine` 일일 손실 (C3) | `cash.unrealized`(진입 이후 누적 평가손익)로 '일일' 손실을 판정해 스윙 보유의 옛 손실로 halt | 오늘 첫 관측 평가손익을 `control.fo_day_open:<계좌>` 에 두고 `realized_today + (unrealized − 기준)` 으로 판정(밤사이 갭은 첫 틱 이전이라 빠진다 — 근사, 문서화). `test_daily_loss_uses_today_change_not_cumulative` |
| S4-9 | P1 | `triggers._fo_rows` (R4) | 같은 계약을 가진 다른 계좌의 트리거가 대기·간격·하루 상한을 함께 소비 | 조회에 `account_id` 추가(트리거 행은 계좌를 적는다), 청산 때 폐기도 계좌 범위. `test_trigger_throttle_is_per_account` |
| S4-10 | P1 | `triggers._OK`·`reeval.run` (X9·X10) | 세션 없이 버린 처리('폐기:')를 성공으로 세고, 처리 시각을 세션 **시작** 시각으로 적음 | `_OK` 가 '실패:'·'폐기:' 를 모두 제외, `handled_at` 은 세션이 끝난 뒤 `ctx.now()`. `test_discarded_trigger_not_counted` |
| S4-11 | P1 | `submit_fo_decision` `_r<n>` (X11) | 재평가 결정의 기준 지수가 아침 `_fo_inputs` 값으로 저장돼 다음 move 트리거 기준이 틀림 | 재평가 제출 때 `market_client.get_kospi200()` 로 `resolved.index_at_decision` 을 갱신(실패면 아침 값) |
| S4-12 | P1 | `tools._fo_account_done` (X12·C10) | 재개 판정이 결정과 무관한 다리·파일 mtime 에 의존 | 원장 **v27** `fo_order_legs.request_id`(+인덱스): `ReservePlan.request_id` 로 진입·축소 다리에 결정 봉투를 남기고 재개는 `account_id + request_id` 다리 유무로 판정(보류 진입을 엔진이 실행해도 같은 request_id) |
| S4-13 | P1 | `reeval.run` (C5) | 아침 파생 세션이 없던 날(상한·실패) 재평가 세션이 떠도 제출 전제(리포트·요약)에 막혀 비용만 씀 | 그날 `_fo_inputs`·`_fo_summary` 가 없으면 세션 없이 '폐기:'(보호선·유지 조건은 엔진이 계속). `test_run_skips_without_morning_summary` |
| S4-14 | P2 | `engine` 트리거 요청 (C6) | 대기열 등록 실패 시 트리거가 미처리로 남아 그날 그 계약의 트리거가 전부 막힘 | `_enqueue` 가 실패면 False(중복은 True) → 엔진이 '실패:' 로 닫아 다음 간격 재시도. `test_enqueue_failure_marks_trigger_failed` |
| S4-15 | P2 | `engine._novel_news` (C7) | 종목 태그가 있는 개별 종목 뉴스까지 세어 지수 포지션의 뉴스 재평가가 무관한 기사로 뜸 | 종목 태그 없는 시장·거시 뉴스만. `test_novel_news_counts_untagged_only` |
| S4-16 | P2 | `engine` 틱 (C8) | 틱마다 잔고 조회 2회 | `fo.balance.refresh(balance=)` 로 같은 틱의 잔고 재사용 |
| S4-17 | P2 | `engine._held_row` (C9) | `executor._broker_qty` 와 같은 매칭 중복 | `executor.broker_row` 하나로(`_broker_qty` 도 그것을 쓴다); 복구 알림 키는 프로세스마다 같은 sha1 |
| S4-18 | P1 | 테스트 범위 (X13) | 수명 시나리오가 진입 중 손절·취소 중 체결을 안 봄, 현물 스냅샷은 KR BUY·HOLD 실행만 | `test_stop_while_entering_recalls_and_counts_fill_during_cancel` 추가. 현물 스냅샷은 execute 경로의 회귀 방어일 뿐이며 계좌 목록·데몬 소비자는 `tests/test_daemon_fo.py` 가 따로 본다(한계를 문서에 명시) |
| S4-19 | P1 | `engine.maintain` 국면 cap 0·대조 0 (X8) | 스펙 §4.4 '배수 한도는 유지 조건' 과 달리 cap 0·대조 0 을 최대 한도로 바꿔 감액하지 않음 | **설계 유지(문서 명시)**: 설정 의미가 '그 국면 신규 금지'(N4)라 보유를 0 으로 강제 청산하지 않는다 — 게이트 `rules.evaluate` 와 같은 해석. 국면 전환 시 청산을 원하면 별도 설정으로 다룬다(실기 3 뒤 판단, TODO 결정 대기) |

확인함(리뷰어 공통): 지수 보호선의 시간·신선도 조건, 옵션 손절의 저장선 단독 발동, 옵션 매도 +1운영일, `hold_overnight=false` 마감 시각, 스트레스 15:00 확정, 진입 다리만 회수, halted 날짜, 보류 진입 실행 전 플래그 삭제·cutoff 폐기, 늦은 재평가 CAS·REPLACE 다음 틱, 현물·파생 계좌 목록 분리와 소비자별 전달, 파생 시계 게이트, order-test 실전 거부·통과 수단만 플래그, 거부 목록, 웹 계좌 검증·합산 분리·SSE 지문, 합성 fixture·비밀정보 없음.

단계 4 결론: **보완 뒤 진행** — P0 7·P1 8·P2 4 반영(S4-19 는 설계 유지로 문서화). `uv run ruff check`·`uv run pytest`·web typecheck/vitest/build 통과 뒤 PR(`--base feat/phase26-3-session`).

### GitHub Codex PR 리뷰 (G4-1~6, PR #64 head `269da3e`)

| # | 심각도 | 위치 | 문제 | 조치 |
|---|---|---|---|---|
| G4-1 | P1 | `fo/engine._tick_account` ① | 접수(accepted)된 진입 다리의 체결은 복구가 다루지 않아, 정기 체결 동기화(16:10·07:10)까지 실제 보유가 보호선·유지 조건 밖 | 매 틱 복구 전에 `fo.fills.sync`(같은 날 계보 누적 교체, 멱등). `test_accepted_entry_fill_synced_each_tick` |
| G4-2 | P2 | `engine` 보호선 발동 | 기록만 모드(dry_run 등)에서도 대기 재평가 트리거를 '폐기:' 로 닫음 | 주문이 나가는 모드일 때만 폐기. `test_record_only_exit_keeps_pending_trigger` |
| G4-3 | P2 | `daemon.queued_job_body` | 게이트(일시정지·휴장·장 밖)로 건너뛴 `fo_reevaluate` 의 트리거가 미처리로 남아 그날 새 트리거가 전부 막힘 | `_release_fo_trigger` 가 '실패: <사유>' 로 닫는다(빈도에 세지 않아 다시 뜬다). `test_skipped_queued_reevaluation_releases_trigger` |
| G4-4 | P1 | `engine.tick`·`executor.reduce` | 실전 파생 계좌(LIVE 확인 auto)도 엔진 보호선·유지 축소로 주문 가능 — `run_leg` 의 paper 전용 가드를 우회 | 근본에서: `executor.fo_mode` 가 **실전 파생 계좌는 항상 off** → reduce·recall·continue_reduce·run_leg·엔진 `_hold` 가 모두 기록만. `test_real_fo_account_never_orders_from_monitor` |
| G4-5 | P2 | `engine.exit_reason` | 오래된 시세의 선물가·프리미엄으로 손절 발동 가능 | 가격 손절은 `quote_asof` 가 `max_quote_age_s` 안일 때만(시간 기반 청산은 그대로). `test_stale_quote_does_not_trigger_price_stop` |
| G4-6 | P2 | `engine._decision_ref` | 라운드가 날마다 1부터라 어제 r5 가 오늘 r1 보다 앞서 기준 지수·무효화 조건이 낡음 | `ORDER BY date DESC, round DESC, id DESC`. `test_decision_ref_prefers_latest_date_over_round` |

체인 병합 메모(2026-10-28): main 에 전체 검토 2차(#58·#59·#62)가 먼저 들어와 원장 v26~v29 를 썼으므로, 파생 마이그레이션을 v26 → **v30**, 다리 `request_id` 를 v27 → **v31** 로 옮겼다(둘 다 아직 운영 원장에 적용된 적 없음). main 의 `orders.origin` 에 파생 주문은 `fo`(체결 동기화의 원장 밖 주문은 `external`), 파생 재평가는 main 의 재평가 레인(`jobs.REEVAL_KINDS`), 텔레그램 `/fo` 는 조회라 미확인 발신자 허용 목록(main 은 조회만 허용으로 좁힘)에 둔다.

## 단계 5 — 전체 교차 리뷰 B (`feat/phase26-5-docs-review`)

리뷰 base `a39e102`(main), head `e946193`. Codex exec 2회(① 주문 안전·프로세스 P0 5·P1 1, ② 투자 방식·화면 P0 2·P1 5·P2 1)와 Claude 리뷰어 2명(서로 모른 채: 주문 다리 수명 P0 3·P1 5·P2 4, 위험 규칙·화면 P1 6·P2 5)을 같은 범위로 돌렸다. 리뷰 도중 main 에 전체 검토 2차(#58·#59·#62)가 먼저 들어와 체인을 병합했고(원장 v30·v31, 단계 4 표 끝 메모), PR #64 의 GitHub Codex 6건(G4)도 같은 시기에 반영했다. 겹치는 지적을 묶어 S5-n 으로 적는다(출처 XA·XB = Codex ①②, CA·CB = Claude ①②).

| # | 심각도 | 위치 | 문제 | 조치 |
|---|---|---|---|---|
| S5-1 | P0 | `executor._transmit` (XA1·CA5) | 응답 대기 중 체결 동기화가 다리를 묶으면 전송기가 두 번째 주문 행을 만들고 끝난 다리를 accepted 로 되돌려 경제값이 이중 반영 | 응답 반영을 한 트랜잭션으로: 다리를 다시 읽어 이미 묶였으면 그 주문을 재사용하고 `sending` 일 때만 상태를 바꾼다(주문 행 삽입도 같은 트랜잭션). `test_transmit_reuses_order_bound_by_sync_during_send` |
| S5-2 | P0 | `positions.recover`·`continue_reduce` (XA2) | 예약 뒤 전송 전에 죽은 `prepared` 다리가 영구히 청산·재개를 막음 | 복구가 120초 넘은 prepared 다리를 조건부 취소(진입은 빈 포지션 해제, 축소는 활성 연결 해제), `continue_reduce` 는 prepared 축소 다리를 취소하고 다시 낸다, 재개 판정은 '전송 없이 취소된 다리' 를 실행 증거로 보지 않는다. `test_prepared_reduce_leg_is_cancelled_and_resent`·`test_recover_cancels_stale_prepared_entry_only` |
| S5-3 | P0 | 실전 파생 계좌 (XA3) | 공통 축소 경로로 실전 주문 가능 | G4-4 의 `executor.fo_mode`(실전 파생은 항상 off)로 해결됨 |
| S5-4 | P0 | `_confirm_leg`·`recall_entries`·`continue_reduce` (XA4·CA4·CA12) | 지난 날 다리를 오늘 날짜·번호로 조회·취소(주문번호는 날마다 재시작) | `positions.leg_day` 로 다리마다 **자기 거래일**에 조회, 묶인 번호 집합도 그날만, 지난 날 다리는 취소 요청을 내지 않고 살아 있다고 나오면 당일 소멸(expired). `test_yesterday_accepted_leg_not_cancelled_today_and_expires` |
| S5-5 | P0 | `.claude/settings.json` (XA5) | 주문이 나갈 수 있는 `fo session` 이 거부 목록에 없음 | `fo session*` 두 표기 추가. `test_settings_denies_fo_session` |
| S5-6 | P1 | `engine._settle` (XA6·CB9) | 체결 조회 실패에도 잔고 부재만으로 정산 확정, 손실만(또는 0)을 정산 손익으로 알림 | 그 틱 체결 조회가 성공했을 때만, `qty_held=0` 과 함께 CAS, 알림은 손익을 추정하지 않고 'HTS 로 확인' 안내. `test_settle_requires_fills_ok_and_zeroes_qty` |
| S5-7 | P0 | `engine` 보호선·유지 대상 (CA1) | 증액 다리 대기 중(`resizing`) 포지션이 손절·유지 검사에서 빠짐 | 대상에 `resizing`(활성 축소 다리 없음) 포함. `test_resizing_up_position_is_stop_protected`. 감액 다리 재시도 중의 손절 우선(취소 후 STOP)은 범위 밖으로 남김(감액이 1~3틱 안에 끝남, 미확인 항목에 적음) |
| S5-8 | P0 | `recover`·`continue_reduce` (CA2) | 다른 프로세스가 응답 대기 중인 sending 다리를 복구가 '확정 불가' 로 보고 unknown, 다음 틱 청산 주문 취소 | sending 은 `SENDING_GRACE_S`(60초) 동안 복구가 건드리지 않고(deferred), `continue_reduce` 는 unknown 포지션의 다리를 취소하지 않는다. `test_recover_defers_inflight_sending_leg` |
| S5-9 | P0 | `executor.reduce` 시도 횟수 (CA3) | 시도 횟수가 날짜와 무관하게 쌓여 넷째 감액에서 전이 오류로 계좌 틱 중단, 다음 날 첫 축소가 시장가 | 시도는 **오늘의** 같은 (의도·라운드) 다리만 세고, `exit_failed` 는 전이표가 허용할 때만. `test_reduce_attempts_count_today_only` |
| S5-10 | P1 | `reeval.apply`·`continue_replace` (CA6·CB7) | REPLACE 새 진입 봉투가 청산 성공과 무관하게 남아 다른 경로로 닫혀도 자동 진입, 갈아타기로 수명 예산 재충전 | 청산이 실제로 나갔을 때만 봉투를 남기고, 이 라운드의 CLOSE 다리가 체결로 보유 0 을 만든 경우에만 진입(아니면 폐기), 새 포지션은 옛 포지션의 사용 예산을 승계. `test_replace_flag_only_when_close_sent_and_discarded_after_stop` |
| S5-11 | P1 | `_send_entry`·`adopt` (CA7) | 최종거래일이 시세 필드에만 의존, adopt 는 비움 → 만기 강제 청산·정산 전환이 꺼짐 | 마스터 규칙(`fo_master.last_trade_date`) 대체, `fo adopt` 가 마스터로 채운다 |
| S5-12 | P1 | `executor.reduce` 잔고 실패 (CA8) | 잔고 조회 실패 시 원장 잔량 전부로 축소 → 외부 청산 뒤 반대 포지션 위험 | 마지막 `fo_positions_snapshot` 수량으로 상한(없으면 보류) |
| S5-13 | P2 | `run_leg` HOLD·RESIZE↓ (CA9) | CAS 실패를 무시해 손절 강화·감액 목표가 조용히 사라짐 | 실패면 `conflict` 로 돌려준다 |
| S5-14 | P2 | `continue_reduce` (CA10) | 방금 낸 지정가 축소를 다음 틱이 바로 취소 | 먼저 상태를 조회하고, 살아 있으면 접수 뒤 `REDUCE_MIN_AGE_S`(50초)는 기다린다 |
| S5-15 | P2 | `_transmit` 기록 실패 (CA11) | 원장 기록 실패 시 접수번호가 다리에 남지 않음 | 예외 전에 다리에 접수번호만 따로 기록 |
| S5-16 | P0 | `engine.maintain` 계좌 총노출 (XB1) | 포지션별 배수 검사 | **변경 없음**: `max_positions` 가 설정에서 1 로 고정(`le=1`)이라 포지션별 = 계좌 총량. 복수 포지션은 Phase 27 |
| S5-17 | P0 | `engine.maintain` 미체결 계획 손실 (XB2) | 미체결 진입을 보유 평균가로 계산 | 그 다리 가격으로(진입 사이징과 같은 식) |
| S5-18 | P1 | `engine.maintain` 스트레스 평가 불가 (XB3) | 시세가 없으면 검사가 조용히 빠짐 | '스트레스 평가 불가(시세 없음)' 경고 |
| S5-19 | P1 | `rules` 일일 손실 (XB4) | 게이트는 실현만, 엔진은 오늘 변화분 | `executor.pnl_today` 하나(엔진이 기준을 남기고 게이트는 읽기만). `test_gate_daily_loss_uses_today_change` |
| S5-20 | P1 | 웹·LLM 예산 (XB5) | 남은 예산에서 보유분 계획 손실이 빠짐 | `sizing.position_budget` {used, planned, limit, free}, 웹 `budget_planned·budget_free`, 묶음 `budget.remaining`=free(모르면 null). `test_position_budget_free_includes_planned` |
| S5-21 | P1 | `fo-trader.md` (XB6) | 역사적 변동성 입력 경로가 틀림 | `technical.quote.data.hist_vol`(없으면 매도 금지)로 고치고 RESIZE 는 계약 수도 늘지 않음을 명시 |
| S5-22 | P1 | `/fo` 재평가 목록 (XB7) | 다른 계좌 재평가가 섞임 | 선택 계좌로 거름 |
| S5-23 | P2 | 배수 표시 (XB8) | 승인 배수를 현재 노출처럼 표시 | 용어 '승인 배수' 와 설명(실제 노출은 엔진이 매 분 확인) |
| S5-24 | P1 | `triggers.check`·세션 상한 (CB1) | 실패한 유료 재평가가 간격 없이 매 틱 다시 뜸 | 간격은 처리된 모든 행 기준, 하루 상한은 세션이 뜬 것(성공 + '실패:')을 센다, 운영일 1회는 성공만. `test_failed_session_waits_interval_then_retries` |
| S5-25 | P1 | `sizing.size` RESIZE (CB2) | 같은 배수라도 델타·지수가 내리면 계약을 더 사는 물타기 | 보유 포지션은 계약 수를 늘리지 않는다(`max(qty_target, 보유+미체결)` 상한). `test_resize_never_adds_contracts_to_existing_position` |
| S5-26 | P1 | `rules` HOLD·감액 손절, 재평가 익절 (CB3) | 오타(3520) 손절선이 저장돼 다음 틱 전량 청산, 익절 부호 미검증 | 손절선은 기초지수 기준 부호·최소 거리일 때만 바꾸고 아니면 기존 유지, 재평가 HOLD 익절선도 현재 지수 기준 부호 확인. `test_hold_stop_typo_rejected_keeps_existing`·`test_reeval_hold_tp_on_wrong_side_ignored` |
| S5-27 | P1 | `sizing` 선물 손절가 (CB4) | 베이시스 공란을 0 으로, 진입가가 손절가 반대편이어도 승인 | `sizing.basis_of`(공란이면 가격 − 기초지수), `부호×(진입가 − 손절가) ≤ 0` 거부. `test_futures_basis_missing_is_derived_not_zero`·`test_futures_entry_on_wrong_side_of_stop_rejected` |
| S5-28 | P1 | `_send_entry` 증액 (CB5) | 증액이 `stop_fut` 를 단조 병합 없이 덮어씀 | `_stop_fut_change` 로 병합 |
| S5-29 | P1 | `rules` roll 판정 (CB6) | 게이트는 달력일 잔존, 엔진은 휴장 반영 영업일 → 진입 직후 강제 청산 | `GateInput.entry_block_day`(엔진과 같은 `triggers.force_close_day` + KIS 휴장일의 직전 운영일)부터 신규 금지. `test_entry_blocked_on_day_before_forced_close` |
| S5-30 | P2 | 장중 전용 옵션 매도 스트레스 (CB8) | `hold_overnight=false` 면 스트레스 검사 꺼짐 | 옵션 매도는 항상 시나리오 한도(진입·유지) |
| S5-31 | P2 | 알림·STATUS 문구 (CB10) | 방향·콜풋 없이 '매도' 만 → 풋 매도가 숏처럼 읽힘, 상태 영문, '손절' 단위 모호 | `messages.fo_what`('롱(상승) · 풋 매도') 를 주문 알림에, STATUS·`/fo` 는 한글 상태·'지수 손절'·'프리미엄 보호선', `resizing` = '수량 조정 중' |
| S5-32 | P2 | 증거금 버퍼 기준 (CB11) | 진입은 주문 전 현금, 유지 조건은 주문 뒤 현금 | 진입도 주문 뒤 잔여 현금 ≥ NAV × buffer |

확인함(네 리뷰어 공통): 방향 × 수단 6조합 매핑, 대조표, 배수 상승·손절 불리 이동 금지, 프리미엄 보호선 단조, 수명 예산 비재충전, Black-76·IV 단위, 지수 보호선 시간·신선도, `store.reserve` 한 트랜잭션·실행 키 유일성, `claim_leg`·`cancel_prepared` 조건부 갱신, `record_fill_progress` 누적값, unknown 비자동, 신규 차단 상태, 보류 진입 소비 규칙, 늦은 재평가 CAS, 데몬 배선, 웹 계좌 검증·합산 분리, 설정 두 파일 일치, 합성 fixture.

단계 5 결론: **보완 뒤 진행** — P0 9·P1 15·P2 7 반영(S5-16 은 설정상 해당 없음, S5-7 의 감액 중 손절 우선은 미확인 항목). 재리뷰 1회는 아래.
