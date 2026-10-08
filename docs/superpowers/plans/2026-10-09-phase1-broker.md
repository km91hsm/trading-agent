# Phase 1 브로커 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** KIS Open API 로 모의·실전 계좌의 잔고와 국내·미국 시세·일봉·휴장일을 조회하고 텔레그램으로 알림을 보내는 브로커 계층을 만든다(`uv run trader kis ping`, `uv run trader notify-test`).

**Architecture:** 세 겹 — `KisAuth(env)`(토큰 캐시) → `KisClient(env, auth)`(HTTP·레이트리밋·오류 통일) → `KisMarket(client)`(시세, 실전 env 만) / `KisBroker(account, client)`(계좌별 잔고). `tr_ids.py` 가 실전/모의·국내/해외 tr_id 의 유일한 출처. 테스트는 `respx` 로 HTTP 를 막고 `tests/broker/fixtures/*.json` 응답을 재생한다. 텔레그램은 토큰이 없으면 `NullNotifier`.

**Tech Stack:** httpx, respx(dev), pydantic. 금액은 `Decimal`.

**Spec:** `docs/superpowers/specs/2026-10-09-trading-agent-design.md` — §0.3 KIS 제약, §2 브로커, §6.4 텔레그램, §10.2 크로스플랫폼, §11 Phase 1, §13 검증.

## Global Constraints

- Phase 0 규칙 전부(경로·UTC·pydantic forbid·한국어·`uv run`).
- KIS 실패는 `KisApiError(msg_cd, msg1, tr_id)` 로 통일. KIS 응답이 아닌 실패는 합성 코드 `AUTH/NETWORK/HTTP<status>/INVALID_JSON`.
- 앱키·토큰은 URL·로그·예외 메시지에 넣지 않는다. 새 httpx 클라이언트는 `httpx/httpcore` 로거 WARNING 유지.
- 토큰 캐시 `state/kis_token_<env>.json`(만료 1시간 전 갱신, 발급 실패 후 60초 재발급 금지). 발급은 1분 1회이므로 테스트 외에는 캐시 우선.
- 레이트리밋: 실전 20/s, 모의 2/s 토큰 버킷. `EGW00201` 은 0.5초 후 1회 재시도.
- `KisMarket` 은 `client.env == "real"` 만 허용(모의 서버 시세 미지원). `KisBroker` 는 `client.env == account.env` 를 요구.
- `Candle`·`InvestorFlow` 목록은 **항상 날짜 오름차순**(마지막이 최신). 일봉은 100건/페이지 페이징, 상한 10페이지.
- 실제 네트워크 테스트 없음. `kis ping`·`notify-test` 실기는 사용자 키로 Task 8 에서 1회.
- 도메인: 실전 `https://openapi.koreainvestment.com:9443`, 모의 `https://openapivts.koreainvestment.com:29443`.

## 범위 조정

| 항목 | 이유 | 재개 Phase |
|---|---|---|
| 주문·취소·체결 조회 | 리스크 게이트·executor 와 함께. `tr_ids` 에 주문 ID 는 미리 등록 | 4 |
| 텔레그램 수신(명령 처리) | 승인 흐름과 함께 | 4 |
| 분봉·호가·투자자동향·지수 | 수집 계층이 쓰는 시점 | 2 |
| WebSocket 실시간 | monitor 는 REST 60초 폴링으로 시작 | 6 이후 필요 시 |

## Review Focus

1. 토큰 캐시 파일이 깨져 있거나(JSON 아님) 만료 필드가 없으면 재발급으로 복구해야 한다 → Task 2 `test_corrupt_cache_refetches`.
2. 발급 직후 60초 안에 다시 실패하면 KIS 에 재요청하지 말고 `KisApiError("AUTH")` 로 끝나야 한다 → Task 2 `test_backoff_after_failed_issue`.
3. KIS 가 HTTP 200 으로 `rt_cd != "0"` 을 주는 경우와 HTTP 500 을 주는 경우 모두 `KisApiError` 여야 하고, 메시지에 앱키가 없어야 한다 → Task 3 `test_business_error_and_http_error`.
4. 일봉 응답이 최신→과거 순으로 오므로 뒤집어 저장해야 하며, 2페이지 이상일 때 중복 날짜가 없어야 한다 → Task 5 `test_daily_paging_ascending_unique`.
5. 텔레그램 메시지가 4,000자를 넘으면 분할 전송되고 HTML 특수문자가 이스케이프돼야 한다 → Task 7 `test_split_and_escape`.

---

## 파일 구조

```
pyproject.toml                         httpx 추가, respx(dev)
src/trader/broker/__init__.py  models.py  tr_ids.py  kis_auth.py  kis_client.py  kis_market.py  accounts.py  fake.py
src/trader/notify/__init__.py  telegram.py
src/trader/cli.py                      kis ping, notify-test, _build_market/_build_brokers/make_notifier
tests/broker/conftest.py  fixtures/*.json  test_models.py  test_tr_ids.py  test_kis_auth.py  test_kis_client.py  test_kis_market.py  test_accounts.py  test_fake.py
tests/notify/test_telegram.py  tests/test_cli_kis.py
```

---

### Task 1: 도메인 모델과 tr_id 표

**Files:** Create `src/trader/broker/{__init__,models,tr_ids}.py`, `tests/broker/{__init__,test_models,test_tr_ids}.py`; Modify `pyproject.toml`(httpx, respx)

**Interfaces:**
- `models.py`(pydantic forbid): `Market = Literal["KR","US"]`, `Quote{ticker, market, price: Decimal, change_pct: float, volume: int, asof: datetime}`, `Candle{date, open, high, low, close: Decimal, volume: int, time: str|None=None}`, `Position{ticker, market, qty: int, avg_price: Decimal, current_price: Decimal, eval_amount: Decimal, pnl_pct: float}`, `Cash{currency: Literal["KRW","USD"], available: Decimal, total: Decimal}`, `InvestorFlow{date, foreign_net: int, institution_net: int, individual_net: int}`, `OrderRequest{ticker, market, side: BUY|SELL, qty: int, order_type: LIMIT|MARKET, price: Decimal|None, exchange: str|None}`, `OrderResult{order_no, accepted: bool, message}`.
- `tr_ids.py`: `BASE_URL = {"real": ..., "paper": ...}`, `TR = {이름: {"real": id, "paper": id|None}}` + `tr_id(name, env) -> str`(모의 미지원이면 `KisApiError("UNSUPPORTED")`). 이름: `TOKEN`(경로만), `DOM_PRICE, DOM_DAILY, DOM_MINUTE, DOM_ORDERBOOK, DOM_INVESTOR, DOM_INDEX, DOM_BALANCE, DOM_PSBL_ORDER, DOM_ORDER_BUY, DOM_ORDER_SELL, DOM_ORDER_CANCEL, OVS_PRICE, OVS_DAILY, OVS_MINUTE, OVS_ORDERBOOK, OVS_BALANCE, OVS_PSBL_ORDER, OVS_ORDER_BUY_US, OVS_ORDER_SELL_US, HOLIDAY`. 값은 공식 `open-trading-api` `examples_llm/` 에서 2026-10-09 확인:

| 이름 | 경로 | 실전 | 모의 | 비고 |
|---|---|---|---|---|
| TOKEN | `/oauth2/tokenP` | — | — | POST, `grant_type=client_credentials` |
| DOM_PRICE | `/uapi/domestic-stock/v1/quotations/inquire-price` | FHKST01010100 | 동일 | `FID_COND_MRKT_DIV_CODE=J, FID_INPUT_ISCD` |
| DOM_DAILY | `/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice` | FHKST03010100 | 동일 | `FID_INPUT_DATE_1/2, FID_PERIOD_DIV_CODE=D, FID_ORG_ADJ_PRC=0`, 100건/호출, `output2` |
| DOM_BALANCE | `/uapi/domestic-stock/v1/trading/inquire-balance` | TTTC8434R | VTTC8434R | |
| DOM_PSBL_ORDER | `/uapi/domestic-stock/v1/trading/inquire-psbl-order` | TTTC8908R | VTTC8908R | |
| DOM_ORDER_BUY / SELL | `/uapi/domestic-stock/v1/trading/order-cash` | TTTC0012U / TTTC0011U | VTTC0012U / VTTC0011U | Phase 4 |
| HOLIDAY | `/uapi/domestic-stock/v1/quotations/chk-holiday` | CTCA0903R | 미지원 | `BASS_DT`, 응답 `opnd_yn` |
| OVS_PRICE | `/uapi/overseas-price/v1/quotations/price` | HHDFS00000300 | 동일 | `AUTH="", EXCD=NAS|NYS|AMS, SYMB` |
| OVS_DAILY | `/uapi/overseas-price/v1/quotations/dailyprice` | HHDFS76240000 | 동일 | `GUBN=0, BYMD, MODP=1`, `output2` |
| OVS_BALANCE | `/uapi/overseas-stock/v1/trading/inquire-balance` | TTTS3012R | VTTS3012R | `OVRS_EXCG_CD=NASD, TR_CRCY_CD=USD` |
| OVS_PSBL_ORDER | `/uapi/overseas-stock/v1/trading/inquire-psamount` | TTTS3007R | VTTS3007R | |
| OVS_ORDER_BUY_US / SELL_US | `/uapi/overseas-stock/v1/trading/order` | TTTT1002U / TTTT1006U | VTTT1002U / VTTT1006U | Phase 4, 모의는 지정가만 |

분봉·호가·투자자동향·지수(`DOM_MINUTE, DOM_ORDERBOOK, DOM_INVESTOR, DOM_INDEX, OVS_MINUTE, OVS_ORDERBOOK`)는 Phase 2 에서 같은 방식으로 확인해 추가한다.

- [ ] Step 1: `test_models_decimal_roundtrip`, `test_tr_id_real_and_paper`, `test_tr_id_unsupported_on_paper`(`DOM_INVESTOR` paper → KisApiError), `test_order_ids_registered`.
- [ ] Step 2~4. Step 5: Commit `feat(broker): 도메인 모델과 KIS tr_id 표`

### Task 2: `KisAuth` 토큰 캐시

**Files:** Create `src/trader/broker/kis_auth.py`, `tests/broker/test_kis_auth.py`

**Interfaces:** `KisAuth(env, app_key, app_secret, *, http: httpx.Client|None=None, state: Path|None=None, clock: Callable[[], datetime]=...)`; `token() -> str`(캐시 유효하면 재사용, 만료 1시간 전부터 재발급, 실패 후 60초 재시도 금지 → `KisApiError("AUTH", ...)`), `headers() -> dict`(authorization·appkey·appsecret). 캐시 파일 `state/kis_token_<env>.json {access_token, expires_at(UTC ISO)}`.

- [ ] Step 1: `test_issues_and_caches`, `test_reuses_valid_cache`, `test_refreshes_near_expiry`, `test_corrupt_cache_refetches`, `test_backoff_after_failed_issue`, `test_secret_not_in_error_message`.
- [ ] Step 2~4. Step 5: Commit `feat(broker): KIS 토큰 발급·캐시`

### Task 3: `KisClient` HTTP·레이트리밋·오류 통일

**Files:** Create `src/trader/broker/kis_client.py`, `tests/broker/test_kis_client.py`, `tests/broker/conftest.py`

**Interfaces:** `KisApiError(Exception){msg_cd, msg1, tr_id}`; `RateLimiter(rate_per_s, *, clock, sleep)`; `KisClient(env, auth, *, http=None, limiter=None)`: `get(path, tr_name, params) -> dict`, `post(path, tr_name, body) -> dict`. 공통 헤더(`content-type, authorization, appkey, appsecret, tr_id, custtype=P`). `rt_cd != "0"` → `KisApiError(msg_cd, msg1)`, HTTP 오류 → `HTTP<status>`, 네트워크 → `NETWORK`, JSON 아님 → `INVALID_JSON`, `EGW00201` 은 0.5초 후 1회 재시도. 픽스처: `paper_client/real_client`(레이트리밋 해제, `respx_mock`).

- [ ] Step 1: `test_headers_include_tr_id`, `test_business_error_and_http_error`, `test_rate_limit_spacing`(clock·sleep 주입으로 20/s 간격), `test_egw00201_retry_once`, `test_invalid_json`.
- [ ] Step 2~4. Step 5: Commit `feat(broker): KisClient 레이트리밋·오류 통일`

### Task 4: `accounts.py` 와 `KisBroker` 잔고

**Files:** Create `src/trader/broker/accounts.py`, `tests/broker/test_accounts.py`, 픽스처 `balance_kr.json`, `balance_us.json`

**Interfaces:** `KisBroker(account: AccountConfig, client: KisClient)`(env 불일치 → `ValueError`); `get_positions(market) -> list[Position]`, `get_cash(market) -> Cash`. US 는 `OVRS_EXCG_CD=NASD`, `TR_CRCY_CD=USD`(NYS/AMS 보유분 포함 여부는 실기에서 확인해 TODO 에 기록). 주문 메서드는 `NotImplementedError("Phase 4")`.

- [ ] Step 1: `test_env_mismatch_rejected`, `test_positions_kr_parsed`, `test_cash_us_parsed`, `test_order_not_implemented`.
- [ ] Step 2~4. Step 5: Commit `feat(broker): 계좌별 잔고·예수금 조회`

### Task 5: `KisMarket` 시세·일봉·휴장일

**Files:** Create `src/trader/broker/kis_market.py`, `tests/broker/test_kis_market.py`, 픽스처 `price_kr.json, daily_kr_p1.json, daily_kr_p2.json, price_us.json, daily_us.json, holiday.json`

**Interfaces:** `KisMarket(client)`(paper → `ValueError`); `get_price_kr(ticker) -> Quote`, `get_price_us(symbol, exchange) -> Quote`, `get_daily_kr(ticker, days=250) -> list[Candle]`(페이징, 오름차순, 중복 제거, 상한 1000봉), `get_daily_us(symbol, exchange, days)`, `is_holiday(date) -> bool`(`CTCA0903R`, `state/cache/holiday_<yyyymm>.json` 7일 캐시). `Quote.asof` 는 조회 시각(UTC).

- [ ] Step 1: `test_paper_env_rejected`, `test_price_kr`, `test_daily_paging_ascending_unique`, `test_daily_us`, `test_holiday_cached`.
- [ ] Step 2~4. Step 5: Commit `feat(broker): 시세·일봉·휴장일 조회`

### Task 6: `FakeBroker`/`FakeMarket`

**Files:** Create `src/trader/broker/fake.py`, `tests/broker/test_fake.py`

**Interfaces:** `FakeMarket(prices: dict[str, Decimal], candles: dict[str, list[Candle]])` 과 `FakeBroker(account, positions, cash)` — `KisMarket`/`KisBroker` 와 같은 메서드, 호출 기록 `calls: list[str]`, `fail: set[str]` 으로 특정 메서드 실패 주입. 뒤 Phase 테스트·dry-run 이 재사용.

- [ ] Step 1: `test_fake_market_same_interface`, `test_fake_fail_injection`. Step 2~4. Step 5: Commit `feat(broker): FakeBroker·FakeMarket`

### Task 7: 텔레그램 발신

**Files:** Create `src/trader/notify/{__init__,telegram}.py`, `tests/notify/test_telegram.py`

**Interfaces:** `Notifier` 프로토콜 `send(text, *, html=True) -> bool`; `TelegramNotifier(token, chat_id, *, http=None)`(4,000자 분할, HTML 이스케이프 기본, 실패는 False + 로그, 토큰은 URL 에 들어가므로 로그·예외에 URL 금지); `NullNotifier`(항상 True); `make_notifier(secrets) -> Notifier`.

- [ ] Step 1: `test_split_and_escape`, `test_null_when_no_token`, `test_failure_returns_false_without_token_in_log`(caplog 에 토큰 없음).
- [ ] Step 2~4. Step 5: Commit `feat(notify): 텔레그램 발신`

### Task 8: CLI `kis ping`·`notify-test`, 실기 검증, 마무리

**Files:** Modify `src/trader/cli.py`, `docs/TODO.md`, `CLAUDE.md`; Create `tests/test_cli_kis.py`

**Interfaces:** `cli._build_market(secrets) -> KisMarket`, `cli._build_brokers(secrets, accounts) -> list[KisBroker]`, `cli.make_notifier` 재노출(테스트에서 monkeypatch). `trader kis ping [--account ID]`: 휴장일·005930 현재가·AAPL 현재가·계좌별 KR/US 현금 조회, 항목별 OK/실패 출력, 실패 > 0 이면 exit 1. `trader notify-test`: 텔레그램 1건.

- [ ] Step 1: `test_kis_ping_reports_failures`(Fake 주입, 1건 실패 → exit 1), `test_notify_test_null`. Step 2~4.
- [ ] Step 5: 실기(사용자 키, 보관본 `secrets/.env` 복원 필요): `uv run trader kis ping` 실패 0건, `uv run trader notify-test` 수신 확인. 키가 없으면 "사용자 확인 대기".
- [ ] Step 6: TODO Phase 1 체크, CLAUDE.md 브로커 규칙 추가, `uv run trader status`, 커밋 `docs: Phase 1 브로커 완료`, PR → `feat/phase0-skeleton`.

## Self-Review 메모
- §2 전부(Auth·Client·Market·Broker·tr_ids·accounts·Fake)와 §6.4 발신이 Task 1~7. 주문·수신은 범위 조정.
- `KisApiError` 는 Task 3 에서 정의하되 Task 1 `tr_id()` 가 쓰므로 `tr_ids.py` 가 `kis_client` 를 import 하면 순환 → `KisApiError` 는 `broker/errors.py` 에 두고 양쪽이 import 한다(파일 구조에 추가).
