"""Contextual bandit: the Bayesian linear model, the planted world and the policy comparison."""
import numpy as np
import pandas as pd
import pytest

from pushpin import bandit as bd

CFG = {
    "seed": 3, "weeks": 2, "fatigue_hours": 4,
    "thresholds": [0.05, 0.5],
    "planted": {"organic": 0.25, "lift": 0.25, "interest_floor": 0.3, "fatigue": 0.04, "peaks": [9, 15],
                "pref_floor": 0.4, "pref_width_hours": 3, "annoyance_half_life_days": 3, "tolerance_median": 10,
                "tolerance_spread": 0.5, "tolerance_open_elasticity": 0.5, "optout_hazard": 0.0004,
                "optout_risk_elasticity": 1.0},
    "bandit": {"prior_sd": 1.0, "noise_sd": 0.4},
}


@pytest.fixture
def data(tmp_path):
    """A tiny sim: 30 readers, 7 days x 2 slots (9 and 15), 4 candidates per slot, one of them the newest."""
    rng = np.random.default_rng(0)
    rows = []
    for day in range(7):
        for hour in (9, 15):
            t = pd.Timestamp("2024-12-02") + pd.Timedelta(days=day, hours=hour)
            for u in range(30):
                for k in range(4):
                    rows.append({"user_id": f"U{u:02d}", "slot_time": t, "news_id": f"N{day}-{hour}-{k}",
                                 "p_click": rng.uniform(0.01, 0.3), "p_return": 0.3 + 0.02 * u,
                                 "subcat_affinity": rng.uniform(0, 0.2), "push_score": rng.random(),
                                 "is_newest": k == 3})
    pd.DataFrame(rows).to_parquet(tmp_path / "scores.parquet", index=False)
    return bd.load_candidates(tmp_path, interest_floor=0.3)


def test_load_candidates_shapes_and_order(data):
    assert data["n"] == 30 and data["n_slots"] == 14 and data["article"].shape == (14, 30, 4)
    assert (data["article"] >= 0).all()
    assert data["hours"].tolist() == [9, 15] * 7
    assert ((data["interest"] >= 0.3) & (data["interest"] <= 1)).all()
    # the newest article is the same for every reader at a slot
    assert (data["newest_article"] == data["newest_article"][:, :1]).all()


def test_linear_thompson_recovers_a_known_effect():
    rng = np.random.default_rng(1)
    model = bd.LinearThompson(d=2, prior_sd=1.0, noise_sd=0.3, seed=0)
    theta_true = np.array([-0.1, 0.4])  # sending helps only when the second feature is large
    for _ in range(50):
        z = np.column_stack([np.ones(200), rng.random(200)])
        a = rng.random(200) < 0.5
        r = 0.2 * z[:, 1] + a * (z @ theta_true) + 0.3 * rng.standard_normal(200)
        model.add(z, a.astype(float), r)
    model.refit()
    mean, sd = model.theta_mean_sd()
    assert mean == pytest.approx(theta_true, abs=0.05)
    assert (sd < 0.05).all()
    draws = model.sample_theta(2000)
    assert draws.mean(axis=0) == pytest.approx(mean, abs=0.01)


def test_planted_world_and_policies(data):
    traits = bd.reader_traits(data, CFG["planted"], CFG["seed"])
    assert traits["pref"].shape == (30, 2) and traits["pref"].mean(axis=1) == pytest.approx(np.ones(30))
    never, _ = bd.simulate(data, traits, CFG, "never", 0.04, 5)
    again, _ = bd.simulate(data, traits, CFG, "never", 0.04, 5)
    assert never["pushes"].sum() == 0 and never["optouts"].sum() == 0
    assert never["sessions"].tolist() == again["sessions"].tolist()  # common random numbers
    breaking, _ = bd.simulate(data, traits, CFG, "breaking", 0.04, 5)
    assert breaking["pushes"].sum() > 0
    assert (breaking["pushes"] <= breaking["active"] + breaking["optouts"]).all()  # only subscribed readers get pushes
    low, _ = bd.simulate(data, traits, CFG, "threshold", 0.04, 5, threshold=0.05)
    high, _ = bd.simulate(data, traits, CFG, "threshold", 0.04, 5, threshold=0.5)
    assert low["pushes"].sum() > high["pushes"].sum() == 0
    log, model = bd.simulate(data, traits, CFG, "bandit", 0.04, 5)
    assert len(log) == 2 * 7 * 2 and model.mean.shape == (2 * len(bd.FEATURES),)


def test_compare_scores_every_policy(data):
    traits = bd.reader_traits(data, CFG["planted"], CFG["seed"])
    table, _logs, _model, baselines = bd.compare(data, traits, CFG, 0.04, 5)
    assert set(table["policy"]) == {"contextual bandit", "never", "breaking", "p_click >= 0.05", "p_click >= 0.5"}
    assert table["best_threshold"].sum() == 1
    assert table.loc[table["policy"] == "never", "reward_vs_never"].item() == 0
    # baselines are reused across prices: only their scoring changes
    again, *_ = bd.compare(data, traits, CFG, 0.08, 5, baselines)
    b4 = table.set_index("policy").loc["breaking"]
    b8 = again.set_index("policy").loc["breaking"]
    assert b8["pushes_per_reader_day"] == b4["pushes_per_reader_day"]
    assert b8["reward_per_reader_day"] == pytest.approx(b4["reward_per_reader_day"] - 0.04 * b4["pushes_per_reader_day"])
