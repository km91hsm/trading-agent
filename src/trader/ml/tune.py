"""퀀트 모델 하이퍼파라미터 탐색 (`trader ml tune`).

워크포워드 폴드의 **마지막 폴드를 홀드아웃**으로 떼고, 나머지(dev) 폴드의 alpha_topn 평균이 가장 큰
후보를 고른다. 홀드아웃은 기본값·최선 후보 두 번만 평가해 보고한다 — 선택에 쓰지 않는다.
자동 적용은 없다: 리포트(`state/models/quant_tune/<ts>.json`)를 보고 사용자가 `ml.yaml`
`models.quant.{learning_rate, num_leaves, max_iter}` 에 반영한다.
"""

from __future__ import annotations

import itertools
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from trader.config import ConfigError, MlConfig
from trader.ml import registry, walkforward
from trader.ml.models import quant
from trader.ml.train import MIN_ROWS, prepare_dataset

logger = logging.getLogger("trader.ml")

GRIDS: dict[str, dict[str, list]] = {
    "small": {
        "learning_rate": [0.03, 0.05, 0.1],
        "num_leaves": [7, 15, 31],
        "max_iter": [100, 200],
    },
    "wide": {
        "learning_rate": [0.01, 0.03, 0.05, 0.1],
        "num_leaves": [7, 15, 31, 63],
        "max_iter": [100, 200, 400],
    },
}
PARAM_KEYS = ("learning_rate", "num_leaves", "max_iter")


def _with_params(cfg: MlConfig, params: dict) -> MlConfig:
    q = cfg.models.quant.model_validate({**cfg.models.quant.model_dump(), **params})
    return cfg.model_copy(update={"models": cfg.models.model_copy(update={"quant": q})})


def _dev_summary(m: dict) -> dict:
    folds = m.get("folds") or []
    pos = sum(1 for f in folds if f.get("alpha_topn", 0.0) > 0)
    return {
        "alpha_topn": m.get("alpha_topn", 0.0),
        "alpha": m.get("alpha", 0.0),
        "accuracy": m.get("accuracy", 0.0),
        "positive_folds": round(pos / len(folds), 4) if folds else 0.0,
        "n_folds": len(folds),
    }


def tune_quant(
    cfg: MlConfig,
    *,
    state: Path | None = None,
    market: str | None = None,
    min_rows: int = MIN_ROWS,
    grid: str = "small",
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict:
    """반환 status(tuned|insufficient_data|insufficient_folds)·candidates·best·holdout·saved."""
    if grid not in GRIDS:
        raise ConfigError(f"알 수 없는 그리드: {grid} (small|wide)")
    df, cols, reason = prepare_dataset(cfg, state=state, market=market, now=now, min_rows=min_rows)
    base = {"status": "insufficient_data", "rows": int(len(df)), "reason": reason, "applied": False}
    if reason is not None:
        return base
    days = sorted(pd.to_datetime(df["date"]).dt.date.unique())
    fs = walkforward.folds(
        days,
        n_folds=cfg.train.walk_forward_folds,
        horizon_days=cfg.label.horizon_days,
        embargo_days=cfg.label.embargo_days,
    )
    n = len(fs)
    if n < 2:
        return base | {
            "status": "insufficient_folds",
            "reason": f"폴드 {n}개 (최소 2)",
            "n_folds": n,
        }
    dev = [f.index for f in fs[:-1]]
    hold = [fs[-1].index]
    default_params = {k: getattr(cfg.models.quant, k) for k in PARAM_KEYS}
    candidates = []
    for values in itertools.product(*(GRIDS[grid][k] for k in PARAM_KEYS)):
        params = dict(zip(PARAM_KEYS, values, strict=True))
        m = quant.evaluate_walkforward(
            df, _with_params(cfg, params), features=cols, fold_indices=dev
        )
        candidates.append({"params": params, "dev": _dev_summary(m)})
    evaluated = min(c["dev"]["n_folds"] for c in candidates)
    if evaluated < 1:
        # 폴드가 '생성' 돼도 학습창이 비면 평가되지 않아 전부 0 으로 동률
        # → 첫 후보를 best 로 보고하면 안 된다
        return base | {
            "status": "insufficient_folds",
            "reason": "dev 폴드가 하나도 평가되지 않음 (학습 행 부족)",
            "n_folds": n,
        }
    # 동률이면 기본값(설정 파일 값)을 우선한다 — 바꿀 근거가 없다
    best = max(
        candidates,
        key=lambda c: (
            c["dev"]["alpha_topn"],
            c["dev"]["accuracy"],
            c["params"] == default_params,
        ),
    )
    holdout = {
        "default": {
            "params": default_params,
            **_dev_summary(quant.evaluate_walkforward(df, cfg, features=cols, fold_indices=hold)),
        },
        "best": {
            "params": best["params"],
            **_dev_summary(
                quant.evaluate_walkforward(
                    df, _with_params(cfg, best["params"]), features=cols, fold_indices=hold
                )
            ),
        },
    }
    min_alpha = cfg.models.quant.promote_min_alpha
    hb, hd = holdout["best"]["alpha_topn"], holdout["default"]["alpha_topn"]
    if best["params"] == default_params:
        rec = {"apply": False, "reason": "최선 후보가 현재 설정과 같음"}
    elif hb < min_alpha or hb <= hd:
        rec = {
            "apply": False,
            "reason": (
                f"홀드아웃 alpha_topn {hb:.4f} 가 승격 기준 {min_alpha} 미만이거나 "
                f"기본값 {hd:.4f} 이하 — dev 선택은 후보 중 최대값이라 위로 치우치므로 "
                "홀드아웃이 확인해야 한다"
            ),
        }
    else:
        rec = {
            "apply": True,
            "reason": f"홀드아웃 alpha_topn {hb:.4f} ≥ {min_alpha} 이고 기본값 {hd:.4f} 초과",
        }
    report = {
        "status": "tuned",
        "market": market,
        "grid": grid,
        "rows": int(len(df)),
        "n_folds": n,
        "dev_folds": dev,
        "holdout_fold": hold[0],
        "candidates": candidates,
        "best": best,
        "holdout": holdout,
        "applied": False,
        "recommendation": rec,
        "note": (
            "자동 적용 없음 — recommendation.apply 가 true 일 때만 ml.yaml "
            "models.quant.{learning_rate,num_leaves,max_iter} 에 반영. 적용 뒤 ml train 의 "
            "승격 지표는 dev 폴드를 다시 쓰므로 낙관적이다"
        ),
    }
    d = registry._dir("quant_tune", state)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{now().astimezone(UTC).strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    logger.info(
        "quant tune %s: best=%s dev alpha_topn=%.4f",
        path.name,
        best["params"],
        best["dev"]["alpha_topn"],
    )
    return report | {"saved": str(path)}
