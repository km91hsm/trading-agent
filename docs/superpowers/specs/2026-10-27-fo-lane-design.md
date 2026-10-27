# 선물옵션 방향성 레인 설계 (Phase 26)

사용자 승인: 2026-10-27(플랜 모드 4차 보완판, Codex 플랜 리뷰 4회 반영). 구현 플랜: `docs/superpowers/plans/2026-10-27-phase26-fo.md`.


## Context

현물(KR/US 주식) 파이프라인은 Phase 0~25 로 완성돼 모의 운용 중이다. 사용자는 **같은 모양(수집 → 분석 세션 → 파이썬 게이트 → 주문 → 장중 모니터·재평가 → 기록)** 으로 **국내 지수 파생(코스피200 선물 롱/숏, 옵션 콜·풋 매수·매도)** 방향성 매매를 더하려 한다. 모의 선물옵션 계좌(상품코드 `03`, 보유)로 먼저 돌리고 실전은 별도 Phase 에서 사용자가 켠다. 방향을 현물과 따로 잡는 규칙도 설계한다. 구현은 단계마다 Codex CLI 교차 리뷰를 거친다.

사용자 결정(2026-10-27): 미니·정규 둘 다 설정(기본 미니) · 모의 선물옵션 계좌 있음 · 스윙/당일 설정(기본 스윙) · 별도 파생 분석 세션 + 장중 재평가 · **옵션 매수·매도 모두** · **배수는 한도(`max_leverage`) 안에서 LLM 이 결정마다 정한다**(올리기·내리기 모두).

Codex 플랜 리뷰 반영 이력: 1차(31건) → 2차판, 2차(R1~R19) → 3차판, 3차(R 중 12 닫힘·7 부분 + 신규 P0 N1~N5·P1 6·P2 2) → 이 4차판. 번호는 아래 각 절에 표시.

### 조사에서 확인된 제약 (플랜의 전제)

| 항목 | 사실 | 출처 |
|---|---|---|
| 모의 지원 | 국내선물옵션 **주간** 주문 `VTTO1101U`·정정취소 `VTTO1103U`·체결 `VTTO5201R`·주문가능 `VTTO5105R`·잔고 `VTFO6118R`(선물·옵션 같은 TR). **야간 모의 TR 없음**. 시세 실전·모의 동일(`FHMIF10000000` 옵션은 델타·감마·세타·베가·IV·행사가, `FHMIF10010000` 호가, `FHKIF03020100` 일봉, `FHKIF03020200` 분봉; `FID_COND_MRKT_DIV_CODE` F/O). 체결통보 `H0IFCNI0` | open-trading-api `examples_llm/domestic_futureoption/*` |
| 해외선물 | `OTFM3001U` 모의 분기 없음 → 제외 | 같은 저장소 |
| 거래시간 | KRX 파생 정규장 **08:45~15:45**, 최종거래일 15:20 종료, 휴일이면 최종거래일 앞당김. 현물 지수는 09:00~15:30 만 갱신(`market_hours.py` KR 창) | KRX 안내 |
| 주문 body | `ORD_PRCS_DVSN_CD=02`, `SLL_BUY_DVSN_CD` 01 매도/02 매수, `SHTN_PDNO`(선물 6자리·옵션 9자리), `UNIT_PRICE`(시장가 0), `NMPR_TYPE_CD` 01/02, `KRX_NMPR_CNDT_CD` 0, `ORD_DVSN_CD` 01/02 → `output.ODNO` | 위 저장소 |
| 체결 응답 | `pdno`(표준 코드일 수 있음 — 단축코드와의 대응은 실기 1), `odno, orgn_odno, qty, tot_ccld_qty, rjct_qty, sll_buy_dvsn_cd` 를 구분한다. 취소는 별도 요청(`VTTO1103U`)이고 취소 확정은 체결 조회의 원주문 잔량으로 판정한다 | `chk_inquire_ccnl.py`, `order_rvsecncl.py`(R15) |
| 주문가능 응답 | `tot_psbl_qty`·`lqd_psbl_qty1`(청산가능)·`ord_psbl_qty`·`bass_idx`. **계약당 필요증거금을 돌려주는 항등식은 없다** | `chk_inquire_psbl_order.py`(R5) |
| 잔고 응답 | output1: `shtn_pdno, prdt_type_cd, prdt_name, sll_buy_dvsn_name, cblc_qty, ccld_avg_unpr1, evlu_pfls_amt, lqd_psbl_qty`; output2: `dnca_cash, tot_dncl_amt, ord_psbl_cash, ord_psbl_tota, mgna_tota, add_mgna_tota, prsm_dpast_amt, opt_dfpa, …`. 파라미터 `MGNA_DVSN`(01 게시/02 유지)·`EXCC_STAT_CD`·연속조회 | `inquire_balance.py` |
| 호가단위 | 선물 정규 0.05·미니 0.02pt. 옵션 정규 <10: 0.01 / ≥10: 0.05. 미니옵션 <3: 0.01 / 3≤p<10: 0.02 / ≥10: 0.05 | KRX 안내 p.58 |
| 종목코드 | 마스터 `fo_idx_code_mts.mst.zip`(`\|` cp949 9필드) + 시세 `futs_last_tr_date`·`hts_rmnn_dynu`. 접두 선물 101/105, 옵션 201·301/205·305, 위클리 209/309 제외. 분류값 의미는 실기로 확정 | `stocks_info/domestic_index_future_code.py` |
| 코드 전제 | `Market = KR\|US`, `Position.qty>0`, 현물 `rules.evaluate` BUY=진입/SELL=보유 매도, 로트 BUY 시작, `views.py:171`·`memory/store.py:55` 는 음수 qty 를 버림. `repo.claim_exec_key/bind_exec_key/insert_order` 는 각각 commit(R2), `claim_exec_key` 는 죽은 주문 키를 재사용(R3). `usage.session_allowance` 는 `spec.kind` 를 직접 받고 `"resume"` 만 예외(R14). `WebDeps.accounts`·`DaemonDeps` 는 단일 목록, `balance.refresh` 는 받은 계좌 전부에 현물 `build_portfolio`(R11). `performance.nav_series(account_id, market)`·DB `accounts` 에 asset 없음(R12) | 탐색·Codex |

## Approaches
| | 방식 | 장점 | 단점 |
|---|---|---|---|
| **A (선택)** | 별도 레인 `fo/`(단타·시장 세션 선례). 계좌 `asset: fo`, 체크포인트 `_fo_*`, 게이트·실행·상태기계·모니터·재평가·**저장 계층** 전용. 공유: 브로커 조립·원장 `orders/fills`·알림·세션 러너·빠른 대기열·웹 틀 | 현물 경로의 **기본 동작 회귀 없음**(diff 0 이 아님 — 공용 파일은 바뀐다, R19). 숏·옵션 매도·증거금·델타·만기를 독립 모델로 | 모듈·웹 페이지 별도 |
| B | `Market` 에 `KRFO` 추가 | 집계 자동 | 118곳 분기·양수 수량 전제 수정, 옵션 매도 표현 불가 |
| C | 시장 세션에 파생 방향 필드 | 비용 0 | 사용자가 별도 세션 선택, 파생 전용 입력 없음 |

## 설계

### 0. 용어(혼용 금지)
- **direction**: 시장 방향 뷰 `LONG|SHORT|FLAT`. **vehicle**: `FUTURES|OPTION_BUY|OPTION_SELL`. 매핑(파이썬): LONG+OPTION_BUY=콜 매수, LONG+OPTION_SELL=풋 매도, SHORT+OPTION_BUY=풋 매수, SHORT+OPTION_SELL=콜 매도.
- **contract_side**: 계약 보유 방향 `LONG(매수)|SHORT(매도)`. **order_side** `BUY|SELL`: 진입 = contract_side 와 같은 쪽, 청산 = 반대. 청산 주문은 **확정된 contract_side 의 반대**로만 만든다 — 방향 미확정 잔고는 주문 대상이 아니다(R9).
- **방향 노출**(1차 민감도) = `signed_qty × kind_sign(FUTURES +1, CALL +1, PUT −1) × |delta| × 기초지수 × 승수`. 옵션 매도의 한도 검사는 1차 민감도가 아니라 **시나리오 재평가**(§4.6, R6).
- **계획 손실**(planned loss): 손절선까지의 손실 추정치. 갭·호가 공백·거부로 보장되지 않는다. **수명 손실 예산**(R4): 포지션 id 하나의 수명 동안 `실현손실 + 비용 + 보유분 계획 손실 + 미체결 다리 계획 손실 ≤ NAV(개설 시점) × risk_per_trade_pct`. 부분 청산·증액이 예산을 리셋하지 않고 **재충전은 없다**(새 id 로 새 예산).
- **진입 다리**(OPEN·RESIZE↑) / **축소 다리**(RESIZE↓·CLOSE·손절·만기·마감·축소). 포지션당 살아 있는 축소 다리는 **한 번에 하나**(`active_reduce_leg_id`, R2).

### 1. 계좌·브로커 (`broker/`)
- `AccountConfig.asset: Literal["stock","fo"] = "stock"`. `paper-fo`: `markets [KR]`, `acnt_prdt_cd "03"`, `mode auto`. 키 `KIS_PAPER_FO_*` → 폴백 `KIS_PAPER_*`. `templates`·`.env.example` 동기화. asset 은 config 가 출처이고 원장 `accounts.asset`(v26) 에 부트 때 upsert 해 조회 쪽(성과·뷰)도 안다(R12).
- **계좌 전달 표와 연결부(R11)**:

| 소비자 | 계좌 | 연결부(바뀌는 파일) |
|---|---|---|
| 현물 analyze/scan/monitor/resume/reprice/rebalance/hedge/cash_park/scalp/ML·universe_members/readiness/daily_report/shadow/현물 `_tool_ctx`/현물 reconcile | **stock 만** | `_Boot.accounts` 기본값·`DaemonDeps.accounts`(현물 잡은 이 목록만) |
| control(pause/mode)·텔레그램 `/mode`·`/status`·STATUS·approvals 목록·ops_check·`kis ping`·`fills_sync`·`balance_refresh`·reconcile 디스패치·웹 `/api/accounts`(asset 꼬리표) | **all** | `_Boot.all_accounts`·`DaemonDeps.all_accounts`·`WebDeps.all_accounts`; `orchestrator/balance.refresh` 가 `a.asset` 로 디스패치(stock → 기존 `build_portfolio`, fo → `fo/balance.refresh`); `fills.sync_fills`·`reconcile.run` 도 asset 분기; `web/routers/accounts.py`·`views.account_summary` 가 fo 계좌는 `fo_nav_snapshot`/`fo_positions_snapshot` 을 읽음 |
| fo 세션·모니터·재평가·`fo close`·fo `_tool_ctx`·웹 `/api/fo/*` | **fo 만** | `_Boot.fo_accounts`·`DaemonDeps.fo_accounts`·`WebDeps.fo_accounts`·`tools._tool_ctx(asset="fo")` |

  `_boot(accounts=True)` 가 세 필드를 채우고 `cli.py:2725`·`readiness.py:100`·`daemon.py:1661` 등 `_boot` 밖 `load_accounts()` 호출은 `config.split_accounts` 를 거친다. 검증은 "직접 호출 개수" 가 아니라 **소비자별 테스트**(각 잡·라우터·명령에 어떤 계좌가 전달되고 어떤 브로커 타입이 호출되는지 스파이로 확인, R11/#19). 웹 단독(`trader web`)·`app` 진입점도 같은 생성자.
- `broker/tr_ids.py`: `FO_PRICE, FO_ORDERBOOK, FO_DAILY, FO_MINUTE, FO_BALANCE, FO_ORDER, FO_ORDER_CANCEL, FO_CCLD, FO_PSBL_ORDER`; 야간 `FO_*_NIGHT` paper `None`.
- `broker/models.py`(기존 불변): `FoPosition{code, name, kind, contract_side(필수), qty, avg_price, current_price, pnl, sellable_qty, strike|None, expiry|None}`, **`UnverifiedHolding{code, raw: dict, reason}`**(방향·종류·승수 중 하나라도 확정 불가한 잔고 행 — `FoPosition` 과 다른 타입, 어떤 자동 주문의 대상도 아님, R9), `FoCash{deposit, orderable_cash, orderable_total, margin_total, add_margin, nav|None, realized_today, unrealized}`, `FoBalance{positions, unverified, cash}`, `FoOrderRequest`, `FoQuote{…, delta, gamma, theta, vega, iv, hist_vol, strike, underlying, underlying_asof, quote_asof, received_at}`(`quote_asof` 는 응답의 시장 시각 필드, `received_at` 은 수신 시각 — 둘을 구분하고 신선도는 `quote_asof` 로 본다, R7), `FoFill{order_no, orgn_order_no, code, side, remaining_qty(=qty), filled_qty(=tot_ccld_qty 누적), filled_amount(누적 체결금액), rejected_qty, cancelled_qty, avg_price, filled_at}`, `FoOrderState{order_no, order_qty, filled_qty, remaining_qty, rejected_qty, cancelled_qty, terminal: bool, state}`.
- **엄격 파서(#12·R9, 3차 P1)**: 잔고 행은 두 검증을 따로 통과해야 `FoPosition` — **방향**: `sll_buy_dvsn_name` 과 `sll_buy_dvsn_cd` 가 둘 다 있고 서로 일치(하나만 있으면 알려진 값일 때만); **종류**: 코드 접두 ↔ `prdt_type_cd` 일치. 하나라도 실패면 `UnverifiedHolding`. NAV 는 `prsm_dpast_amt` 가 비면 `None`(항등식 폴백 금지). 잔고는 **연속조회 완주**가 성공 조건이고 실패·미완주는 예외(그 틱 신규 보류; 축소는 마지막 확정 잔량으로 §5 규칙). 시세는 `quote_asof`(시장 시각 — 현물 지수 API 의 `asof` 는 수신시각이라 **지수는 응답의 시각 필드로 바꿔 읽는다**, N1) 가 `fo.max_quote_age_s`(60) 안·양수 가격·호가 공백 아님·옵션 델타 유한 ∧ `0.05 ≤ |δ| ≤ 1` 일 때만 "위험 산정용 유효". 유효하지 않으면 신규 거부. **축소에 필요한 것은 확정 방향·확정 잔량·활성 다리 상태뿐**이며 체인·IV·지수 신선도는 축소의 전제가 아니다(N1).
- `broker/fo.py` `KisFoBroker`: `get_balance()` → `FoBalance`, `orderable(code, side, price, ord_dvsn)`, `place_order`, `cancel_order(order_no, qty=0)`(접수 응답만, **확정 아님**), `get_fills(d)`(연속조회 완주해야 반환), `get_order_state(order_no, d) -> FoOrderState`: 체결 조회의 원주문 계보(`odno` + `orgn_odno` 로 묶은 정정·취소 행)에서 `filled_qty = tot_ccld_qty`, `remaining_qty = qty`(KIS `qty` 는 **잔량**), `rejected_qty`; **terminal 판정식** `remaining_qty == 0 ∨ state ∈ {cancelled, rejected}` ∧ `filled + rejected + cancelled_qty == 주문수량`. `cancelled_qty` 는 계보 안의 **취소 행**(`rvse_cncl_dvsn` 취소, 같은 `orgn_odno`)이 있을 때만 `주문수량 − filled − rejected − remaining` 로 산출하고, 취소 행이 없으면 0 이며 잔량 > 0 이면 `terminal=False`(취소 접수 응답은 근거가 아님). 항등식 불일치·합 > 주문수량은 모순 → 예외, 원주문 행 누락은 `terminal=False` 로 돌려 unknown 유지. 취소 행의 필드명은 실기 1 fixture 로 확정(3차 P1-5). 코드 정규화 `fo_codes.normalize(pdno|shtn_pdno)`(표준↔단축 대응은 마스터 표준코드 열로, 실기 1 에서 확정). `CONTRACT_VERSION = 1`(R18). `FakeFoBroker`(부분체결·취소 중 추가 체결·unknown·잔고 실패·모순 응답 시나리오 주입).
- `kis_market.py`: `get_fo_price/orderbook/daily/minute`, `get_option_chain(product, expiry, ±4)`(≤18 호출), `get_index(kospi200)`(현물 지수 + 시각). `fo_master.py`: 다운로드·24h 캐시·`Contract`·`front_month`·`resolve_option`(행사가 간격은 마스터 인접값)·`last_trade_date(code)`.
- `market_hours.FO_SESSION = ("08:45","15:45")`, 최종거래일 `("08:45","15:20")`, `is_fo_open`, `index_live(now)`(09:00~15:30). `risk/ticks.quantize_tick(table, price)`·`tick_table_for(product, kind)`. `fo/pricing.py`: Black-76 근사(`price(kind, F, K, iv, days_left)`)로 시나리오 재평가(R6) — 시세 IV·잔존일 입력, 무위험이자 0.

### 2. 설정 (`FoCfg`, `settings.yaml`·`templates`)
```yaml
fo:
  enabled: false            # 신규 진입 허용. false 여도 보유분 감시·축소는 계속. 꺼지면 살아 있는 진입 다리를 회수한다(R1)
  product: mini
  vehicles: [FUTURES, OPTION_BUY, OPTION_SELL]   # 신규 진입 허용 수단
  hold_overnight: true      # false 면 매일 마감 close_before_min 전에 전량 청산. true 면 마감 청산 없음(만기·위험 청산만, R10)
  entry_window: {start: "09:00", cutoff: "15:30"}  # cutoff 뒤 진입 다리 회수·신규 금지
  max_input_age_s: 120      # 제출·실행 직전 필수 절(시세·잔고·체인) 최대 지연. 과거 asof 세션은 강제 dry-run(R17)
  max_quote_age_s: 60
  max_leverage: 2.0
  leverage_by_regime: {bull: null, neutral: null, bear: null}
  max_contracts: 4
  max_positions: 1
  risk_per_trade_pct: 0.02  # 수명 손실 예산(§0)
  overnight_risk_pct: 0.05  # 스윙 유지 조건: 시나리오 최악 손실 ≤ NAV × 이 값(진입 때 + 매 틱 + 15:00 확정 검사)
  stress: {index_pct: [0.03, 0.06], iv_rel: [0.0, 0.5]}   # 시나리오 격자(양방향 × IV 상승), 옵션은 Black-76 재평가(R6)
  margin_rate: {futures: 0.12, option_sell: 0.15, asof: "2026-10-27", max_age_days: 30}   # KRX 공표 위탁증거금률을 사람이 적는다(출처·날짜). asof 가 max_age_days 를 넘으면 선물·옵션 매도 신규 거부(N5)
  margin_buffer_pct: 0.3
  daily_loss_stop_pct: 0.03
  min_stop_pt: 0.5
  slippage_pt: 0.1
  min_confidence: 0.6
  roll_days: 2              # D-roll 아침 강제 청산·신규 금지(옵션 매도 +1). 재평가 의견은 D-(roll+1) 에(R16)
  close_before_min: 10
  exit_retry_max: 3         # 축소 다리 재시도(취소 확정 → 잔량 시장가) 횟수. 넘으면 exit_failed + critical(R10)
  cross_check: tilt
  session_cap: 7
  reeval: {interval_min: 30, max_per_day: 6, periodic_min: 60, periodic_max_per_day: 4, tp_near_pct: 0.002, iv_spike_pct: 0.3}
  option:
    max_premium_pct: 0.05
    buy_stop_pct: 0.5
    sell_stop_mult: {min: 1.5, default: 2.0, max: 3.0}
    sell_max_notional_pct: 1.0
    sell_against_regime: false
    min_days_left: 5
    allowed_strikes: [ATM, OTM1, OTM2, ITM1]
    weekly: false
  night_session: false
  products:
    mini:    {futures_prefix: "105", call_prefix: "205", put_prefix: "305", multiplier: 50000,  futures_tick: "0.02", option_ticks: [["3","0.01"],["10","0.02"],["inf","0.05"]], name: 미니코스피200}
    regular: {futures_prefix: "101", call_prefix: "201", put_prefix: "301", multiplier: 250000, futures_tick: "0.05", option_ticks: [["10","0.01"],["inf","0.05"]], name: 코스피200}
```
`schedule.yaml fo: {kr: {session: "08:20", monitor: {start: "08:45", end: "15:45", interval_s: 60}}}`. 08:20 세션의 결정은 **의도**(`pending_entry`)이고 실행은 09:00 진입 창에서 §5 `run_leg` 가 시세·잔고·주문가능을 다시 받아 **게이트·사이징을 처음부터 다시 계산**한 결과로만 한다(3차 P1 — 120초 유효기간은 실행 직전 입력에 적용, 아침 결정 자체에는 적용하지 않는다). `leverage_by_regime` 의 `null` 은 미설정(= `max_leverage`), `0` 은 **그 국면 신규 금지** — `or` 로 합치지 않고 명시 분기(N4).
**검증 플래그(R18)**: control `fo_verified:<env>:<account>:<product>:<vehicle>` = `CONTRACT_VERSION`. 게이트는 (그 계좌·상품·수단) 키의 값이 현재 `CONTRACT_VERSION` 과 같을 때만 신규 허용. `trader fo verify` 는 빈 응답을 검증으로 세지 않는다 — 방향·kind·NAV 는 실제 보유 행이 있을 때만 통과(실기 절차 §검증).

### 3. 결정·에이전트·세션
- **결정 봉투**(R13): `_fo_decision[_r<n>_<acct>]` payload = `{decision: FoDecision, account_id|None, position_id|None, expected_version|None, request_id}`. **아침**: 결정 1개(방향·수단·배수, `account_id=None`) + **계좌별 사이징·판정**(`_fo_risk_check_<acct>`), bundle `context.accounts[]` 는 fo 계좌 전부 요약. **재평가**: (계좌, 포지션) 단위 — 큐 요청이 `account_id, position_id, expected_version, request_id` 를 주고 세션 stdin 으로 전달, 결정 봉투는 그대로 되돌려야 하며(`extra="forbid"`, 불일치면 도구가 거부) 체크포인트·`fo_decisions UNIQUE(date, account_id, position_id, round)`·실행 키가 같은 단위.
- **`FoDecision`**: `{intent OPEN|RESIZE|HOLD|CLOSE|REPLACE, direction, vehicle, target_leverage(0~5), strike_rule, expiry_rule, order_type, entry_price|None, stop_pt(OPEN/RESIZE/REPLACE 필수), take_profit_pt|None, premium_stop_mult|None, horizon_days, confidence, rationale, invalidation_conditions, vs_spot}`. RESIZE 는 같은 계약의 목표 총량, REPLACE 는 청산 완료 뒤 새 OPEN(다음 틱 이후). 검증: LONG 은 stop < 기초지수 < tp, SHORT 반대.
- `.claude/agents/fo-trader.md`(opus, 공통 규칙 동일, md 16): 입력 `{reports, research_summary, context: {index, option_chain, products, accounts[{id, nav, orderable_cash, margin_total, add_margin, positions}], regime, market_view(state), spot_exposure, costs, limits, position(재평가: id, version, 수명 예산 잔여, 현재 손절·배수)}}`. 규칙: 기본 HOLD/FLAT · 확신 < 기준이면 FLAT · 배수는 한도 안에서 스스로 · 옵션 매도는 IV > 역사적 변동성 ∧ `premium_stop_mult` 필수 · 손절은 기초지수 레벨, **기존 포지션의 손절은 불리하게 옮길 수 없고 배수도 올릴 수 없다**(파이썬이 강제, R8) · 역행이면 `vs_spot` 필수.
- 애널리스트 재사용 매핑(#23): `analyst-technical`→`technical`(현물 technical 절 형태), `analyst-sentiment`→`sentiment`, `analyst-market`→**role `news_macro`** 파일 `_fo_analyst_news_macro`. `fo bundle` 이 어댑트. `registry.FO_AGENTS` + `schema_model_for("fo-trader")`.
- **세션 종류·사용량(R14)**: `kind="fo"|"fo_resume"`. `usage.RESUME_KINDS = frozenset({"resume","fo_resume"})` 로 `session_allowance` 의 soft 예외를 바꾸고(파일 `src/trader/usage.py`), `run_kind` 매핑에 `fo→"fo"`, `fo_resume→"monitor"`. 상한 예약은 `_start_call(cap=)` 한 트랜잭션에 `fo.session_cap`·`llm.daily_session_cap`. 테스트: kind → usage 등급 → 상한 예약 연결.
- 도구 쓰기 권한(#22): `fo write` 는 `_fo_analyst_<technical|sentiment|news_macro>`·`_fo_debate_r<n>_<bull|bear>` 만; `_fo_summary` 는 `fo summarize`; `_fo_decision*` 는 `submit-fo-decision` 전용(전제: 서로 다른 유효 role ≥2 + 양측 r0 + 요약; `_r<n>` 은 봉투의 `position_id` 행 존재 + `expected_version` 일치).
- **asof·실행 허용(R17)**: `SessionSpec.asof` 가 `now − max_input_age_s` 보다 과거면 **`dry_run=True` 강제**(세션 생성 시점, 로그에 명시). `submit-fo-decision` 은 실행 전 `_fo_inputs` 필수 절(`quote`·`balance`·옵션이면 `chain`)의 `received_at`·`quote_asof` 가 `max_input_age_s` 안인지 검사하고, 아니면 판정만 기록(`note: "입력 노후 — 실행 안 함"`). `executor.run_leg` 도 전송 직전 같은 검사를 **재시세·재잔고**로 다시 한다.

### 4. 게이트 (`fo/rules.py`·`fo/sizing.py` 순수 함수) — 거부 사유 전부 수집
`evaluate(decision, port: FoPortfolio, cfg, *, quotes, index, regime, view, now, today, killswitch, paused, orderable, verified, inputs_fresh) -> FoVerdict{approved, legs[], leverage_used, exposure_after, stress_loss_after, lifetime_budget{used, planned, limit}, reasons, note}`
1. **축소 다리**는 `enabled`·`vehicles`·`verified`·대조·배수·입력 신선도와 무관. 막는 것은 `paused`·`off`·`dry_run`(기록만, 취소도 안 냄)뿐.
2. **진입 다리 거부**(전부 수집): `enabled=false` · 수단 밖 · `verified(계좌·상품·수단)` 아님 · `inputs_fresh` 아님 · 진입 창 밖 · 만기 · 당일 손실/halted · 킬스위치 · `add_margin > 0` · 확신 · 손절 부호·거리 · 반대 방향 보유/청산 중/unknown 잔고 존재 · `max_positions` · 옵션 매도 `premium_stop_mult` 없음 · **과거 asof**.
3. **대조** `cross_factor(regime, view_state, mode, direction, vehicle)`: `view_state ∈ {RISK_ON, NEUTRAL, RISK_OFF, NONE}`(제안 없음·오늘 날짜 아님·확신 < `hedge.agent_min_confidence` 는 NONE). `mode=off` → 1.0. `tilt/block`:

| 국면 | 전략가 | LONG | SHORT |
|---|---|---|---|
| bull | RISK_ON / NEUTRAL / NONE | 1 | 역행: tilt 0.5 · block 0 |
| bull | RISK_OFF | 0.5 | 0.5 |
| neutral | NONE / NEUTRAL | 1 | 1 |
| neutral | RISK_ON | 1 | 0.5 |
| neutral | RISK_OFF | 0.5 | 1 |
| bear | RISK_OFF / NEUTRAL / NONE | 역행: tilt 0.5 · block 0 | 1 |
| bear | RISK_ON | 0.5 | 0.5 |

   "역행" = bull+SHORT, bear+LONG. **옵션 매도는 역행이면 mode 와 무관하게 0**(`option.sell_against_regime=false`). 단방향(파생이 현물 결정·헤지를 바꾸지 않음), 현물 헤지 ETF 보유 + SHORT 는 note·알림에 합산 숏 노출.
4. **배수** `L = min(target_leverage, max_leverage, regime_cap) × cross_factor`(`regime_cap`: null → `max_leverage`, 0 → 신규 거부). **기존 포지션의 RESIZE 는 `leverage_used`·`approved_mult` 를 올릴 수 없고 `stop_pt` 는 유리한 쪽으로만**(모든 의도, HOLD 포함, R8). **배수 한도는 유지 조건**이기도 하다(N4): 계좌 실제 노출 배수 `Σ델타명목(보유+미체결) ÷ NAV` 가 현재 `min(max_leverage, regime_cap) × cross_factor` 를 넘으면 진입 회수 → `RESIZE↓`(§5 ③).
5. **사이징(총량·수명 예산, R4·R8·N3)**:
   - 보유분 기준가 = 잔고 평균체결가(`ccld_avg_unpr1`), 미체결 다리 기준가 = 지정가(시장가는 보수가: 매수 ask+slippage / 매도 bid−slippage). 옵션 증액 뒤 `entry_premium` 은 수량 가중 평균으로 갱신하되 **절대 프리미엄 보호선 `premium_stop` 을 따로 저장**하고 단조로 유지한다: 매도 = `min(기존 premium_stop, 새 평균가 × approved_mult)`, 매수 = `max(기존, 새 평균가 × (1 − buy_stop_pct))`. 모니터·계획 손실·수명 예산은 `premium_stop` 으로 계산하고 부분체결·증액 뒤 즉시 재계산(초과면 `RESIZE↓`).
   - 선물 `stop_fut = stop_pt + basis_now − sign×slippage_pt`; 계획 손실/계약 = `|기준가 − stop_fut| × 승수`. 옵션 매수 = `(기준가 − premium_stop) × 승수`(전액 = 기준가 × 승수). 옵션 매도 = `(premium_stop − 기준가) × 승수`.
   - **수명 예산 검사**: `realized_loss + fees + Σ보유 계획 손실 + Σ미체결·이번 다리 계획 손실 ≤ nav_at_open × r`. 초과면 진입 거부(축소는 허용).
   - 델타명목/계약 = `|δ| × 기초지수 × 승수`; `n_target = floor(NAV × L ÷ 델타명목/계약)`; 총 계약 ≤ `max_contracts`, 옵션 매수 전액 ≤ `max_premium_pct`, 옵션 매도 비조정 명목 ≤ `sell_max_notional_pct`.
   - 이번 다리 = `n_target − 보유 − 미체결`; ≤0 이면 축소 또는 거부.
6. **시나리오 스트레스(R6)**: `stress.index_pct × {+,−}` × `iv_rel` 격자로 포트폴리오 전체(보유 + 미체결 + 이번 다리)를 재평가 — 선물은 선형, 옵션은 Black-76(`fo/pricing`) — 최악 손실 `stress_loss_after ≤ NAV × overnight_risk_pct`(스윙) 를 **진입 조건이자 유지 조건**으로 둔다(§5 ③).
7. **증거금·현금 버퍼(R5·N5)**: 계약당 필요증거금의 **검증된 산식은 없고 공표 위탁증거금률도 상한이 아니다**(옵션 매도는 이론가 변화·최소증거금 산식). 그래서 버퍼 보장은 브로커 사전 계산에서만 끌어낸다: `orderable(code, side, price, ord_dvsn)` 가 "현금 C(`ord_psbl_cash` 또는 응답의 기준 현금)로 이 계약을 q 계약(`ord_psbl_qty`) 신규 주문 가능" 이라고 답하면 계약당 증거금 `m ≤ C ÷ q` 이므로 **`n ≤ floor(q × (1 − margin_buffer_pct))` 면 `n × m ≤ (1 − buffer) × C`**, 즉 주문 뒤 잔여 현금 ≥ `buffer × C` 가 보장된다. 이 추론의 전제(같은 현금 풀·상계 없음)를 함께 검사한다: `max_positions = 1` 로 반대·상계 보유가 없고, `tot_psbl_qty == ord_psbl_qty`(청산가능분 섞임 없음), `lqd_psbl_qty1 == 0`, q ≥ 1, C 가 None 아님 — 하나라도 깨지면 **선물·옵션 매도 신규 거부**. 추가로 `C − 로컬 예약 ≥ NAV × margin_buffer_pct` 와 `add_margin == 0`. `margin_rate`(공표값·날짜)는 상한이 아니라 **정합성 검사**에만 쓴다: `C ÷ q` 가 `명목 × margin_rate × 3` 을 넘거나 `max_age_days` 를 넘으면 응답을 신뢰하지 않고 거부. 체결 뒤 잔고 재조회로 `ord_psbl_cash ≥ NAV × buffer ∧ add_margin == 0` 사후 검사 — 미달이면 신규 금지 + §5 축소. 실기 2 는 파싱 검증일 뿐 증거금 상한의 증명이 아니다.
   **로컬 예약(N5-b)** = `prepared|sending|unknown` 다리 + **`accepted` 미체결 다리 전부**(수명 내내 차감, 브로커 스냅샷으로 풀지 않는다 — 시각 비교는 반영 증거가 아님) + 체결분은 **요청을 체결 시각 뒤에 시작한 잔고 응답**이 그 보유 수량을 보여 줄 때 예약에서 보유로 옮긴다(`balance.requested_at > fill.filled_at`, 아니면 예약 유지). 다리가 terminal(취소·거부·만료) 이면 해제. 분모 0·필드 None 은 거부. 테스트: 주문 전 시작한 잔고 응답이 접수 뒤 도착해도 예약이 풀리지 않음.
8. 가격: LIMIT 은 tick 표로 `quantize_tick`; 옵션 매도 진입 MARKET 금지 → 호가 1단 지정가; 축소는 선물 시장가, 옵션은 호가 1단 지정가 → 취소 확정 뒤 잔량 시장가.

### 5. 포지션 상태기계·실행·모니터·재평가 (`fo/store.py`, `fo/positions.py`, `fo/executor.py`, `fo/engine.py`, `fo/triggers.py`, `fo/reeval.py`)
- **저장 계층 `fo/store.py`(R2·R3)**: commit 하지 않는 헬퍼만 두고 트랜잭션 경계는 호출자(`with store.tx(conn): …` = `BEGIN IMMEDIATE` … commit). **현물 `repo.claim_exec_key` 는 쓰지 않는다** — FO 의 실행 키는 `fo_order_legs` 행 자체(UNIQUE `(position_id, intent, round, attempt)`). `fo_positions{…, status entering|held|resizing|closing|exit_failed|awaiting_settlement|settled|closed|unknown, qty_held, nav_at_open, realized_loss, fees, active_reduce_leg_id, version}`, `fo_order_legs{id, position_id, account_id, intent, round, attempt, status prepared|sending|accepted|unknown|rejected|cancelled|filled|expired, order_id|None, order_no|None, qty, filled_qty, sent_at}`.
  - **예약 트랜잭션** `store.reserve(conn, account, verdict)`: 한 `BEGIN IMMEDIATE` 안에서 계좌 총량(`max_positions`·`max_contracts`·수명 예산·로컬 예약) 을 **살아 있는 다리 포함**으로 다시 세고, 포지션 상태 전환(version+1), 다리 행 `prepared` 삽입 → commit. 실패면 아무것도 쓰지 않는다. 축소 다리는 `active_reduce_leg_id` 가 비어 있을 때만 만든다(RESIZE↓·CLOSE·손절이 같은 잔량을 공유).
  - **전송**: 잠금 밖에서 `prepared → sending(sent_at)` 커밋 → `place_order` → `accepted(order_no, orders 행 바인딩)` / `rejected`(키 자리 해제: 같은 attempt 재사용 불가, 다음 attempt) / `unknown`(응답 없음·5xx·비JSON). **재사용 정책**: `sending`·`unknown` 다리는 절대 재선점하지 않고 복구 틱이 `get_fills`·`get_order_state`(코드·날짜·방향·수량) 로 확정한다 — 확정 불가면 포지션 `unknown` + 신규 금지 + 알림(수동 대조). `sent_at` 만 있고 주문번호가 없는 다리를 "미접수" 로 추정하지 않는다.
  - 기록 실패는 `OrderBookkeepingError`(CRITICAL + 접수번호 알림).
- **청산·축소 절차(R1·R10·R15)**: ① 대기 재평가·`pending_entry` 폐기 ② **살아 있는 진입 다리 전부 취소 요청 → `get_order_state` 로 확정**(취소 중 추가 체결분은 `filled_qty` 로 반영) ③ 잔고 재조회로 `qty_held` 확정 ④ 축소 다리 1개(지정가) → 다음 틱 미체결이면 취소 → 확정 → **실제 잔량만** 시장가 `attempt+1` … `exit_retry_max` 까지 → 넘으면 `exit_failed` + `[파생 청산 실패]` critical(매 틱 재시도 계속, 신규 금지). unknown 다리가 하나라도 있으면 포지션을 `closed` 로 만들지 않는다. **진입 회수**: `enabled=false`·halted·킬스위치·cutoff·`verified` 상실 때도 살아 있는 진입 다리를 같은 ②로 회수한다(paused/off/dry_run 은 현물 정책대로 취소도 안 냄).
- **만기·정산(R10)**: D-`roll_days`(매도 +1) 09:00 강제 청산; 최종거래일 15:20 이후 또는 잔고에서 사라졌는데 체결이 없으면 `awaiting_settlement` → 다음 운영일 체결·잔고 대조로 `settled`(정산 손익 기록) — **잔고에서 사라졌다고 체결 청산으로 적지 않는다**. `hold_overnight=false` 일 때만 매일 마감 전 청산.
- **재시작 복구**: 첫 틱에 `fo_positions`/다리 vs 브로커 잔고·체결: 일치 → 승계; `UnverifiedHolding` → 신규 금지 + `[파생 잔고 출처 불명]` 알림, 자동 주문 없음; `fo adopt --position <id> --side LONG|SHORT --kind --qty N --stop-pt X` 는 브로커 행(코드·방향 이름·수량)과 **대조 일치할 때만** `FoPosition` 으로 승격(R9). 수량 불일치(원장 > 브로커) → 체결 조회로 차이 설명 안 되면 `unknown`.
- `executor.run_leg(account, intent)`(N1·N2): **먼저 진입/축소로 분기**. **축소 다리**: 전제는 확정 `contract_side`·확정 잔량(마지막 완주 잔고 또는 `get_order_state`)·활성 다리 상태뿐 — 체인·IV·지수·시세 노후는 막지 않는다(가격은 시장가 또는 마지막 호가, 없으면 시장가). **진입 다리**: `effective_mode` → 시세·잔고·주문가능 **재조회** → 그 값으로 `rules.evaluate`+`sizing` **전체 재계산**(이전 verdict 의 수량은 참고값일 뿐 재사용하지 않음; 재계산 수량 < 이전이면 축소, 0 이면 거부 + note "실행 직전 재계산 n→m") → `store.reserve`(재계산 결과만) → 전송 → 바인딩 → `_fo_risk_check_<acct>[_r<n>]`·`_fo_order_<acct>[_r<n>]` → 알림. 테스트: 시세 급변·NAV 감소·델타 증가·주문가능 감소 시 축소/거부, 체인 실패만으로 긴급 청산이 막히지 않음.
- `engine.tick`(`KR_fo_monitor`, 08:45~, 60초): ① 복구·동기화(다리 상태 확정, 잔고) ② 보호선 점검(R7·N3): 손절 조건 = **기초지수(`index_live` ∧ 신선)** `idx ≤/≥ stop_pt` **또는 선물 가격** `fut ≤/≥ stop_fut`(둘 중 하나로 발동; 08:45~09:00·15:30 이후·지수 노후 때는 선물 가격만), 옵션 프리미엄 손절은 **저장된 절대선만** 쓴다 — 매수 `premium ≤ premium_stop`, 매도 `premium ≥ premium_stop`(평균가·배수·비율은 §4.5 의 후보선 생성에만 쓰고 발동식에는 쓰지 않는다), tp, horizon, 만기, `hold_overnight=false` 마감 ③ **유지 조건**(R6·N4): 시나리오 스트레스·`add_margin`·수명 예산·**실제 노출 배수(§4.4)**·현금 버퍼를 매 틱 다시 계산, 15:00 에 확정 검사 — 초과면 진입 회수 뒤 축소 순서 **옵션 매도 → 선물 → 옵션 매수** 로 `RESIZE↓`(한도 안으로); 체결 뒤에도 같은 검사 ④ 일일 손실 → halted(신규 금지 + 진입 회수) ⑤ 트리거 ⑥ `pending_entry`(진입 창·게이트 재통과) ⑦ `fo_nav_snapshot`(포지션 없어도 매 틱 NAV·현금, R12) + `fo_positions_snapshot`.
- **트리거**(`triggers.check`): `move`(기초지수 수익률 ≥ 프로필 4/7/10%)·`take_profit` 근접·`invalidation`·`news`(novel ≥ 프로필)·`iv_spike` 는 코드·종류 운영일 1회, `periodic` 은 시간 버킷(60분, 하루 `periodic_max_per_day`). 공통: 코드당 `interval_min`, 하루 `max_per_day`(성공 세션만), 같은 틱 여러 트리거는 우선순위 invalidation > move > take_profit > news > iv_spike > periodic > expiry 하나로 묶음, `pending` 있으면 새로 넣지 않음. **expiry 의견(R16·3차 P1)**: `force_close_day = D-roll_days`(선물·옵션 매수) 또는 `D-(roll_days+1)`(옵션 매도) 로 먼저 정하고, 의견 트리거는 **`force_close_day` 의 직전 운영일 09:05 비차단**(CLOSE/HOLD). 강제 청산은 트리거와 무관. 기록은 `triggers(kind='fo:<종류>')` + 빠른 대기열 `fo_reevaluate{봉투}`.
- **늦은 재평가 CAS**: `reeval.apply` 는 `version == expected_version ∧ status == held` 일 때만, 적용 자체도 `store.reserve` 트랜잭션 안(계좌 총량 재검사 포함).
- 체결 동기화(`fills.sync_fills` fo 분기, R15·#21·3차 P1): `FoFill` 을 **원주문 계보(`orgn_odno`) 단위로 누적 행 교체**(현물 `fills.py:229` 와 같은 방식: 계보당 `fills` 행 1개를 `filled_qty`·`filled_amount` 누적값으로 덮어쓴다 — 차분 삽입 아님, 증분 평균가는 `Δ금액 ÷ Δ수량` 으로만 계산해 비용·실현손익에 쓴다, 4차 P1), `fo_codes.normalize` 로 다리 매칭(`order_no + code + side`, 같은 현지 날짜), 주문번호 없는 후보 = `sending|unknown` 다리(정확히 1건일 때만), 외부 체결은 `orders(asset='fo', mode='external')`. 만료는 **연속조회 완주 ∧ 파생 세션 종료 뒤**만. `legs.filled_qty`·`positions.qty_held`·`realized_loss`·`fees` 는 이 경로에서만 갱신.

### 6. 원장 (`ledger/db.py` v26 — 추가만)
`orders.asset`, `accounts.asset`, `fo_positions`(+`premium_stop`), `fo_order_legs`(+`accepted_at`), `fo_decisions{…, account_id, position_id, round, request_id, index_at_decision, quote_asof, matured_at, index_return, direction_hit, contract_pnl, cost, UNIQUE(date, account_id, position_id, round)}`, `fo_nav_snapshot{account_id, date, nav, cash, margin_total, add_margin}`, `fo_positions_snapshot`. **성과 어댑터(R12)**: `performance.nav_series(conn, account_id, market, *, asset="stock")` 가 `fo` 면 `fo_nav_snapshot`; 호출자(`performance.summary`·평균 NAV·`views`·readiness·daily_report) 는 `accounts.asset` 로 분기. `lots.*`·`memory.held_at`·`sector_weights` 는 fo 계좌에 빈 결과(ValueError 아님) + 테스트. 거래대금·비용 민감도(`performance.py:135`)는 fo 계좌에 **승수 × 계약 수 명목 + `fo.costs`(계약당 수수료 bp)** 를 쓰는 분기(3차 P2).

### 7. 데몬·CLI·웹
- 데몬: `KR_fo_session`(cron, heavy) → `fo.session.run`. `KR_fo_monitor`(FO 시간창 wrapper). 큐 `fo_session`(heavy)·`fo_reevaluate`(FAST, payload = 봉투)·`fo_close`(실행 직전 모드·dry_run·장 상태 재확인). `_auto_gate`·`TRADING_KINDS` asset 별. `ops/health`: `unknown`·`exit_failed`·`awaiting_settlement` 지연·만기 D-1 옵션 매도(critical). 텔레그램 `/fo`, `/run fo_session`.
- CLI: `fo verify --account paper-fo [--vehicle]`(조회 전용 + 보유 행 대조, 플래그 기록), `fo order-test --account paper-fo --vehicle FUTURES|OPTION_BUY|OPTION_SELL`(**거부 목록**: 1계약 진입 → 잔고 조회로 방향·kind·NAV·증거금 필드 검증 → 즉시 청산 → 체결 확정; 수단별 플래그를 연다, R18), `fo status | contracts | chain | session [--dry-run] | monitor --once [--dry-run] | adopt | close --confirm`(`close` 거부 목록). `kis ping`·`trader status` 파생 포함.
- 웹: `/fo` 페이지, API `/api/fo/today|positions|legs|scorecard|report`, 계좌 선택기 asset 묶음, '전체' 합산 제외, SSE 키 `fo`. UI 리뷰는 **`impeccable`**(critique·polish) + 저장소 DESIGN.md(R19).

### 8. 문서
스펙 `docs/superpowers/specs/2026-10-27-fo-lane-design.md`, 플랜 `docs/superpowers/plans/2026-10-27-phase26-fo.md`, `TODO.md` Phase 26, `architecture.md` 「선물옵션 방향성 레인」+「미확인」, `CLAUDE.md` 한 줄, runbook "파생 실전은 별도 Phase".

## 구현 단계 (브랜치 체인, 단계마다 Codex 리뷰)
`main` → `feat/phase26-1-fo-broker` → `-2-store-gate-exec` → `-3-session` → `-4-daemon-web` → `-5-docs-review`. 게이트: ruff · pytest · (4부터) web typecheck/vitest/build. `.codex/`·`AGENTS.md` 불변. TDD. 합성 응답만.

### 단계 1 — 계좌·브로커·마스터·설정·가격모형
**Files:** `config.py`, `config/{settings,schedule,accounts}.yaml`+`templates`, `.env.example`, `broker/{tr_ids,models,fo,fo_codes,fo_master,kis_market,fake}.py`, `fo/pricing.py`, `cli.py`(`_Boot` 3필드·`split_accounts`·`_build_brokers`·`kis ping`·`fo verify|contracts|chain|order-test`), `risk/ticks.py`, `market_hours.py`, `.claude/settings.json`
**Tests:** `test_config_fo.py`, `broker/test_fo_broker.py`(세 근거 일치만 `FoPosition`·아니면 `UnverifiedHolding`·NAV None·잔고 실패 예외·주문 body·야간 `UNSUPPORTED`·`NETWORK_UNKNOWN`·`get_order_state` 판정식/모순 응답 예외·`quote_asof` vs `received_at`·코드 정규화·`CONTRACT_VERSION`), `broker/test_fo_master.py`, `fo/test_pricing.py`(Black-76 콜/풋 패리티·만기 수렴), `risk/test_ticks.py`, `test_cli_boot.py`(세 목록 + `split_accounts`), `test_market_hours_fo.py`(08:45·15:20·`index_live`)

### 단계 2 — 저장 계층·상태기계·게이트·실행·원장
**Files:** `fo/{store,positions,rules,sizing,executor}.py`, `ledger/{db,repo,performance,lots}.py`, `orchestrator/{fills,reconcile,balance}.py`, `fo/balance.py`, `ops/health.py`, `notify/messages.py`, `agents/schemas.py`
**Tests:** `fo/test_store.py`(예약 트랜잭션: 두 신규가 같은 여유를 못 씀·축소 다리 단일·commit 없는 헬퍼·외부 호출 중 잠금 없음), `fo/test_rules.py`(대조 매개변수화·축소 다리 무관 조건·진입 거부 목록·과거 asof/노후 입력·단조: 손절·배수·approved_mult 모든 의도), `fo/test_sizing.py`(수명 예산: 부분 손절 뒤 증액 거부·재충전 없음·`premium_stop` 후보선·베이시스·시나리오 스트레스 최악 손실·버퍼 보장 `n ≤ floor(q×(1−buffer))` 와 전제 검사(상계·`tot≠ord`·q=0·C None → 거부)·`margin_rate` 정합성·로컬 예약 = 주문번호 없는 다리 + accepted 미체결 전부·분모 0 거부), `fo/test_positions.py`(상태 전이 전체·CAS·복구: 일치/미확정/수량 불일치/sending 다리·adopt 대조), `fo/test_executor.py`(sending 뒤 죽음 → 재선점 없음·rejected 는 다음 attempt·unknown 보존·진입 회수 → 확정 → 잔량 청산·취소 중 추가 체결·exit_retry_max → exit_failed·awaiting_settlement·off/paused/dry_run 무취소·**진입은 실행 직전 전체 재계산(급변·NAV 감소·델타 증가·주문가능 감소 → 축소/거부)·축소는 체인/지수 실패에도 진행**·`premium_stop` 단조(매도 min·매수 max)·`accepted` 예약은 잔고 반영 확인 뒤 제외·`margin_rate` 노후 거부·`regime_cap` 0/null 구분), `ledger/test_db_migrations.py`, `ledger/test_performance_fo.py`(어댑터·`lots` 빈 결과), `orchestrator/test_fills_fo.py`(정규화·orgn_odno 계보·연속조회 실패 시 만료 없음·asset 전파), `orchestrator/test_balance_dispatch.py`(asset 별 브로커 호출 스파이)

### 단계 3 — 세션·도구·에이전트
**Files:** `.claude/agents/{fo-trader,orchestrator}.md`, `agents/{registry,fake,schemas}.py`, `orchestrator/{session,tools}.py`, `usage.py`, `fo/{inputs,session}.py`, `cli.py`
**Tests:** `agents/test_registry.py`, `orchestrator/test_tools_fo.py`(쓰기 허용·전제·봉투 불일치 거부·`expected_version`·노후 입력은 기록만), `fo/test_inputs.py`(asof·`received_at`·과거 asof 강제 dry-run), `orchestrator/test_session_fo.py`(kind → `usage.RESUME_KINDS` → 상한 예약 연결·FAKE)

### 단계 4 — 데몬·모니터·재평가·웹
**Files:** `daemon.py`(`DaemonDeps` 3목록·잡), `jobs.py`, `fo/{engine,triggers,reeval}.py`, `notify/commands.py`, `web/app.py`(`WebDeps` 3목록), `web/routers/{fo,accounts,sse,_common}.py`, `ledger/views.py`, `web/src/{nav,glossary,types,api}.ts`·`pages/Fo.tsx`+vitest, `status.py`
**Tests:** `fo/test_engine.py`(보호선 이중 조건·지수 노후 시간대·**옵션 손절은 저장 `premium_stop` 으로만 발동(평균가 변동 뒤 비율식과 달라지는 사례)**·유지 조건 축소 순서·15:00 확정·진입 회수 경로 5종·만기/정산·`hold_overnight` 두 값), `fo/test_triggers.py`(expiry D-(roll+1) 비차단·버킷·pending 억제), `fo/test_reeval.py`(CAS·예약 안 적용), `test_daemon_fo.py`(소비자별 계좌 전달 스파이·FO 시간창·`_auto_gate`), `web/test_fo_api.py`·`test_accounts_asset.py`, `Fo.test.tsx`, **E2E `fo/test_lifecycle.py`**: 4 옵션 조합 + 선물 양방향 × {진입→부분체결→RESIZE↑/↓→손절(진입 회수 포함)→취소 중 체결→unknown→재시작→정산}, 현물 회귀 스냅샷(asset 기본값 경로의 executor/monitor 출력 불변)

### 단계 5 — 문서·전체 교차 리뷰 → 체인 ff 머지(사용자 확인 항목 외 자율)

## Codex 교차 리뷰 절차
- **A. 단계마다**(리뷰 전 커밋해 head 를 고정하고 base/head SHA 를 결과 파일에 적는다, 3차 P2): ① `codex review --base <직전 브랜치>` ② `codex exec --sandbox read-only --ephemeral -o <결과>` 에 base/head SHA 명시 초점 프롬프트(방향·수단 부호, 배수·손절 단조, 옵션 매도 스트레스, 예약 트랜잭션·다리 상태·unknown, 진입 회수, 모의 야간 TR 없음, **stock 기본 동작 회귀 없음**(허용 변경 영역 목록 + 회귀 스냅샷)) ③ Claude `/code-review` 같은 SHA. 결과는 `docs/reviews/2026-10-27-phase26-fo-review.md`. P0·P1 은 PR 전 수정 + 테스트.
- **B. 단계 5 전체**: `codex exec` 읽기 전용 2회 + Claude 2명 → 표 → 수정 → 재리뷰 1회. 계좌번호·키 금지. `.codex/` 불변.

## 검증·실기 순서(단계적, 확정 전 수단은 계속 거부)
1. 단계 게이트 + `selfcheck`.
2. `FAKE_CLAUDE=1 uv run trader fo session --dry-run` / `fo monitor --once --dry-run` + 가짜 포지션 / `daemon --dry-run` → 체크포인트·트리거·`/fo`·합산 제외.
3. **실기 1(조회 전용, 사용자 확인 대기)**: `uv run trader fo verify --account paper-fo` → 시세·마스터·주문가능·잔고(빈 잔고면 방향·NAV 는 미검증으로 남김) → 비식별 fixture.
4. **실기 2(사용자 실행, 수단별 1계약 왕복)**: `fo order-test --vehicle FUTURES` → `OPTION_BUY` → `OPTION_SELL` 순서. 각각 진입 → 잔고 행으로 방향·kind·승수·NAV·증거금 필드 검증 → 즉시 청산 → 체결·취소 확정 로그 → 그 수단의 `fo_verified` 기록. 실패한 수단은 닫힌 채.
5. **실기 3(사용자 확인 대기, 약 $1~2)**: `fo.enabled: true` 뒤 세션 1회 → 결정·배수·진입·재평가·청산 로그.

## 범위 조정
| 제외 | 이유 | 재개 |
|---|---|---|
| 해외선물옵션 | 모의 미지원 | 실전 뒤 |
| 야간 세션·야간 손절 | 모의 TR 없음(스윙은 시나리오 스트레스 유지 조건으로만) | 실전 전환 Phase |
| 실전 파생 계좌 | paper 전용 게이트·승인 모드·야간 미구현 | 별도 Phase |
| 위클리·스프레드·합성 | 결정 1개 = 단일 수단 1 포지션 | Phase 27 |
| 선물 숏으로 현물 헤지 대체 | 계좌 분리, 경고만 | 적중률 뒤 |
| 메모리·reflector 연동 | `fo_decisions` 채점 | 20건 뒤 |
| 체결통보 `H0IFCNI0`·실시간 파생 호가 | `fills_sync`·REST | Phase 27 |
| 백테스트·ML/RL 사이징 | 신호가 LLM | — |

## 미확인(실기에서 확정, 확정 전 해당 수단 신규 거부)
잔고 방향·`prdt_type_cd` 값 · `prsm_dpast_amt` · 체결 `pdno` ↔ `SHTN_PDNO` 대응 · 마스터 분류값·미니 접두 · 미니옵션 승수 5만 · 모의 파생 계좌의 공용 모의 앱키 · 옵션 매도 `ord_psbl_qty` 의 증거금 반영 · 옵션 `bid/ask` 필드명 · `margin_rate` 보수성(실기 2 의 `mgna_tota` 변화로 상향 조정).
