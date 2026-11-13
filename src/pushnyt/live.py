"""Live scoring: score one request at a time from stored state, instead of a batch over the log.

State has two parts:
  - frozen at training time: `Globals` (text transform, priors, complement matrix, users'
    pre-period histories, LLM theme profiles) and the click and return models
  - updated as events arrive: each user's impression log, and each article's first-seen time

A request is (user, time, candidate articles). Its features come from the same `features_for`
code as batch training, run on that user's own events plus the request, so the two paths can't
drift apart. `pushnyt-live-parity` replays the test day request by request and checks that the
live features and scores match batch exactly.
"""

import argparse
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb
import yaml
from scipy import sparse
from sklearn.metrics import roc_auc_score

from pushnyt.calibration import apply_platt
from pushnyt.data import (
    explode_impressions,
    load_behaviors,
    load_entity_embeddings,
    load_news,
    time_slot,
)
from pushnyt.features import (
    FEATURES,
    LLM_FEATURES,
    VECTOR_FEATURES,
    Globals,
    article_first_seen,
    features_for,
    fit_globals,
    project_text,
)
from pushnyt.profile import load_profiles
from pushnyt.risk import RETURN_FEATURES
from pushnyt.vectordb import ArticleStore

LOG_COLUMNS = ["impression_id", "user_id", "time", "history", "impressions", "slot"]


class LiveScorer:
    def __init__(self, g: Globals, click_model: xgb.XGBClassifier, platt: dict, features: list[str],
                 return_model: xgb.XGBClassifier, first_seen: np.ndarray):
        self.g, self.click_model, self.platt, self.features = g, click_model, platt, features
        self.return_model = return_model
        self.first_seen = first_seen.copy()  # article state: earliest time shown to anyone
        self.log: dict[str, list[tuple]] = {}  # user state: their impressions, oldest first

    def observe(self, impression: dict) -> None:
        """Record a completed impression (with its click labels) in the user's and articles' state."""
        self.log.setdefault(impression["user_id"], []).append(tuple(impression[c] for c in LOG_COLUMNS))
        self._seen([item[:-2] for item in impression["impressions"].split()], impression["time"])

    def add_user(self, user_id: str) -> None:
        """Register a user the log has never seen: a cold start, with no click history and no
        impressions, like a MIND user on their first impression with an empty history. No-op for
        known users. Each new user gets their own (empty) row, since features group by user row."""
        g = self.g
        if user_id in g.user_idx:
            return
        g.user_idx = {**g.user_idx, user_id: g.H.shape[0]}
        g.H = sparse.vstack([g.H, sparse.csr_matrix((1, g.H.shape[1]), dtype=g.H.dtype)], format="csr")

        def blank(m):
            return np.vstack([m, np.zeros((1, m.shape[1]), m.dtype)])
        g.profile_text, g.profile_ent = blank(g.profile_text), blank(g.profile_ent)
        if g.profile_abs is not None:
            g.profile_abs = blank(g.profile_abs)
        g.subcat_weights, g.complement_by_user = blank(g.subcat_weights), blank(g.complement_by_user)
        g.has_history = np.append(g.has_history, False)

    def _seen(self, news_ids: list[str], when) -> None:
        pos = [self.g.news_idx[n] for n in news_ids if n in self.g.news_idx]
        t = np.datetime64(pd.Timestamp(when).to_datetime64(), "ns")
        self.first_seen[pos] = np.fmin(self.first_seen[pos], t)

    def score(self, user_id: str, when, news_ids: list[str], history: list[str] | None = None,
              impression_id: int = -1) -> pd.DataFrame:
        """P(click), novelty and P(return) for each candidate, from state as of `when`.

        `history` is the user's pre-period click history (MIND carries it on every impression);
        defaults to the history from the user's logged impressions.
        """
        return self.score_many([{"user_id": user_id, "time": when, "news_ids": news_ids,
                                 "history": history, "impression_id": impression_id}])

    def score_many(self, requests: list[dict]) -> pd.DataFrame:
        """Score several requests (one per user) in a single pass of the feature code.

        Each request: user_id, time, news_ids, and optionally history and impression_id. Features
        are per-user, so this equals scoring each request alone; it is just faster.
        """
        users = [r["user_id"] for r in requests]
        if len(set(users)) != len(users):
            raise ValueError("score_many takes at most one request per user")
        for u in users:
            self.add_user(u)
        events, request_rows, history_len = [], [], []
        for r in requests:
            past = self.log.get(r["user_id"], [])
            history = r.get("history")
            if history is None:
                history = past[0][3] if past else []
            when = pd.Timestamp(r["time"])
            # articles in this impression are being shown now (batch first-seen includes the current one)
            self._seen(r["news_ids"], when)
            events += past
            request_rows.append(len(events))
            history_len.append(len(history))
            events.append((r.get("impression_id", -1), r["user_id"], when, history,
                           " ".join(f"{n}-0" for n in r["news_ids"]), int(time_slot(pd.Series([when.hour]))[0])))
        events = pd.DataFrame(events, columns=LOG_COLUMNS)
        events["time"] = events["time"].astype("datetime64[us]")
        rows = features_for(events, self.g, self.first_seen)
        which = pd.Series(np.arange(len(requests)), index=request_rows)
        rows = rows[rows["row"].isin(which.index)].reset_index(drop=True)
        rows["request"] = which.reindex(rows["row"]).to_numpy()
        rows["news_id"] = self.g.news_ids[rows["news"].to_numpy()]
        rows["p_click_raw"] = self.click_model.predict_proba(rows[self.features])[:, 1]
        rows["p_click"] = apply_platt(rows["p_click_raw"].to_numpy(), self.platt)
        # P(return) is per request: its features are the same on every candidate row
        first = rows.drop_duplicates("request")
        X = first.assign(hour=first["time"].dt.hour,
                         user_history_len=np.asarray(history_len)[first["request"].to_numpy()])[RETURN_FEATURES]
        p_return = pd.Series(self.return_model.predict_proba(X)[:, 1], index=first["request"].to_numpy())
        rows["p_return"] = p_return.reindex(rows["request"]).to_numpy()
        return rows

    def add_articles(self, news: pd.DataFrame, abstract_vec: np.ndarray, published: np.ndarray) -> None:
        """Add new articles to the catalog (article state), after training.

        `news` has news_id, title, abstract and subcategory (one of the training subcategories);
        text vectors are projected with the training text transform, and articles have no
        entity vectors. `published` (datetime64) is each article's first-seen time.
        """
        g = self.g
        codes = pd.Index(g.subcat_names).get_indexer(news["subcategory"])
        if (codes < 0).any():
            raise ValueError("new articles must use training subcategories")
        n_old, n_new = len(g.news_ids), len(news)
        g.news_ids = np.concatenate([g.news_ids, news["news_id"].to_numpy()])
        g.news_idx = {**g.news_idx, **dict(zip(news["news_id"], range(n_old, n_old + n_new)))}
        g.subcat = np.concatenate([g.subcat, codes])
        g.text_vec = np.vstack([g.text_vec, project_text(news, g.text_model)])
        g.ent_vec = np.vstack([g.ent_vec, np.zeros((n_new, g.ent_vec.shape[1]), g.ent_vec.dtype)])
        g.abstract_vec = np.vstack([g.abstract_vec, abstract_vec.astype(g.abstract_vec.dtype)])
        g.H = sparse.csr_matrix((g.H.data, g.H.indices, g.H.indptr), shape=(g.H.shape[0], n_old + n_new))
        self.first_seen = np.concatenate([self.first_seen, np.asarray(published, "datetime64[ns]")])


def load_scorer(run_dir: Path, beh: pd.DataFrame, up_to: pd.Timestamp) -> LiveScorer:
    """A scorer for a training run, with state built from every impression before `up_to`.

    Globals are refitted from the log the way training fitted them (deterministic, same seed);
    a production service would load them from the run folder instead.
    """
    config = yaml.safe_load((run_dir / "config.yml").read_text())
    data_dir = Path(config["data"]["dir"])
    features = [f for f in FEATURES if f not in config["features"].get("exclude", [])]
    news = load_news(data_dir / config["data"]["news"])
    emb = load_entity_embeddings(data_dir / config["data"]["entity_embeddings"])
    store = abstract_vec = None
    if set(VECTOR_FEATURES) & set(features):
        store = ArticleStore(data_dir / config["embeddings"]["chroma_dir"], config["embeddings"]["collection"])
        abstract_vec = store.vectors(news["news_id"].tolist())
    profiles = load_profiles(data_dir, config) if set(LLM_FEATURES) & set(features) else None
    g = fit_globals(beh, news, emb, abstract_vec, store, pd.Timestamp(config["split"]["fit_end"]),
                    config["features"], config["seed"], profiles)

    click_model, return_model = xgb.XGBClassifier(), xgb.XGBClassifier()
    click_model.load_model(run_dir / "xgb_click.json")
    return_model.load_model(run_dir / "xgb_return.json")
    platt = json.loads((run_dir / "metrics.json").read_text())["calibration"]["platt"]

    # article state at the start: articles in pre-period histories predate the log
    no_rows = pd.DataFrame({"row": np.array([], np.int64), "news": np.array([], np.int64)})
    first_seen = article_first_seen(beh, no_rows, g.H)
    scorer = LiveScorer(g, click_model, platt, features, return_model, first_seen)
    for rec in beh[beh["time"] < up_to][LOG_COLUMNS].to_dict("records"):
        scorer.observe(rec)
    return scorer


def parity(run_dir: Path, n_users: int | None, seed: int) -> dict:
    """Replay the test day through the live scorer and compare with the batch predictions."""
    config = yaml.safe_load((run_dir / "config.yml").read_text())
    data_dir = Path(config["data"]["dir"])
    beh = load_behaviors(data_dir / config["data"]["behaviors"])
    test_start = pd.Timestamp(config["split"]["train_end"])
    print("building state from every impression before", test_start.date())
    scorer = load_scorer(run_dir, beh, test_start)

    day = beh[beh["time"] >= test_start]
    scored_users = set(day["user_id"])
    if n_users:
        scored_users = set(day["user_id"].drop_duplicates().sample(n_users, random_state=seed))
    test = day[day["user_id"].isin(scored_users)]
    batch = pd.read_parquet(run_dir / "predictions.parquet").set_index(["impression_id", "news_id"])
    print(f"replaying {len(day):,} impressions, scoring {len(test):,} requests from {len(scored_users):,} users")

    # Every impression updates state (article first-seen times are shared across users), but
    # only the sampled users' impressions are scored.
    out, latency = [], []
    for rec in day[LOG_COLUMNS].to_dict("records"):
        if rec["user_id"] in scored_users:
            news_ids = [item[:-2] for item in rec["impressions"].split()]
            start = time.perf_counter()
            scored = scorer.score(rec["user_id"], rec["time"], news_ids, rec["history"], rec["impression_id"])
            latency.append(time.perf_counter() - start)
            out.append(scored[["impression_id", "news_id", "label", "p_click", "p_click_raw", "novelty",
                               "p_return", *scorer.features]])
        scorer.observe(rec)  # the outcome arrives after the request
    live = pd.concat(out, ignore_index=True)
    # the request carries no labels; attach the true ones for evaluation
    live["label"] = test.pipe(explode_impressions, scorer.g.news_idx)["label"].to_numpy()

    ref = batch.reindex(pd.MultiIndex.from_frame(live[["impression_id", "news_id"]]))
    report = {"requests": len(test), "rows": len(live), "users": int(test["user_id"].nunique()),
              "latency_ms": {"median": 1000 * float(np.median(latency)),
                             "p95": 1000 * float(np.percentile(latency, 95))}}
    for col in ("p_click", "p_click_raw", "novelty", "p_return"):
        a, b = live[col].to_numpy(), ref[col].to_numpy()
        report[col] = {"identical": bool(np.array_equal(a, b)), "max_abs_diff": float(np.nanmax(np.abs(a - b)))}
    report["auc_live"] = float(roc_auc_score(live["label"], live["p_click"]))
    report["auc_batch_same_rows"] = float(roc_auc_score(ref["label"], ref["p_click"]))
    return report, live


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay the test day through live scoring and compare with batch")
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--users", type=int, help="only this many test-day users (default: all)")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    report, live = parity(args.run_dir, args.users, args.seed)
    print(json.dumps(report, indent=2))
    name = f"live_parity{'_' + str(args.users) if args.users else ''}"
    (args.run_dir / f"{name}.json").write_text(json.dumps(report, indent=2))
    live.to_parquet(args.run_dir / f"{name}_features.parquet", index=False)


if __name__ == "__main__":
    main()
