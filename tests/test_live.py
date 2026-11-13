from pathlib import Path

import numpy as np
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


def _scorer(run_dir, train_config):
    beh = load_behaviors(Path(train_config["data"]["dir"]) / train_config["data"]["behaviors"])
    return load_scorer(run_dir, beh, beh["time"].max() + pd.Timedelta(seconds=1))


def test_score_many_equals_scoring_each_request_alone(run_dir, train_config):
    scorer = _scorer(run_dir, train_config)
    users, news = list(scorer.log)[:4], list(scorer.g.news_ids[:5])
    requests = [{"user_id": u, "time": "2019-11-15 09:00", "news_ids": news} for u in users]
    together = scorer.score_many(requests)
    for i, r in enumerate(requests):
        alone = scorer.score(r["user_id"], r["time"], r["news_ids"])
        mine = together[together["request"] == i]
        assert mine["p_click"].tolist() == alone["p_click"].tolist()
        assert mine["p_return"].tolist() == alone["p_return"].tolist()
    with pytest.raises(ValueError):
        scorer.score_many(requests[:1] * 2)


def test_added_articles_can_be_scored(run_dir, train_config):
    scorer = _scorer(run_dir, train_config)
    g = scorer.g
    new = pd.DataFrame({"news_id": ["NEW1", "NEW2"], "title": ["Football tonight", "Markets rally"],
                        "abstract": ["A big game.", "Stocks rose."], "subcategory": g.subcat_names[:2]})
    dim = g.text_vec.shape[1]
    scorer.g.abstract_vec = np.zeros((len(g.news_ids), dim), np.float32)  # synthetic run has no vector DB
    scorer.add_articles(new, np.zeros((2, dim), np.float32), np.array(["2019-11-15T08:00"] * 2, "datetime64[ns]"))
    rows = scorer.score(next(iter(scorer.log)), "2019-11-15 09:00", ["NEW1", "NEW2"])
    assert rows["news_id"].tolist() == ["NEW1", "NEW2"]
    assert rows["p_click"].between(0, 1).all()
    assert rows["sim_entity"].isna().all()  # new articles have no entity annotations
    assert (rows["article_age_hours"] == 1.0).all()
    with pytest.raises(ValueError):
        scorer.add_articles(new.assign(subcategory="not-a-subcategory"), np.zeros((2, dim)),
                            np.array(["2019-11-15T08:00"] * 2, "datetime64[ns]"))


def test_new_user_is_scored_as_a_cold_start(run_dir, train_config):
    scorer = _scorer(run_dir, train_config)
    known, news = next(iter(scorer.log)), list(scorer.g.news_ids[:5])
    before = scorer.score(known, "2019-11-15 09:00", news)
    rows = scorer.score_many([{"user_id": "new-a", "time": "2019-11-15 09:00", "news_ids": news},
                              {"user_id": "new-b", "time": "2019-11-15 09:00", "news_ids": news}])
    assert len(rows) == 10 and rows["p_click"].between(0, 1).all() and rows["p_return"].between(0, 1).all()
    assert (rows["user_history_len"] == 0).all() and (rows["user_prior_impr"] == 0).all()
    assert rows[["sim_text", "subcat_affinity", "complement_score"]].isna().all().all()
    assert (rows["novelty"] == 1).all()  # never shown anything
    a, b = (rows[rows["request"] == i]["p_click"].tolist() for i in (0, 1))
    assert a == b  # two new users look the same
    again = scorer.score("new-a", "2019-11-15 09:00", news)  # registered once, scored the same
    assert again["p_click"].tolist() == a
    after = scorer.score(known, "2019-11-15 09:00", news)  # adding users leaves known ones alone
    assert after["p_click"].tolist() == before["p_click"].tolist()
