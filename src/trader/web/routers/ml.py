"""ml: 모델 레지스트리 자리 (Phase 7 에서 실데이터)."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/ml", tags=["ml"])


@router.get("/models")
def models() -> dict:
    return {"models": [], "note": "ML 레지스트리는 Phase 7 에서 채운다"}
