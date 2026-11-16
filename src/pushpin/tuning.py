"""Hyperparameter tuning for the click model: Bayesian optimization, scored on the validation slice.

Features are built once; each trial only refits XGBoost on the fit slice (early stopping on the
validation slice) and is scored by group AUC there. The test day is never seen, so its AUC stays
an honest estimate. Trial 0 is the config's own params, so every search reports what tuning
gained over them.

Search (`tuning.method`):
  bayes   the first `init_trials` are random; after that a Gaussian process is fit to every trial so
          far (score as a smooth function of the params) and the next trial goes where expected
          improvement over the best score is highest: high predicted score, or high uncertainty.
  random  every trial is an independent random draw.

Either way a final Gaussian process is fit to all trials. The winner is the trial with the highest
*predicted* score, not the highest observed one: the observed best is partly the luckiest trial,
and the prediction averages over its neighbors. The GP's per-parameter length scales say which
knobs matter: a short length scale means the score changes quickly along that parameter.

Ranges in `tuning.space`, each [distribution, low, high]:
  int      uniform integer, inclusive
  uniform  uniform float
  log      uniform in log space, for parameters whose effect scales by orders of magnitude
The GP works in the unit cube: each parameter is mapped to [0, 1] on that same scale.
"""

import time
import warnings

import numpy as np
import pandas as pd
import xgboost as xgb
from scipy.stats import norm
from sklearn.exceptions import ConvergenceWarning
from sklearn.gaussian_process import GaussianProcessRegressor
from sklearn.gaussian_process.kernels import ConstantKernel, Matern, WhiteKernel
from sklearn.metrics import roc_auc_score

BOOST_KEYS = ("n_estimators", "early_stopping_rounds")  # passed to xgb.train, not in the param dict
KINDS = ("int", "uniform", "log")
N_CANDIDATES = 5000  # random points where expected improvement is evaluated each Bayesian step


def _check(space: dict) -> None:
    for name, (kind, _, _) in space.items():
        if kind not in KINDS:
            raise ValueError(f"tuning.space.{name}: unknown distribution {kind!r} (int, uniform or log)")


def from_unit(u: np.ndarray, space: dict) -> dict:
    """Map a point in [0, 1]^d to params; uniform u gives each distribution its stated draw."""
    params = {}
    for x, (name, (kind, low, high)) in zip(u, space.items()):
        if kind == "int":
            n = high - low + 1
            params[name] = int(low + min(int(x * n), n - 1))
        elif kind == "uniform":
            params[name] = float(f"{low + x * (high - low):.4g}")
        else:
            params[name] = float(f"{np.exp(np.log(low) + x * (np.log(high) - np.log(low))):.4g}")
    return params


def to_unit(params: dict, space: dict) -> np.ndarray:
    """Inverse of from_unit. Values outside a range are clipped (e.g. reg_alpha 0 on a log range)."""
    u = []
    for name, (kind, low, high) in space.items():
        v = float(np.clip(params[name], low, high))
        if kind == "int":
            u.append((v - low + 0.5) / (high - low + 1))
        elif kind == "uniform":
            u.append((v - low) / (high - low))
        else:
            u.append((np.log(v) - np.log(low)) / (np.log(high) - np.log(low)))
    return np.array(u)


def sample(space: dict, rng: np.random.Generator) -> dict:
    """One random draw of every parameter in the search space."""
    _check(space)
    return from_unit(rng.random(len(space)), space)


def fit_gp(X: np.ndarray, y: np.ndarray, seed: int) -> GaussianProcessRegressor:
    """GP over the unit cube: one length scale per parameter, plus a noise term for trial-to-trial jitter."""
    kernel = (ConstantKernel(1.0, (1e-2, 1e2))
              * Matern(length_scale=np.full(X.shape[1], 0.5), length_scale_bounds=(1e-2, 1e2), nu=2.5)
              + WhiteKernel(0.1, (1e-6, 1.0)))
    gp = GaussianProcessRegressor(kernel, normalize_y=True, n_restarts_optimizer=3, random_state=seed)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ConvergenceWarning)
        return gp.fit(X, y)


def propose(X: np.ndarray, y: np.ndarray, space: dict, rng: np.random.Generator, seed: int) -> dict:
    """Next trial: the random candidate with the highest expected improvement over the best score."""
    gp = fit_gp(X, y, seed)
    cand = rng.random((N_CANDIDATES, X.shape[1]))
    mu, sd = gp.predict(cand, return_std=True)
    gain = mu - y.max() - 0.01 * y.std()  # small margin so it doesn't only polish the current best
    z = gain / np.maximum(sd, 1e-12)
    ei = gain * norm.cdf(z) + sd * norm.pdf(z)
    return from_unit(cand[int(np.argmax(ei))], space)


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


def search(fit_df: pd.DataFrame, valid_df: pd.DataFrame, features: list[str], base: dict,
           tuning: dict, seed: int) -> tuple[dict, pd.DataFrame, dict]:
    """Run the config params plus `tuning.trials` searched trials.

    Returns the chosen params, every trial (with the final GP's predicted score) and a summary.
    """
    space, method = tuning["space"], tuning.get("method", "bayes")
    _check(space)
    if method not in ("bayes", "random"):
        raise ValueError(f"tuning.method: {method!r} (bayes or random)")
    col = f"valid_{tuning.get('metric', 'group_auc')}"
    n_init = tuning.get("init_trials", 8)
    rng = np.random.default_rng(seed)
    dfit = xgb.QuantileDMatrix(fit_df[features], fit_df["label"])
    dvalid = xgb.QuantileDMatrix(valid_df[features], valid_df["label"], ref=dfit)
    y, imp = valid_df["label"].to_numpy(), valid_df["impression_id"].to_numpy()

    rows, used = [], []
    for trial in range(tuning["trials"] + 1):
        if trial == 0:
            drawn, source = {}, "config"
        elif method == "random" or trial <= n_init:
            drawn, source = sample(space, rng), "random"
        else:
            scores = np.array([r[col] for r in rows])
            drawn, source = propose(np.array([to_unit(p, space) for p in used]), scores, space, rng, seed), "bayes"
        params = {**base, **drawn}
        start = time.time()
        booster = xgb.train(
            {k: v for k, v in params.items() if k not in BOOST_KEYS}
            | {"objective": "binary:logistic", "eval_metric": "auc", "tree_method": "hist", "seed": seed},
            dfit, num_boost_round=params["n_estimators"], evals=[(dvalid, "valid")],
            early_stopping_rounds=params["early_stopping_rounds"], verbose_eval=False)
        pred = booster.predict(dvalid, iteration_range=(0, booster.best_iteration + 1))
        used.append(params)
        rows.append({"trial": trial, "source": source, **{k: params[k] for k in space},
                     "valid_auc": float(roc_auc_score(y, pred)), "valid_group_auc": group_auc(imp, y, pred),
                     "best_iteration": int(booster.best_iteration), "seconds": round(time.time() - start, 1)})
        r = rows[-1]
        print(f"trial {trial:>3} {source:<7} group_auc={r['valid_group_auc']:.4f}  auc={r['valid_auc']:.4f}  "
              f"trees={r['best_iteration'] + 1}  {r['seconds']}s", flush=True)

    trials = pd.DataFrame(rows)
    X, scores = np.array([to_unit(p, space) for p in used]), trials[col].to_numpy()
    summary = {"method": method}
    if len(trials) >= 4:  # enough points for the GP to say something
        gp = fit_gp(X, scores, seed)
        trials["gp_predicted"] = gp.predict(X)
        best = int(trials["gp_predicted"].idxmax())
        matern = gp.kernel_.k1.k2
        summary["picked_by"] = "highest GP-predicted score among tried trials"
        summary["length_scales"] = {k: round(float(v), 3) for k, v in zip(space, matern.length_scale)}
    else:
        best = int(trials[col].idxmax())
        summary["picked_by"] = "highest observed score"
    trials["chosen"] = trials["trial"] == best
    summary["best_trial"] = best
    return dict(used[best]), trials, summary
