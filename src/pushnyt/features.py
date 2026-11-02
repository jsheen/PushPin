"""Features for predicting a click on (user, article) at a time slot (morning/afternoon/evening).

Nine feature groups:
  - time-slot engagement: how the user has engaged in this slot before
  - similarity: candidate article vs. the user's reading history
  - complementarity: Alibaba-style s_ab = p_ab - q_ab over subcategories
    (https://eugeneyan.com/writing/push/), where p = "clicked b after reading a"
    and q = "clicked b instead of a" (a shown and skipped in the same impression)
  - push fatigue: articles recently shown and skipped in this slot, plus overall volume
  - embedding retrieval: candidate vs. history in sentence-embedding space, using the
    abstract vectors in the local vector database (see embed.py)
  - subcategory skips: the user's shrunk click rate and skip count in the candidate's subcategory
  - click/skip contrast: candidate's closest match among what the user clicked vs. what they
    were shown and skipped, in the same embedding space
  - article recency: hours since the article was first seen (MIND has no publish time)
  - user depth: how much we know about the user (pre-period history, impressions logged so far)

Every per-user temporal feature uses only impressions strictly before the current one.
"""

import json

import numpy as np
import pandas as pd
from scipy import sparse
from sklearn.decomposition import TruncatedSVD
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.preprocessing import normalize

from pushnyt.data import explode_impressions
from pushnyt.vectordb import ArticleStore

EMBEDDING_FEATURES = ["emb_sim_mean", "emb_sim_max", "emb_sim_top5", "emb_n_close", "emb_retrieval_rank"]
CONTRAST_FEATURES = ["emb_sim_clicked_max", "emb_sim_skipped_max", "emb_contrast"]
VECTOR_FEATURES = EMBEDDING_FEATURES + CONTRAST_FEATURES  # need the vector database

FEATURES = [
    # time-slot engagement
    "slot", "slot_prior_impr", "slot_prior_clicks", "slot_engaged_rate", "slot_click_share",
    # similarity
    "sim_text", "sim_entity", "subcat_affinity",
    # complementarity
    "complement_score",
    # push fatigue
    "slot_skipped_72h", "slot_skip_rate_72h", "impr_last_24h",
    "hours_since_last_impr", "hours_since_last_slot_impr",
    # embedding retrieval
    *EMBEDDING_FEATURES,
    # subcategory skips
    "subcat_engaged_rate", "subcat_prior_skips",
    # click/skip contrast
    *CONTRAST_FEATURES,
    # article recency
    "article_age_hours",
    # user depth
    "user_history_len", "user_prior_impr",
]


def text_vectors(news: pd.DataFrame, dim: int, seed: int) -> np.ndarray:
    text = news["title"].fillna("") + " " + news["abstract"].fillna("")
    tfidf = TfidfVectorizer(stop_words="english", min_df=2, max_features=50_000,
                            sublinear_tf=True).fit_transform(text)
    return normalize(TruncatedSVD(dim, random_state=seed).fit_transform(tfidf)).astype(np.float32)


def entity_vectors(news: pd.DataFrame, emb: dict[str, np.ndarray]) -> np.ndarray:
    """Mean TransE embedding of the entities in each article's title and abstract."""
    out = np.zeros((len(news), len(next(iter(emb.values())))), np.float32)
    for i, cols in enumerate(zip(news["title_entities"], news["abstract_entities"])):
        vecs = [emb[e["WikidataId"]] for col in cols if isinstance(col, str)
                for e in json.loads(col) if e["WikidataId"] in emb]
        if vecs:
            out[i] = np.mean(vecs, axis=0)
    return normalize(out)


def history_matrix(behaviors: pd.DataFrame, news_idx: dict[str, int]):
    """Sparse users x articles matrix of each user's pre-period click history."""
    users = behaviors.drop_duplicates("user_id")
    user_idx = dict(zip(users["user_id"], range(len(users))))
    rows, cols = [], []
    for u, hist in enumerate(users["history"]):
        ids = [news_idx[n] for n in hist if n in news_idx]
        rows += [u] * len(ids)
        cols += ids
    H = sparse.csr_matrix((np.ones(len(rows), np.float32), (rows, cols)),
                          shape=(len(users), len(news_idx)))
    return H, user_idx


def _onehot(codes: np.ndarray, n: int) -> sparse.csr_matrix:
    return sparse.csr_matrix((np.ones(len(codes), np.float32), (np.arange(len(codes)), codes)),
                             shape=(len(codes), n))


def _pmi(counts: np.ndarray, alpha: float = 1.0) -> np.ndarray:
    n = counts + alpha
    return np.log(n * n.sum() / (n.sum(1, keepdims=True) * n.sum(0, keepdims=True)))


def fit_complements(rows: pd.DataFrame, has_subcat: sparse.csr_matrix, n_sub: int) -> np.ndarray:
    """Subcategory complement matrix s[a, b] = pmi(p)[a, b] - pmi(q)[a, b], diagonal zeroed."""
    clicked = rows[rows["label"] == 1]
    # p: user had read subcategory a, then clicked subcategory b
    p = (has_subcat[clicked["user"].to_numpy()].T @ _onehot(clicked["subcat"].to_numpy(), n_sub))
    # q: a was shown and skipped while b was clicked in the same impression
    imp = rows["row"].factorize()[0]
    by_imp = lambda mask: sparse.csr_matrix(
        (np.ones(mask.sum(), np.float32), (imp[mask], rows["subcat"].to_numpy()[mask])),
        shape=(imp.max() + 1, n_sub))
    q = by_imp(rows["label"].to_numpy() == 0).T @ by_imp(rows["label"].to_numpy() == 1)
    s = _pmi(p.toarray()) - _pmi(q.toarray())
    np.fill_diagonal(s, 0.0)  # same-subcategory overlap is similarity, not complementarity
    return s.astype(np.float32)


def _window_sum(keys: np.ndarray, t: np.ndarray, window: int, weights: np.ndarray) -> np.ndarray:
    """Sum of weights over earlier rows with the same key and time in [t - window, t)."""
    order = np.lexsort((t, keys))
    combined = keys[order].astype(np.int64) * (1 << 40) + t[order]
    lo = np.searchsorted(combined, combined - window, "left")
    hi = np.searchsorted(combined, combined, "left")
    cum = np.concatenate([[0.0], np.cumsum(weights[order])])
    out = np.empty(len(keys))
    out[order] = cum[hi] - cum[lo]
    return out


def temporal_features(beh: pd.DataFrame, fit_end: pd.Timestamp, prior_strength: float) -> pd.DataFrame:
    """Per-impression time-slot engagement and fatigue, using only earlier impressions."""
    # Every MIND impression contains at least one click, so engagement and fatigue
    # are measured per article shown rather than per impression.
    df = beh[["user_id", "time", "slot"]].copy()
    df["n_clicks"] = beh["impressions"].str.count("-1")
    df["n_shown"] = beh["impressions"].str.count(" ") + 1
    df["n_skipped"] = df["n_shown"] - df["n_clicks"]
    fit = df["time"] < fit_end
    base_rate = df.loc[fit, "n_clicks"].sum() / df.loc[fit, "n_shown"].sum()

    by_slot = df.groupby(["user_id", "slot"], sort=False)
    by_user = df.groupby("user_id", sort=False)
    shown_before = by_slot["n_shown"].cumsum() - df["n_shown"]
    user_clicks_before = by_user["n_clicks"].cumsum() - df["n_clicks"]

    out = pd.DataFrame(index=df.index)
    out["slot_prior_impr"] = by_slot.cumcount()
    out["user_prior_impr"] = by_user.cumcount()
    out["slot_prior_clicks"] = by_slot["n_clicks"].cumsum() - df["n_clicks"]
    out["slot_engaged_rate"] = ((out["slot_prior_clicks"] + base_rate * prior_strength)
                                / (shown_before + prior_strength))
    out["slot_click_share"] = out["slot_prior_clicks"] / user_clicks_before.replace(0, np.nan)

    user = df["user_id"].factorize()[0]
    slot_key = user * 3 + df["slot"].to_numpy()
    secs = ((df["time"] - df["time"].min()).dt.total_seconds()).to_numpy().astype(np.int64)
    window = 72 * 3600
    out["slot_skipped_72h"] = _window_sum(slot_key, secs, window, df["n_skipped"].to_numpy(float))
    shown_72h = _window_sum(slot_key, secs, window, df["n_shown"].to_numpy(float))
    out["slot_skip_rate_72h"] = out["slot_skipped_72h"] / np.where(shown_72h > 0, shown_72h, np.nan)
    out["impr_last_24h"] = _window_sum(user, secs, 24 * 3600, np.ones(len(df)))
    out["hours_since_last_impr"] = by_user["time"].diff().dt.total_seconds() / 3600
    out["hours_since_last_slot_impr"] = by_slot["time"].diff().dt.total_seconds() / 3600
    return out


def article_first_seen(beh: pd.DataFrame, rows: pd.DataFrame, H: sparse.csr_matrix) -> np.ndarray:
    """Earliest time each article was shown in any impression, as a proxy for publish time.

    Articles in a pre-period click history predate the log, so they are capped at its start
    (their age is then a lower bound). Never-seen articles are NaT.
    """
    first = np.full(H.shape[1], np.datetime64("NaT", "ns"), "datetime64[ns]")
    shown = pd.Series(beh["time"].to_numpy()[rows["row"]]).groupby(rows["news"].to_numpy()).min()
    first[shown.index] = shown.to_numpy()
    in_history = np.diff(H.tocsc().indptr) > 0
    first[in_history] = np.fmin(first[in_history], beh["time"].min().to_datetime64())
    return first


def subcat_novelty(rows: pd.DataFrame, H: sparse.csr_matrix, subcat: np.ndarray, n_sub: int) -> np.ndarray:
    """1 - share of the user's earlier exposure that was in the candidate's subcategory.

    Exposure = pre-period history clicks plus articles shown in earlier impressions (not the
    current one). 1 = never shown this subcategory; users with no exposure at all get 1.
    Not a model feature: it is the novelty half of the push objective (see objective.py).
    """
    u, b, r = rows["user"].to_numpy(), rows["subcat"].to_numpy(), rows["row"].to_numpy()
    per_sub = rows.groupby(["user", "subcat", "row"]).size()
    per_row = rows.groupby(["user", "row"]).size()
    prior_sub = (per_sub.groupby(level=[0, 1]).cumsum() - per_sub).reindex(
        pd.MultiIndex.from_arrays([u, b, r])).to_numpy()
    prior_all = (per_row.groupby(level=0).cumsum() - per_row).reindex(
        pd.MultiIndex.from_arrays([u, r])).to_numpy()
    hist_sub = (H @ _onehot(subcat, n_sub)).toarray()[u, b]
    hist_all = np.diff(H.indptr)[u]
    total = prior_all + hist_all
    share = (prior_sub + hist_sub) / np.where(total > 0, total, 1)
    return 1.0 - share


def subcat_engagement(rows: pd.DataFrame, prior_rate: np.ndarray, prior_strength: float):
    """The user's click rate and skip count in the candidate's subcategory over earlier impressions.

    The rate is shrunk toward the subcategory's global click rate `prior_rate` with
    `prior_strength` pseudo-impressions, like slot_engaged_rate. Pre-period history is not used:
    it holds clicks only, with nothing shown and skipped.
    """
    u, b, r = rows["user"].to_numpy(), rows["subcat"].to_numpy(), rows["row"].to_numpy()
    per_sub = rows.groupby(["user", "subcat", "row"])["label"].agg(["sum", "size"])
    prior = (per_sub.groupby(level=[0, 1]).cumsum() - per_sub).reindex(
        pd.MultiIndex.from_arrays([u, b, r]))
    clicks, shown = prior["sum"].to_numpy(float), prior["size"].to_numpy(float)
    rate = (clicks + prior_rate[b] * prior_strength) / (shown + prior_strength)
    return rate, shown - clicks


def embedding_features(u: np.ndarray, a: np.ndarray, H: sparse.csr_matrix, abstract_vec: np.ndarray,
                       store: ArticleStore, news_ids: np.ndarray, cfg: dict) -> pd.DataFrame:
    """Candidate vs. the user's history articles in abstract-embedding space (cosine)."""
    n = len(u)
    out = {f: np.full(n, np.nan) for f in ("emb_sim_max", "emb_sim_top5", "emb_n_close")}
    profile = normalize(H @ abstract_vec)
    out["emb_sim_mean"] = np.einsum("ij,ij->i", profile[u], abstract_vec[a])

    # max / top-k / count over the individual history articles, one user at a time
    order = np.argsort(u, kind="stable")
    users, starts = np.unique(u[order], return_index=True)
    for user, idx in zip(users, np.split(order, starts[1:])):
        hist = H.indices[H.indptr[user]:H.indptr[user + 1]]
        if len(hist) == 0:
            continue
        sims = -np.sort(-(abstract_vec[a[idx]] @ abstract_vec[hist].T), axis=1)  # each row descending
        out["emb_sim_max"][idx] = sims[:, 0]
        out["emb_sim_top5"][idx] = sims[:, :cfg["emb_top_k"]].mean(1)
        out["emb_n_close"][idx] = (sims >= cfg["emb_close_threshold"]).sum(1)

    # rank of the candidate among the user's nearest articles in the vector database,
    # skipping articles already in their history; k + 1 = not retrieved
    k = cfg["retrieval_k"]
    users = users[np.diff(H.indptr)[users] > 0]
    nearest = store.nearest(profile[users], 2 * k)
    news_pos = dict(zip(news_ids, range(len(news_ids))))
    ranked = []
    for user, ids in zip(users, nearest):
        hist = set(H.indices[H.indptr[user]:H.indptr[user + 1]])
        hits = [news_pos[i] for i in ids if news_pos[i] not in hist][:k]
        ranked += [(user, art, r) for r, art in enumerate(hits, 1)]
    ranks = pd.DataFrame(ranked, columns=["u", "a", "rank"]).set_index(["u", "a"])["rank"]
    out["emb_retrieval_rank"] = ranks.reindex(pd.MultiIndex.from_arrays([u, a])).fillna(k + 1).to_numpy()
    return pd.DataFrame(out)


def contrast_features(rows: pd.DataFrame, H: sparse.csr_matrix, abstract_vec: np.ndarray,
                      score: np.ndarray) -> pd.DataFrame:
    """Candidate vs. what the user clicked and what they skipped, in abstract-embedding space.

    Clicked = pre-period history plus clicks in earlier impressions; skipped = shown but not
    clicked in earlier impressions, minus anything clicked since. Only rows where `score` is
    set are computed (the rest only build up the sets); NaN where a set is empty.
    """
    u, r, a = rows["user"].to_numpy(), rows["row"].to_numpy(), rows["news"].to_numpy()
    label = rows["label"].to_numpy()
    clicked_max, skipped_max = np.full(len(rows), np.nan), np.full(len(rows), np.nan)
    order = np.lexsort((r, u))
    users, starts = np.unique(u[order], return_index=True)
    for user, idx in zip(users, np.split(order, starts[1:])):
        if not score[idx].any():
            continue
        clicked = set(H.indices[H.indptr[user]:H.indptr[user + 1]].tolist())
        skipped = set()
        for imp in np.split(idx, np.flatnonzero(np.diff(r[idx])) + 1):
            if score[imp[0]]:
                cand = abstract_vec[a[imp]]
                if clicked:
                    clicked_max[imp] = (cand @ abstract_vec[list(clicked)].T).max(1)
                if skipped - clicked:
                    skipped_max[imp] = (cand @ abstract_vec[list(skipped - clicked)].T).max(1)
            clicked.update(a[imp][label[imp] == 1].tolist())
            skipped.update(a[imp][label[imp] == 0].tolist())
    return pd.DataFrame({"emb_sim_clicked_max": clicked_max, "emb_sim_skipped_max": skipped_max,
                         "emb_contrast": clicked_max - skipped_max})


def build_features(beh: pd.DataFrame, news: pd.DataFrame, entity_emb: dict[str, np.ndarray],
                   abstract_vec: np.ndarray | None, store: ArticleStore | None,
                   fit_end: pd.Timestamp, cfg: dict, seed: int) -> pd.DataFrame:
    """Feature rows for every candidate shown at or after `fit_end`.

    Impressions before `fit_end` are used only to fit the complement matrix
    (and as earlier history for the temporal features). With no `store`, the embedding
    retrieval features are skipped.
    """
    news_idx = dict(zip(news["news_id"], range(len(news))))
    subcat, subcat_names = news["subcategory"].factorize()
    n_sub = len(subcat_names)
    text_vec = text_vectors(news, cfg["text_dim"], seed)
    ent_vec = entity_vectors(news, entity_emb)

    H, user_idx = history_matrix(beh, news_idx)
    profile_text = normalize(H @ text_vec)
    profile_ent = normalize(H @ ent_vec)
    subcat_weights = normalize(H @ _onehot(subcat, n_sub), norm="l1").toarray()
    has_history = np.asarray(H.sum(1)).ravel() > 0

    rows = explode_impressions(beh, news_idx)
    rows["user"] = beh["user_id"].map(user_idx).to_numpy()[rows["row"]]
    rows["subcat"] = subcat[rows["news"]]
    is_fit = (beh["time"] < fit_end).to_numpy()[rows["row"]]
    first_seen = article_first_seen(beh, rows, H)  # uses all rows; min over times <= t never looks ahead
    rows["novelty"] = subcat_novelty(rows, H, subcat, n_sub)

    # per-user subcategory engagement, shrunk toward each subcategory's click rate in the fit period
    k = cfg["ctr_prior_strength"]
    fit_sub, fit_label = rows.loc[is_fit, "subcat"].to_numpy(), rows.loc[is_fit, "label"].to_numpy()
    sub_clicks = np.bincount(fit_sub, weights=fit_label, minlength=n_sub)
    sub_shown = np.bincount(fit_sub, minlength=n_sub)
    sub_rate = (sub_clicks + fit_label.mean() * k) / (sub_shown + k)
    rows["subcat_engaged_rate"], rows["subcat_prior_skips"] = subcat_engagement(rows, sub_rate, k)
    if abstract_vec is not None:
        contrast = contrast_features(rows, H, abstract_vec, ~is_fit)
        rows[contrast.columns] = contrast.to_numpy()

    s = fit_complements(rows[is_fit], sparse.csr_matrix(subcat_weights > 0), n_sub)
    complement_by_user = subcat_weights @ s  # weighted mean of s[a, b] over the user's subcategories a

    rows = rows[~is_fit].reset_index(drop=True)
    u, a, b = rows["user"].to_numpy(), rows["news"].to_numpy(), rows["subcat"].to_numpy()
    no_hist = ~has_history[u]

    rows["sim_text"] = np.einsum("ij,ij->i", profile_text[u], text_vec[a])
    rows["sim_entity"] = np.einsum("ij,ij->i", profile_ent[u], ent_vec[a])
    rows["subcat_affinity"] = subcat_weights[u, b]
    other_weight = 1.0 - subcat_weights[u, b]  # exclude the candidate's own subcategory
    rows["complement_score"] = complement_by_user[u, b] / np.where(other_weight > 1e-6, other_weight, np.nan)
    rows.loc[no_hist, ["sim_text", "sim_entity", "subcat_affinity", "complement_score"]] = np.nan
    rows.loc[rows["sim_entity"] == 0, "sim_entity"] = np.nan  # article or history has no entities

    if store is not None:
        emb_cols = embedding_features(u, a, H, abstract_vec, store, news["news_id"].to_numpy(), cfg)
        rows[emb_cols.columns] = emb_cols.to_numpy()
        rows.loc[no_hist, emb_cols.columns] = np.nan

    rows["user_history_len"] = np.diff(H.indptr)[u]  # 0 = cold-start user; deliberately not NaN

    temporal = temporal_features(beh, fit_end, cfg["ctr_prior_strength"])
    rows = rows.join(temporal, on="row")
    rows["slot"] = beh["slot"].to_numpy()[rows["row"]]
    rows["impression_id"] = beh["impression_id"].to_numpy()[rows["row"]]
    rows["user_id"] = beh["user_id"].to_numpy()[rows["row"]]
    rows["time"] = beh["time"].to_numpy()[rows["row"]]
    rows["article_age_hours"] = (rows["time"].to_numpy() - first_seen[a]) / np.timedelta64(1, "h")
    return rows
