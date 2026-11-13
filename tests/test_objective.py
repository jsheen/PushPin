import numpy as np
import pandas as pd
import pytest

from pushpin.objective import push_scores, send_decisions, tradeoff


@pytest.fixture
def candidates() -> pd.DataFrame:
    rng = np.random.default_rng(0)
    n_imp, per = 50, 6
    return pd.DataFrame({"impression_id": np.repeat(np.arange(n_imp), per),
                         "p_click": rng.random(n_imp * per),
                         "novelty": rng.random(n_imp * per),
                         "label": rng.integers(0, 2, n_imp * per)})


def _top(df: pd.DataFrame, friction: float) -> pd.DataFrame:
    score = pd.Series(push_scores(df, df["p_click"].to_numpy(), friction))
    return df.loc[score.groupby(df["impression_id"]).idxmax()]


@pytest.mark.parametrize("friction", [-0.1, 1.5])
def test_friction_must_be_in_unit_interval(candidates, friction):
    with pytest.raises(ValueError):
        push_scores(candidates, candidates["p_click"].to_numpy(), friction)


def test_friction_zero_is_the_click_model(candidates):
    top = _top(candidates, 0.0)
    best = candidates.groupby("impression_id")["p_click"].max()
    assert np.allclose(top.set_index("impression_id")["p_click"], best)


def test_friction_one_is_pure_novelty(candidates):
    top = _top(candidates, 1.0)
    best = candidates.groupby("impression_id")["novelty"].max()
    assert np.allclose(top.set_index("impression_id")["novelty"], best)


def test_scores_are_scale_free(candidates):
    """Ranks make the objective ignore monotone rescaling of P(click), e.g. recalibration."""
    p = candidates["p_click"].to_numpy()
    assert np.allclose(push_scores(candidates, p, 0.3), push_scores(candidates, p ** 3 / 10, 0.3))


def test_more_friction_means_more_novel_pushes(candidates):
    curve = tradeoff(candidates, candidates["p_click"].to_numpy(), [0.0, 0.5, 1.0])
    assert curve["novelty_at_1"].is_monotonic_increasing


def _pushes(p_click, p_return, hours, users=None) -> pd.DataFrame:
    return pd.DataFrame({"user_id": users or ["u"] * len(p_click), "p_click": p_click, "p_return": p_return,
                         "time": pd.Timestamp("2019-11-14") + pd.to_timedelta(hours, unit="h")})


def test_send_threshold_rises_with_risk():
    pushes = _pushes([0.10, 0.10], [1.0, 0.0], [1, 2], users=["safe", "at_risk"])
    # threshold 0.08 for the safe user, 0.08 + 0.05 for the at-risk one
    assert send_decisions(pushes, 0.08, 0.05, daily_cap=5).tolist() == [True, False]


def test_daily_cap_keeps_the_earliest_pushes():
    pushes = _pushes([0.2, 0.2, 0.01, 0.2, 0.2], [1.0] * 5, [9, 3, 1, 5, 30])  # last one is the next day
    # by time: 1h (below threshold), 3h, 5h, 9h -> cap of 2 sends 3h and 5h; the next day resets the cap
    assert send_decisions(pushes, 0.1, 0.0, daily_cap=2).tolist() == [False, True, False, True, True]
