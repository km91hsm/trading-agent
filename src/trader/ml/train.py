"""학습 파이프라인 (스펙 §9.1·9.5): 적재 → 라벨 → 워크포워드 평가 → 최종 적합 → 저장 → 승격 판정."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from trader.config import MlConfig
from trader.ml import dataset, registry, walkforward
from trader.ml.models import quant

logger = logging.getLogger("trader.ml")

MIN_ROWS = 100
NAME = "quant"


def train_quant(
    cfg: MlConfig,
    *,
    state: Path | None = None,
    market: str | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    min_rows: int = MIN_ROWS,
) -> dict:
    """반환: status(trained|insufficient_data)·version·metrics·promoted·promote_reason·rows."""
    df = dataset.load_all(state, market=market)
    base = {
        "status": "insufficient_data",
        "version": None,
        "metrics": None,
        "promoted": False,
        "promote_reason": None,
        "rows": int(len(df)),
        "features": [],
    }
    if df.empty:
        return base | {"promote_reason": "피처 스토어가 비어 있음"}
    df = dataset.make_labels(
        df,
        horizon_days=cfg.label.horizon_days,
        up=cfg.label.thresholds.up,
        down=cfg.label.thresholds.down,
    )
    labeled = df[df["label"].notna()]
    cols = dataset.feature_columns(df)
    if len(labeled) < min_rows or labeled["label"].nunique() < 2 or not cols:
        return base | {
            "rows": int(len(labeled)),
            "promote_reason": f"데이터 부족: 라벨 행 {len(labeled)} (최소 {min_rows}), "
            f"라벨 종류 {labeled['label'].nunique()}",
        }
    metrics = quant.evaluate_walkforward(df, cfg, features=cols)
    X, y = dataset.split_xy(df, cols)
    model = quant.QuantModel(cols).fit(X, y)
    days = (
        sorted(labeled["date"].dt.date.unique())
        if hasattr(labeled["date"], "dt")
        else sorted(labeled["date"].unique())
    )
    fs = walkforward.folds(
        days,
        n_folds=cfg.train.walk_forward_folds,
        horizon_days=cfg.label.horizon_days,
        embargo_days=cfg.label.embargo_days,
    )
    importances: dict[str, float] = {}
    if fs:
        _, test = walkforward.split(df, fs[-1])
        if not test.empty:
            Xt, yt = dataset.split_xy(test, cols)
            try:
                importances = model.importances(Xt, yt)
            except Exception as e:  # noqa: BLE001 - 중요도 계산 실패는 메타만 비운다
                logger.warning("피처 중요도 계산 실패: %s", e)
    q = cfg.models.quant
    ok_acc = metrics["accuracy"] >= q.promote_min_accuracy
    ok_alpha = metrics["alpha"] >= q.promote_min_alpha
    promoted = bool(metrics["folds"]) and ok_acc and ok_alpha
    reason = (
        f"정확도 {metrics['accuracy']:.3f} ≥ {q.promote_min_accuracy} 및 알파 "
        f"{metrics['alpha']:.4f} ≥ {q.promote_min_alpha}"
        if promoted
        else f"정확도 {metrics['accuracy']:.3f} (기준 {q.promote_min_accuracy}) / 알파 "
        f"{metrics['alpha']:.4f} (기준 {q.promote_min_alpha}) 미달"
    )
    meta = {
        "market": market,
        "rows": int(len(labeled)),
        "train_range": [str(days[0]), str(days[-1])] if days else None,
        "features": cols,
        "metrics": metrics,
        "importances": importances,
        "backend": model.backend,
        "label": cfg.label.model_dump(),
    }
    version = registry.save(NAME, model, meta, state=state, now=now)
    if promoted:
        registry.promote(NAME, version, state=state, reason=reason)
    logger.info("quant 학습 %s: 정확도 %.3f, 승격=%s", version, metrics["accuracy"], promoted)
    return {
        "status": "trained",
        "version": version,
        "metrics": metrics,
        "promoted": promoted,
        "promote_reason": reason,
        "rows": int(len(labeled)),
        "features": cols,
        "backend": model.backend,
    }
