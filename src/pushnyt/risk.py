"""Return model: a churn proxy for the send policy.

MIND has no opt-out events, so disengagement is proxied by "does the user come back (have another
impression) within `horizon_hours`?". Risk = 1 - P(return). This is a user-level signal: it
mostly reflects how active a user is, not what a given push did to them, so it is used to set
how much to send each user (the send threshold in objective.py), not which article to send.

Labels near the end of the log are censored (the horizon runs past the data) and are dropped,
so the model is trained on impressions before `fit_end` and evaluated on [fit_end, train_end);
the test day itself is only scored. In production this would be replaced by a model of real
notification opt-outs (see the README).
"""

import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.metrics import roc_auc_score

RETURN_FEATURES = ["slot", "hour", "user_history_len", "user_prior_impr", "slot_prior_impr",
                   "impr_last_24h", "hours_since_last_impr", "slot_engaged_rate", "slot_skip_rate_72h"]


def return_labels(beh: pd.DataFrame, horizon_hours: float) -> np.ndarray:
    """1 if the user's next impression is within the horizon, 0 if not, NaN if censored."""
    horizon = pd.Timedelta(hours=horizon_hours)
    gap = beh.groupby("user_id")["time"].shift(-1) - beh["time"]
    returned = (gap <= horizon).to_numpy()
    observable = (beh["time"] + horizon <= beh["time"].max()).to_numpy()
    return np.where(returned, 1.0, np.where(observable, 0.0, np.nan))


def return_features(beh: pd.DataFrame, temporal: pd.DataFrame) -> pd.DataFrame:
    """The return model's inputs for each impression in `beh` (temporal = temporal_features(beh))."""
    return temporal.assign(slot=beh["slot"].to_numpy(), hour=beh["time"].dt.hour.to_numpy(),
                           user_history_len=beh["history"].str.len().to_numpy())[RETURN_FEATURES]


def fit_return_model(beh: pd.DataFrame, temporal: pd.DataFrame, fit_end: pd.Timestamp, train_end: pd.Timestamp,
                     cfg: dict, seed: int) -> tuple[np.ndarray, dict, xgb.XGBClassifier]:
    """P(return within the horizon) for every impression in `beh`, held-out metrics, and the model."""
    X = return_features(beh, temporal)
    y = return_labels(beh, cfg["horizon_hours"])
    labeled = ~np.isnan(y)
    fit = labeled & (beh["time"] < fit_end).to_numpy()
    held = labeled & ((beh["time"] >= fit_end) & (beh["time"] < train_end)).to_numpy()

    model = xgb.XGBClassifier(n_estimators=cfg["n_estimators"], max_depth=cfg["max_depth"],
                              learning_rate=cfg["learning_rate"], tree_method="hist",
                              random_state=seed, n_jobs=-1)
    model.fit(X[fit], y[fit])
    p = model.predict_proba(X)[:, 1]
    metrics = {"horizon_hours": cfg["horizon_hours"], "rows_fit": int(fit.sum()), "rows_eval": int(held.sum()),
               "return_rate_eval": float(y[held].mean()), "auc": float(roc_auc_score(y[held], p[held]))}
    return p, metrics, model
