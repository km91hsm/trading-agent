"""KIS 실시간 웹소켓 — 국내 주식 체결가(H0STCNT0) 프레임 해석과 구독 메시지 (표시 전용).

접속키(`KisAuth.approval_key`)로 접속하고 종목마다 등록 메시지를 보낸다. 응답은 JSON(제어 메시지)과
`|`·`^` 구분 문자열(실시간 데이터)이 섞여 온다. 서버는 주기적으로 `PINGPONG` 을 보내고 **같은
메시지를 돌려받지 못하면 연결을 끊는다.** 체결이 몰리면 한 프레임에 여러 건(세 번째 칸이 건수)이
이어 붙어 온다 — 첫 건만 읽으면 나머지를 잃는다. 한 연결의 구독은 41칸까지(KIS 제한).
신호·주문은 이 값을 읽지 않는다(시세 조회·재시세는 REST 그대로). 연결·구독 관리는
`web/live_quotes.QuoteHub`.
"""

from __future__ import annotations

import json
from typing import Any

TR_TRADE = "H0STCNT0"
TR_INDEX = "H0UPCNT0"  # 업종(지수) 체결 — KOSPI 0001·KOSDAQ 1001
TR_BOOK = "H0STASP0"  # 호가 10단
# 체결통보(접수·체결·정정·취소, HTS ID 로 구독, 본문 AES 암호화). 실전/모의 × 국내/해외
TR_NOTICE = {"real": ("H0STCNI0", "H0GSCNI0"), "paper": ("H0STCNI9", "H0GSCNI9")}
INDEX_CODES = {"0001": "KOSPI", "1001": "KOSDAQ"}
BOOK_LEVELS = 10
TR_PINGPONG = "PINGPONG"
MAX_SUBSCRIPTIONS = 41
# H0STCNT0 한 건의 칸 수(공식 샘플 ccnl_krx). 여러 건 묶음을 이 크기로 자른다
TRADE_FIELD_COUNT = 46
# 앞쪽 칸 이름(공식 샘플 순서): 종목·체결시각·현재가·전일대비부호·전일대비·등락률
TRADE_FIELDS = ("ticker", "time", "price", "sign", "change", "change_pct")
_DOWN = {"4", "5"}  # 전일대비부호 4(하한)·5(하락)


def subscribe_message(
    approval_key: str, ticker: str, *, release: bool = False, tr_id: str = TR_TRADE
) -> str:
    return json.dumps(
        {
            "header": {
                "approval_key": approval_key,
                "custtype": "P",
                "tr_type": "2" if release else "1",  # 1 등록, 2 해제
                "content-type": "utf-8",
            },
            "body": {"input": {"tr_id": tr_id, "tr_key": ticker}},
        }
    )


def _json(raw: str) -> dict | None:
    text = raw.strip()
    if not text.startswith("{"):
        return None
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def is_pingpong(raw: str) -> bool:
    payload = _json(raw)
    header = (payload or {}).get("header")
    return isinstance(header, dict) and header.get("tr_id") == TR_PINGPONG


def control_problem(raw: str) -> tuple[str, str, str] | None:
    """제어 메시지가 **거절**이면 (tr_id, tr_key, msg1), 아니면 None — 그대로 흘리면 구독이 거절돼도
    '연결은 됐는데 틱이 안 온다' 로만 보인다."""
    payload = _json(raw)
    if payload is None:
        return None
    header, body = payload.get("header"), payload.get("body")
    if not isinstance(body, dict) or str(body.get("rt_cd", "0")) == "0":
        return None
    header = header if isinstance(header, dict) else {}
    msg = str(body.get("msg1") or f"rt_cd={body.get('rt_cd')}").strip()
    return str(header.get("tr_id", "")), str(header.get("tr_key", "")), msg


def is_appkey_in_use(msg: str) -> bool:
    """'ALREADY IN USE appkey' — KIS 는 앱키당 웹소켓 세션 하나만 받는다(다른 연결이 쥐고 있다).
    같은 키를 두 번 구독한 'ALREADY IN SUBSCRIBE' 와 구별한다."""
    return "in use" in msg.lower()


def _signed_pct(value: str, sign: str) -> float | None:
    """KIS 등락률(%) → 비율(REST `KisMarket.get_price_kr` 와 같은 단위, 1.25% → 0.0125)."""
    try:
        v = float(value.strip()) / 100
    except (TypeError, ValueError):
        return None
    return -abs(v) if sign.strip() in _DOWN else v


def _records(raw: str) -> tuple[str, list[list[str]]] | None:
    """평문 데이터 프레임 → (tr_id, 건별 값 목록). 여러 건 묶음은 세 번째 칸(건수)으로 나눈다."""
    parts = raw.strip().split("|")
    if len(parts) < 4 or parts[0].strip() != "0":
        return None
    values = parts[3].split("^")
    try:
        count = max(int(parts[2]), 1)
    except ValueError:
        count = 1
    size = len(values) // count if len(values) % count == 0 else len(values)
    size = size or len(values)
    return parts[1], [values[i : i + size] for i in range(0, len(values), size)][:count]


def is_notice(raw: str) -> bool:
    """체결통보 데이터 프레임(첫 칸 1 = 암호화)인가 — 본문은 풀지 않고 '무언가 체결·접수됐다'
    신호로만 쓴다(데몬이 체결 동기화를 바로 당긴다)."""
    parts = raw.strip().split("|")
    notice_trs = {tr for pair in TR_NOTICE.values() for tr in pair}
    return len(parts) >= 4 and parts[1] in notice_trs


def parse_frame(raw: str) -> list[dict[str, Any]]:
    """평문 프레임 → 레코드. 체결가 {tr_id, ticker, price, change_pct, time}, 지수 {tr_id, code,
    name, price, change_pct, time}, 호가 {tr_id, ticker, time, asks:[[가격, 잔량]×10], bids,
    total_ask, total_bid}. 제어·암호화·모르는 TR·깨진 값은 빈 목록."""
    got = _records(raw)
    if got is None:
        return []
    tr, rows = got
    out: list[dict[str, Any]] = []
    for vals in rows:
        if tr == TR_TRADE:
            rec = dict(zip(TRADE_FIELDS, vals, strict=False))
            if len(rec) < len(TRADE_FIELDS) or not rec["ticker"] or not rec["price"].strip():
                continue
            out.append(
                {
                    "tr_id": tr,
                    "ticker": rec["ticker"],
                    "time": rec["time"],
                    "price": rec["price"].strip(),
                    "change_pct": _signed_pct(rec["change_pct"], rec["sign"]),
                }
            )
        elif tr == TR_INDEX:
            # 공식 샘플 index_ccnl: 업종코드·시각·현재지수·부호·대비 … 9번째 칸이 등락률
            if len(vals) < 10 or not vals[0] or not vals[2].strip():
                continue
            out.append(
                {
                    "tr_id": tr,
                    "code": vals[0],
                    "name": INDEX_CODES.get(vals[0], vals[0]),
                    "time": vals[1],
                    "price": vals[2].strip(),
                    "change_pct": _signed_pct(vals[9], vals[3]),
                }
            )
        elif tr == TR_BOOK:
            # 공식 샘플 asking_price_krx: 종목·시각·시간구분, 매도호가 1~10, 매수호가 1~10,
            # 매도잔량 1~10, 매수잔량 1~10, 총매도잔량, 총매수잔량
            n = BOOK_LEVELS
            if len(vals) < 3 + 4 * n + 2 or not vals[0]:
                continue
            asks, bids = vals[3 : 3 + n], vals[3 + n : 3 + 2 * n]
            ask_q, bid_q = vals[3 + 2 * n : 3 + 3 * n], vals[3 + 3 * n : 3 + 4 * n]
            out.append(
                {
                    "tr_id": tr,
                    "ticker": vals[0],
                    "time": vals[1],
                    "asks": [[p.strip(), q.strip()] for p, q in zip(asks, ask_q, strict=True)],
                    "bids": [[p.strip(), q.strip()] for p, q in zip(bids, bid_q, strict=True)],
                    "total_ask": vals[3 + 4 * n].strip(),
                    "total_bid": vals[4 + 4 * n].strip(),
                }
            )
    return out
