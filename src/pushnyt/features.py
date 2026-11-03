"""Features for predicting a click on (user, article) at a time slot (morning/afternoon/evening).

Ten feature groups:
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
  - LLM reader profile: candidate vs. its best-matching LLM theme of what the user clicks and
    what they skip, written from retrieved clicks and nearest skips (see profile.py)
  - article recency: hours since the article was first seen (MIND has no publish time)
  - user depth: how much we know about the user (pre-period history, impressions logged so far)

Every per-user temporal feature uses only impressions strictly before the current one.
"""

import json
from dataclasses import dataclass

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
LLM_FEATURES = ["llm_sim_click", "llm_sim_skip", "llm_contrast"]
VECTOR_FEATURES = EMBEDDING_FEATURES + CONTRAST_FEATURES + LLM_FEATURES  # need the vector database

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
    # LLM reader profile
    *LLM_FEATURES,
    # article recency
    "article_age_hours",
    # user depth
    "user_history_len", "user_prior_impr",
]


def _article_text(news: pd.DataFrame) -> pd.Series:
    return news["title"].fillna("") + " " + news["abstract"].fillna("")


def text_vectors(news: pd.DataFrame, dim: int, seed: int, return_model: bool = False):
    """TF-IDF -> SVD vectors of title + abstract, fitted on `news`; with return_model, also the
    fitted (tfidf, svd) pair, to project articles added later (project_text)."""
    tfidf = TfidfVectorizer(stop_words="english", min_df=2, max_features=50_000, sublinear_tf=True)
    svd = TruncatedSVD(dim, random_state=seed)
    vec = normalize(svd.fit_transform(tfidf.fit_transform(_article_text(news)))).astype(np.float32)
    return (vec, (tfidf, svd)) if return_model else vec


def project_text(news: pd.DataFrame, model: tuple) -> np.ndarray:
    """Text vectors for new articles in an already-fitted text space."""
    tfidf, svd = model
    return normalize(svd.transform(tfidf.transform(_article_text(news)))).astype(np.float32)


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


CHUNK = 100_000  # rows per slice when gathering per-row vectors; bounds peak memory


def _rowdot(A: np.ndarray, ia: np.ndarray, B: np.ndarray, ib: np.ndarray) -> np.ndarray:
    """A[ia[i]] . B[ib[i]] for each i, without materializing A[ia] and B[ib] in full."""
    out = np.empty(len(ia), np.result_type(A, B))
    for s in range(0, len(ia), CHUNK):
        out[s:s + CHUNK] = np.einsum("ij,ij->i", A[ia[s:s + CHUNK]], B[ib[s:s + CHUNK]])
    return out


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


def fit_base_rate(beh: pd.DataFrame, fit_end: pd.Timestamp) -> float:
    """Click rate per article shown over impressions before fit_end."""
    fit = beh["time"] < fit_end
    clicks = beh.loc[fit, "impressions"].str.count("-1").sum()
    shown = (beh.loc[fit, "impressions"].str.count(" ") + 1).sum()
    return clicks / shown


def temporal_features(beh: pd.DataFrame, fit_end: pd.Timestamp, prior_strength: float,
                      base_rate: float | None = None) -> pd.DataFrame:
    """Per-impression time-slot engagement and fatigue, using only earlier impressions.

    `base_rate` (the fit-period click rate the engagement rate shrinks toward) is computed from
    `beh` unless given; live scoring passes the training value, since it sees one user at a time.
    """
    # Every MIND impression contains at least one click, so engagement and fatigue
    # are measured per article shown rather than per impression.
    df = beh[["user_id", "time", "slot"]].copy()
    df["n_clicks"] = beh["impressions"].str.count("-1")
    df["n_shown"] = beh["impressions"].str.count(" ") + 1
    df["n_skipped"] = df["n_shown"] - df["n_clicks"]
    if base_rate is None:
        base_rate = fit_base_rate(beh, fit_end)

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
                       store: ArticleStore, news_pos: dict[str, int], cfg: dict,
                       profile: np.ndarray | None = None) -> pd.DataFrame:
    """Candidate vs. the user's history articles in abstract-embedding space (cosine).

    `profile` = normalize(H @ abstract_vec), the users' mean history embeddings, if precomputed.
    """
    n = len(u)
    out = {f: np.full(n, np.nan) for f in ("emb_sim_max", "emb_sim_top5", "emb_n_close")}
    if profile is None:
        profile = normalize(H @ abstract_vec)
    out["emb_sim_mean"] = _rowdot(profile, u, abstract_vec, a)

    # max / top-k / count over the individual history articles, one user at a time
    order = np.argsort(u, kind="stable")
    users, starts = np.unique(u[order], return_index=True)
    groups = np.split(order, starts[1:])  # row indices of each user's candidates
    for user, idx in zip(users, groups):
        hist = H.indices[H.indptr[user]:H.indptr[user + 1]]
        if len(hist) == 0:
            continue
        sims = -np.sort(-(abstract_vec[a[idx]] @ abstract_vec[hist].T), axis=1)  # each row descending
        out["emb_sim_max"][idx] = sims[:, 0]
        out["emb_sim_top5"][idx] = sims[:, :cfg["emb_top_k"]].mean(1)
        out["emb_n_close"][idx] = (sims >= cfg["emb_close_threshold"]).sum(1)

    # rank of the candidate among the user's nearest articles in the vector database,
    # skipping articles already in their history; k + 1 = not retrieved
    # Users are queried in batches and each batch is turned into ranks right away, so only
    # one batch of retrieved IDs is ever held in memory.
    k = cfg["retrieval_k"]
    out["emb_retrieval_rank"] = np.full(n, k + 1, np.float64)
    keep = np.flatnonzero(np.diff(H.indptr)[users] > 0)
    for s in range(0, len(keep), 2000):
        batch = keep[s:s + 2000]
        for j, ids in zip(batch, store.nearest(profile[users[batch]], 2 * k)):
            user, idx = users[j], groups[j]
            hist = set(H.indices[H.indptr[user]:H.indptr[user + 1]])
            hits = [p for p in map(news_pos.__getitem__, ids) if p not in hist][:k]
            rank = dict(zip(hits, range(1, len(hits) + 1)))
            out["emb_retrieval_rank"][idx] = [rank.get(art, k + 1) for art in a[idx].tolist()]
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


def profile_lookup(keys: pd.DataFrame) -> tuple[np.ndarray, pd.Index, np.ndarray]:
    """(profile days, user index, days x users table of profile rows, -1 = none)."""
    days = np.sort(keys["day"].unique())
    # a small days x users table; a MultiIndex over every impression row hashes millions of
    # (day, user_id string) pairs and costs far more memory
    user_index = pd.Index(keys["user_id"].unique())
    lut = np.full((len(days), len(user_index)), -1, np.int64)
    lut[np.searchsorted(days, keys["day"].to_numpy()), user_index.get_indexer(keys["user_id"])] = np.arange(len(keys))
    return days, user_index, lut


def llm_features(rows: pd.DataFrame, abstract_vec: np.ndarray,
                 profiles: tuple[pd.DataFrame, np.ndarray, np.ndarray], lookup=None) -> pd.DataFrame:
    """Candidate vs. the user's LLM click and skip themes: cosine similarity to the best-matching
    theme, from the latest profile at or before the impression's day (each profile only saw data
    before its day). Theme vectors are (n_profiles, n_themes, dim), NaN-padded. NaN without a
    profile, or without any theme of that kind. `lookup` is profile_lookup(keys), if precomputed."""
    keys, click_vec, skip_vec = profiles
    days, user_index, lut = lookup if lookup is not None else profile_lookup(keys)
    i = np.searchsorted(days, rows["time"].to_numpy(), "right") - 1
    uc = user_index.get_indexer(rows["user_id"])
    p = np.full(len(rows), -1, np.int64)
    ok = (i >= 0) & (uc >= 0)
    p[ok] = lut[i[ok], uc[ok]]
    has = np.flatnonzero(p >= 0)
    prof, news = p[has], rows["news"].to_numpy()[has]
    sims = {}
    for name, vecs in (("llm_sim_click", click_vec), ("llm_sim_skip", skip_vec)):
        sims[name] = np.full(len(rows), np.nan)
        for s in range(0, len(has), CHUNK):  # gathering (rows, themes, dim) at once needs ~15 GB
            sl = slice(s, s + CHUNK)
            per_theme = np.einsum("itd,id->it", vecs[prof[sl]], abstract_vec[news[sl]])  # NaN = padded
            best = np.where(np.isnan(per_theme), -np.inf, per_theme).max(axis=1)
            sims[name][has[sl]] = np.where(np.isfinite(best), best, np.nan)
    sims["llm_contrast"] = sims["llm_sim_click"] - sims["llm_sim_skip"]
    return pd.DataFrame(sims)


@dataclass
class Globals:
    """Everything feature computation needs beyond a user's own events, fitted once on the log.

    Batch training fits these from all impressions; live scoring reuses them and computes
    features for one user at a time from that user's events (see live.py).
    """
    fit_end: pd.Timestamp
    cfg: dict
    news_ids: np.ndarray
    news_idx: dict[str, int]
    subcat: np.ndarray            # subcategory code per article
    subcat_names: np.ndarray      # subcategory name per code
    n_sub: int
    text_vec: np.ndarray          # TF-IDF -> SVD, fitted on all articles
    text_model: tuple             # the fitted (tfidf, svd), to project new articles
    ent_vec: np.ndarray
    abstract_vec: np.ndarray | None
    store: ArticleStore | None
    profiles: tuple | None        # LLM theme profiles (keys, click vectors, skip vectors)
    profile_lookup: tuple | None
    H: sparse.csr_matrix          # users x articles pre-period click history
    user_idx: dict[str, int]
    profile_text: np.ndarray
    profile_ent: np.ndarray
    profile_abs: np.ndarray | None  # users' mean history embedding (for embedding retrieval)
    subcat_weights: np.ndarray
    has_history: np.ndarray
    complement_by_user: np.ndarray
    sub_rate: np.ndarray          # fit-period click rate per subcategory (shrunk)
    base_rate: float              # fit-period click rate per article shown


def fit_globals(beh: pd.DataFrame, news: pd.DataFrame, entity_emb: dict[str, np.ndarray],
                abstract_vec: np.ndarray | None, store: ArticleStore | None,
                fit_end: pd.Timestamp, cfg: dict, seed: int,
                profiles: tuple[pd.DataFrame, np.ndarray, np.ndarray] | None = None,
                rows: pd.DataFrame | None = None) -> Globals:
    """Fit the shared state from the impression log (`rows` = its exploded candidates, if at hand)."""
    news_idx = dict(zip(news["news_id"], range(len(news))))
    subcat, subcat_names = news["subcategory"].factorize()
    n_sub = len(subcat_names)
    text_vec, text_model = text_vectors(news, cfg["text_dim"], seed, return_model=True)
    ent_vec = entity_vectors(news, entity_emb)

    H, user_idx = history_matrix(beh, news_idx)
    subcat_weights = normalize(H @ _onehot(subcat, n_sub), norm="l1").toarray()

    if rows is None:
        rows = _candidate_rows(beh, news_idx, user_idx, subcat)
    is_fit = (beh["time"] < fit_end).to_numpy()[rows["row"]]
    # per-subcategory click rate in the fit period, shrunk toward the overall rate
    k = cfg["ctr_prior_strength"]
    fit_sub, fit_label = rows.loc[is_fit, "subcat"].to_numpy(), rows.loc[is_fit, "label"].to_numpy()
    sub_clicks = np.bincount(fit_sub, weights=fit_label, minlength=n_sub)
    sub_shown = np.bincount(fit_sub, minlength=n_sub)
    sub_rate = (sub_clicks + fit_label.mean() * k) / (sub_shown + k)
    s = fit_complements(rows[is_fit], sparse.csr_matrix(subcat_weights > 0), n_sub)

    return Globals(
        fit_end=fit_end, cfg=cfg, news_ids=news["news_id"].to_numpy(), news_idx=news_idx,
        subcat=subcat, subcat_names=np.asarray(subcat_names), n_sub=n_sub, text_vec=text_vec, text_model=text_model, ent_vec=ent_vec, abstract_vec=abstract_vec,
        store=store, profiles=profiles,
        profile_lookup=profile_lookup(profiles[0]) if profiles is not None else None,
        H=H, user_idx=user_idx, profile_text=normalize(H @ text_vec), profile_ent=normalize(H @ ent_vec),
        profile_abs=normalize(H @ abstract_vec) if store is not None else None,
        subcat_weights=subcat_weights, has_history=np.asarray(H.sum(1)).ravel() > 0,
        complement_by_user=subcat_weights @ s,  # weighted mean of s[a, b] over the user's subcategories a
        sub_rate=sub_rate, base_rate=fit_base_rate(beh, fit_end))


def _candidate_rows(beh: pd.DataFrame, news_idx: dict[str, int], user_idx: dict[str, int],
                    subcat: np.ndarray) -> pd.DataFrame:
    rows = explode_impressions(beh, news_idx)
    rows["user"] = beh["user_id"].map(user_idx).to_numpy()[rows["row"]]
    rows["subcat"] = subcat[rows["news"]]
    return rows


def features_for(beh: pd.DataFrame, g: Globals, first_seen: np.ndarray,
                 rows: pd.DataFrame | None = None) -> pd.DataFrame:
    """Feature rows for every candidate in `beh` shown at or after g.fit_end.

    Earlier impressions in `beh` only build up per-user history (temporal, subcategory and
    click/skip features). `beh` can be the whole log (batch) or one user's events ending with
    the impression being scored (live): every feature depends only on the user's own earlier
    events and on `g`, plus `first_seen` (earliest time each article was shown to anyone).
    """
    cfg, H = g.cfg, g.H
    if rows is None:
        rows = _candidate_rows(beh, g.news_idx, g.user_idx, g.subcat)
    is_fit = (beh["time"] < g.fit_end).to_numpy()[rows["row"]]
    rows["novelty"] = subcat_novelty(rows, H, g.subcat, g.n_sub)
    rows["subcat_engaged_rate"], rows["subcat_prior_skips"] = subcat_engagement(
        rows, g.sub_rate, cfg["ctr_prior_strength"])
    if g.abstract_vec is not None:
        contrast = contrast_features(rows, H, g.abstract_vec, ~is_fit)
        rows[contrast.columns] = contrast.to_numpy()

    rows = rows[~is_fit].reset_index(drop=True)
    u, a, b = rows["user"].to_numpy(), rows["news"].to_numpy(), rows["subcat"].to_numpy()
    no_hist = ~g.has_history[u]

    rows["sim_text"] = _rowdot(g.profile_text, u, g.text_vec, a)
    rows["sim_entity"] = _rowdot(g.profile_ent, u, g.ent_vec, a)
    rows["subcat_affinity"] = g.subcat_weights[u, b]
    other_weight = 1.0 - g.subcat_weights[u, b]  # exclude the candidate's own subcategory
    rows["complement_score"] = g.complement_by_user[u, b] / np.where(other_weight > 1e-6, other_weight, np.nan)
    rows.loc[no_hist, ["sim_text", "sim_entity", "subcat_affinity", "complement_score"]] = np.nan
    rows.loc[rows["sim_entity"] == 0, "sim_entity"] = np.nan  # article or history has no entities

    if g.store is not None:
        emb_cols = embedding_features(u, a, H, g.abstract_vec, g.store, g.news_idx, cfg, g.profile_abs)
        rows[emb_cols.columns] = emb_cols.to_numpy()
        rows.loc[no_hist, emb_cols.columns] = np.nan

    rows["user_history_len"] = np.diff(H.indptr)[u]  # 0 = cold-start user; deliberately not NaN

    temporal = temporal_features(beh, g.fit_end, cfg["ctr_prior_strength"], g.base_rate)
    rows = rows.join(temporal, on="row")
    rows["slot"] = beh["slot"].to_numpy()[rows["row"]]
    rows["impression_id"] = beh["impression_id"].to_numpy()[rows["row"]]
    rows["user_id"] = beh["user_id"].to_numpy()[rows["row"]]
    rows["time"] = beh["time"].to_numpy()[rows["row"]]
    rows["article_age_hours"] = (rows["time"].to_numpy() - first_seen[a]) / np.timedelta64(1, "h")
    if g.profiles is not None:
        rows[LLM_FEATURES] = llm_features(rows, g.abstract_vec, g.profiles, g.profile_lookup).to_numpy()
    return rows


def build_features(beh: pd.DataFrame, news: pd.DataFrame, entity_emb: dict[str, np.ndarray],
                   abstract_vec: np.ndarray | None, store: ArticleStore | None,
                   fit_end: pd.Timestamp, cfg: dict, seed: int,
                   profiles: tuple[pd.DataFrame, np.ndarray, np.ndarray] | None = None) -> pd.DataFrame:
    """Feature rows for every candidate shown at or after `fit_end` (batch).

    Impressions before `fit_end` are used only to fit the shared state (complement matrix,
    click-rate priors) and as earlier history for the per-user features. With no `store`, the
    embedding retrieval features are skipped.
    """
    news_idx = dict(zip(news["news_id"], range(len(news))))
    subcat = news["subcategory"].factorize()[0]
    user_idx = history_matrix(beh, news_idx)[1]
    rows = _candidate_rows(beh, news_idx, user_idx, subcat)
    g = fit_globals(beh, news, entity_emb, abstract_vec, store, fit_end, cfg, seed, profiles, rows)
    first_seen = article_first_seen(beh, rows, g.H)  # min over times <= t never looks ahead
    return features_for(beh, g, first_seen, rows)
