"""Calibration: make P(click) mean what it says, so it can be compared against a send threshold.

Ranking only needs the scores in the right order; a send/don't-send threshold needs them to be
right in absolute terms (of pushes scored 0.10, about 10% are clicked). Platt scaling fits
p = sigmoid(a * logit(raw) + b) on held-out rows. It is monotone, so rankings, AUC, and the push
objective's choices are unchanged; only the probability scale moves.
"""

import numpy as np
from scipy.special import expit, logit
from sklearn.linear_model import LogisticRegression

EPS = 1e-6


def fit_platt(raw: np.ndarray, y: np.ndarray) -> dict:
    z = logit(np.clip(raw, EPS, 1 - EPS))[:, None]
    lr = LogisticRegression(C=1e6).fit(z, y)  # effectively unregularized
    return {"a": float(lr.coef_[0, 0]), "b": float(lr.intercept_[0])}


def apply_platt(raw: np.ndarray, platt: dict) -> np.ndarray:
    return expit(platt["a"] * logit(np.clip(raw, EPS, 1 - EPS)) + platt["b"])


def reliability(y: np.ndarray, p: np.ndarray, bins: int = 20) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Mean predicted vs. observed click rate in equal-count bins of p, with bin sizes."""
    order = np.argsort(p, kind="stable")
    chunks = np.array_split(order, bins)
    return (np.array([p[c].mean() for c in chunks]), np.array([y[c].mean() for c in chunks]),
            np.array([len(c) for c in chunks]))


def calibration_metrics(y: np.ndarray, p: np.ndarray, bins: int = 20) -> dict:
    """Brier score, expected calibration error (count-weighted |predicted - observed| over bins),
    and the mean prediction vs. the observed rate."""
    pred, obs, n = reliability(y, p, bins)
    return {"brier": float(np.mean((p - y) ** 2)), "ece": float(np.sum(n * np.abs(pred - obs)) / n.sum()),
            "mean_pred": float(p.mean()), "observed": float(y.mean())}
