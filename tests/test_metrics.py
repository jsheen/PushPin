import numpy as np
import pandas as pd
import pytest
from sklearn.metrics import roc_auc_score

from pushpin.train import _weighted_auc, bootstrap_ci, check_quality, impression_aucs


def test_weighted_auc_matches_sklearn_with_ties():
    rng = np.random.default_rng(0)
    y = rng.integers(0, 2, 2000)
    s = np.round(rng.random(2000) + 0.3 * y, 2)  # rounding creates ties
    w = rng.integers(0, 3, 2000).astype(float)
    rank = np.unique(s, return_inverse=True)[1]
    assert _weighted_auc(rank, y.astype(float), w) == pytest.approx(roc_auc_score(y, s, sample_weight=w))


def test_bootstrap_ci_brackets_point_estimates():
    rng = np.random.default_rng(1)
    n_imp, per = 300, 10
    df = pd.DataFrame({"impression_id": np.repeat(np.arange(n_imp), per),
                       "slot": np.repeat(rng.integers(0, 3, n_imp), per),
                       "label": rng.integers(0, 2, n_imp * per)})
    scores = rng.random(len(df)) + 0.2 * df["label"].to_numpy()
    imp_auc = impression_aucs(df, scores)
    ci = bootstrap_ci(df, scores, imp_auc, n=200, level=0.95, seed=0)
    lo, hi = ci["auc"]
    assert lo < roc_auc_score(df["label"], scores) < hi
    lo, hi = ci["group_auc"]
    assert lo < imp_auc.mean() < hi
    assert set(ci["auc_by_slot"]) == {"morning", "afternoon", "evening"}


def test_quality_gate():
    check_quality({"auc": 0.65}, {"min_auc": 0.644})
    check_quality({"auc": 0.50}, {})  # no gate configured
    with pytest.raises(SystemExit, match="quality gate failed"):
        check_quality({"auc": 0.60}, {"min_auc": 0.644})
