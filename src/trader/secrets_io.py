"""다른 프로젝트의 .env 에서 이 저장소가 쓰는 키만 이름 매핑으로 secrets/.env 에 옮긴다.

값은 로그·stdout·반환값 어디에도 넣지 않는다 (키 이름만). 기본은 비어 있는 키만 채우고,
`overwrite=True` 면 (keys 로 고른) 키를 바꾼다. 기존 줄·주석은 그대로 두고 새 키는 끝에 붙인다.
"""

from __future__ import annotations

import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

from dotenv import dotenv_values

from trader.config import ConfigError, Secrets

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
        if dst.exists():
            # 되돌릴 수 있게 바꾸기 전 파일을 남긴다 (파일명에 ':' 금지 → YYYYMMDD_HHMMSS)
            stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
            bak = dst.with_name(f"{dst.name}.bak-{stamp}")
            shutil.copy2(dst, bak)
            backup = bak.name
        _write(dst, {k: mapped[k] for k in copied})
    return {
        "copied": copied,
        "kept": kept,
        "same": same,  # 유지했지만 원본과 값이 같은 키
        "differs": differs,  # 유지했지만 원본과 값이 다른 키 (--overwrite 판단용, 값은 내지 않음)
        "unavailable": unavailable,
        "backup": backup,
    }


def _write(dst: Path, values: dict[str, str]) -> None:
    """있는 줄은 자리에서 바꾸고 없는 키는 끝에 붙인다 (주석·순서 보존). 파일 권한은 0600."""
    lines = dst.read_text(encoding="utf-8").splitlines() if dst.exists() else []
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        is_kv = "=" in line and not line.lstrip().startswith("#")
        key = line.split("=", 1)[0].strip() if is_kv else None
        if key in values:
            out.append(f"{key}={values[key]}")
            seen.add(key)
        else:
            out.append(line)
    for k, v in values.items():
        if k not in seen:
            out.append(f"{k}={v}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8")
    try:
        os.chmod(dst, 0o600)
    except OSError:
        pass
