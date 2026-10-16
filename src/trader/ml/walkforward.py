"""워크포워드 비중첩 폴드 + 라벨 엠바고 (스펙 §9.1).

테스트 창은 마지막 n_folds 등분이고 서로 겹치지 않는다. 학습은 그 이전 전부(확장 창)이되
마지막 학습 날짜는 test_start 보다 (horizon_days + embargo_days + 1) 거래일 앞이어야 한다:
학습 행의 라벨이 쓰는 미래 봉(date + horizon)이 테스트 창에 들어가지 않도록.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

import pandas as pd


@dataclass(frozen=True)
class Fold:
    index: int
    train_end: date  # 학습 = date <= train_end
    test_start: date  # 테스트 = test_start <= date <= test_end
    test_end: date


def _unique_sorted(dates: Sequence) -> list[date]:
    out = sorted({pd.Timestamp(d).date() for d in dates})
    return out


def folds(
    dates: Sequence,
    *,
    n_folds: int,
    horizon_days: int,
    embargo_days: int,
    min_train: int = 60,
    min_test: int = 10,
) -> list[Fold]:
    """테스트 구간 = 마지막 (n − min_train − gap) 거래일을 n_folds 등분. 창이 min_test 보다
    짧아지면 폴드 수를 줄이고, 그래도 안 되면 빈 목록."""
    days = _unique_sorted(dates)
    n = len(days)
    gap = horizon_days + embargo_days
    usable = n - min_train - gap
    if usable < min_test:
        return []
    n_folds = max(1, min(n_folds, usable // min_test))
    size = usable // n_folds
    out: list[Fold] = []
    start_idx = n - size * n_folds
    for k in range(n_folds):
        ts = start_idx + k * size
        te = n - 1 if k == n_folds - 1 else ts + size - 1
        tr_end = ts - gap - 1
        if tr_end + 1 < min_train:
            continue
        out.append(
            Fold(index=len(out), train_end=days[tr_end], test_start=days[ts], test_end=days[te])
        )
    return out


def check_no_leak(
    train_dates: Sequence, test_start, *, horizon_days: int, embargo_days: int
) -> None:
    """학습 마지막 날짜 + (horizon + embargo) 거래일이 test_start 이전이 아니면 ValueError."""
    days = _unique_sorted(train_dates)
    if not days:
        return
    ts = pd.Timestamp(test_start).date()
    # 학습 날짜 집합에는 테스트 이후가 없으므로 달력일로 보수적으로 검사한다 (거래일 ≤ 달력일)
    last = days[-1]
    need = horizon_days + embargo_days
    # 거래일 간격: train 날짜 목록만으로는 이후 거래일을 모르므로 달력일 기준 1.0 배 이상 요구
    if (ts - last).days <= need:
        raise ValueError(
            f"라벨 누출: 학습 마지막 {last} 와 테스트 시작 {ts} 사이가 {need}거래일보다 짧습니다"
        )


def split(df: pd.DataFrame, fold: Fold) -> tuple[pd.DataFrame, pd.DataFrame]:
    """폴드로 학습·테스트 행을 나눈다. 학습은 라벨이 있는 행만."""
    d = pd.to_datetime(df["date"]).dt.date
    train = df[(d <= fold.train_end) & df["label"].notna()]
    test = df[(d >= fold.test_start) & (d <= fold.test_end) & df["label"].notna()]
    return train, test
