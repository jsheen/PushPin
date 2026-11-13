import numpy as np
import pandas as pd
from scipy import sparse

from pushpin.data import load_behaviors, load_entity_embeddings, load_news
from pushpin.features import (
    article_first_seen,
    build_features,
    contrast_features,
    subcat_engagement,
    subcat_novelty,
    temporal_features,
)

T0 = pd.Timestamp("2019-11-09")


def test_article_first_seen():
    beh = pd.DataFrame({"time": [T0, T0 + pd.Timedelta(hours=5), T0 + pd.Timedelta(hours=9)]})
    rows = pd.DataFrame({"row": [0, 1, 2, 2], "news": [1, 1, 2, 0]})
    H = sparse.csr_matrix(np.array([[1, 0, 0, 0]]))  # article 0 is in a pre-period history
    first = article_first_seen(beh, rows, H)
    assert first[0] == T0                           # history article: capped at the log start
    assert first[1] == T0                           # shown in the first impression
    assert first[2] == T0 + pd.Timedelta(hours=9)  # first shown later
    assert np.isnat(first[3])                       # never seen


def _novelty(rows: pd.DataFrame) -> np.ndarray:
    subcat = np.array([0, 1, 2])  # article i is in subcategory i
    H = sparse.csr_matrix((1, 3))  # no history
    return subcat_novelty(rows.assign(subcat=subcat[rows["news"]]), H, subcat, 3)


def test_subcat_novelty_uses_only_earlier_impressions():
    rows = pd.DataFrame({"user": 0, "row": [0, 1, 1], "news": [0, 0, 1]})
    # impression 0: no exposure yet -> 1; impression 1: all earlier exposure was subcat 0
    assert np.allclose(_novelty(rows), [1.0, 0.0, 1.0])
    # a later impression must not change earlier values
    later = pd.concat([rows, pd.DataFrame({"user": 0, "row": [2, 2], "news": [1, 2]})], ignore_index=True)
    assert np.allclose(_novelty(later)[:3], [1.0, 0.0, 1.0])


def test_subcat_engagement_uses_only_earlier_impressions():
    # impression 0: subcat 0 clicked, subcat 1 skipped; impression 1: both shown again
    rows = pd.DataFrame({"user": 0, "row": [0, 0, 1, 1], "subcat": [0, 1, 0, 1], "label": [1, 0, 0, 0]})
    rate, skips = subcat_engagement(rows, prior_rate=np.array([0.1, 0.1]), prior_strength=1)
    assert np.allclose(rate, [0.1, 0.1, (1 + 0.1) / 2, 0.1 / 2])  # no history -> the prior
    assert skips.tolist() == [0, 0, 0, 1]
    later = pd.concat([rows, pd.DataFrame({"user": 0, "row": [2], "subcat": [1], "label": [1]})],
                      ignore_index=True)
    assert np.allclose(subcat_engagement(later, np.array([0.1, 0.1]), 1)[0][:4], rate)


def test_contrast_features():
    vec = np.eye(4, dtype=np.float32)  # article i only matches itself
    H = sparse.csr_matrix(np.array([[1, 0, 0, 0]]))  # pre-period click on article 0
    # impression 0: 1 skipped, 2 clicked; impression 1: 1 clicked; impression 2: scored candidates
    rows = pd.DataFrame({"user": 0, "row": [0, 0, 1, 2, 2, 2], "news": [1, 2, 1, 0, 1, 3],
                         "label": [0, 1, 1, 0, 0, 0]})
    out = contrast_features(rows, H, vec, score=np.array([True, True, False, True, True, True]))
    assert np.allclose(out["emb_sim_clicked_max"].iloc[:2], 0)    # history only (article 0)
    assert out["emb_sim_skipped_max"].iloc[:2].isna().all()      # nothing skipped yet
    assert out.iloc[2].isna().all()                              # not scored
    # by impression 2, article 1 was clicked after being skipped: clicked only, so no skips remain
    assert out["emb_sim_clicked_max"].iloc[5] == 0 and np.isnan(out["emb_sim_skipped_max"].iloc[5])


def test_contrast_prefers_clicked_side():
    vec = np.array([[1, 0], [0, 1], [0.8, 0.6]], dtype=np.float32)
    rows = pd.DataFrame({"user": 0, "row": [0, 0, 1], "news": [0, 1, 2], "label": [1, 0, 0]})
    out = contrast_features(rows, sparse.csr_matrix((1, 3)), vec, score=np.array([False, False, True]))
    assert np.isclose(out["emb_sim_clicked_max"].iloc[2], 0.8)
    assert np.isclose(out["emb_sim_skipped_max"].iloc[2], 0.6)
    assert np.isclose(out["emb_contrast"].iloc[2], 0.2)


def test_user_prior_impr_counts_earlier_impressions_in_any_slot():
    beh = pd.DataFrame({"user_id": ["a", "b", "a", "a"],
                        "time": T0 + pd.to_timedelta([6, 7, 13, 20], unit="h"),
                        "slot": [0, 0, 1, 2], "impressions": "N1-1 N2-0"})
    out = temporal_features(beh, fit_end=T0 + pd.Timedelta(days=1), prior_strength=5)
    assert out["user_prior_impr"].tolist() == [0, 0, 1, 2]


def test_build_features_on_synthetic_data(mind_dir, train_config):
    beh = load_behaviors(mind_dir / "behaviors.tsv")
    rows = build_features(beh, load_news(mind_dir / "news.tsv"),
                          load_entity_embeddings(mind_dir / "entity_embedding.vec"), None, None,
                          pd.Timestamp(train_config["split"]["fit_end"]), train_config["features"], seed=0)
    assert (rows["time"] >= pd.Timestamp(train_config["split"]["fit_end"])).all()
    assert (rows["article_age_hours"] >= 0).all()
    assert rows["novelty"].between(0, 1).all()
    assert rows["subcat_engaged_rate"].between(0, 1).all()
    assert (rows["subcat_prior_skips"] >= 0).all()
    assert "emb_contrast" not in rows  # no vectors given
    cold = beh.loc[beh["history"].str.len() == 0, "user_id"].unique()
    assert (rows.loc[rows["user_id"].isin(cold), "user_history_len"] == 0).all()
    assert (rows.loc[~rows["user_id"].isin(cold), "user_history_len"] > 0).all()


def test_build_features_with_vectors(mind_dir, train_config):
    news = load_news(mind_dir / "news.tsv")
    vec = np.random.default_rng(0).normal(size=(len(news), 8)).astype(np.float32)
    vec /= np.linalg.norm(vec, axis=1, keepdims=True)
    rows = build_features(load_behaviors(mind_dir / "behaviors.tsv"), news,
                          load_entity_embeddings(mind_dir / "entity_embedding.vec"), vec, None,
                          pd.Timestamp(train_config["split"]["fit_end"]), train_config["features"], seed=0)
    # every user has fit-period impressions with a click and a skip, so both sides are known
    assert rows[["emb_sim_clicked_max", "emb_sim_skipped_max"]].notna().all().all()
    assert np.allclose(rows["emb_contrast"], rows["emb_sim_clicked_max"] - rows["emb_sim_skipped_max"])


def test_every_feature_has_a_plot_group():
    from pushpin.features import FEATURES
    from pushpin.plots import GROUPS
    grouped = [f for _, feats in GROUPS.values() for f in feats]
    assert len(grouped) == len(set(grouped)), "a feature is in two plot groups"
    assert set(FEATURES) <= set(grouped), f"no plot group (drawn grey): {set(FEATURES) - set(grouped)}"
