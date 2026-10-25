"""모델 레지스트리 (스펙 §9.5): state/models/<name>/<YYYYMMDD_HHMMSS>/{model.joblib, meta.json}.

승격(promoted) 은 메타에 기록하며, 파이프라인은 승격된 최신 버전만 쓴다.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import joblib

from trader.platform import paths

logger = logging.getLogger("trader.ml")
META = "meta.json"
MODEL = "model.joblib"


def _dir(name: str, state: Path | None) -> Path:
    base = state if state is not None else paths.state_dir()
    return base / "models" / name


def save(
    name: str,
    model: Any,
    meta: dict,
    *,
    state: Path | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> str:
    version = now().astimezone(UTC).strftime("%Y%m%d_%H%M%S")
    d = _dir(name, state) / version
    d.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, d / MODEL)
    full = {
        "name": name,
        "version": version,
        "saved_at": now().astimezone(UTC).isoformat(),
        "promoted": False,
        "promote_reason": None,
        **meta,
    }
    (d / META).write_text(
        json.dumps(full, ensure_ascii=False, indent=1, default=str), encoding="utf-8"
    )
    return version


def list_models(name: str, state: Path | None = None) -> list[dict]:
    """메타 목록, 최신 버전 먼저."""
    d = _dir(name, state)
    if not d.exists():
        return []
    out = []
    for v in sorted((p for p in d.iterdir() if p.is_dir()), reverse=True):
        try:
            out.append(json.loads((v / META).read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


_LOAD_CACHE: dict[tuple[str, str, float], tuple[Any, dict]] = {}


def load(name: str, version: str, state: Path | None = None) -> tuple[Any, dict]:
    """모델 파일을 읽는다. 같은 (경로, 메타 mtime) 은 메모해 scan·성과 페이지가 호출마다 joblib 을
    다시 풀지 않는다(승격·해제는 메타 파일을 바꿔 mtime 이 달라진다)."""
    d = _dir(name, state) / version
    key = (str(d), version, (d / META).stat().st_mtime)
    hit = _LOAD_CACHE.get(key)
    if hit is not None:
        return hit
    meta = json.loads((d / META).read_text(encoding="utf-8"))
    out = (joblib.load(d / MODEL), meta)
    _LOAD_CACHE.clear()  # 버전 하나만 기억한다(메모리)
    _LOAD_CACHE[key] = out
    return out


def latest(
    name: str,
    *,
    promoted_only: bool = True,
    state: Path | None = None,
    market: str | None = None,
) -> tuple[Any, dict] | None:
    """가장 최근(승격) 모델. market 이 있으면 그 시장으로 학습한 모델을 우선하고, 없으면 전시장
    모델(meta.market 없음)로 폴백하며 **다른 시장 한정 모델은 쓰지 않는다**(`train --market KR`
    모델이 US scan 에 들어가던 문제)."""
    metas = list_models(name, state)
    if market is not None:
        same = [m for m in metas if m.get("market") == market]
        any_market = [m for m in metas if not m.get("market")]
        metas = same + any_market
    for meta in metas:
        if promoted_only and not meta.get("promoted"):
            continue
        try:
            return load(name, meta["version"], state)
        except Exception as e:  # noqa: BLE001 - 깨진 pickle·클래스 변경 등은 건너뛴다 (파이프라인 보호)
            backend = meta.get("backend")
            hint = ""
            if backend == "lightgbm":
                hint = (
                    " — lightgbm 으로 학습한 모델입니다. 이 환경(번들 포함)에 lightgbm"
                    "(macOS 는 libomp)이 없으면 `ml demote <버전>` 하거나 backend sklearn 으로"
                    " 다시 학습하세요"
                )
            logger.warning(
                "모델 로드 실패 %s %s (backend=%s): %s%s",
                name,
                meta.get("version"),
                backend,
                e,
                hint,
            )
            continue
    return None


def demote(name: str, version: str, *, state: Path | None = None, reason: str = "manual") -> dict:
    """승격 해제. 이후 파이프라인은 다른 승격 버전이 없으면 ML 없이 돈다."""
    p = _dir(name, state) / version / META
    meta = json.loads(p.read_text(encoding="utf-8"))
    meta["promoted"] = False
    meta["promote_reason"] = f"해제: {reason}"
    meta["demoted_at"] = datetime.now(UTC).isoformat()
    p.write_text(json.dumps(meta, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return meta


def promote(name: str, version: str, *, state: Path | None = None, reason: str = "manual") -> dict:
    d = _dir(name, state) / version
    p = d / META
    meta = json.loads(p.read_text(encoding="utf-8"))
    meta["promoted"] = True
    meta["promote_reason"] = reason
    meta["promoted_at"] = datetime.now(UTC).isoformat()
    p.write_text(json.dumps(meta, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return meta
