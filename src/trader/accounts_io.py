"""config/accounts.yaml 계좌 추가·삭제 — 설정 화면의 '계좌' 섹션.

한 계좌 = flow 스타일 한 줄(`- {id: …}`)이라 추가는 목록 끝에 한 줄, 삭제는 그 줄 하나만 뺀다
(다른 줄·주석은 그대로). 바꾸기 전 `accounts.yaml.bak-<YYYYMMDD_HHMMSS>` 를 남기고, 쓴 뒤
`load_accounts` 로 다시 읽어 검증해 실패하면 백업으로 되돌린다. 계좌번호는 항상 자리표시자이고
실제 번호는 연결 키(`KIS_<ID>_CANO`)에 둔다. 실행 중인 데몬·웹은 재시작 뒤에 새 목록을 읽는다.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import uuid
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from trader.config import (
    CANO_PLACEHOLDER,
    AccountConfig,
    Asset,
    ConfigError,
    Env,
    Market,
    Mode,
    Secrets,
    account_prefix,
    load_accounts,
)
from trader.ledger.repo import dead_status_sql
from trader.platform.fsutil import atomic_replace
from trader.secrets_io import _backup, _editing

ID_RE = re.compile(r"^[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*$")
# 계좌 전용 키 접두사가 env 공용 키(KIS_PAPER_APP_KEY 등)와 겹치는 id 는 받지 않는다
_RESERVED_PREFIXES = frozenset({"KIS_PAPER_", "KIS_REAL_"})
_FO_PRODUCT = "03"
_STOCK_PRODUCT = "01"


class AccountInUse(ConfigError):
    """삭제 조건(살아 있는 주문·승인·보유·파생 포지션 없음)을 못 채움."""


class AccountSpec(BaseModel):
    """설정 화면에서 받는 새 계좌. 나머지(cano·상품코드·실전 모드)는 규칙이 정한다."""

    model_config = ConfigDict(extra="forbid")

    id: str
    env: Env
    asset: Asset = "stock"
    markets: list[Market] = ["KR"]
    mode: Mode = "auto"


def _probe(account_id: str, env: Env = "paper") -> AccountConfig:
    return AccountConfig(id=account_id, env=env, cano=CANO_PLACEHOLDER, mode="off")


def _line(spec: AccountSpec, indent: str) -> str:
    fo = spec.asset == "fo"
    markets = ["KR"] if fo else sorted(set(spec.markets), key=["KR", "US"].index)
    mode = "off" if spec.env == "real" else spec.mode
    tail = ", asset: fo" if fo else ""
    return (
        f'{indent}- {{id: {spec.id}, env: {spec.env}, cano: "{CANO_PLACEHOLDER}", '
        f'acnt_prdt_cd: "{_FO_PRODUCT if fo else _STOCK_PRODUCT}", '
        f'markets: [{", ".join(markets)}], mode: "{mode}"{tail}}}'
    )


def _check_spec(spec: AccountSpec, existing: list[AccountConfig]) -> None:
    if not ID_RE.match(spec.id):
        raise ConfigError(
            f"계좌 id 는 영문·숫자와 '-' 만 쓸 수 있습니다(앞뒤 '-' 금지): {spec.id!r}"
        )
    prefix = account_prefix(_probe(spec.id))
    if prefix in _RESERVED_PREFIXES:
        raise ConfigError(f"계좌 id {spec.id!r} 는 공용 키 이름과 겹쳐 쓸 수 없습니다")
    for a in existing:
        if a.id.lower() == spec.id.lower() or account_prefix(a) == prefix:
            raise ConfigError(f"이미 있는 계좌와 겹칩니다: {a.id} (키 접두사 {prefix})")
    if spec.asset == "stock" and not spec.markets:
        raise ConfigError("시장을 하나 이상 고르세요")


def _section(lines: list[str]) -> tuple[int, int] | None:
    """`accounts:` 줄 번호와, 그 목록의 마지막 내용 줄 번호(없으면 키 줄)."""
    start = next((i for i, ln in enumerate(lines) if re.match(r"^accounts\s*:", ln)), None)
    if start is None:
        return None
    last = start
    for i in range(start + 1, len(lines)):
        ln = lines[i]
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        if not ln[0].isspace() and not ln.startswith("-"):
            break
        last = i
    return start, last


def _write(path: Path, lines: list[str]) -> None:
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    tmp.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
    atomic_replace(tmp, path)


def _verify(path: Path, backup: str | None, check) -> list[AccountConfig]:
    """다시 읽어 검증, 실패하면 백업으로 되돌린다(백업이 없던 새 파일은 지운다)."""
    try:
        accounts = load_accounts(path, secrets=Secrets())
        check(accounts)
        return accounts
    except Exception as e:
        if backup:
            shutil.copy2(path.with_name(backup), path)
        else:
            path.unlink(missing_ok=True)
        raise ConfigError(f"accounts.yaml 검증 실패로 되돌렸습니다: {e}") from e


def _current(path: Path) -> list[AccountConfig]:
    return load_accounts(path, secrets=Secrets()) if path.exists() else []


def add_account(path: Path, spec: AccountSpec) -> dict:
    """새 계좌 한 줄을 목록 끝에 붙인다. 반환 {id, backup}."""
    with _editing(path):
        _check_spec(spec, _current(path))
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        sec = _section(lines)
        if sec is None:
            lines += ["accounts:", _line(spec, "  ")]
        else:
            start, last = sec
            head = lines[start]
            if re.match(r"^accounts\s*:\s*\[\s*\]", head):  # `accounts: []` → 블록 목록으로
                lines[start] = "accounts:" + head.split("]", 1)[1]
            items = [ln for ln in lines[start + 1 : last + 1] if ln.lstrip().startswith("- ")]
            indent = re.match(r"^(\s*)", items[-1]).group(1) if items else "  "
            lines.insert(last + 1, _line(spec, indent))
        backup = _backup(path)
        _write(path, lines)

        def check(accts: list[AccountConfig]) -> None:
            if spec.id not in {a.id for a in accts}:
                raise ConfigError(f"추가한 계좌 {spec.id} 가 읽히지 않습니다")

        _verify(path, backup, check)
    return {"id": spec.id, "backup": backup}


def blockers(conn: sqlite3.Connection, account_id: str) -> list[str]:
    """삭제를 막는 사유(없으면 빈 목록). 원장은 읽기만 한다."""
    reasons: list[str] = []
    n = conn.execute(
        f"SELECT COUNT(*) FROM orders WHERE account_id=? "
        f"AND status NOT IN ({dead_status_sql()},'filled')",
        (account_id,),
    ).fetchone()[0]
    if n:
        reasons.append(f"살아 있는 주문 {n}건(체결·취소 뒤 삭제)")
    n = conn.execute(
        "SELECT COUNT(*) FROM approvals WHERE account_id=? AND status='pending'", (account_id,)
    ).fetchone()[0]
    if n:
        reasons.append(f"대기 중인 승인 요청 {n}건")
    held = conn.execute(
        "SELECT p.ticker FROM positions_snapshot p WHERE p.account_id=? AND p.qty > 0"
        " AND p.ticker NOT LIKE '\\_CASH%' ESCAPE '\\'"
        " AND p.date = (SELECT MAX(q.date) FROM positions_snapshot q"
        "  WHERE q.account_id=p.account_id AND COALESCE(q.market,'')=COALESCE(p.market,''))",
        (account_id,),
    ).fetchall()
    if held:
        reasons.append(f"최근 잔고에 보유 종목 {len(held)}개(전량 매도 뒤 삭제)")
    n = conn.execute(
        "SELECT COUNT(*) FROM fo_positions WHERE account_id=?"
        " AND status NOT IN ('closed','settled')",
        (account_id,),
    ).fetchone()[0]
    if n:
        reasons.append(f"열린 파생 포지션 {n}개")
    return reasons


def remove_account(path: Path, account_id: str, conn: sqlite3.Connection) -> dict:
    """계좌 줄 하나를 뺀다(원장 기록·연결 키는 그대로). 반환 {id, backup}."""
    with _editing(path):
        if account_id not in {a.id for a in _current(path)}:
            raise ConfigError(f"그런 계좌가 없습니다: {account_id}")
        reasons = blockers(conn, account_id)
        if reasons:
            raise AccountInUse(f"계좌 {account_id} 를 지울 수 없습니다: " + " · ".join(reasons))
        lines = path.read_text(encoding="utf-8").splitlines()
        pat = re.compile(r"^\s*-\s*\{\s*id\s*:\s*[\"']?" + re.escape(account_id) + r"[\"']?\s*[,}]")
        hits = [i for i, ln in enumerate(lines) if pat.match(ln)]
        if len(hits) != 1:
            raise ConfigError(
                f"계좌 {account_id} 가 한 줄(`- {{id: …}}`) 형식이 아니라 화면에서 지울 수 없습니다"
                " — accounts.yaml 을 직접 고치세요"
            )
        del lines[hits[0]]
        backup = _backup(path)
        _write(path, lines)

        def check(accts: list[AccountConfig]) -> None:
            if account_id in {a.id for a in accts}:
                raise ConfigError(f"계좌 {account_id} 가 아직 남아 있습니다")

        _verify(path, backup, check)
    return {"id": account_id, "backup": backup}
