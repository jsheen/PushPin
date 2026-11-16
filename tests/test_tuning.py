import json

import numpy as np
import pandas as pd
import pytest
import yaml

from pushpin.train import impression_aucs, train
from pushpin.tuning import fit_gp, from_unit, group_auc, propose, sample, search, to_unit

SPACE = {"max_depth": ["int", 4, 10], "min_child_weight": ["log", 1, 50],
         "subsample": ["uniform", 0.5, 1.0], "gamma": ["uniform", 0, 5]}


def test_sample_respects_ranges_and_types():
    rng = np.random.default_rng(0)
    draws = pd.DataFrame([sample(SPACE, rng) for _ in range(2000)])
    assert draws["max_depth"].map(type).eq(int).all()
    assert set(draws["max_depth"]) == set(range(4, 11))  # both ends inclusive
    for name, (_, lo, hi) in SPACE.items():
        assert draws[name].between(lo, hi).all(), name
    # log-uniform: the median sits at the geometric mean (~7.1), well below the midpoint (25.5)
    assert np.median(draws["min_child_weight"]) == pytest.approx(np.sqrt(50), rel=0.1)
    assert np.median(draws["subsample"]) == pytest.approx(0.75, abs=0.02)


def test_sample_rejects_unknown_distribution():
    with pytest.raises(ValueError, match="unknown distribution"):
        sample({"max_depth": ["normal", 4, 10]}, np.random.default_rng(0))


def test_group_auc_matches_impression_aucs():
    rng = np.random.default_rng(1)
    n_imp, per = 200, 12
    df = pd.DataFrame({"impression_id": np.repeat(np.arange(n_imp), per),
                       "label": rng.integers(0, 2, n_imp * per)})
    df.loc[df["impression_id"] < 5, "label"] = 0  # all-skip impressions are left out by both
    scores = np.round(rng.random(len(df)) + 0.3 * df["label"].to_numpy(), 1)  # ties
    expected = impression_aucs(df, scores).mean()
    assert group_auc(df["impression_id"].to_numpy(), df["label"].to_numpy(), scores) == pytest.approx(expected)


def test_unit_cube_round_trip():
    rng = np.random.default_rng(3)
    for _ in range(200):
        p = sample(SPACE, rng)
        assert from_unit(to_unit(p, SPACE), SPACE) == pytest.approx(p, rel=1e-3)
    assert to_unit({"max_depth": 4, "min_child_weight": 0.1, "subsample": 2, "gamma": 0}, SPACE) == \
        pytest.approx([1 / 14, 0, 1, 0])  # out-of-range values clip to the ends


def test_bayes_proposals_move_toward_the_optimum():
    """On a smooth bowl peaking at subsample=0.9, gamma=1, proposals land closer than random draws."""
    space = {"subsample": ["uniform", 0.5, 1.0], "gamma": ["uniform", 0, 5]}
    rng = np.random.default_rng(4)
    score = lambda p: -((p["subsample"] - 0.9) / 0.5) ** 2 - ((p["gamma"] - 1) / 5) ** 2
    tried = [sample(space, rng) for _ in range(8)]
    for _ in range(8):
        X = np.array([to_unit(p, space) for p in tried])
        tried.append(propose(X, np.array([score(p) for p in tried]), space, rng, seed=0))
    random_best = max(score(p) for p in tried[:8])
    assert max(score(p) for p in tried[8:]) > random_best
    gp = fit_gp(np.array([to_unit(p, space) for p in tried]), np.array([score(p) for p in tried]), 0)
    assert gp.kernel_.k1.k2.length_scale.shape == (2,)


def _toy(n=4000):
    rng = np.random.default_rng(2)
    x = rng.normal(size=(n, 3))
    df = pd.DataFrame(x, columns=["a", "b", "c"]).assign(
        label=(x[:, 0] + rng.normal(size=n) > 1).astype(int), impression_id=np.arange(n) // 20)
    base = {"n_estimators": 30, "max_depth": 3, "learning_rate": 0.3, "subsample": 1.0,
            "min_child_weight": 1, "gamma": 0, "early_stopping_rounds": 5}
    return df[:3000], df[3000:], base


@pytest.mark.parametrize("method", ["random", "bayes"])
def test_search_includes_config_and_picks_by_gp(method):
    fit, valid, base = _toy()
    params, trials, summary = search(fit, valid, ["a", "b", "c"], base,
                                     {"trials": 6, "method": method, "init_trials": 3, "space": SPACE}, seed=0)
    assert list(trials["trial"]) == list(range(7))
    assert trials.loc[0, "source"] == "config" and trials.loc[0, "max_depth"] == 3
    expected = {"random": {"random"}, "bayes": {"random", "bayes"}}[method]
    assert set(trials.loc[1:, "source"]) == expected
    best = trials.loc[trials["gp_predicted"].idxmax()]
    assert trials["chosen"].sum() == 1 and best["chosen"] and summary["best_trial"] == best["trial"]
    assert params["max_depth"] == best["max_depth"] and params["learning_rate"] == 0.3
    assert isinstance(params["max_depth"], int) and set(summary["length_scales"]) == set(SPACE)


def test_train_with_tuning(train_config, tmp_path):
    train_config["tuning"]["trials"] = 2
    train(train_config)
    run = tmp_path / "run"
    trials = pd.read_csv(run / "tuning.csv")
    assert len(trials) == 3 and (run / "tuning.png").exists()
    metrics = json.loads((run / "metrics.json").read_text())
    assert metrics["tuning"]["trials"] == 2 and metrics["tuning"]["method"] == "bayes"
    saved = yaml.safe_load((run / "config.yml").read_text())
    assert saved["tuning"]["trials"] == 0  # rerunning the saved config reproduces the final model
    assert saved["model"]["max_depth"] == metrics["tuning"]["best"]["params"]["max_depth"]
