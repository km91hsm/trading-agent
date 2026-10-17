# 분봉 차트 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 웹 차트 페이지에서 일봉 대신 당일 1분봉을 볼 수 있게 한다(KIS 실전 시세, 주문·트리거 마커를 분 단위에 찍음).

**Architecture:** 세 층을 그대로 확장한다. `KisMarket.get_minute_kr` 가 KIS 의 30건 제한을 역방향 페이징으로 넘어 `count` 봉을 모으고, 새 라우트 `GET /api/charts/minute` 가 봉을 **UTC epoch 초**(lightweight-charts 의 분봉 시간 형식)로 바꿔 60초 캐시와 함께 돌려주며 같은 날의 주문·트리거를 분 단위 마커로 붙인다. 프런트는 `Chart.tsx` 에 간격 선택(일봉 | 1분)을 더해 같은 캔들 차트·SMA 코드를 쓴다. 결정 마커는 시각이 없으므로 분봉에는 찍지 않는다.

**Tech Stack:** 기존 그대로(FastAPI, httpx+respx, React 19 + lightweight-charts v5, Vitest).

**Spec:** `docs/superpowers/specs/2026-10-09-trading-agent-design.md` §2(KisMarket 분봉은 실전 env 만), §8(차트: Lightweight Charts, 결정·체결·트리거 마커, 지표 오버레이). 출처는 `docs/TODO.md` Phase 5 "이연 유지: 분봉 차트(사용자 결정)" 이며 사용자가 2026-10-17 진행을 결정했다.

## Global Constraints

- CLAUDE.md 코딩 규칙 전부(UTC 저장·표시만 현지, `extra="forbid"`, 테스트 `tmp_path`·`respx`, 한국어, ruff 0건, 셸 스크립트 금지).
- 조회 API 는 토큰 없음, 계좌 필터는 `account_id` 가 모르는 계좌면 400(§8). 웹이 브로커 주문·잔고를 직접 부르지 않는다(시세 클라이언트만).
- KIS 실전 한도 20/s: KR 분봉 페이징은 한 요청당 최대 13회(390봉) 이하이며 모의 서버는 시세를 주지 않으므로 `deps.market` 이 없으면 503.
- 기존 `get_minute_kr(ticker)` 호출부(`sources/inputs.py` 의 `technical.minute`)는 동작이 바뀌지 않는다(기본 `count=30` 은 1회 호출).

## Review Focus

1. KIS 가 같은 30봉을 다시 돌려줘도 페이징이 멈춘다(무한 루프 없음) → Task 1 `test_minute_kr_pages_backward_and_stops`.
2. 장 시작 전·휴장일처럼 분봉이 비어도 200 과 빈 목록이다(500 아님) → Task 2 `test_minute_chart_empty_candles_ok`.
3. US 분봉의 `xhms` 는 거래소 현지(뉴욕) 시각이라 epoch 변환에 `America/New_York`(DST) 을 쓴다 → Task 2 `test_minute_chart_us_epoch_uses_new_york`.
4. 마커는 분봉의 마지막 날짜(시장 현지)와 같은 날 주문·트리거만, 분 단위 내림 epoch 로 붙고 다른 날 주문은 제외된다 → Task 2 `test_minute_chart_markers_same_day_only`.
5. `count` 범위 밖(0, 391)은 422, 모르는 계좌는 400, US 는 exchange 필수 400 → Task 2 `test_minute_chart_bounds`.

## 범위 조정

| 제외 | 이유 | 재개 |
|---|---|---|
| US 분봉 120봉 초과 페이징(`NEXT`/`KEYB`) | KIS 해외 분봉 `NREC` 상한 120, 연속 조회 키 의미가 문서에 불명확해 실기 확인 필요 | 사용자 확인 대기(실기) |
| 분봉 간격 선택(3·5·15분) | KIS KR 분봉은 1분만 주고 집계는 프런트 몫 → 수요 확인 뒤 | 사용자 결정 |
| 분봉 자동 갱신(SSE·폴링) | 장중 60초 캐시 + 조회 버튼으로 충분, KIS 호출 폭주 방지 | 사용자 결정 |
| 분봉에 결정 마커 | `decisions` 는 날짜만 있어 분 단위 위치가 없음 | 변경 없음 |

---

### Task 1: KR 분봉 역방향 페이징 + FakeMarket 분봉

**Files:**
- Modify: `src/trader/broker/kis_market.py` (`get_minute_kr`)
- Modify: `src/trader/broker/fake.py` (`FakeMarket`)
- Test: `tests/broker/test_kis_market.py`

**Interfaces:**
- Consumes: `tr_ids.path("DOM_MINUTE")`, `Candle(time="HHMMSS")`.
- Produces: `KisMarket.get_minute_kr(ticker: str, *, count: int = 30) -> list[Candle]` — 시간 오름차순, `(date, time)` 중복 제거, 최대 `count` 봉(최신 쪽 우선). 페이징: 첫 호출 `FID_INPUT_HOUR_1 = 지금(KST) HHMMSS`, 다음 호출은 직전 응답의 가장 이른 봉 시각 − 1분. 중단 조건: 모은 봉 ≥ count, 응답 봉이 30 미만, 새 봉이 하나도 없음, 가장 이른 시각 ≤ `090000`. `KisMarket.MINUTE_PAGE = 30`.
- Produces: `FakeMarket(minutes: dict[str, list[Candle]] | None = None)`, `get_minute_kr(ticker, *, count=30)`·`get_minute_us(symbol, exchange, *, count=120)` 가 `calls` 에 기록하고 `minutes[ticker][-count:]` 를 돌려준다.

- [ ] **Step 1: 실패 테스트 작성** (`tests/broker/test_kis_market.py` 끝에)

```python
def _minute_payload(times: list[str], d: str = "20261008") -> dict:
    rows = [
        {"stck_bsop_date": d, "stck_cntg_hour": t, "stck_prpr": "71500", "stck_oprc": "71400",
         "stck_hgpr": "71600", "stck_lwpr": "71300", "cntg_vol": "100", "acml_tr_pbmn": "1"}
        for t in times
    ]
    return {"rt_cd": "0", "msg_cd": "MCA00000", "msg1": "ok", "output1": {}, "output2": rows}


def _hhmm_range(end: str, n: int) -> list[str]:
    """end(HHMMSS) 부터 1분씩 내려가며 n 개 (KIS 처럼 내림차순)."""
    from datetime import datetime, timedelta

    t = datetime.strptime(end, "%H%M%S")
    return [(t - timedelta(minutes=i)).strftime("%H%M%S") for i in range(n)]


def test_minute_kr_pages_backward_and_stops(market, respx_mock):
    import httpx

    first = _minute_payload(_hhmm_range("153000", 30))  # 153000..150100
    second = _minute_payload(_hhmm_range("150000", 30))  # 150000..143100
    route = respx_mock.get(REAL + DOM_MINUTE).mock(
        side_effect=[httpx.Response(200, json=first), httpx.Response(200, json=second),
                     httpx.Response(200, json=second)]  # 세 번째는 같은 봉 반복 → 멈춰야 한다
    )
    candles = market.get_minute_kr("005930", count=70)
    assert len(route.calls) == 3
    assert route.calls[1].request.url.params["FID_INPUT_HOUR_1"] == "150000"
    assert route.calls[2].request.url.params["FID_INPUT_HOUR_1"] == "143000"
    assert len(candles) == 60 and candles[0].time == "143100" and candles[-1].time == "153000"
    assert len({c.time for c in candles}) == 60


def test_minute_kr_stops_at_open_and_short_page(market, respx_mock):
    route = respx_mock.get(REAL + DOM_MINUTE).mock(
        return_value=httpx_resp(_minute_payload(_hhmm_range("092000", 21)))  # 092000..090000, 21 < 30
    )
    candles = market.get_minute_kr("005930", count=390)
    assert len(route.calls) == 1 and len(candles) == 21 and candles[0].time == "090000"


def test_minute_kr_default_count_single_call(market, respx_mock):
    route = respx_mock.get(REAL + DOM_MINUTE).mock(return_value=resp("minute_kr.json"))
    assert len(market.get_minute_kr("005930")) == 3 and len(route.calls) == 1


def test_fake_market_minutes():
    from decimal import Decimal

    from trader.broker.fake import FakeMarket
    from trader.broker.models import Candle

    c = [Candle(date=date(2026, 10, 8), open=Decimal(1), high=Decimal(1), low=Decimal(1),
                close=Decimal(1), volume=1, time=f"09{i:02d}00") for i in range(5)]
    fm = FakeMarket(minutes={"005930": c})
    assert [x.time for x in fm.get_minute_kr("005930", count=2)] == ["090300", "090400"]
    assert fm.get_minute_us("AAPL", "NAS") == [] and fm.calls == ["get_minute_kr", "get_minute_us"]
```

`httpx_resp` 는 파일 상단에 `def httpx_resp(payload): return httpx.Response(200, json=payload)` 로 두고 `import httpx` 를 추가한다(`resp()` 헬퍼가 이미 httpx 를 쓰면 그 방식에 맞춘다).

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/broker/test_kis_market.py -q -k "minute or fake_market"`
Expected: FAIL — `get_minute_kr() got an unexpected keyword argument 'count'`, `FakeMarket.__init__() got an unexpected keyword argument 'minutes'`.

- [ ] **Step 3: 구현**

`kis_market.py`: `MINUTE_PAGE = 30` 클래스 상수. `get_minute_kr(self, ticker, *, count=30)` 는 `hour = now_kst.strftime("%H%M%S")` 로 시작해 루프: 요청 → 행 파싱 → `seen: set[tuple[date, str]]` 로 새 봉만 누적 → 중단 조건 4개 검사 → `hour = (datetime.combine(date.today(), earliest_time) - timedelta(minutes=1)).strftime("%H%M%S")`. 반환은 `sorted(...)[-count:]`. 기존 docstring 을 "KIS 는 기준 시각부터 과거 30건이라 count 만큼 역방향 페이징" 으로 바꾼다.
`fake.py`: `minutes` 인자·속성 추가, 두 메서드 추가(`_record` 사용).

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/broker/test_kis_market.py tests/sources tests/test_cli_collect.py -q`
Expected: 전부 PASS (기존 `test_minute_kr_ascending_with_time` 포함).

- [ ] **Step 5: 커밋**

```bash
git add src/trader/broker/kis_market.py src/trader/broker/fake.py tests/broker/test_kis_market.py
git commit -m "feat(broker): KR 분봉 count 역방향 페이징 · FakeMarket 분봉"
```

---

### Task 2: `GET /api/charts/minute`

**Files:**
- Modify: `src/trader/web/routers/charts.py`
- Modify: `tests/helpers_web.py` (`make_deps` 의 FakeMarket 에 `minutes`)
- Test: `tests/web/test_runs_charts.py`

**Interfaces:**
- Consumes: Task 1 의 `get_minute_kr(ticker, count=)`, `get_minute_us(symbol, exchange, count=)`, `FakeMarket(minutes=)`.
- Produces: `GET /api/charts/minute?ticker&market&exchange&count=120(ge=1,le=390)&account_id` → `{"ticker", "market", "interval": "1m", "tz": "Asia/Seoul"|"America/New_York", "session_date": "YYYY-MM-DD"|None, "candles": [{"time": <int epoch 초>, "open","high","low","close": str, "volume": int}], "markers": [{"time": <int>, "kind": "order"|"trigger", "side": str, "text": str}]}`. 모듈 상수 `MINUTE_CACHE_TTL_S = 60.0`, 캐시 `_minute_cache` 키 `(market, ticker, exchange, count)`. 헬퍼 `_epoch(d: date, hhmmss: str, tz: ZoneInfo) -> int`, `MARKET_TZ = {"KR": ZoneInfo("Asia/Seoul"), "US": ZoneInfo("America/New_York")}`.
- 마커 규칙: `session_date` = 마지막 봉의 `date`(없으면 None → 마커 없음). `orders`(status NOT IN rejected/cancelled/failed, `account_id` 필터)·`triggers`(`kind`, `detail`, `account_id` 필터는 orders 만) 의 `created_at` 을 시장 tz 로 바꿔 날짜가 `session_date` 와 같을 때만, `time = created_at 을 60초 단위로 내림한 epoch`. 트리거 마커는 `side="TRIGGER"`, `text=f"트리거 {kind}: {detail}"`.
- 검증 순서는 일봉 라우트와 같다: market → `_validate_ids` → account_id → market 클라이언트 503 → US exchange 400.

- [ ] **Step 1: 실패 테스트 작성** (`tests/web/test_runs_charts.py`)

`tests/helpers_web.py` 에 `_minutes(n=5, d=date(2026, 10, 9), start="0900")` 헬퍼를 추가해 KST 09:00 부터 1분 간격 `Candle(time="HHMM00")` n 개를 만들고 `make_deps` 의 `FakeMarket(minutes={"005930": _minutes(), "AAPL": _minutes()})` 로 꽂는다.

```python
def test_minute_chart_epoch_and_markers_same_day_only(client, conn):
    dec_id = repo.ingest_checkpoint(conn, D, "KR", "005930", "30_decision", {"data": ok_decision()})
    for created in ("2026-10-09T00:02:30+00:00", "2026-10-08T00:02:30+00:00"):  # 09:02 KST 오늘 / 어제
        repo.insert_order(conn, decision_id=dec_id, account_id="paper-main", ticker="005930",
                          market="KR", side="BUY", qty=1, order_type="LIMIT", price=Decimal("71000"),
                          status="accepted", order_no="1", message="", mode="auto", created_at=created)
    conn.execute(
        "INSERT INTO triggers(account_id, market, ticker, kind, detail, created_at) "
        "VALUES (?,?,?,?,?,?)", ("paper-main", "KR", "005930", "take_profit", "+7%", "2026-10-09T00:03:10+00:00"))
    conn.commit()
    r = client.get("/api/charts/minute?ticker=005930&market=KR&count=5").json()
    assert r["interval"] == "1m" and r["tz"] == "Asia/Seoul" and r["session_date"] == "2026-10-09"
    t0 = int(datetime(2026, 10, 9, 0, 0, tzinfo=UTC).timestamp())
    assert [c["time"] for c in r["candles"]] == [t0 + 60 * i for i in range(5)]
    assert isinstance(r["candles"][0]["close"], str)
    assert [(m["kind"], m["time"]) for m in r["markers"]] == [("order", t0 + 120), ("trigger", t0 + 180)]
    assert "트리거 take_profit" in r["markers"][1]["text"]
    assert all(m["kind"] != "decision" for m in r["markers"])


def test_minute_chart_us_epoch_uses_new_york(client):
    r = client.get("/api/charts/minute?ticker=AAPL&market=US&exchange=NAS&count=2").json()
    # 2026-10-09 09:00 뉴욕(EDT, UTC−4) = 13:00Z
    assert r["tz"] == "America/New_York"
    assert r["candles"][0]["time"] == int(datetime(2026, 10, 9, 13, 0, tzinfo=UTC).timestamp())


def test_minute_chart_empty_candles_ok(client):
    r = client.get("/api/charts/minute?ticker=000660&market=KR")
    assert r.status_code == 200 and r.json()["candles"] == [] and r.json()["markers"] == []
    assert r.json()["session_date"] is None


def test_minute_chart_bounds(client, project):
    assert client.get("/api/charts/minute?ticker=005930&market=KR&count=0").status_code == 422
    assert client.get("/api/charts/minute?ticker=005930&market=KR&count=391").status_code == 422
    assert client.get("/api/charts/minute?ticker=005930&market=KR&account_id=ghost").status_code == 400
    assert client.get("/api/charts/minute?ticker=AAPL&market=US").status_code == 400
    assert client.get("/api/charts/minute?ticker=../x&market=KR").status_code == 400
    c = TestClient(create_app(make_deps(project, market=False)))
    assert c.get("/api/charts/minute?ticker=005930&market=KR").status_code == 503


def test_minute_chart_cache_and_count_passthrough(client, deps):
    before = len(deps.market.calls)
    assert client.get("/api/charts/minute?ticker=005930&market=KR&count=3").status_code == 200
    assert client.get("/api/charts/minute?ticker=005930&market=KR&count=3").status_code == 200
    assert deps.market.calls[before:] == ["get_minute_kr"]  # 60초 캐시
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/web/test_runs_charts.py -q -k minute_chart`
Expected: FAIL — 404 (라우트 없음).

- [ ] **Step 3: 구현** (`charts.py`)

`@router.get("/minute")` 를 **`@router.get("")` 보다 뒤에 둬도 경로가 달라 충돌 없음**. 일봉 라우트의 검증 블록을 `_validate_request(request, ticker, market, exchange, account_id) -> tuple[str, WebDeps]` 로 뽑아 두 라우트가 공유한다. 봉 변환은 `{"time": _epoch(c.date, c.time or "000000", tz), ...}`. 주문·트리거 조회는 `created_at` 을 `datetime.fromisoformat(...).astimezone(tz)` 로 바꿔 `.date().isoformat() == session_date` 인 행만, `time = int(dt.timestamp()) // 60 * 60`. 마커는 time 오름차순 정렬.

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/web -q && uv run ruff check src tests`
Expected: 전부 PASS, ruff 0건.

- [ ] **Step 5: 커밋**

```bash
git add src/trader/web/routers/charts.py tests/helpers_web.py tests/web/test_runs_charts.py
git commit -m "feat(web): /api/charts/minute — 당일 분봉 epoch 변환·60초 캐시·주문/트리거 분 마커"
```

---

### Task 3: 차트 페이지 간격 선택(일봉 | 1분)

**Files:**
- Modify: `web/src/pages/Chart.tsx`
- Modify: `web/src/types.ts`
- Test: `web/src/chart.test.ts` (새 파일, Vitest)

**Interfaces:**
- Consumes: Task 2 응답 형식.
- Produces: `types.ts` — `Candle.time: string | number`, `Marker.kind: "decision" | "order" | "trigger"`, `Marker.time: string | number`, `ChartData.interval?: "1d" | "1m"`, `ChartData.tz?: string`, `ChartData.session_date?: string | null`. `Chart.tsx` 에서 `export function markerStyle(m: Pick<Marker, "kind" | "side">): { position: "aboveBar" | "belowBar"; color: string; shape: "arrowUp" | "arrowDown" | "circle" | "square" }` (주문: 주황 `#f5a524` 화살표, 결정: 보라 `#8b5cf6` 원, 트리거: 청록 `#2dd4bf` 사각형 aboveBar). `export function fetchParams(q, interval)` 는 두지 않는다(컴포넌트 안에서 분기).
- 동작: 상태 `interval: "1d" | "1m"`(기본 `"1d"`), `select` 로 선택. `"1m"` 이면 `/api/charts/minute` 에 `count: "390"` 로 요청하고 `createChart` 옵션에 `timeScale: { timeVisible: true, secondsVisible: false }` 를 넣는다. SMA 체크박스는 두 간격 모두 적용. 안내 문구에 "1분봉은 당일(KIS 30봉 단위 페이징), 주문·트리거 마커만" 을 덧붙인다. `sma()` 는 `time` 타입만 `Time` 그대로라 변경 없음.

- [ ] **Step 1: 실패 테스트 작성** (`web/src/chart.test.ts`)

```ts
import { describe, expect, it } from "vitest";
import { markerStyle } from "./pages/Chart";

describe("markerStyle", () => {
  it("주문은 방향 화살표, 결정은 원, 트리거는 사각형", () => {
    expect(markerStyle({ kind: "order", side: "BUY" })).toEqual({ position: "belowBar", color: "#f5a524", shape: "arrowUp" });
    expect(markerStyle({ kind: "order", side: "SELL" })).toEqual({ position: "aboveBar", color: "#f5a524", shape: "arrowDown" });
    expect(markerStyle({ kind: "decision", side: "HOLD" })).toEqual({ position: "belowBar", color: "#8b5cf6", shape: "circle" });
    expect(markerStyle({ kind: "trigger", side: "TRIGGER" })).toEqual({ position: "aboveBar", color: "#2dd4bf", shape: "square" });
  });
});
```

- [ ] **Step 2: 실패 확인**

Run: `cd web && npm test -- --run`
Expected: FAIL — `markerStyle` is not exported.

- [ ] **Step 3: 구현** (`types.ts`, `Chart.tsx`)

기존 마커 매핑을 `markerStyle` 로 옮기고 `SeriesMarker` 생성에서 spread. 간격 상태·select·요청 분기·`timeScale` 옵션·안내 문구. `q` 에 `interval` 을 넣어 조회 버튼으로만 갱신한다. 마커 정렬은 `Number(a.time) - Number(b.time)` 이 아니라 문자열·숫자 모두 되는 `(a, b) => (a.time < b.time ? -1 : a.time > b.time ? 1 : 0)` 로 바꾼다(같은 타입끼리만 비교됨).

- [ ] **Step 4: 통과 확인**

Run: `cd web && npm test -- --run && npm run typecheck && npm run build`
Expected: Vitest 전부 PASS, 타입 오류 0, `web/dist` 생성.

- [ ] **Step 5: 커밋**

```bash
git add web/src/pages/Chart.tsx web/src/types.ts web/src/chart.test.ts
git commit -m "feat(web): 차트 페이지 1분봉 간격 선택·트리거 마커"
```

---

### Task 4: 문서·상태 갱신

**Files:**
- Modify: `docs/TODO.md` (Phase 5 절), `CLAUDE.md` (웹 UI 불릿의 차트 문장, 실행 명령의 `web` 설명은 변경 없음)

- [ ] **Step 1: TODO** — Phase 5 절 끝에 `- [x] 분봉 차트(2026-10-17, 사용자 결정으로 진행): \`GET /api/charts/minute\`(당일 1분봉 epoch 초·60초 캐시·주문/트리거 분 마커, KR 은 30봉 역방향 페이징 최대 390), 차트 페이지 간격 선택. 이연 유지: US 120봉 초과 페이징(실기 확인), 3·5·15분 집계·자동 갱신(사용자 결정)` 추가. 이전 줄의 "이연 유지: 분봉 차트(사용자 결정)" 는 "분봉 차트는 아래 줄" 로 바꾼다.
- [ ] **Step 2: CLAUDE.md** — 웹 UI 불릿의 "차트 일봉은 … 5분 캐시" 문장 뒤에 "분봉(`/api/charts/minute`)은 `(시장·종목·거래소·count)` 키 60초 캐시이고 시간은 UTC epoch 초(KR 은 `Asia/Seoul`, US 는 `America/New_York` 현지 시각을 변환), 마커는 마지막 봉 날짜의 주문·트리거만(결정은 날짜만 있어 제외). KR 분봉은 KIS 가 기준 시각부터 30봉만 주므로 `get_minute_kr(count=)` 가 1분 전 시각으로 역방향 페이징한다(새 봉 없음·30 미만·09:00 도달이면 중단)." 추가.
- [ ] **Step 3: 검증** — `uv run pytest -q 2>&1 | tail -3`, `uv run ruff check src tests`, `uv run trader status`.
  Expected: 전부 통과, STATUS.md 갱신.
- [ ] **Step 4: 커밋**

```bash
git add docs/TODO.md CLAUDE.md
git commit -m "docs: 분봉 차트 TODO·CLAUDE.md 갱신"
```
