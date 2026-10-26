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
TR_PINGPONG = "PINGPONG"
MAX_SUBSCRIPTIONS = 41
# H0STCNT0 한 건의 칸 수(공식 샘플 ccnl_krx). 여러 건 묶음을 이 크기로 자른다
TRADE_FIELD_COUNT = 46
# 앞쪽 칸 이름(공식 샘플 순서): 종목·체결시각·현재가·전일대비부호·전일대비·등락률
TRADE_FIELDS = ("ticker", "time", "price", "sign", "change", "change_pct")
_DOWN = {"4", "5"}  # 전일대비부호 4(하한)·5(하락)


def subscribe_message(approval_key: str, ticker: str, *, release: bool = False) -> str:
    return json.dumps(
        {
            "header": {
                "approval_key": approval_key,
                "custtype": "P",
                "tr_type": "2" if release else "1",  # 1 등록, 2 해제
                "content-type": "utf-8",
            },
            "body": {"input": {"tr_id": TR_TRADE, "tr_key": ticker}},
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


def _signed_pct(value: str, sign: str) -> float | None:
    try:
        v = float(value.strip())
    except (TypeError, ValueError):
        return None
    return -abs(v) if sign.strip() in _DOWN else v


def parse_frame(raw: str) -> list[dict[str, Any]]:
    """체결가 프레임 → [{ticker, price, change_pct, time}]. 제어·암호화(첫 칸 1)·다른 TR·깨진 값은
    빈 목록."""
    parts = raw.strip().split("|")
    if len(parts) < 4 or parts[0].strip() != "0" or parts[1] != TR_TRADE:
        return []
    values = parts[3].split("^")
    try:
        count = max(int(parts[2]), 1)
    except ValueError:
        count = 1
    size = len(values) // count if len(values) % count == 0 else len(values)
    out = []
    for i in range(0, len(values), size or 1):
        rec = dict(zip(TRADE_FIELDS, values[i : i + size], strict=False))
        if len(rec) < len(TRADE_FIELDS) or not rec["ticker"] or not rec["price"].strip():
            continue
        out.append(
            {
                "ticker": rec["ticker"],
                "time": rec["time"],
                "price": rec["price"].strip(),
                "change_pct": _signed_pct(rec["change_pct"], rec["sign"]),
            }
        )
        if size == len(values):
            break
    return out
