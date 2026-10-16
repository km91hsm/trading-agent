"""ml: 모델 레지스트리 조회 (목록은 요약, 상세는 메타 전체)."""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Request

from trader.ml import registry

router = APIRouter(prefix="/ml", tags=["ml"])
_VERSION_RE = re.compile(r"^\d{8}_\d{6}$")
SUMMARY_KEYS = (
    "name",
    "version",
    "saved_at",
    "promoted",
    "promote_reason",
    "promoted_at",
    "market",
    "rows",
    "train_range",
    "backend",
    "metrics",
)


def _summary(meta: dict) -> dict:
    out = {k: meta.get(k) for k in SUMMARY_KEYS}
    m = meta.get("metrics") or {}
    out["metrics"] = {k: m.get(k) for k in ("accuracy", "baseline", "alpha", "n_test")}
    return out


@router.get("/models")
def models(request: Request) -> dict:
    deps = request.app.state.deps
    metas = registry.list_models("quant", deps.state)
    active = next((m for m in metas if m.get("promoted")), None)
    return {
        "models": [_summary(m) for m in metas],
        "active": _summary(active) if active else None,
        "note": "승격(promoted)된 최신 모델만 05_quant·scan 랭킹에 쓰인다",
    }


@router.get("/models/{version}")
def model_detail(request: Request, version: str) -> dict:
    if not _VERSION_RE.match(version):
        raise HTTPException(status_code=400, detail="버전 형식 오류")
    deps = request.app.state.deps
    for m in registry.list_models("quant", deps.state):
        if m.get("version") == version:
            return m
    raise HTTPException(status_code=404, detail=f"모델 없음: {version}")
