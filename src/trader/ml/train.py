"""학습 파이프라인 (스펙 §9.1·9.5): 적재 → 라벨 → 워크포워드 평가 → 최종 적합 → 저장 → 승격 판정."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

from trader.config import MlConfig
from trader.ml import dataset, registry, walkforward
from trader.ml.models import quant

logger = logging.getLogger("trader.ml")

MIN_ROWS = 100
NAME = "quant"


def promotion_decision(
    metrics: dict, cfg: MlConfig, *, market: str | None = None
) -> tuple[bool, str]:
    """승격 판정. 공통: 폴드가 있고 모든 폴드의 up 예측 ≥ min_up_predictions("전부 중립" 모델 차단).
    - accuracy 모드: 합산 정확도 > 기준선(최빈 라벨) ∧ ≥ promote_min_accuracy ∧ 알파 > min_alpha.
    - alpha 모드(scan 랭커 용도): 평균 알파 ≥ min_alpha ∧ 알파 > 0 인 폴드 비율 ≥
      promote_min_positive_folds. 정확도는 보지 않는다.
    min_alpha 는 시장별(`promote_min_alpha_by_market`, 왕복비용 차이) 값이 있으면 그것, 없으면
    `promote_min_alpha`."""
    q = cfg.models.quant
    min_alpha = q.min_alpha_for(market)
    folds = metrics.get("folds") or []
    acc, base, alpha = (
        metrics.get("accuracy", 0.0),
        metrics.get("baseline", 0.0),
        metrics.get("alpha", 0.0),
    )
    fails: list[str] = []
    if not folds:
        fails.append("평가 폴드 없음")
    if q.promote_metric == "alpha":
        # 랭커가 실제로 쓰는 "상위 N 알파"(alpha_topn). 옛 메타엔 없을 수 있어 풀링 알파로 대신한다
        alpha = float(metrics.get("alpha_topn", alpha))
        pos = sum(1 for f in folds if float(f.get("alpha_topn", f.get("alpha", 0.0))) > 0)
        frac = pos / len(folds) if folds else 0.0
        if alpha < min_alpha:
            fails.append(f"상위 {q.promote_top_n} 평균 알파 {alpha:.4f} < {min_alpha}")
        if frac < q.promote_min_positive_folds:
            fails.append(
                f"상위 알파 양인 폴드 {pos}/{len(folds)} ({frac:.2f}) "
                f"< {q.promote_min_positive_folds}"
            )
    else:
        if acc <= base:
            fails.append(f"정확도 {acc:.3f} ≤ 기준선 {base:.3f}")
        if acc < q.promote_min_accuracy:
            fails.append(f"정확도 {acc:.3f} < {q.promote_min_accuracy}")
        if alpha <= min_alpha:
            fails.append(f"알파 {alpha:.4f} ≤ {min_alpha}")
    low = [f["index"] for f in folds if int(f.get("up_count", 0)) < q.min_up_predictions]
    if low:
        fails.append(f"up 예측 {q.min_up_predictions}건 미만 폴드 {low}")
    if fails:
        return False, "미승격: " + "; ".join(fails)
    if q.promote_metric == "alpha":
        return True, (
            f"승격(상위 {q.promote_top_n} 알파): 평균 {alpha:.4f} ≥ {min_alpha}, "
            "양인 폴드 "
            f"{pos}/{len(folds)}, 모든 폴드 up 예측 ≥ {q.min_up_predictions}"
        )
    return True, (
        f"승격: 정확도 {acc:.3f} > 기준선 {base:.3f} (≥ {q.promote_min_accuracy}), "
        f"알파 {alpha:.4f} > {min_alpha}, 모든 폴드 up 예측 ≥ {q.min_up_predictions}"
    )


def prepare_dataset(
    cfg: MlConfig,
    *,
    state: Path | None,
    market: str | None,
    now: Callable[[], datetime],
    min_rows: int,
) -> tuple[pd.DataFrame, list[str], str | None]:
    """학습 창 안의 피처 행에 라벨을 붙인다. 반환 (df, 피처 열, 부족 사유|None) — 사유가 있으면
    df 는 그 시점까지의 행(개수 보고용)이고 학습하지 않는다. train·tune 이 같은 데이터로 돈다."""
    df = dataset.load_all(state, market=market)
    if df.empty:
        return df, [], "피처 스토어가 비어 있음"
    cutoff = (now().astimezone(UTC) - timedelta(days=365 * cfg.train.window_years)).date()
    df = df[df["date"].dt.date >= cutoff].reset_index(drop=True)
    if df.empty:
        return df, [], f"학습 창({cfg.train.window_years}년) 안에 행 없음"
    df = dataset.make_labels(
        df,
        horizon_days=cfg.label.horizon_days,
        up=cfg.label.thresholds.up,
        down=cfg.label.thresholds.down,
        mode=cfg.label.mode,
    )
    # 라벨은 종목의 연속 봉으로 먼저 계산하고(행을 먼저 빼면 앞으로의 수익이 틀어진다) 그다음 그날
    # 유니버스 구성원이 아니었던 행을 학습에서 뺀다(시점 기준 유니버스, Stock D-037)
    df, pit_info = _apply_pit(df, cfg, state)
    labeled = df[df["label"].notna()]
    cols = dataset.feature_columns(df)
    if len(labeled) < min_rows or labeled["label"].nunique() < 2 or not cols:
        reason = (
            f"데이터 부족: 라벨 행 {len(labeled)} (최소 {min_rows}), "
            f"라벨 종류 {labeled['label'].nunique()}"
        )
        return labeled, cols, reason
    df.attrs["pit"] = pit_info
    return df, cols, None


def _apply_pit(df: pd.DataFrame, cfg: MlConfig, state: Path | None) -> tuple[pd.DataFrame, dict]:
    import sqlite3

    from trader.ledger import db
    from trader.ml import universe_pit

    try:
        conn = db.connect((state / "trader.db") if state is not None else None)
    except (sqlite3.Error, OSError) as e:
        logger.warning("시점 기준 유니버스 기록을 열 수 없어 걸러내지 않음: %s", e)
        return df, {"error": str(e)}
    try:
        return universe_pit.apply(df, conn, cfg.train.pit)
    except sqlite3.OperationalError as e:  # 마이그레이션 전 원장(표 없음)
        logger.warning("universe_members 없음 — 걸러내지 않음: %s", e)
        return df, {"error": str(e)}
    finally:
        conn.close()


# Stock D-037 교훈: "지금 구성 종목" 으로 고른 과거는 상장폐지·편출 종목이 빠져 알파가 부풀려진다
# (Stock 은 연 +6% 가 선택 편향이었음을 재측정에서 확인). 시점 기준 유니버스 전까지는 경고로 남긴다
SURVIVORSHIP_CAVEAT = (
    "생존 편향: 학습 종목이 현재 universe.yaml 구성(오늘의 KOSPI 200 등)이라 과거에 빠진·상장폐지 "
    "종목이 없다 — 워크포워드 알파가 실제보다 클 수 있다. 승격 판단은 섀도 포트폴리오"
    "(지수 기준선)와 함께 본다"
)


def _caveats(pit: dict | None) -> list[str]:
    """시점 기준 기록이 학습 행을 전부 덮으면 생존 편향 경고를 빼고, 아니면 덮은 비율을 붙인다."""
    cov = (pit or {}).get("coverage")
    if cov is not None and cov >= 1.0:
        return []
    note = f" (시점 기준 기록이 덮은 학습 행 {cov:.0%})" if cov else " (시점 기준 기록 없음)"
    return [SURVIVORSHIP_CAVEAT + note]


def train_quant(
    cfg: MlConfig,
    *,
    state: Path | None = None,
    market: str | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    min_rows: int = MIN_ROWS,
) -> dict:
    """반환: status(trained|insufficient_data)·version·metrics·promoted·promote_reason·rows."""
    df, cols, reason = prepare_dataset(cfg, state=state, market=market, now=now, min_rows=min_rows)
    if reason is not None:
        return {
            "status": "insufficient_data",
            "version": None,
            "metrics": None,
            "promoted": False,
            "promote_reason": reason,
            "rows": int(len(df)),
            "features": [],
        }
    labeled = df[df["label"].notna()]
    kwargs = quant.model_kwargs(cfg)
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
                importances = quant.QuantModel(cols, **kwargs).fit(Xa, ya).importances(Xt, yt)
            except Exception as e:  # noqa: BLE001 - 중요도 계산 실패는 메타만 비운다
                logger.warning("피처 중요도 계산 실패: %s", e)
    X, y = dataset.split_xy(df, cols)
    model = quant.QuantModel(cols, **kwargs).fit(X, y)
    promoted, reason = promotion_decision(metrics, cfg, market=market)
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
        "params": {k: v for k, v in kwargs.items() if k != "backend"},
        "label": cfg.label.model_dump(),
        "caveats": _caveats(df.attrs.get("pit")),
        "pit": df.attrs.get("pit"),
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
        "caveats": _caveats(df.attrs.get("pit")),
        "pit": df.attrs.get("pit"),
    }
