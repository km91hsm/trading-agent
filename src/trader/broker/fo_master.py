"""지수선물옵션 종목 마스터(`fo_idx_code_mts.mst`, Phase 26).

KIS 다운로드 서버의 zip 을 받아 24시간 캐시하고 `|` 구분 cp949 9필드를 `Contract` 로 푼다
(공식 `stocks_info/domestic_index_future_code.py` 의 열 순서). 월물 선택·행사가 해석은 여기서만.
최종거래일은 시세(`futs_last_tr_date`)가 우선이고 없으면 월물의 둘째 목요일로 추정한다(로그).
미확인: 상품종류·ATM 구분·월물구분코드의 실제 값 형식(실기 1 에서 fixture 로 확정).
"""

from __future__ import annotations

import io
import logging
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

from trader.broker.models import FoKind
from trader.config import FoProductCfg

logger = logging.getLogger("trader.kis")

MASTER_URL = "https://new.real.download.dws.co.kr/common/master/fo_idx_code_mts.mst.zip"
MASTER_NAME = "fo_idx_code_mts.mst"
# 코드 접두 → (종류, 위클리). 위클리(209/309)는 해석 대상에서 뺀다
PREFIX_KIND: dict[str, tuple[FoKind, bool]] = {
    "101": ("FUTURES", False),
    "105": ("FUTURES", False),
    "201": ("CALL", False),
    "205": ("CALL", False),
    "301": ("PUT", False),
    "305": ("PUT", False),
    "209": ("CALL", True),
    "309": ("PUT", True),
}
StrikeRule = Literal["ATM", "OTM1", "OTM2", "ITM1"]
ExpiryRule = Literal["front", "next"]


@dataclass(frozen=True)
class Contract:
    code: str  # 단축코드(주문·시세)
    std_code: str  # 표준코드(체결·잔고 pdno 가 이것일 수 있다)
    name: str
    kind: FoKind
    weekly: bool
    strike: Decimal | None
    expiry_ym: str | None  # "YYYYMM" (월물구분코드가 그 형식일 때)
    underlying: str
    atm_flag: str | None
    product_type: str  # 상품종류 원문


def _dec(v: str) -> Decimal | None:
    s = v.strip()
    if not s:
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def _ym(v: str) -> str | None:
    s = v.strip()
    return s if len(s) == 6 and s.isdigit() else None


def parse(raw: bytes) -> list[Contract]:
    """마스터 본문(cp949, `|` 9필드) → Contract 목록. 접두를 모르는 행·필드 부족 행은 건너뛴다."""
    text = raw.decode("cp949", errors="replace")
    out: list[Contract] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 9:
            continue
        ptype, code, std, name, atm, strike, ym, under, _under_name = parts[:9]
        code = code.upper()
        info = PREFIX_KIND.get(code[:3])
        if info is None:
            continue
        kind, weekly = info
        out.append(
            Contract(
                code=code,
                std_code=std.upper(),
                name=name,
                kind=kind,
                weekly=weekly,
                strike=None if kind == "FUTURES" else _dec(strike),
                expiry_ym=_ym(ym),
                underlying=under,
                atm_flag=atm or None,
                product_type=ptype,
            )
        )
    return out


def load(
    *,
    cache_dir: Path,
    http: Any = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    max_age_h: int = 24,
) -> list[Contract]:
    """캐시(`cache_dir/fo_idx_code_mts.mst`)가 max_age_h 안이면 그것, 아니면 zip 다운로드 뒤 갱신.
    다운로드 실패 때 오래된 캐시가 있으면 경고 후 그것을 쓴다."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache = cache_dir / MASTER_NAME
    if cache.exists():
        age = now() - datetime.fromtimestamp(cache.stat().st_mtime, UTC)
        if age <= timedelta(hours=max_age_h):
            return parse(cache.read_bytes())
    try:
        if http is None:
            import httpx

            http = httpx.Client(timeout=30)
        r = http.get(MASTER_URL)
        r.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            name = next((n for n in z.namelist() if n.endswith(".mst")), None)
            if name is None:
                raise ValueError("마스터 zip 안에 .mst 가 없습니다")
            data = z.read(name)
    except Exception as e:  # noqa: BLE001 — 다운로드 실패는 캐시 폴백
        if cache.exists():
            logger.warning("선물옵션 마스터 다운로드 실패, 오래된 캐시 사용: %s", e)
            return parse(cache.read_bytes())
        raise
    tmp = cache.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(cache)
    # mtime 을 now() 로 맞춘다(테스트 시계·시스템 시계 차이)
    ts = now().timestamp()
    import os

    os.utime(cache, (ts, ts))
    return parse(data)


def second_thursday(ym: str) -> date:
    """월물 YYYYMM 의 둘째 목요일(코스피200 선물·옵션 최종거래일 규칙, 휴일 앞당김은 반영 안 함)."""
    y, m = int(ym[:4]), int(ym[4:6])
    first = date(y, m, 1)
    offset = (3 - first.weekday()) % 7  # 목요일=3
    return first + timedelta(days=offset + 7)


def last_trade_date(contract: Contract, quote: Any | None) -> date | None:
    """시세의 `last_trade_date` 우선, 없으면 둘째 목요일 추정(로그). 월물을 모르면 None."""
    ltd = getattr(quote, "last_trade_date", None) if quote is not None else None
    if isinstance(ltd, date):
        return ltd
    if contract.expiry_ym is None:
        return None
    est = second_thursday(contract.expiry_ym)
    logger.debug("최종거래일 추정(둘째 목요일): %s → %s", contract.code, est)
    return est


def of_product(
    contracts: Iterable[Contract], product: FoProductCfg, kind: FoKind
) -> list[Contract]:
    """상품(접두)·종류의 월물 계약만(위클리 제외)."""
    return _of_product(contracts, product, kind)


def _of_product(
    contracts: Iterable[Contract], product: FoProductCfg, kind: FoKind
) -> list[Contract]:
    prefix = {
        "FUTURES": product.futures_prefix,
        "CALL": product.call_prefix,
        "PUT": product.put_prefix,
    }[kind]
    return [c for c in contracts if c.kind == kind and not c.weekly and c.code.startswith(prefix)]


def _expiries(
    contracts: list[Contract],
    *,
    today: date,
    roll_days: int,
    last_trade_date_of: Callable[[Contract], date | None],
) -> list[str]:
    """roll 창 밖의 월물을 가까운 순으로. 최종거래일을 못 구하는 월물은 뺀다."""
    out: dict[str, date] = {}
    for c in contracts:
        if c.expiry_ym is None or c.expiry_ym in out:
            continue
        ltd = last_trade_date_of(c)
        if ltd is None:
            ltd = second_thursday(c.expiry_ym)
        if (ltd - today).days <= roll_days:
            continue
        out[c.expiry_ym] = ltd
    return sorted(out, key=lambda ym: out[ym])


def front_month(
    contracts: Iterable[Contract],
    product: FoProductCfg,
    kind: FoKind,
    *,
    today: date,
    roll_days: int,
    last_trade_date_of: Callable[[Contract], date | None],
) -> Contract | None:
    """가장 가까운 월물 중 최종거래일이 today + roll_days 보다 뒤인 것(선물용)."""
    cs = _of_product(contracts, product, kind)
    yms = _expiries(cs, today=today, roll_days=roll_days, last_trade_date_of=last_trade_date_of)
    if not yms:
        return None
    return next(c for c in cs if c.expiry_ym == yms[0])


def option_front(
    contracts: Iterable[Contract],
    product: FoProductCfg,
    *,
    today: date,
    roll_days: int,
    last_trade_date_of: Callable[[Contract], date | None],
) -> tuple[str | None, list[Contract]]:
    """roll 창 밖의 가장 가까운 옵션 월물(YYYYMM)과 그 월물의 콜·풋 계약 전부. 없으면 (None, [])."""
    calls = _of_product(contracts, product, "CALL")
    yms = _expiries(calls, today=today, roll_days=roll_days, last_trade_date_of=last_trade_date_of)
    if not yms:
        return None, []
    ym = yms[0]
    puts = _of_product(contracts, product, "PUT")
    return ym, [c for c in calls + puts if c.expiry_ym == ym]


def resolve_option(
    contracts: Iterable[Contract],
    product: FoProductCfg,
    *,
    call_put: FoKind,
    strike_rule: StrikeRule,
    expiry_rule: ExpiryRule,
    underlying: Decimal,
    today: date,
    roll_days: int,
    last_trade_date_of: Callable[[Contract], date | None],
) -> Contract | None:
    """월물(front|next) 안에서 행사가 규칙으로 옵션 계약 하나. ATM 은 기초지수에 가장 가까운
    행사가, OTM 은 콜이면 위·풋이면 아래로 n 단계, ITM1 은 반대로 한 단계. 간격은 마스터의 인접
    행사가. 없으면 None."""
    if call_put not in ("CALL", "PUT"):
        return None
    cs = _of_product(contracts, product, call_put)
    yms = _expiries(cs, today=today, roll_days=roll_days, last_trade_date_of=last_trade_date_of)
    idx = 0 if expiry_rule == "front" else 1
    if len(yms) <= idx:
        return None
    chain = sorted(
        (c for c in cs if c.expiry_ym == yms[idx] and c.strike is not None),
        key=lambda c: c.strike,  # type: ignore[arg-type, return-value]
    )
    if not chain:
        return None
    atm_i = min(range(len(chain)), key=lambda i: (abs(chain[i].strike - underlying), i))  # type: ignore[operator]
    up = 1 if call_put == "CALL" else -1  # 콜의 OTM 은 위, 풋의 OTM 은 아래
    step = {"ATM": 0, "OTM1": up, "OTM2": 2 * up, "ITM1": -up}[strike_rule]
    j = atm_i + step
    if 0 <= j < len(chain):
        return chain[j]
    return None
