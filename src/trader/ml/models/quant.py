"""퀀트 3분류 모델 (스펙 §9.1): 5거래일 수익률 up/flat/down.

백엔드: lightgbm 이 import 되면 LGBMClassifier, 아니면 scikit-learn HistGradientBoostingClassifier
(둘 다 NaN 입력 지원, random_state 고정). macOS 에서 lightgbm 은 libomp 가 있어야 로드된다.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

from trader.config import MlConfig
from trader.ml import dataset, walkforward

logger = logging.getLogger("trader.ml")

N_CLASSES = 3


def _lightgbm_available() -> bool:
    try:
        import lightgbm  # noqa: F401
    except Exception:  # noqa: BLE001 - ImportError 뿐 아니라 OSError(libomp 없음)도 폴백
        return False
    return True


def align(df: pd.DataFrame, features: list[str]) -> np.ndarray:
    """학습 때의 피처 순서로 맞춘다. 없는 열은 NaN, 여분 열은 무시."""
    cols = {}
    for c in features:
        cols[c] = pd.to_numeric(df[c], errors="coerce") if c in df.columns else np.nan
    return pd.DataFrame(cols, index=df.index).to_numpy(dtype="float64")


class QuantModel:
    def __init__(self, features: list[str], *, random_state: int = 0) -> None:
        for c in features:
            if c in dataset.TARGET_COLUMNS:
                raise ValueError(f"타깃 열이 피처에 섞였습니다: {c}")
        self.features = list(features)
        self.random_state = random_state
        if _lightgbm_available():
            from lightgbm import LGBMClassifier

            self.backend = "lightgbm"
            self._est: Any = LGBMClassifier(
                objective="multiclass",
                n_estimators=200,
                learning_rate=0.05,
                num_leaves=15,
                random_state=random_state,
                verbose=-1,
            )
        else:
            from sklearn.ensemble import HistGradientBoostingClassifier

            self.backend = "sklearn"
            self._est = HistGradientBoostingClassifier(
                max_iter=200, learning_rate=0.05, max_leaf_nodes=15, random_state=random_state
            )

    def fit(self, X: np.ndarray, y: np.ndarray) -> QuantModel:
        # 전부 NaN 인 열은 뺀다 (sklearn binning 이 빈 열에서 실패). 예측 때도 같은 마스크를 쓴다.
        self._keep = ~np.isnan(X).all(axis=0)
        if not self._keep.any():
            raise ValueError("학습 가능한 피처 열이 없습니다 (전부 NaN)")
        self._est.fit(X[:, self._keep], y)
        return self

    def _x(self, X: np.ndarray) -> np.ndarray:
        keep = getattr(self, "_keep", None)
        return X if keep is None else X[:, keep]

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        """(n, 3) down/flat/up. 학습에 없던 클래스는 0 으로 채운다."""
        raw = self._est.predict_proba(self._x(X))
        classes = list(getattr(self._est, "classes_", range(N_CLASSES)))
        out = np.zeros((raw.shape[0], N_CLASSES))
        for j, c in enumerate(classes):
            out[:, int(c)] = raw[:, j]
        return out

    def predict(self, X: np.ndarray) -> np.ndarray:
        return self.predict_proba(X).argmax(axis=1)

    def importances(self, X: np.ndarray, y: np.ndarray, *, n_repeats: int = 3) -> dict[str, float]:
        from sklearn.inspection import permutation_importance

        r = permutation_importance(
            self._est, self._x(X), y, n_repeats=n_repeats, random_state=self.random_state
        )
        kept = [f for f, k in zip(self.features, self._keep, strict=True) if k]
        out = dict.fromkeys(self.features, 0.0)
        out.update({f: float(v) for f, v in zip(kept, r.importances_mean, strict=True)})
        return out


def evaluate_walkforward(df: pd.DataFrame, cfg: MlConfig, *, features: list[str]) -> dict:
    """폴드별 정확도·기준선(최빈 라벨)·알파(예측 up 행의 평균 fwd_return − 전체 평균)."""
    days = sorted(pd.to_datetime(df["date"]).dt.date.unique())
    fs = walkforward.folds(
        days,
        n_folds=cfg.train.walk_forward_folds,
        horizon_days=cfg.label.horizon_days,
        embargo_days=cfg.label.embargo_days,
    )
    out: list[dict] = []
    correct = total = 0
    alphas: list[float] = []
    for f in fs:
        train, test = walkforward.split(df, f)
        if train.empty or test.empty or train["label"].nunique() < 2:
            continue
        Xtr, ytr = dataset.split_xy(train, features)
        Xte, yte = dataset.split_xy(test, features)
        model = QuantModel(features).fit(Xtr, ytr)
        pred = model.predict(Xte)
        acc = float((pred == yte).mean())
        baseline = float(np.bincount(yte, minlength=N_CLASSES).max() / len(yte))
        fwd = test.loc[test["label"].notna(), "fwd_return"].to_numpy(dtype="float64")
        up = pred == 2
        alpha = float(fwd[up].mean() - fwd.mean()) if up.any() else 0.0
        out.append(
            {
                "index": f.index,
                "train_rows": int(len(ytr)),
                "test_rows": int(len(yte)),
                "accuracy": round(acc, 4),
                "baseline": round(baseline, 4),
                "alpha": round(alpha, 6),
            }
        )
        correct += int((pred == yte).sum())
        total += len(yte)
        alphas.append(alpha)
    return {
        "folds": out,
        "accuracy": round(correct / total, 4) if total else 0.0,
        "baseline": round(float(np.mean([f["baseline"] for f in out])), 4) if out else 0.0,
        "alpha": round(float(np.mean(alphas)), 6) if alphas else 0.0,
        "n_test": total,
    }
