"""Random-search hyperparameter tuning for the click model, scored on the validation slice.

Features are built once; each trial only refits XGBoost on the fit slice (early stopping on the
validation slice) and is scored by group AUC there. The test day is never seen, so its AUC stays
an honest estimate. Trial 0 is the config's own params, so every search reports what tuning
gained over them.

Each parameter is drawn independently from a range in `tuning.space`:
  [int, low, high]      uniform integer, inclusive
  [uniform, low, high]  uniform float
  [log, low, high]      uniform in log space, for parameters whose effect scales by orders of magnitude
"""

import time

import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.metrics import roc_auc_score

BOOST_KEYS = ("n_estimators", "early_stopping_rounds")  # passed to xgb.train, not in the param dict


def sample(space: dict, rng: np.random.Generator) -> dict:
    """One draw of every parameter in the search space."""
    params = {}
    for name, (kind, low, high) in space.items():
        if kind == "int":
            params[name] = int(rng.integers(low, high + 1))
        elif kind == "uniform":
            params[name] = float(f"{rng.uniform(low, high):.4g}")
        elif kind == "log":
            params[name] = float(f"{np.exp(rng.uniform(np.log(low), np.log(high))):.4g}")
        else:
            raise ValueError(f"tuning.space.{name}: unknown distribution {kind!r} (int, uniform or log)")
    return params


def group_auc(impression: np.ndarray, y: np.ndarray, scores: np.ndarray) -> float:
    """Mean AUC within impressions that have both a click and a skip (vectorized rank-sum form).

    Same number as train.impression_aucs(...).mean(), fast enough to run once per trial.
    """
    df = pd.DataFrame({"g": impression, "y": y.astype(float)})
    df["r"] = pd.Series(scores).groupby(impression).rank().to_numpy() * df["y"]  # ties get average ranks
    g = df.groupby("g").agg(n=("y", "size"), pos=("y", "sum"), rank_pos=("r", "sum"))
    g = g[(g["pos"] > 0) & (g["pos"] < g["n"])]
    neg = g["n"] - g["pos"]
    return float(((g["rank_pos"] - g["pos"] * (g["pos"] + 1) / 2) / (g["pos"] * neg)).mean())


def random_search(fit_df: pd.DataFrame, valid_df: pd.DataFrame, features: list[str], base: dict,
                  tuning: dict, seed: int) -> tuple[dict, pd.DataFrame]:
    """Run `tuning.trials` random trials plus the base params; return the best params and every trial."""
    rng = np.random.default_rng(seed)
    metric = tuning.get("metric", "group_auc")
    dfit = xgb.QuantileDMatrix(fit_df[features], fit_df["label"])
    dvalid = xgb.QuantileDMatrix(valid_df[features], valid_df["label"], ref=dfit)
    y, imp = valid_df["label"].to_numpy(), valid_df["impression_id"].to_numpy()

    rows = []
    candidates = [{}] + [sample(tuning["space"], rng) for _ in range(tuning["trials"])]
    for trial, drawn in enumerate(candidates):
        params = {**base, **drawn}
        start = time.time()
        booster = xgb.train(
            {k: v for k, v in params.items() if k not in BOOST_KEYS}
            | {"objective": "binary:logistic", "eval_metric": "auc", "tree_method": "hist", "seed": seed},
            dfit, num_boost_round=params["n_estimators"], evals=[(dvalid, "valid")],
            early_stopping_rounds=params["early_stopping_rounds"], verbose_eval=False)
        pred = booster.predict(dvalid, iteration_range=(0, booster.best_iteration + 1))
        rows.append({"trial": trial, **{k: params.get(k, np.nan) for k in tuning["space"]},
                     "valid_auc": float(roc_auc_score(y, pred)), "valid_group_auc": group_auc(imp, y, pred),
                     "best_iteration": int(booster.best_iteration), "seconds": round(time.time() - start, 1)})
        r = rows[-1]
        print(f"trial {trial:>3}{' (config)' if trial == 0 else '         '}  "
              f"group_auc={r['valid_group_auc']:.4f}  auc={r['valid_auc']:.4f}  "
              f"trees={r['best_iteration'] + 1}  {r['seconds']}s", flush=True)

    trials = pd.DataFrame(rows)
    best = int(trials[f"valid_{metric}"].idxmax())
    return {**base, **candidates[best]}, trials
