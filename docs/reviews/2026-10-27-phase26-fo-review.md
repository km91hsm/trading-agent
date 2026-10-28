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
