from pathlib import Path

import pandas as pd
import pytest

from pushnyt.data import load_behaviors
from pushnyt.live import load_scorer, parity
from pushnyt.train import train


@pytest.fixture
def run_dir(train_config) -> Path:
    train(train_config)
    return Path(train_config["output"]["dir"])


def test_live_replay_matches_batch_exactly(run_dir):
    report, _ = parity(run_dir, n_users=None, seed=0)
    assert report["rows"] > 0
    for col in ("p_click", "p_click_raw", "novelty", "p_return"):
        assert report[col]["identical"], (col, report[col])
    assert report["auc_live"] == report["auc_batch_same_rows"]


def test_live_replay_on_a_user_sample_still_matches(run_dir):
    # sampled users must still see every user's impressions in the shared article state
    report, _ = parity(run_dir, n_users=5, seed=1)
    assert report["users"] == 5
    assert all(report[c]["identical"] for c in ("p_click", "novelty", "p_return"))


def test_scoring_does_not_change_user_state(run_dir, train_config):
    beh = load_behaviors(Path(train_config["data"]["dir"]) / train_config["data"]["behaviors"])
    scorer = load_scorer(run_dir, beh, pd.Timestamp(train_config["split"]["train_end"]))
    user = next(iter(scorer.log))
    logged = len(scorer.log[user])
    news = list(scorer.g.news_ids[:3])
    first = scorer.score(user, "2019-11-14 09:00", news)
    again = scorer.score(user, "2019-11-14 09:00", news)
    assert first["p_click"].tolist() == again["p_click"].tolist()
    assert len(scorer.log[user]) == logged  # only observe() records events
