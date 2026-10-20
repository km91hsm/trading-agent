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
    def __init__(
        self,
        features: list[str],
        *,
        random_state: int = 0,
        backend: str = "sklearn",
        max_iter: int = 200,
        learning_rate: float = 0.05,
        num_leaves: int = 15,
    ) -> None:
        for c in features:
            if c in dataset.TARGET_COLUMNS:
                raise ValueError(f"타깃 열이 피처에 섞였습니다: {c}")
        self.features = list(features)
        self.random_state = random_state
        if backend not in ("auto", "sklearn", "lightgbm"):
            raise ValueError(f"알 수 없는 백엔드: {backend}")
        use_lgbm = backend == "lightgbm" or (backend == "auto" and _lightgbm_available())
        if backend == "lightgbm" and not _lightgbm_available():
            raise ValueError("lightgbm 을 불러올 수 없습니다 (macOS 는 libomp 필요)")
        if use_lgbm:
            from lightgbm import LGBMClassifier

            self.backend = "lightgbm"
            self._est: Any = LGBMClassifier(
                objective="multiclass",
                n_estimators=max_iter,
                learning_rate=learning_rate,
                num_leaves=num_leaves,
                random_state=random_state,
                verbose=-1,
            )
        else:
            from sklearn.ensemble import HistGradientBoostingClassifier

            self.backend = "sklearn"
            self._est = HistGradientBoostingClassifier(
                max_iter=max_iter,
                learning_rate=learning_rate,
                max_leaf_nodes=num_leaves,
                random_state=random_state,
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


def model_kwargs(cfg: MlConfig) -> dict[str, Any]:
    """설정의 하이퍼파라미터 → QuantModel 키워드 (train·evaluate·tune 이 같은 값을 쓴다)."""
    q = cfg.models.quant
    return {
        "backend": q.backend,
        "max_iter": q.max_iter,
        "learning_rate": q.learning_rate,
        "num_leaves": q.num_leaves,
    }


def evaluate_walkforward(
    df: pd.DataFrame,
    cfg: MlConfig,
    *,
    features: list[str],
    fold_indices: list[int] | None = None,
) -> dict:
    """폴드별 정확도·기준선(최빈 라벨)·알파·up 예측 건수. fold_indices 를 주면 그 폴드만
    (`ml tune` 의 dev/홀드아웃 분리; 폴드 번호는 전체 폴드 기준이라 결과의 index 가 같다).

    - alpha: 예측 up 행의 평균 fwd_return − 전체 평균 (풀링, 타이밍이 섞인다 — 참고용)
    - alpha_topn: 날짜·시장마다 p_up 상위 N(promote_top_n) 종목의 fwd_return 평균 − 그날 유니버스
      평균을 날짜 평균한 값. scan 랭커가 실제로 쓰는 방식이라 승격(alpha 모드)은 이 값을 본다.
    전체 정확도·기준선은 테스트 행 합산(pooled) 기준으로 같은 방식으로 집계한다.
    """
    days = sorted(pd.to_datetime(df["date"]).dt.date.unique())
    fs = walkforward.folds(
        days,
        n_folds=cfg.train.walk_forward_folds,
        horizon_days=cfg.label.horizon_days,
        embargo_days=cfg.label.embargo_days,
    )
    out: list[dict] = []
    correct = total = 0
    class_counts = np.zeros(N_CLASSES, dtype=int)
    alphas: list[float] = []
    alphas_topn: list[float] = []
    top_n = cfg.models.quant.promote_top_n
    kwargs = model_kwargs(cfg)
    if fold_indices is not None:
        wanted = set(fold_indices)
        fs = [f for f in fs if f.index in wanted]
    for f in fs:
        train, test = walkforward.split(df, f)
        if train.empty or test.empty or train["label"].nunique() < 2:
            continue
        Xtr, ytr = dataset.split_xy(train, features)
        Xte, yte = dataset.split_xy(test, features)
        model = QuantModel(features, **kwargs).fit(Xtr, ytr)
        pred = model.predict(Xte)
        acc = float((pred == yte).mean())
        baseline = float(np.bincount(yte, minlength=N_CLASSES).max() / len(yte))
        labeled = test[test["label"].notna()]
        fwd = labeled["fwd_return"].to_numpy(dtype="float64")
        up = pred == 2
        alpha = float(fwd[up].mean() - fwd.mean()) if up.any() else 0.0
        p_up = model.predict_proba(Xte)[:, 2]
        alpha_topn = _topn_alpha(labeled, p_up, top_n)
        out.append(
            {
                "index": f.index,
                "train_rows": int(len(ytr)),
                "test_rows": int(len(yte)),
                "accuracy": round(acc, 4),
                "baseline": round(baseline, 4),
                "alpha": round(alpha, 6),
                "alpha_topn": round(alpha_topn, 6),
                "up_count": int(up.sum()),
            }
        )
        correct += int((pred == yte).sum())
        total += len(yte)
        class_counts += np.bincount(yte, minlength=N_CLASSES)
        alphas.append(alpha)
        alphas_topn.append(alpha_topn)
    return {
        "folds": out,
        "accuracy": round(correct / total, 4) if total else 0.0,
        "baseline": round(float(class_counts.max() / total), 4) if total else 0.0,
        "alpha": round(float(np.mean(alphas)), 6) if alphas else 0.0,
        "alpha_topn": round(float(np.mean(alphas_topn)), 6) if alphas_topn else 0.0,
        "n_test": total,
    }


def _topn_alpha(labeled: pd.DataFrame, p_up: np.ndarray, top_n: int) -> float:
    """날짜·시장 그룹마다 p_up 상위 N 의 fwd_return 평균 − 그룹 평균을 그룹 평균.

    N 이하 그룹은 건너뜀.
    """
    d = pd.DataFrame(
        {
            "date": pd.to_datetime(labeled["date"]).to_numpy(),
            "market": labeled["market"].to_numpy() if "market" in labeled else "",
            "fwd": labeled["fwd_return"].to_numpy(dtype="float64"),
            "p": np.asarray(p_up, dtype="float64"),
        }
    )
    vals: list[float] = []
    for _, g in d.groupby(["date", "market"], sort=False):
        if len(g) <= top_n:
            continue
        top = g.nlargest(top_n, "p")["fwd"].mean()
        vals.append(float(top - g["fwd"].mean()))
    return float(np.mean(vals)) if vals else 0.0
