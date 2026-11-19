"""Uplift: the DR estimator, the randomized design and the semi-synthetic benchmark."""
import numpy as np
import pandas as pd
import pytest

from pushpin import uplift as up
from pushpin.uplift_design import propensity, pushes_today

SMALL_CFG = {
    "replicates": 2, "folds": 3, "seed": 1, "daily_cap": 2,
    "design": {"holdout": 0.2, "explore": 0.2},
    "planted": {"organic": 1.0, "lift": 0.4, "interest_floor": 0.3, "fatigue": 0.04},
    "outcome_model": {"n_estimators": 60, "max_depth": 3, "learning_rate": 0.1},
    "effect_model": {"n_estimators": 60, "max_depth": 2, "learning_rate": 0.1, "min_child_weight": 50},
}


def confounded(n=200_000, seed=0):
    """Readers likely to return anyway (high x) are also likely to be pushed: naive comparisons lie."""
    rng = np.random.default_rng(seed)
    x = rng.random(n)
    e = 0.2 + 0.6 * x
    t = (rng.random(n) < e).astype(int)
    y0, tau = 0.2 + 0.6 * x, 0.15 * (1 - x)
    y = (rng.random(n) < y0 + t * tau).astype(int)
    return x, e, t, y, y0, tau


def test_dr_is_unbiased_when_the_outcome_models_are_wrong():
    x, e, t, y, y0, tau = confounded()
    naive = y[t == 1].mean() - y[t == 0].mean()
    assert naive > 2 * tau.mean()  # confounding: pushed readers return anyway
    wrong = np.full_like(x, 0.5)  # outcome models that know nothing
    for mu0, mu1 in ((wrong, wrong), (0.0, 0.0), (y0, y0 + tau)):
        assert up.dr_pseudo_outcome(y, t, e, mu0, mu1).mean() == pytest.approx(tau.mean(), abs=0.006)


def test_good_outcome_models_shrink_the_variance():
    _x, e, t, y, y0, tau = confounded()
    ipw = up.dr_pseudo_outcome(y, t, e, 0.0, 0.0)
    dr = up.dr_pseudo_outcome(y, t, e, y0, y0 + tau)
    assert dr.var() < 0.5 * ipw.var()


def test_cluster_mean_widens_for_correlated_rows():
    rng = np.random.default_rng(3)
    per_reader = rng.normal(size=200)
    values = np.repeat(per_reader, 10)  # each reader's 10 rows are identical
    _, se_rows = up.cluster_mean(values, np.arange(len(values)))
    _, se_readers = up.cluster_mean(values, np.repeat(np.arange(200), 10))
    assert se_readers == pytest.approx(se_rows * np.sqrt(10), rel=0.1)


def test_propensity_by_design():
    assert propensity(np.array([True, False]), 0.2, 0.1).tolist() == [0.8, 0.1]


def test_pushes_today_counts_earlier_pushes_that_day_only():
    df = pd.DataFrame({"reader": ["a"] * 4 + ["b"],
                       "slot_time": pd.to_datetime(["2024-12-02 08:00", "2024-12-02 13:00", "2024-12-02 19:00",
                                                    "2024-12-03 08:00", "2024-12-02 13:00"]),
                       "treated": [1, 0, 1, 1, 1]})
    assert pushes_today(df.iloc[::-1]).tolist() == [0, 1, 1, 0, 0][::-1]  # any row order


def fake_picks(users=300, days=3, seed=0):
    rng = np.random.default_rng(seed)
    slots = [pd.Timestamp("2024-12-02") + pd.Timedelta(days=d, hours=h) for d in range(days) for h in (8, 13, 19)]
    df = pd.DataFrame([(f"U{u}", s) for s in slots for u in range(users)], columns=["user_id", "slot_time"])
    n = len(df)
    df["p_return"] = np.repeat(rng.uniform(0.1, 0.95, users)[None], len(slots), 0).ravel()
    df["p_click"] = np.clip(0.03 + 0.1 * df["p_return"] + rng.normal(0, 0.02, n), 0.01, 0.5)
    for col in ("novelty", "subcat_affinity", "emb_sim_max", "llm_sim_click", "llm_sim_skip"):
        df[col] = rng.random(n)
    df["article_age_hours"] = rng.uniform(0, 12, n)
    df["sent"] = df["p_click"] > df["p_click"].quantile(0.75)  # the click policy prefers engaged readers
    return df


def test_semi_synthetic_benchmark_recovers_the_planted_effect():
    df = up.cross_fit(up.simulate(fake_picks(), SMALL_CFG, seed=1), SMALL_CFG, seed=1)
    assert df.groupby("reader")["user_id"].nunique().max() == 1 and df["reader"].nunique() == 600
    assert set(df.loc[df["eligible"], "propensity"]) == {0.8} and set(df.loc[~df["eligible"], "propensity"]) == {0.2}

    effects = up.average_effects(df)
    assert effects["naive_difference"] > effects["true"] + 0.05  # biased by the design
    assert abs(effects["dr"]["estimate"] - effects["true"]) < 3 * effects["dr"]["se"]
    assert effects["dr"]["se"] < effects["ipw"]["se"]
    assert np.corrcoef(df["tau_dr"], df["tau"])[0, 1] > 0.5

    groups = up.gates(df)
    assert len(groups) == 5 and groups["true_effect"].iloc[-1] > groups["true_effect"].iloc[0]

    policies = up.evaluate_policies(df, cap=2, seed=1).set_index("policy")
    assert policies["pushes"].nunique() == 1  # same budget for every policy
    true = policies["true_effect_per_1000"]
    assert true["oracle"] >= true["DR-learner"] > true["current (click threshold)"]
