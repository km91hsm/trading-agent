"""학습 파이프라인 (스펙 §9.1·9.5): 적재 → 라벨 → 워크포워드 평가 → 최종 적합 → 저장 → 승격 판정."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from trader.config import MlConfig
from trader.ml import dataset, registry, walkforward
from trader.ml.models import quant

logger = logging.getLogger("trader.ml")

MIN_ROWS = 100
NAME = "quant"


def promotion_decision(metrics: dict, cfg: MlConfig) -> tuple[bool, str]:
    """승격 조건: 폴드가 있고, 합산 정확도 > 기준선(최빈 라벨) 이면서 ≥ promote_min_accuracy,
    알파 > promote_min_alpha(엄격), 모든 폴드의 up 예측 건수 ≥ min_up_predictions."""
    q = cfg.models.quant
    folds = metrics.get("folds") or []
    acc, base, alpha = (
        metrics.get("accuracy", 0.0),
        metrics.get("baseline", 0.0),
        metrics.get("alpha", 0.0),
    )
    fails: list[str] = []
    if not folds:
        fails.append("평가 폴드 없음")
    if acc <= base:
        fails.append(f"정확도 {acc:.3f} ≤ 기준선 {base:.3f}")
    if acc < q.promote_min_accuracy:
        fails.append(f"정확도 {acc:.3f} < {q.promote_min_accuracy}")
    if alpha <= q.promote_min_alpha:
        fails.append(f"알파 {alpha:.4f} ≤ {q.promote_min_alpha}")
    low = [f["index"] for f in folds if int(f.get("up_count", 0)) < q.min_up_predictions]
    if low:
        fails.append(f"up 예측 {q.min_up_predictions}건 미만 폴드 {low}")
    if fails:
        return False, "미승격: " + "; ".join(fails)
    return True, (
        f"승격: 정확도 {acc:.3f} > 기준선 {base:.3f} (≥ {q.promote_min_accuracy}), "
        f"알파 {alpha:.4f} > {q.promote_min_alpha}, 모든 폴드 up 예측 ≥ {q.min_up_predictions}"
    )


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
    cutoff = (now().astimezone(UTC) - timedelta(days=365 * cfg.train.window_years)).date()
    df = df[df["date"].dt.date >= cutoff].reset_index(drop=True)
    if df.empty:
        return base | {
            "rows": 0,
            "promote_reason": f"학습 창({cfg.train.window_years}년) 안에 행 없음",
        }
    df = dataset.make_labels(
        df,
        horizon_days=cfg.label.horizon_days,
        up=cfg.label.thresholds.up,
        down=cfg.label.thresholds.down,
        mode=cfg.label.mode,
    )
    labeled = df[df["label"].notna()]
    cols = dataset.feature_columns(df)
    if len(labeled) < min_rows or labeled["label"].nunique() < 2 or not cols:
        return base | {
            "rows": int(len(labeled)),
            "promote_reason": f"데이터 부족: 라벨 행 {len(labeled)} (최소 {min_rows}), "
            f"라벨 종류 {labeled['label'].nunique()}",
        }
    backend = cfg.models.quant.backend
    max_iter = cfg.models.quant.max_iter
    metrics = quant.evaluate_walkforward(df, cfg, features=cols)
    # 피처 중요도는 마지막 폴드의 홀드아웃(그 폴드까지만 학습한 모델)로 계산한다
    days = sorted(df["date"].dt.date.unique())
    fs = walkforward.folds(
        days,
        n_folds=cfg.train.walk_forward_folds,
        horizon_days=cfg.label.horizon_days,
        embargo_days=cfg.label.embargo_days,
    )
    importances: dict[str, float] = {}
    if fs:
        train_df, test_df = walkforward.split(df, fs[-1])
        if not train_df.empty and not test_df.empty and train_df["label"].nunique() >= 2:
            try:
                Xa, ya = dataset.split_xy(train_df, cols)
                Xt, yt = dataset.split_xy(test_df, cols)
                importances = (
                    quant.QuantModel(cols, backend=backend, max_iter=max_iter)
                    .fit(Xa, ya)
                    .importances(Xt, yt)
                )
            except Exception as e:  # noqa: BLE001 - 중요도 계산 실패는 메타만 비운다
                logger.warning("피처 중요도 계산 실패: %s", e)
    X, y = dataset.split_xy(df, cols)
    model = quant.QuantModel(cols, backend=backend, max_iter=max_iter).fit(X, y)
    promoted, reason = promotion_decision(metrics, cfg)
    meta = {
        "market": market,
        "rows": int(len(labeled)),
        "train_range": [str(days[0]), str(days[-1])] if days else None,
        "window_years": cfg.train.window_years,
        "features": cols,
        "metrics": metrics,
        "importances": importances,
        "importances_scope": "last_fold_holdout",
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
