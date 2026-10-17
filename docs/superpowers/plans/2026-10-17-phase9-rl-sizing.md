# Phase 9 RL 사이징(실험) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 결정(LLM)·포지션·변동성을 상태로 받아 비중 0~0.25 를 제안하는 RL 사이저를 워크포워드(시드 3회)로 학습·평가하고, 모의 계좌에서만 섀도(기록)·적용하며, 승격은 수동으로만 한다.

**Architecture:** `ml/rl/` 패키지가 피처 스토어(또는 만기 메모리)로 스텝 시계열을 만들고(`dataset`), gymnasium 호환 환경(`env`)에서 정책(`policy`)을 평가한다. 학습기는 두 백엔드(`cem` numpy 기본, `ppo` stable-baselines3 선택 extra)로 같은 `Policy` 인터페이스를 만들고, `train` 이 워크포워드 폴드 × 시드로 고정 10%·LLM 비중·변동성 타깃 기준선과 비교해 레지스트리 `rl_sizer` 에 저장한다(자동 승격 없음). `shadow` 는 `executor.execute` 안에서 **모의 계좌·BUY/ADD** 에만 제안을 계산해 `rl_shadow` 원장에 남기고, `mode: apply` 일 때만 리스크 게이트 전 `target_weight` 를 바꾼다(한도는 `rules.evaluate` 가 그대로 강제).

**Tech Stack:** Python 3.12, numpy/pandas, 기존 `ml.walkforward`·`ml.registry`·`ledger.performance.metrics_of`·`memory.vector`, 선택 extra `rl`(gymnasium, stable-baselines3), FastAPI, React.

**Spec:** `docs/superpowers/specs/2026-10-09-trading-agent-design.md` §6.1(RL 사이저 제안은 한도 안에서만), §9.4, §9.5, §11 Phase 9, §12(FinRL: RL 비정상성·과최적화 → 모의 한정·수동 승격).

## Global Constraints

- 스펙 §9.4: 상태 = 결정·포지션·변동성, 행동 = 비중 0~0.25, PPO 워크포워드, 드로다운 페널티, 시드 3회, 모의 한정 섀도, 수동 승격.
- 스펙 §6.1: RL 사이저 제안은 리스크 한도 안에서만(게이트는 그대로 돈다). 실전 계좌는 어떤 경로로도 RL 의 영향을 받지 않는다(기록도 하지 않는다).
- 스펙 §9.5: 레지스트리 `state/models/rl_sizer/<YYYYMMDD_HHMMSS>/` + 메타. 승격 플래그는 `trader rl promote` 만 켠다.
- 스펙 §10.2: `pathlib`, 파일명 `:` 금지, UTC 저장, `uv`, 셸 스크립트 없음, Windows 휠 있는 라이브러리만(`stable-baselines3`·`torch` 는 **선택 extra**, 기본 설치 없이 전체 테스트가 돈다).
- CLAUDE.md: pydantic `extra="forbid"`(yaml 키 추가 시 모델 동반), 마이그레이션은 버전 추가만, 테스트는 `tmp_path`·네트워크 없음, 한국어 메시지, 로거 `trader.ml`, ruff 0건.
- 플랜 추가 조건(사용자): 섀도 모드는 모의 계좌 한정, 승격은 수동, 시드 3회.

## Review Focus

1. 실전 계좌(`env == "real"`)가 `mode: apply` 이거나 승격 모델이 있어도 `target_weight`·`rl_shadow` 모두 건드리지 않아야 한다 → Task 6 `test_real_account_untouched_even_in_apply_mode`.
2. 제안 비중이 `max_weight`(0.25) 와 `RiskLimits.max_position_pct` 를 넘어도 주문 수량은 게이트 상한을 넘지 않아야 한다 → Task 6 `test_apply_mode_still_capped_by_risk_gate`.
3. 피처 스토어가 비어 있거나 스텝이 `min_steps` 미만이면 학습은 `insufficient_data` 로 끝나고 레지스트리에 아무것도 쓰지 않아야 한다 → Task 5 `test_train_insufficient_data_saves_nothing`.
4. 승격 모델이 없거나 `enabled: false` 면 executor 경로가 전혀 달라지지 않아야 한다(쿼리·체크포인트·결과 동일) → Task 6 `test_disabled_or_no_model_is_noop`.
5. `fwd_return` 이 NaN 인 마지막 `horizon` 스텝은 환경에 들어가지 않아야 하며(미래 봉 없음), 폴드 학습·테스트 사이 엠바고가 지켜져야 한다 → Task 2 `test_steps_drop_rows_without_forward_return`, Task 5 `test_train_folds_respect_embargo`.

## 범위 조정

| 제외 | 이유 | 재개 |
|---|---|---|
| PPO 를 기본 백엔드로 | 이 머신(macOS x86_64 Python)은 최신 torch 휠이 없고 Windows 도 extra 설치가 필요 → `cem`(numpy) 기본, `ppo` 는 `uv sync --extra rl` 선택 | 사용자 확인 대기 |
| 멀티 종목 동시 포트폴리오 환경 | 스펙 상태는 "결정·포지션·변동성"(결정 1건 단위). 종목 1건씩 순차 스텝 환경으로 충분 | Phase 9 이후 |
| UI 실험 전용 페이지 | ML 페이지에 "RL 실험" 절 추가로 대체 | Phase 9 이후 |
| 8주 운영 비교 리포트의 실제 수치 | 데이터가 8주 쌓여야 함. 명령·API 는 만들고 수치는 사용자 확인 대기 | 사용자 확인 대기 |

---

### Task 1: 설정·의존성·원장 마이그레이션

**Files:**
- Modify: `src/trader/config.py` (`RlSizerCfg`)
- Modify: `config/ml.yaml`
- Modify: `pyproject.toml` (optional extra `rl`)
- Modify: `src/trader/ledger/db.py` (`MIGRATIONS` v7)
- Test: `tests/test_config_ml.py`(기존 ml 설정 테스트 파일이 있으면 거기), `tests/ledger/test_db.py`

**Interfaces:**
- Produces: `RlSizerCfg` 필드 `enabled: bool=False, seeds: int=3 (ge=1, le=10), mode: Literal["shadow","apply"]="shadow", backend: Literal["auto","cem","ppo"]="auto", max_weight: float=0.25, drawdown_penalty: float=1.0, cost_bp: int=30, total_timesteps: int=20000, cem_iterations: int=30, cem_population: int=64, min_memory_steps: int=200, min_steps: int=300, fixed_weight: float=0.10`.
- Produces: 테이블 `rl_shadow(id INTEGER PRIMARY KEY, decision_id INTEGER NOT NULL, account_id TEXT NOT NULL, created_at TEXT NOT NULL, model_version TEXT NOT NULL, obs TEXT NOT NULL, llm_weight REAL NOT NULL, proposed_weight REAL NOT NULL, applied INTEGER NOT NULL DEFAULT 0, UNIQUE(decision_id, account_id))`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_config_ml.py (없으면 새로)
def test_rl_sizer_defaults_and_mode(project):
    cfg = config.MlConfig()
    rl = cfg.models.rl_sizer
    assert rl.enabled is False and rl.seeds == 3 and rl.mode == "shadow"
    assert rl.backend == "auto" and rl.max_weight == 0.25 and rl.min_steps == 300
    with pytest.raises(ValidationError):
        config.MlConfig.model_validate({"models": {"rl_sizer": {"mode": "live"}}})
    assert config.load_ml().models.rl_sizer.mode == "shadow"   # config/ml.yaml 과 모델이 맞는다

# tests/ledger/test_db.py
def test_v7_creates_rl_shadow(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(rl_shadow)")}
    assert {"decision_id", "account_id", "model_version", "obs", "llm_weight",
            "proposed_weight", "applied"} <= cols
    conn.execute("INSERT INTO rl_shadow(decision_id, account_id, created_at, model_version, obs, "
                 "llm_weight, proposed_weight) VALUES (1,'paper-main','t','v','[]',0.1,0.2)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO rl_shadow(decision_id, account_id, created_at, model_version, obs, "
                     "llm_weight, proposed_weight) VALUES (1,'paper-main','t','v','[]',0.1,0.2)")
```

- [ ] **Step 2: Run to verify they fail** — `uv run pytest tests/test_config_ml.py tests/ledger/test_db.py -q` → FAIL (`mode` 속성 없음 / `rl_shadow` 테이블 없음).
- [ ] **Step 3: Implement** — `RlSizerCfg` 필드 추가; `config/ml.yaml` 의 `rl_sizer` 를 `{enabled: false, seeds: 3, mode: shadow, backend: auto}` 로; `pyproject.toml` `[project.optional-dependencies] rl = ["gymnasium>=1.0", "stable-baselines3>=2.3"]`; `MIGRATIONS` 에 `(7, ["CREATE TABLE IF NOT EXISTS rl_shadow (...)"])` 추가(기존 버전 수정 금지).
- [ ] **Step 4: Run** — 같은 명령 → PASS. `uv lock` 으로 lock 갱신(extra 만, 설치는 하지 않는다).
- [ ] **Step 5: Commit** — `feat(rl): rl_sizer 설정·선택 extra·rl_shadow 원장 v7`

---

### Task 2: RL 스텝 데이터셋

**Files:**
- Create: `src/trader/ml/rl/__init__.py`, `src/trader/ml/rl/dataset.py`
- Test: `tests/ml/test_rl_dataset.py`

**Interfaces:**
- Consumes: `memory.vector.DIMS`·`situation_vector(row, market)`, `ml.dataset.load_all(state, market=)`·`make_labels(df, horizon_days=, up=, down=)`, `memory_entries`(`situation_vector`, `decision` JSON, `price_return`).
- Produces: `OBS_COLUMNS: tuple[str, ...] = (*DIMS, "confidence", "llm_weight")` (14개); `build_steps(conn, state, *, market: str | None, horizon_days: int, min_memory_steps: int) -> pd.DataFrame` 열 `date(datetime64), ticker, obs(list[float] 14), fwd_return(float), source("memory"|"features")`, `date, ticker` 오름차순. 메모리 행(`price_return IS NOT NULL`, market 필터)이 `min_memory_steps` 이상이면 메모리만, 아니면 피처 스토어(`fwd_return` NaN 행 제외, `confidence=0.5, llm_weight=0.0`). 둘 다 비면 빈 DataFrame(열은 유지).

- [ ] **Step 1: Write the failing tests**

```python
def test_steps_from_features_when_memory_sparse(project, conn):
    features.write_rows(synthetic_features(3, 60, signal=False))
    df = rl_dataset.build_steps(conn, None, market=None, horizon_days=5, min_memory_steps=200)
    assert set(df["source"]) == {"features"} and len(df["obs"].iloc[0]) == 14
    assert df["obs"].iloc[0][-2:] == [0.5, 0.0]

def test_steps_drop_rows_without_forward_return(project, conn):
    features.write_rows(synthetic_features(1, 20, signal=False))
    df = rl_dataset.build_steps(conn, None, market=None, horizon_days=5, min_memory_steps=200)
    assert len(df) == 15 and df["fwd_return"].notna().all()

def test_steps_prefer_memory_when_enough(project, conn):
    seed_matured(conn, 12, hit_rate=0.5)
    df = rl_dataset.build_steps(conn, None, market="KR", horizon_days=5, min_memory_steps=10)
    assert set(df["source"]) == {"memory"} and len(df) == 12
    assert df["obs"].iloc[0][-2] == 0.6          # confidence
    assert df["fwd_return"].abs().round(2).eq(0.03).all()

def test_steps_empty_when_no_data(project, conn):
    df = rl_dataset.build_steps(conn, None, market=None, horizon_days=5, min_memory_steps=200)
    assert df.empty and list(df.columns) == ["date", "ticker", "obs", "fwd_return", "source"]
```

- [ ] **Step 2: Run** — `uv run pytest tests/ml/test_rl_dataset.py -q` → FAIL (모듈 없음).
- [ ] **Step 3: Implement** `build_steps` — 메모리 행의 `llm_weight` 는 `decision.target_weight`(없으면 0.0), `confidence` 는 `decision.confidence`(없으면 0.5); 피처 행은 `situation_vector(row.to_dict(), row.market)`.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** — `feat(rl): 스텝 데이터셋(메모리 우선, 피처 스토어 폴백)`

---

### Task 3: 환경·정책·평가

**Files:**
- Create: `src/trader/ml/rl/env.py`, `src/trader/ml/rl/policy.py`
- Test: `tests/ml/test_rl_env.py`, `tests/ml/test_rl_policy.py`

**Interfaces:**
- Consumes: Task 2 `OBS_COLUMNS`, 스텝 DataFrame; `ledger.performance.metrics_of`.
- Produces (`env.py`): `OBS_SIZE = 16`(14 + `position_weight` + `drawdown`); `class SizingEnv(steps: pd.DataFrame, *, max_weight: float, drawdown_penalty: float, cost_bp: int)` 메서드 `reset(seed: int | None = None) -> tuple[np.ndarray, dict]`, `step(action: float | np.ndarray) -> tuple[np.ndarray, float, bool, bool, dict]`(info `{"nav": float, "drawdown": float, "weight": float}`), 속성 `observation_space`/`action_space` 는 `Box(low, high, shape)` 데이터클래스(gymnasium 없이도 동작), `nav_series() -> list[tuple[str, Decimal]]`(날짜, NAV); `episode_metrics(env) -> dict`(= `metrics_of(nav_series)` + `steps`); `gym_env(env) -> gymnasium.Env`(지연 import, 없으면 `ConfigError("RL PPO 백엔드는 `uv sync --extra rl` 이 필요합니다")`).
- 보상: `w·r − cost_bp/1e4·|w − w_prev| − drawdown_penalty·max(0, dd_prev − dd_new)` (dd ≤ 0, NAV 피크 대비), NAV `*= 1 + w·r`, action 은 `[0, max_weight]` 로 클립. 종료는 마지막 스텝(`terminated=True`), `truncated=False`.
- Produces (`policy.py`): `Policy` 프로토콜 `act(obs: np.ndarray) -> float`; `FixedPolicy(weight)`; `LlmPolicy()`(= `obs[13]`); `VolTargetPolicy(target_vol: float = 0.02, max_weight: float = 0.25)`(= `min(max_weight, target_vol / max(obs[4], 1e-4))`, `obs[4]` 는 `atr_pct`); `LinearPolicy(theta: np.ndarray, max_weight: float)`(= `max_weight · sigmoid(theta[:-1]·obs + theta[-1])`, `theta` 길이 17); `evaluate(env: SizingEnv, policy: Policy, *, seed: int = 0) -> dict`(한 에피소드 결정적 실행 → `episode_metrics`).

- [ ] **Step 1: Write the failing tests**

```python
# test_rl_env.py
def _steps(returns):  # 1종목, obs 는 0, atr_pct(인덱스 4) 0.02
    return pd.DataFrame({"date": pd.to_datetime(trading_days(len(returns))), "ticker": "A",
                         "obs": [[0.0]*4 + [0.02] + [0.0]*9]*len(returns),
                         "fwd_return": returns, "source": "features"})

def test_env_obs_size_and_nav_compounding():
    env = SizingEnv(_steps([0.10, -0.10]), max_weight=0.25, drawdown_penalty=0.0, cost_bp=0)
    obs, _ = env.reset(seed=0)
    assert obs.shape == (OBS_SIZE,)
    _, r1, done, _, info = env.step(0.5)      # 0.25 로 클립
    assert not done and r1 == pytest.approx(0.025) and info["nav"] == pytest.approx(1.025)
    _, r2, done, _, info = env.step(0.25)
    assert done and info["nav"] == pytest.approx(1.025 * 0.975)
    assert episode_metrics(env)["mdd"] == pytest.approx(-0.025) and episode_metrics(env)["steps"] == 2

def test_env_penalizes_drawdown_and_turnover():
    env = SizingEnv(_steps([-0.10, -0.10]), max_weight=0.25, drawdown_penalty=2.0, cost_bp=100)
    env.reset()
    _, r, *_ = env.step(0.25)
    # w·r = −0.025, 회전 비용 0.01·0.25 = 0.0025, 드로다운 증가 0.025 × 2
    assert r == pytest.approx(-0.025 - 0.0025 - 0.05)

def test_env_rejects_empty_steps():
    with pytest.raises(ValueError):
        SizingEnv(_steps([]), max_weight=0.25, drawdown_penalty=1.0, cost_bp=30)

def test_gym_env_requires_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, "gymnasium", None)
    with pytest.raises(ConfigError):
        gym_env(SizingEnv(_steps([0.0]), max_weight=0.25, drawdown_penalty=1.0, cost_bp=30))

# test_rl_policy.py
def test_fixed_llm_vol_policies():
    obs = np.zeros(OBS_SIZE); obs[13] = 0.12; obs[4] = 0.04
    assert FixedPolicy(0.1).act(obs) == 0.1 and LlmPolicy().act(obs) == pytest.approx(0.12)
    assert VolTargetPolicy(target_vol=0.02, max_weight=0.25).act(obs) == pytest.approx(0.5 * 0.0 + 0.02 / 0.04)

def test_linear_policy_bounded_and_evaluate_deterministic():
    pol = LinearPolicy(np.full(OBS_SIZE + 1, 5.0), max_weight=0.25)
    assert 0 <= pol.act(np.ones(OBS_SIZE)) <= 0.25
    env = SizingEnv(_steps([0.01, 0.02, -0.01]), max_weight=0.25, drawdown_penalty=1.0, cost_bp=30)
    a, b = evaluate(env, pol), evaluate(env, pol)
    assert a == b and a["steps"] == 3 and a["return"] is not None
```

- [ ] **Step 2: Run** → FAIL (모듈 없음).
- [ ] **Step 3: Implement** `env.py`·`policy.py`. `VolTargetPolicy` 테스트의 기대값은 `min(0.25, 0.02/0.04) = 0.5 → 0.25` 가 아니라 `0.5` 가 되지 않도록 `max_weight` 클립을 적용한다(기대값 `0.25`). 테스트 식을 `== pytest.approx(0.25)` 로 적는다.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** — `feat(rl): SizingEnv(드로다운 페널티·회전 비용)·기준 정책·평가`

---

### Task 4: 학습기 (CEM 기본, PPO 선택)

**Files:**
- Create: `src/trader/ml/rl/learners.py`
- Test: `tests/ml/test_rl_learners.py`

**Interfaces:**
- Consumes: Task 3 `SizingEnv`, `LinearPolicy`, `evaluate`, `gym_env`; `RlSizerCfg`.
- Produces: `fit_cem(steps: pd.DataFrame, cfg: RlSizerCfg, *, seed: int) -> LinearPolicy`(모집단 `cem_population`, 반복 `cem_iterations`, 상위 10% 평균·표준편차로 갱신, 목적 = 에피소드 보상 합, `np.random.default_rng(seed)`); `class PpoPolicy(model_bytes: bytes)`(`act` 는 SB3 `predict(obs, deterministic=True)`, `__getstate__` 가 bytes 만 들고 다녀 joblib 저장 가능); `fit_ppo(steps, cfg, *, seed) -> PpoPolicy`(SB3 `PPO("MlpPolicy", gym_env(env), seed=seed, verbose=0).learn(cfg.total_timesteps)`); `resolve_backend(cfg) -> Literal["cem","ppo"]`(`auto` → `stable_baselines3` import 가능하면 `ppo`, 아니면 `cem`; `ppo` 지정인데 없으면 `ConfigError`); `fit(steps, cfg, *, seed, backend: str | None = None) -> tuple[Policy, str]`(정책, 실제 백엔드).

- [ ] **Step 1: Write the failing tests**

```python
def _trend_steps(n=120):  # 양의 수익이 atr_pct 낮을 때만 나오도록: 사이저가 저변동에서 크게 가야 이득
    rng = np.random.default_rng(1)
    atr = rng.choice([0.01, 0.05], size=n)
    ret = np.where(atr < 0.03, 0.02, -0.02)
    obs = [[0.0]*4 + [float(a)] + [0.0]*9 for a in atr]
    return pd.DataFrame({"date": pd.to_datetime(trading_days(n)), "ticker": "A", "obs": obs,
                         "fwd_return": ret, "source": "features"})

def test_cem_is_seed_deterministic_and_beats_fixed():
    cfg = RlSizerCfg(cem_iterations=10, cem_population=32)
    steps = _trend_steps()
    a, b = fit_cem(steps, cfg, seed=0), fit_cem(steps, cfg, seed=0)
    assert np.allclose(a.theta, b.theta)
    env = SizingEnv(steps, max_weight=0.25, drawdown_penalty=0.0, cost_bp=0)
    assert evaluate(env, a)["return"] > evaluate(env, FixedPolicy(0.10))["return"]

def test_resolve_backend_auto_falls_back_to_cem(monkeypatch):
    monkeypatch.setitem(sys.modules, "stable_baselines3", None)
    assert resolve_backend(RlSizerCfg(backend="auto")) == "cem"
    with pytest.raises(ConfigError):
        resolve_backend(RlSizerCfg(backend="ppo"))

def test_fit_dispatches_and_reports_backend():
    pol, backend = fit(_trend_steps(40), RlSizerCfg(backend="cem", cem_iterations=2, cem_population=8), seed=1)
    assert backend == "cem" and isinstance(pol, LinearPolicy)

def test_ppo_policy_roundtrips_joblib(tmp_path):
    pytest.importorskip("stable_baselines3")
    pol = fit_ppo(_trend_steps(40), RlSizerCfg(total_timesteps=256), seed=0)
    joblib.dump(pol, tmp_path / "p.joblib")
    back = joblib.load(tmp_path / "p.joblib")
    obs = np.zeros(OBS_SIZE)
    assert back.act(obs) == pytest.approx(pol.act(obs)) and 0 <= back.act(obs) <= 0.25
```

- [ ] **Step 2: Run** → FAIL (모듈 없음).
- [ ] **Step 3: Implement** `learners.py`. CEM 초기 평균 0·표준편차 1, 표준편차 하한 0.05. PPO 는 `action_space Box(0, max_weight)`.
- [ ] **Step 4: Run** → PASS (PPO 테스트는 extra 없으면 skip 1건 — 결과 출력에 `1 skipped` 로 확인).
- [ ] **Step 5: Commit** — `feat(rl): CEM 학습기(기본)·PPO 학습기(선택 extra)·백엔드 결정`

---

### Task 5: 워크포워드 학습 파이프라인 + 레지스트리 + CLI

**Files:**
- Create: `src/trader/ml/rl/train.py`
- Modify: `src/trader/ml/registry.py`(없으면 `version_dir` 헬퍼 불필요 — joblib 저장으로 충분하므로 수정 없음)
- Modify: `src/trader/cli.py`(`rl_app`: `train|models|promote|demote`)
- Test: `tests/ml/test_rl_train.py`, `tests/test_cli_rl.py`

**Interfaces:**
- Consumes: Task 2 `build_steps`, Task 3 `SizingEnv`·기준 정책·`evaluate`, Task 4 `fit`, `ml.walkforward.folds(dates, n_folds=, horizon_days=, embargo_days=)`·`Fold`, `ml.registry.save/list_models/latest/promote/demote`.
- Produces: `NAME = "rl_sizer"`; `train_sizer(cfg: MlConfig, conn, *, state: Path | None = None, market: str | None = None, now=..., fit_fn: Callable | None = None) -> dict` 반환 `{status: "trained"|"insufficient_data", version, backend, source, steps, seeds, folds: [{index, train_steps, test_steps, test_start, test_end, seeds: [{seed, rl: metrics}], baselines: {fixed, llm, vol_target: metrics}}], summary: {rl: {return_mean, return_std, mdd_mean, sortino_mean}, fixed: {...}, llm: {...}, vol_target: {...}}, promoted: False, promote_reason}`. 폴드별 시드 `range(cfg.models.rl_sizer.seeds)`; 최종 모델은 전체 스텝으로 시드 0 적합; 메타에 위 dict 전부 + `obs_columns`·`cfg`(`rl_sizer.model_dump()`); **승격하지 않는다**(`promote_reason = "수동 승격 전용: trader rl promote <버전>"`). `fit_fn(steps, rl_cfg, *, seed) -> tuple[Policy, str]` 를 주입할 수 있다(테스트·데몬).
- CLI: `trader rl train [--market] [--seeds N]`(JSON 1줄), `rl models`, `rl promote <버전> [--reason]`, `rl demote <버전> [--reason]`(quant 명령과 같은 형식, 이름만 `rl_sizer`).

- [ ] **Step 1: Write the failing tests**

```python
# test_rl_train.py
def _fake_fit(steps, rl_cfg, *, seed):
    return FixedPolicy(0.2 if seed == 0 else 0.05), "fake"

def test_train_runs_folds_times_seeds_and_never_promotes(project, conn):
    features.write_rows(synthetic_features(4, 400, signal=True))
    cfg = MlConfig.model_validate({"models": {"rl_sizer": {"seeds": 2}},
                                   "train": {"walk_forward_folds": 3}})
    r = train_sizer(cfg, conn, now=lambda: NOW, fit_fn=_fake_fit)
    assert r["status"] == "trained" and r["source"] == "features" and r["backend"] == "fake"
    assert len(r["folds"]) == 3 and [s["seed"] for s in r["folds"][0]["seeds"]] == [0, 1]
    assert set(r["summary"]) == {"rl", "fixed", "llm", "vol_target"}
    assert r["promoted"] is False and registry.latest("rl_sizer") is None
    meta = registry.list_models("rl_sizer")[0]
    assert meta["version"] == r["version"] and meta["promote_reason"].startswith("수동 승격")

def test_train_folds_respect_embargo(project, conn):
    features.write_rows(synthetic_features(2, 400, signal=False))
    r = train_sizer(MlConfig(), conn, now=lambda: NOW, fit_fn=_fake_fit)
    days = sorted(dataset.load_all()["date"].dt.date.unique())
    for f in r["folds"]:
        walkforward.check_no_leak(days, f["train_end"], f["test_start"], horizon_days=5, embargo_days=5)

def test_train_insufficient_data_saves_nothing(project, conn):
    features.write_rows(synthetic_features(1, 40, signal=False))
    r = train_sizer(MlConfig(), conn, now=lambda: NOW, fit_fn=_fake_fit)
    assert r["status"] == "insufficient_data" and r["version"] is None
    assert registry.list_models("rl_sizer") == []

def test_train_default_fit_is_cem_and_loadable(project, conn):
    features.write_rows(synthetic_features(3, 400, signal=True))
    cfg = MlConfig.model_validate({"models": {"rl_sizer": {"backend": "cem", "cem_iterations": 2,
                                                            "cem_population": 8, "seeds": 1}}})
    r = train_sizer(cfg, conn, now=lambda: NOW)
    assert r["backend"] == "cem"
    pol, meta = registry.load("rl_sizer", r["version"])
    assert 0 <= pol.act(np.zeros(OBS_SIZE)) <= 0.25 and meta["obs_columns"][-1] == "llm_weight"

# test_cli_rl.py (CliRunner, project 픽스처)
def test_rl_train_models_promote_demote(project, monkeypatch):
    features.write_rows(synthetic_features(3, 400, signal=True))
    monkeypatch.setattr("trader.ml.rl.learners.fit", lambda s, c, *, seed, backend=None: (FixedPolicy(0.1), "fake"))
    out = runner.invoke(app, ["rl", "train", "--seeds", "1"]); assert out.exit_code == 0
    v = json.loads(out.stdout)["version"]
    assert "미승격" in runner.invoke(app, ["rl", "models"]).stdout
    assert runner.invoke(app, ["rl", "promote", v]).exit_code == 0
    assert registry.latest("rl_sizer") is not None
    assert runner.invoke(app, ["rl", "demote", v]).exit_code == 0 and registry.latest("rl_sizer") is None
    assert runner.invoke(app, ["rl", "promote", "19990101_000000"]).exit_code == 1
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** `train.py`·`rl_app`. 폴드 dict 에 `train_end` 도 넣는다(테스트가 읽는다). `summary` 는 폴드×시드 평균/표준편차(`return_std` 는 시드·폴드 전체 표본 표준편차, 1개면 0.0). `insufficient_data` 조건: 스텝 < `min_steps` 또는 폴드 0개.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** — `feat(rl): 워크포워드×시드 학습·기준선 비교·레지스트리(수동 승격)·rl CLI`

---

### Task 6: 섀도·적용 (모의 계좌 한정) + executor 연결

**Files:**
- Create: `src/trader/ml/rl/shadow.py`
- Modify: `src/trader/orchestrator/executor.py`(`ExecContext.rl_sizer`, `execute` 안 분기)
- Modify: `src/trader/orchestrator/tools.py:742`, `src/trader/orchestrator/monitor.py:288`, `src/trader/daemon.py:465`, `src/trader/cli.py:739`, `src/trader/web/routers/control.py:39` — `rl_sizer=shadow.load(...)` 전달(monitor 손절·승인 경로는 BUY 가 아니므로 `None` 그대로 두어도 되지만 일관성을 위해 tools·daemon analyze 경로만 연결하고 나머지는 기본 None)
- Test: `tests/ml/test_rl_shadow.py`, `tests/orchestrator/test_executor_rl.py`

**Interfaces:**
- Consumes: `ml.registry.latest("rl_sizer")`, `ml.quant_report.latest_row(ticker, market, state)`, `memory.vector.situation_vector`, `ledger.repo.nav_history(conn, account_id, market, days=30)`, `rules.Portfolio`, `RlSizerCfg`, `Decision`.
- Produces: `@dataclass Sizer(policy: Policy, version: str, cfg: RlSizerCfg)`; `load(cfg: MlConfig, state: Path | None) -> Sizer | None`(`enabled` 꺼짐·승격 모델 없음·로드 실패(경고 로그) → None); `observe(state, *, ticker, market, decision, portfolio: Portfolio, nav_hist: list[tuple[str, Decimal]]) -> list[float]`(길이 `OBS_SIZE`; `position_weight = held.eval_amount / nav`(없으면 0), `drawdown = nav_last / max(nav_hist) − 1`(2개 미만이면 0)); `propose(sizer, obs) -> float`(`min(policy.act(obs), cfg.max_weight)`, 0 이상); `record(conn, *, decision_id: int, account_id: str, model_version: str, obs: list[float], llm_weight: float, proposed_weight: float, applied: bool) -> int`(`INSERT OR IGNORE`, 멱등); `recent(conn, account_id: str, *, days: int = 30) -> list[dict]`(`rl_shadow` ⟕ `memory_entries.price_return`/`decisions.ticker`, 최신 먼저, 각 행에 `realized_rl = proposed × price_return`, `realized_llm = llm × price_return`(미만기면 None)).
- `ExecContext.rl_sizer: Sizer | None = None`. `execute` 안: `account.env == "paper"` ∧ `decision.action in ("BUY","ADD")` ∧ `ctx.rl_sizer` ∧ `decision_id is not None` 일 때만 `observe → propose → record`; `cfg.mode == "apply"` 면 `decision = decision.model_copy(update={"target_weight": proposed})` 를 **그 계좌의** `rules.evaluate` 에만 넘기고 `reasons` 앞에 `"RL 사이저 적용: {llm:.2f} → {proposed:.2f} (v{version})"` 를 붙인다. 실전 계좌·HOLD·SELL 은 어떤 호출도 하지 않는다. 관측·제안 예외는 경고 로그 후 RL 없이 진행한다.

- [ ] **Step 1: Write the failing tests**

```python
# test_rl_shadow.py
def test_load_requires_enabled_and_promoted(project):
    cfg = MlConfig.model_validate({"models": {"rl_sizer": {"enabled": True}}})
    assert shadow.load(cfg, None) is None                        # 모델 없음
    v = registry.save("rl_sizer", FixedPolicy(0.07), {"obs_columns": list(OBS_COLUMNS)})
    assert shadow.load(cfg, None) is None                        # 미승격
    registry.promote("rl_sizer", v, reason="t")
    s = shadow.load(cfg, None); assert s is not None and s.version == v
    assert shadow.load(MlConfig(), None) is None                 # enabled False

def test_observe_position_and_drawdown():
    pf = Portfolio(account_id="p", market="KR", nav=Decimal(1000), cash_available=Decimal(500),
                   positions={"005930": Position(ticker="005930", market="KR", qty=1, avg_price=Decimal(1),
                                                 current_price=Decimal(1), eval_amount=Decimal(250))})
    obs = shadow.observe(None, ticker="005930", market="KR", decision=Decision(**ok_decision()),
                         portfolio=pf, nav_hist=[("d1", Decimal(1200)), ("d2", Decimal(1000))])
    assert len(obs) == OBS_SIZE and obs[12] == 0.6 and obs[13] == pytest.approx(ok_decision()["target_weight"])
    assert obs[14] == pytest.approx(0.25) and obs[15] == pytest.approx(-1/6)

def test_record_idempotent_and_recent_joins_outcome(conn):
    ids = seed_matured(conn, 1)                                  # decision_id 1, price_return 0.03
    dec_id = conn.execute("SELECT decision_id FROM memory_entries").fetchone()[0]
    a = shadow.record(conn, decision_id=dec_id, account_id="paper-main", model_version="v",
                      obs=[0.0]*OBS_SIZE, llm_weight=0.10, proposed_weight=0.20, applied=False)
    b = shadow.record(conn, decision_id=dec_id, account_id="paper-main", model_version="v",
                      obs=[0.0]*OBS_SIZE, llm_weight=0.10, proposed_weight=0.20, applied=False)
    assert a == b
    rows = shadow.recent(conn, "paper-main", days=30)
    assert len(rows) == 1 and rows[0]["realized_rl"] == pytest.approx(0.006)
    assert rows[0]["realized_llm"] == pytest.approx(0.003) and rows[0]["ticker"] == "T000"
    assert shadow.recent(conn, "real-main") == []

# test_executor_rl.py (test_executor.py 의 ectx·_run 헬퍼를 import 해 재사용)
def _sizer(mode, weight=0.05):
    return shadow.Sizer(policy=FixedPolicy(weight), version="v1",
                        cfg=RlSizerCfg(enabled=True, mode=mode))

def test_shadow_mode_records_but_does_not_change_qty(ectx):
    ectx.rl_sizer = _sizer("shadow")
    by = {r["account_id"]: r for r in _run(ectx)}
    assert by["paper-main"]["qty"] == 13                          # 변경 없음
    rows = ectx.conn.execute("SELECT * FROM rl_shadow").fetchall()
    assert len(rows) == 1 and rows[0]["account_id"] == "paper-main"
    assert rows[0]["proposed_weight"] == 0.05 and rows[0]["applied"] == 0

def test_apply_mode_changes_paper_qty_and_marks_applied(ectx):
    ectx.rl_sizer = _sizer("apply")
    by = {r["account_id"]: r for r in _run(ectx)}
    assert by["paper-main"]["qty"] == 6                           # 50만/71500
    assert any(r.startswith("RL 사이저 적용") for r in by["paper-main"]["reasons"])
    assert ectx.conn.execute("SELECT applied FROM rl_shadow").fetchone()[0] == 1

def test_real_account_untouched_even_in_apply_mode(ectx):
    for a in ectx.accounts:
        if a.env == "real":
            control.set_account_mode(ectx.conn, a, "approve")
    ectx.rl_sizer = _sizer("apply")
    by = {r["account_id"]: r for r in _run(ectx)}
    assert not any(r.startswith("RL") for r in by["real-main"]["reasons"])
    assert ectx.conn.execute("SELECT COUNT(*) FROM rl_shadow WHERE account_id='real-main'").fetchone()[0] == 0

def test_apply_mode_still_capped_by_risk_gate(ectx):
    ectx.rl_sizer = _sizer("apply", weight=0.90)
    by = {r["account_id"]: r for r in _run(ectx)}
    assert by["paper-main"]["qty"] == 34                          # 25% 상한 250만/71500

def test_disabled_or_no_model_is_noop(ectx):
    ectx.rl_sizer = None
    before = _run(ectx)
    assert ectx.conn.execute("SELECT COUNT(*) FROM rl_shadow").fetchone()[0] == 0
    assert before[0]["qty"] == 13

def test_hold_and_sell_skip_rl(ectx):
    ectx.rl_sizer = _sizer("apply")
    _run(ectx, ok_decision(action="HOLD"))
    assert ectx.conn.execute("SELECT COUNT(*) FROM rl_shadow").fetchone()[0] == 0
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** `shadow.py`·executor 분기·`tools.py`(`rl_sizer=shadow.load(load_ml(), ctx.state)` — `load_ml` 실패는 경고 후 None)·`daemon.py` analyze 경로(`DaemonDeps` 에 `rl_sizer` 는 두지 않고 `_analyze_body` 가 tools 경로를 쓰므로 tools 연결로 충분한지 확인; 아니면 같은 방식으로 연결).
- [ ] **Step 4: Run** `uv run pytest tests/ml/test_rl_shadow.py tests/orchestrator -q` → PASS, 기존 executor 테스트 영향 없음.
- [ ] **Step 5: Commit** — `feat(rl): 모의 계좌 한정 섀도/적용·rl_shadow 기록·executor 연결`

---

### Task 7: 비교 리포트·API·UI·STATUS·데몬

**Files:**
- Create: `src/trader/ml/rl/report.py`, `src/trader/web/routers/rl.py`
- Modify: `src/trader/cli.py`(`rl report`), `src/trader/web/app.py`(라우터 등록), `web/src/pages/Ml.tsx`("RL 실험" 절), `src/trader/status.py`(`_ml_lines` 에 RL 줄), `src/trader/daemon.py:540`(`_ml_train_body` 가 `rl_sizer.enabled` 면 `train_sizer` 도 실행·통보)
- Test: `tests/ml/test_rl_report.py`, `tests/web/test_rl_api.py`, `tests/test_status.py`(기존), `tests/test_daemon.py`(기존)

**Interfaces:**
- Consumes: Task 5 메타(`summary`, `folds`), Task 6 `recent`.
- Produces: `comparison(conn, state, *, account_id: str, market: str | None = None, weeks: int = 8) -> dict` `{weeks, model: {version, backend, source, promoted, summary} | None, shadow: {n, applied, avg_llm_weight, avg_proposed_weight, matured, realized_rl_sum, realized_llm_sum}}`(모델은 승격 최신, 없으면 최신). `rl report --account paper-main [--weeks 8] [--market]` → JSON 1줄.
- API: `GET /api/rl/experiments` → `{experiments: [요약(version, saved_at, backend, source, steps, seeds, promoted, promote_reason, summary)], active}`; `GET /api/rl/experiments/{version}` → 메타 전체(형식 오류 400, 없음 404); `GET /api/rl/shadow?account_id&days=Query(30, ge=1, le=365)` → `{rows: recent(...)}`(account_id 없거나 모르면 400); `GET /api/rl/report?account_id&weeks=Query(8, ge=1, le=52)` → `comparison`.
- UI: `Ml.tsx` 아래에 "RL 실험(사이저)" 절 — 실험 표(버전·백엔드·소스·스텝·RL 수익 평균±표준편차·고정·LLM·변동성 타깃·승격), 선택 계좌의 섀도 표(날짜·종목·LLM 비중·제안 비중·적용·실현 RL/LLM). `useApp().accountId` 사용.
- STATUS: "## ML 모델" 절에 `- rl_sizer <버전>: RL <수익 평균> / 고정 <…> / LLM <…> · 승격|미승격` 또는 `- rl_sizer 없음`.

- [ ] **Step 1: Write the failing tests**

```python
# test_rl_report.py
def test_comparison_without_model_and_with_shadow(conn, project):
    assert comparison(conn, None, account_id="paper-main")["model"] is None
    seed_matured(conn, 2); dec_ids = [r[0] for r in conn.execute("SELECT decision_id FROM memory_entries")]
    for d in dec_ids:
        shadow.record(conn, decision_id=d, account_id="paper-main", model_version="v", obs=[0.0]*OBS_SIZE,
                      llm_weight=0.1, proposed_weight=0.2, applied=False)
    registry.save("rl_sizer", FixedPolicy(0.1), {"summary": {"rl": {"return_mean": 0.01}}, "backend": "cem", "source": "features"})
    r = comparison(conn, None, account_id="paper-main", weeks=8)
    assert r["model"]["backend"] == "cem" and r["shadow"]["n"] == 2 and r["shadow"]["matured"] == 2
    assert r["shadow"]["avg_proposed_weight"] == pytest.approx(0.2)

# test_rl_api.py
def test_rl_experiments_and_shadow_endpoints(client, conn):
    assert client.get("/api/rl/experiments").json() == {"experiments": [], "active": None}
    v = registry.save("rl_sizer", FixedPolicy(0.1), {"summary": {}, "backend": "cem", "source": "features", "steps": 10, "seeds": 3})
    assert client.get("/api/rl/experiments").json()["experiments"][0]["version"] == v
    assert client.get(f"/api/rl/experiments/{v}").status_code == 200
    assert client.get("/api/rl/experiments/bad").status_code == 400
    assert client.get("/api/rl/shadow").status_code == 400
    assert client.get("/api/rl/shadow?account_id=nope").status_code == 400
    assert client.get("/api/rl/shadow?account_id=paper-main").json() == {"rows": []}
    assert client.get("/api/rl/report?account_id=paper-main").json()["shadow"]["n"] == 0

# tests/test_daemon.py 추가
def test_ml_train_job_also_trains_sizer_when_enabled(deps_factory, monkeypatch):
    calls = []
    monkeypatch.setattr("trader.ml.train.train_quant", lambda *a, **k: {"status": "trained", "metrics": {}, "promoted": False})
    monkeypatch.setattr("trader.ml.rl.train.train_sizer", lambda *a, **k: calls.append(1) or {"status": "trained", "version": "v", "summary": {}})
    monkeypatch.setattr("trader.config.load_ml", lambda: MlConfig.model_validate({"models": {"rl_sizer": {"enabled": True}}}))
    r = daemon._ml_train_body(deps_factory())
    assert calls == [1] and r["rl_sizer"]["status"] == "trained"

# tests/test_status.py 추가
def test_status_has_rl_sizer_line(conn, project):
    assert "rl_sizer 없음" in build_status(conn, Settings(), [], project / "docs/TODO.md")
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** `report.py`, `routers/rl.py`(+ `app.py` 등록), CLI `rl report`, `status._ml_lines`, `daemon._ml_train_body`(반환 dict 에 `rl_sizer` 키 추가, 실패는 통보만), `Ml.tsx` 절(타입 `Experiment`, `ShadowRow`). `npm run typecheck && npm test && npm run build`.
- [ ] **Step 4: Run** `uv run pytest -q` 전체 → PASS, `uv run ruff check src tests` 0건.
- [ ] **Step 5: Commit** — `feat(rl): 비교 리포트·/api/rl·ML 페이지 RL 실험 절·STATUS·데몬 학습 연결`

---

### Task 8: 검증·문서·실기

**Files:**
- Modify: `docs/TODO.md`(Phase 9 항목 체크, 범위 조정 표의 이연·사용자 확인 대기 추가), `CLAUDE.md`(현황 문장 "Phase 0~9 구현", 명령 `rl train|models|promote|demote|report`, 규칙 1항목 "RL 사이저"), `pyproject.toml` 확인

- [ ] **Step 1: 실기(FAKE)** — `uv run trader features snapshot --market KR`(피처 행 없으면 생략) → `uv run trader rl train --market KR --seeds 1`(스텝 < 300 이면 `insufficient_data` 가 정상; 메시지 확인) → `uv run trader rl models` → `uv run trader rl report --account paper-main` → `FAKE_CLAUDE=1 uv run trader analyze --ticker 005930 --dry-run`(HOLD 라 `rl_shadow` 0행이 정상) → `uv run trader status` 에 `rl_sizer` 줄 → `uv run trader web` 으로 `/api/rl/experiments` 200.
  Expected: 모든 명령 exit 0, 어떤 계좌 모드도 바뀌지 않음, 실전 계좌 관련 행 0.
- [ ] **Step 2: 전체 검증** — `uv run pytest -q`(전부 통과, PPO 1건 skip 허용), `uv run ruff check src tests`(0건), `cd web && npm run typecheck && npm test && npm run build`.
- [ ] **Step 3: 문서** — TODO 체크(완료 기준은 "8주 비교 리포트 수치" 사용자 확인 대기로 남김), CLAUDE.md 갱신(규칙: "RL 사이저는 `ml.yaml rl_sizer.enabled` ∧ 승격 모델(`rl promote` 수동)일 때 **모의 계좌·BUY/ADD** 에만 관측·제안을 `rl_shadow` 에 남기고, `mode: apply` 여도 게이트 상한은 그대로다. 실전 계좌는 기록도 하지 않는다. 기본 백엔드 `cem`(numpy), `ppo` 는 `uv sync --extra rl`. 자동 승격 없음."), `uv run trader status`.
- [ ] **Step 4: Commit** — `docs(phase9): TODO·CLAUDE.md RL 사이저 규칙·명령`
- [ ] **Step 5: 최종 리뷰·PR** — executing-plans 의 최종 리뷰(Fable) → Critical/Important 수정 → PR `feat/phase9-rl-sizing` → `feat/phase8-learning`.

---

## Self-Review

1. **Spec coverage**: §9.4 gym env(Task 3), PPO 워크포워드(Task 4·5; PPO 는 선택 extra, 기본 CEM — 범위 조정 표에 기록), 드로다운 페널티(Task 3), 시드 3회(Task 1 기본값·Task 5), 모의 한정 섀도(Task 6), 수동 승격(Task 5·CLI), UI 실험 로그(Task 7), 8주 비교 리포트(Task 7 `comparison(weeks=8)`; 수치는 사용자 확인 대기). §6.1 한도 안에서만(Task 6 테스트). §9.5 레지스트리(Task 5).
2. **Step scan**: Task 3 `VolTargetPolicy` 테스트 기대값은 Step 3 메모대로 `pytest.approx(0.25)` 로 적는다. Task 6 의 데몬 연결은 tools 경로 확인으로 결정(실행 시 룰링).
3. **Type consistency**: `OBS_COLUMNS`(14)·`OBS_SIZE`(16)·`obs[4]=atr_pct`·`obs[12]=confidence`·`obs[13]=llm_weight`·`obs[14]=position_weight`·`obs[15]=drawdown` 를 Task 2·3·6 이 같은 인덱스로 쓴다. `fit_fn` 시그니처 `(steps, rl_cfg, *, seed) -> (Policy, str)` 는 Task 4 `fit` 과 Task 5 주입점이 동일(단, `fit` 은 `backend` 키워드를 추가로 받는다 — Task 5 는 `fit_fn(steps, rl_cfg, seed=seed)` 로만 부른다).
4. **Review Focus**: 5개 모두 소유 Task 의 테스트에 붙어 있다.
5. **Proportion**: 스펙 §9.4 한 줄을 8 태스크로 푼다. 코드 블록은 테스트와 시그니처만이다.
