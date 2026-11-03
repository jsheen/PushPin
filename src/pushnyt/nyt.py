"""Simulation: push real NYT articles to MIND readers, scored live as the articles are published.

The model was trained on MIND (Microsoft News, Nov 2019). Here it scores NYT articles from
`data/nyt-recent.csv` for a sample of MIND users, three push slots a day for a week:

  - Users keep their real MIND state (pre-period history, logged impressions, LLM themes) as of
    the end of the log. It stays frozen: there are no NYT clicks, so nothing is invented.
  - Each NYT article is embedded with the training MiniLM model, projected into the training
    text space, and mapped to the nearest MIND subcategory (centroid of that subcategory's
    abstract embeddings). NYT has no entity annotations, so entity similarity is missing.
  - Article age comes from the real publish time. The retrieval index holds MIND plus the NYT
    articles published so far. The NYT week is shifted to start the day after the MIND log, so
    gaps since a user's last visit stay realistic; reported times are the real NYT times.
  - At each slot, a user's candidates are the articles published since the previous slot. The
    push objective and send policy from configs/objective.yml pick and gate one push.

There are no labels, so this demonstrates the mechanics; it does not measure accuracy. The model
is applied to a different outlet and era, so P(click) is a ranking signal here, not a rate.

`LocalScorer` is the scoring side: it holds the model and user state. The push job itself
(slots, objective, send policy, reports) is in push_job.py. `pushnyt-nyt-sim` runs both in one
process; `pushnyt-scorer` serves LocalScorer over HTTP for the networked version.

Usage: pushnyt-nyt-sim outputs/ablation-with-llm [--users 1000] [--start 2024-12-02] [--days 7]
Writes outputs/nyt-sim/: scores.parquet, pushes.parquet, daily.csv, summary.json.
"""

import argparse
from pathlib import Path

import numpy as np
import pandas as pd
import yaml
from sklearn.preprocessing import normalize

from pushnyt.data import load_behaviors
from pushnyt.live import load_scorer
from pushnyt.push_job import SCORE_COLUMNS, run_week, write_outputs
from pushnyt.vectordb import ExactStore

MIN_SUBCAT_ARTICLES = 20  # map only to MIND subcategories with enough articles for a stable centroid


def embed(texts: list[str], config: dict) -> np.ndarray:
    from sentence_transformers import SentenceTransformer  # heavy; only this step needs it

    emb = config["embeddings"]
    model = SentenceTransformer(emb["model"], cache_folder=str(Path(config["data"]["dir"]) / "models"))
    return model.encode(texts, batch_size=emb["batch_size"], normalize_embeddings=True, convert_to_numpy=True)


def map_subcategories(vec: np.ndarray, mind_vec: np.ndarray, mind_subcat: np.ndarray,
                      names: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Nearest MIND subcategory centroid for each vector: (subcategory names, cosine similarity)."""
    counts = np.bincount(mind_subcat, minlength=len(names))
    keep = np.flatnonzero(counts >= MIN_SUBCAT_ARTICLES)
    sums = np.zeros((len(names), mind_vec.shape[1]), np.float32)
    np.add.at(sums, mind_subcat, mind_vec)
    centroids = normalize(sums[keep])
    sims = vec @ centroids.T
    best = sims.argmax(axis=1)
    return names[keep[best]], sims[np.arange(len(vec)), best]


def release_memory() -> None:
    """Collect garbage and, on Linux (glibc), return freed heap pages to the OS. Without the
    trim, a container keeps its peak RSS from startup even after the temporaries are gone."""
    import ctypes
    import gc

    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass  # not glibc (e.g. macOS)


class LocalScorer:
    """The scorer interface (see push_job.py) backed by an in-process LiveScorer.

    User state is the whole MIND log of the run's data; new articles join the catalog through
    add_articles; the retrieval index only searches articles published by each request's time.
    """

    def __init__(self, run_dir: Path, embedder=embed):
        self.config = yaml.safe_load((run_dir / "config.yml").read_text())
        data_dir = Path(self.config["data"]["dir"])
        beh = load_behaviors(data_dir / self.config["data"]["behaviors"])
        self.log_end = beh["time"].max()
        print("building user state from the whole MIND log")
        self.live = load_scorer(run_dir, beh, self.log_end + pd.Timedelta(seconds=1))
        self.n_mind = len(self.live.g.news_ids)
        test_start = pd.Timestamp(self.config["split"]["train_end"])
        self.test_users = beh.loc[beh["time"] >= test_start, "user_id"].drop_duplicates()
        self.embedder = embedder
        self.live.g.store = ExactStore(self.live.g.news_ids, self.live.g.abstract_vec)
        del beh
        release_memory()  # hand the build's temporaries back before the embedder loads torch


    def info(self) -> dict:
        return {"log_end": self.log_end.isoformat(), "articles": len(self.live.g.news_ids),
                "users": len(self.live.log)}

    def users(self, n: int, seed: int) -> list[str]:
        return self.test_users.sample(min(n, len(self.test_users)), random_state=seed).tolist()

    def add_articles(self, articles: pd.DataFrame) -> pd.DataFrame:
        """Embed and map new articles (news_id, title, abstract, published; oldest first) and
        add them to the catalog. Returns news_id, subcategory, subcat_sim."""
        g = self.live.g
        vec = self.embedder(articles["abstract"].tolist(), self.config)
        subcat, sim = map_subcategories(vec, g.abstract_vec[:self.n_mind], g.subcat[:self.n_mind], g.subcat_names)
        published = pd.to_datetime(articles["published"]).to_numpy()
        self.live.add_articles(articles.assign(subcategory=subcat), vec, published)
        g.store = ExactStore(g.news_ids, g.abstract_vec)
        return pd.DataFrame({"news_id": articles["news_id"].to_numpy(), "subcategory": subcat, "subcat_sim": sim})

    def score(self, requests: list[dict]) -> pd.DataFrame:
        """Score a batch of requests; the retrieval index sees only articles published by then."""
        latest = np.datetime64(max(pd.Timestamp(r["time"]) for r in requests).to_datetime64(), "ns")
        self.live.g.store.size = self.n_mind + int((self.live.first_seen[self.n_mind:] <= latest).sum())
        return self.live.score_many(requests)[SCORE_COLUMNS]


def main() -> None:
    parser = argparse.ArgumentParser(description="Push NYT articles to MIND users, scored live (one process)")
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--users", type=int, default=1000)
    parser.add_argument("--start", default="2024-12-02", help="first day (NYT time); dense weeks: Dec 2022-May 2023, Dec 2023, Dec 2024")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path, default=Path("outputs/nyt-sim"))
    args = parser.parse_args()
    scorer = LocalScorer(args.run_dir)
    objective = yaml.safe_load(Path("configs/objective.yml").read_text())
    data_dir = Path(scorer.config["data"]["dir"])
    scores, pushes, summary = run_week(scorer, data_dir / "nyt-recent.csv", args.users, pd.Timestamp(args.start),
                                       args.days, args.seed, objective)
    write_outputs(args.out, scores, pushes, {"run": str(args.run_dir), **summary})


if __name__ == "__main__":
    main()
