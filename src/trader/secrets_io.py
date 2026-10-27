"""secrets/.env 읽고 쓰기 — 다른 프로젝트 .env 가져오기(`import_env`)와 설정 화면의 키 입력
(`catalog`·`status`·`update`), 웹 토큰 자동 생성(`ensure_web_token`).

값은 로그·stdout·반환값 어디에도 넣지 않는다 (키 이름과 '설정됨' 여부만). 기존 줄·주석은 그대로
두고 새 키는 끝에 붙이며, 바꾸기 전 파일은 `.env.bak-<YYYYMMDD_HHMMSS>` 로 남긴다.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from dotenv import dotenv_values

from trader.config import (
    CRED_SUFFIXES,
    AccountConfig,
    ConfigError,
    Secrets,
    account_prefix,
)
from trader.platform.filelock import locked
from trader.platform.fsutil import atomic_replace

# 원본 키 이름 → 이 저장소 키 이름. 같은 이름은 그대로 (Secrets 필드 전부가 대상).
NAME_MAP: dict[str, str] = {
    "KIS_APP_KEY": "KIS_PAPER_APP_KEY",
    "KIS_APP_SECRET": "KIS_PAPER_APP_SECRET",
    "KIS_LIVE_APP_KEY": "KIS_REAL_APP_KEY",
    "KIS_LIVE_APP_SECRET": "KIS_REAL_APP_SECRET",
    "KIS_CANO": "KIS_PAPER_CANO",
    "KIS_LIVE_CANO": "KIS_REAL_CANO",
}


def _read(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        raw = dotenv_values(path, encoding="utf-8")
    except UnicodeDecodeError as e:
        raise ConfigError(f"{path.name} 은 UTF-8 로 저장해야 합니다: {e}") from e
    return {k: (v or "").strip() for k, v in raw.items()}


def import_env(
    src: Path,
    dst: Path,
    *,
    overwrite: bool = False,
    keys: list[str] | None = None,
) -> dict[str, list[str]]:
    """src 의 값을 dst 로. 반환 {copied, kept, same, differs, unavailable, backup} 은 이름만."""
    if not src.exists():
        raise ConfigError(f"원본 .env 가 없습니다: {src}")
    source = _read(src)
    mapped: dict[str, str] = {}
    for k, v in source.items():
        target = NAME_MAP.get(k, k)
        if target in Secrets.model_fields and v:
            mapped[target] = v
    wanted = list(Secrets.model_fields) if keys is None else list(keys)
    current = _read(dst)
    copied: list[str] = []
    kept: list[str] = []
    same: list[str] = []
    differs: list[str] = []
    unavailable: list[str] = []
    for name in wanted:
        if name not in mapped:
            unavailable.append(name)
            continue
        if current.get(name) and not overwrite:
            kept.append(name)
            (same if current[name] == mapped[name] else differs).append(name)
            continue
        copied.append(name)
    backup: str | None = None
    if copied:
        with _editing(dst):
            backup = _backup(dst)
            _write(dst, {k: mapped[k] for k in copied})
    return {
        "copied": copied,
        "kept": kept,
        "same": same,  # 유지했지만 원본과 값이 같은 키
        "differs": differs,  # 유지했지만 원본과 값이 다른 키 (--overwrite 판단용, 값은 내지 않음)
        "unavailable": unavailable,
        "backup": backup,
    }


_THREAD_LOCK = threading.Lock()


@contextlib.contextmanager
def _editing(dst: Path) -> Iterator[None]:
    """백업·읽기·고치기·바꿔 넣기 전체를 한 번에 하나만(같은 프로세스의 웹 요청 스레드 둘, 데몬과
    CLI 처럼 다른 프로세스 둘이 동시에 저장해도 한쪽 변경이 사라지지 않게)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    with _THREAD_LOCK, locked(dst.with_name(dst.name + ".lock"), timeout=10.0):
        yield


def _backup(dst: Path) -> str | None:
    """되돌릴 수 있게 바꾸기 전 파일을 남긴다 (파일명에 ':' 금지 → YYYYMMDD_HHMMSS)."""
    if not dst.exists():
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    bak = dst.with_name(f"{dst.name}.bak-{stamp}")
    n = 1
    while bak.exists():  # 같은 초에 두 번 저장해도 앞 백업을 덮지 않는다
        n += 1
        bak = dst.with_name(f"{dst.name}.bak-{stamp}-{n}")
    shutil.copy2(dst, bak)
    try:
        os.chmod(bak, 0o600)
    except OSError:
        pass
    return bak.name


def _write(dst: Path, values: dict[str, str], remove: set[str] | frozenset = frozenset()) -> None:
    """있는 줄은 자리에서 바꾸고 없는 키는 끝에 붙인다(주석·순서 보존), remove 의 키 줄은 뺀다.
    임시 파일에 쓴 뒤 바꿔 넣고(쓰다 끊겨도 원본이 남는다) 파일 권한은 0600."""
    lines = dst.read_text(encoding="utf-8").splitlines() if dst.exists() else []
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        is_kv = "=" in line and not line.lstrip().startswith("#")
        key = line.split("=", 1)[0].strip() if is_kv else None
        if key is not None and key.startswith("export "):
            key = key[len("export ") :].strip()
        if key in remove:
            continue
        if key in values:
            out.append(f"{key}={values[key]}")
            seen.add(key)
        else:
            out.append(line)
    for k, v in values.items():
        if k not in seen:
            out.append(f"{k}={v}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f"{dst.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    tmp.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    atomic_replace(tmp, dst)


# --- 설정 화면의 키 입력 -----------------------------------------------------

# (이름, 묶음, 설명). 값은 어디에도 돌려주지 않고 '설정됨' 여부만 — 웹 토큰은 자동 생성이라 뺀다
CATALOG: tuple[tuple[str, str, str], ...] = (
    ("KIS_PAPER_APP_KEY", "한국투자증권 · 모의 공용", "모의 앱키"),
    ("KIS_PAPER_APP_SECRET", "한국투자증권 · 모의 공용", "모의 앱시크릿"),
    ("KIS_PAPER_CANO", "한국투자증권 · 모의 공용", "모의 계좌번호(8자리)"),
    ("KIS_REAL_APP_KEY", "한국투자증권 · 실전 공용", "실전 앱키 — 시세 조회도 이 키"),
    ("KIS_REAL_APP_SECRET", "한국투자증권 · 실전 공용", "실전 앱시크릿"),
    ("KIS_REAL_CANO", "한국투자증권 · 실전 공용", "실전 계좌번호(8자리)"),
    ("KIS_HTS_ID", "한국투자증권 · 실전 공용", "HTS ID — 체결통보 구독(같은 명의 계좌 전부)"),
    ("TELEGRAM_BOT_TOKEN", "알림", "텔레그램 봇 토큰"),
    ("TELEGRAM_CHAT_ID", "알림", "텔레그램 채팅 ID"),
    ("DART_API_KEY", "데이터", "DART 공시 API 키"),
    ("NAVER_CLIENT_ID", "데이터", "네이버 API Client ID"),
    ("NAVER_CLIENT_SECRET", "데이터", "네이버 API Client Secret"),
    ("FRED_API_KEY", "데이터", "FRED(미국 거시) API 키"),
    ("ECOS_API_KEY", "데이터", "ECOS(한국은행) API 키"),
    ("REDDIT_CLIENT_ID", "데이터", "Reddit Client ID"),
    ("REDDIT_CLIENT_SECRET", "데이터", "Reddit Client Secret"),
    ("REDDIT_USER", "데이터", "Reddit 사용자명"),
)
_ACCOUNT_LABELS = {
    "APP_KEY": "앱키",
    "APP_SECRET": "앱시크릿",
    "CANO": "계좌번호(8자리)",
    "HTS_ID": "HTS ID(비우면 공용)",
}
MAX_VALUE_LEN = 1024


def catalog(accounts: list[AccountConfig]) -> list[dict]:
    """입력할 수 있는 키 목록: 공용 + 계좌마다 전용 키 묶음(접두사가 공용과 같으면 공용)."""
    out = [{"name": n, "group": g, "label": lbl, "account": None} for n, g, lbl in CATALOG]
    for a in accounts:
        prefix = account_prefix(a)
        if prefix == f"KIS_{a.env.upper()}_":
            continue
        env = "실전" if a.env == "real" else "모의"
        for suf in CRED_SUFFIXES:
            out.append(
                {
                    "name": prefix + suf,
                    "group": f"계좌 전용 · {a.id} ({env})",
                    "label": _ACCOUNT_LABELS[suf],
                    "account": a.id,
                }
            )
    return out


def _env_overrides(names: list[str]) -> list[str]:
    """비어 있지 않은 환경변수가 .env 값을 덮어쓰는 이름(`config.load_secrets` 와 같은 규칙)."""
    return [n for n in names if (os.environ.get(n) or "").strip()]


def status(path: Path, accounts: list[AccountConfig]) -> dict:
    """{keys: [{name, group, label, account, set, env}], accounts: [{id, env, uses, prefix}]}
    — 값은 없다. 판정은 실제 로더(`config.load_secrets`: .env + 환경변수)와 같고, env=True 인
    칸은 환경변수가 우선이라 여기서 바꿔도 적용되지 않는다. uses 는 계좌가 쓰는 키."""
    from trader.config import load_secrets

    secrets = load_secrets(path)
    entries = catalog(accounts)
    overridden = set(_env_overrides([e["name"] for e in entries]))
    keys = [
        {**e, "set": secrets.get(e["name"]) is not None, "env": e["name"] in overridden}
        for e in entries
    ]
    uses = []
    for a in accounts:
        c = secrets.kis_creds(a)
        kind = "없음" if not c.app_key else ("공용" if c.shared else "전용")
        uses.append({"id": a.id, "env": a.env, "uses": kind, "prefix": c.prefix})
    return {"keys": keys, "accounts": uses}


def update(
    path: Path,
    accounts: list[AccountConfig],
    *,
    values: dict[str, str] | None = None,
    clear: list[str] | None = None,
) -> dict:
    """설정 화면 입력을 .env 에 쓴다 — 목록(`catalog`)에 있는 이름만, 한 줄 값만. 반환은 이름만."""
    allowed = {e["name"] for e in catalog(accounts)}
    values = {k: (v or "").strip() for k, v in (values or {}).items()}
    clear_set = set(clear or [])
    bad = sorted((set(values) | clear_set) - allowed)
    if bad:
        raise ConfigError(f"입력할 수 없는 키: {', '.join(bad)}")
    for k, v in values.items():
        if not v:
            raise ConfigError(f"{k}: 빈 값은 '지우기'로 보내세요")
        if "${" in v:
            raise ConfigError(f"{k}: ${{…}} 는 다른 키 값으로 바뀌므로 넣을 수 없습니다")
        if (
            len(v) > MAX_VALUE_LEN
            or any(ch.isspace() or ch == "\x00" for ch in v)
            or v[0] in "'\"#"
        ):
            raise ConfigError(f"{k}: 공백 없는 한 줄 값만 넣을 수 있습니다(따옴표·# 로 시작 불가)")
    clear_set -= set(values)
    if not values and not clear_set:
        return {"updated": [], "cleared": [], "backup": None, "env_overridden": []}
    with _editing(path):
        backup = _backup(path)
        _write(path, values, remove=clear_set)
    return {
        "updated": sorted(values),
        "cleared": sorted(clear_set),
        "backup": backup,
        # 환경변수가 덮어쓰는 이름 — 파일은 바뀌었지만 재시작해도 환경변수 값이 쓰인다
        "env_overridden": _env_overrides(sorted(set(values) | clear_set)),
    }


def ensure_web_token(path: Path) -> tuple[str, bool]:
    """WEB_TOKEN 이 없으면 만들어 .env 에 넣는다(첫 실행에 제어 API 가 통째로 닫히지 않게).
    (토큰, 새로 만들었는가). 값은 반환만 하고 로그에 남기지 않는다."""
    import secrets as _secrets

    env = (os.environ.get("WEB_TOKEN") or "").strip()
    if env:
        return env, False
    with _editing(path):
        current = _read(path).get("WEB_TOKEN")
        if current:
            return current, False
        token = _secrets.token_urlsafe(24)
        _write(path, {"WEB_TOKEN": token})
    return token, True
