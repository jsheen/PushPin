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

Usage: pushnyt-nyt-sim outputs/ablation-with-llm [--users 1000] [--start 2024-12-02] [--days 7]
Writes outputs/nyt-sim/: scores.parquet, pushes.parquet, daily.csv, summary.json.
"""

import argparse
import ast
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
import yaml
from sklearn.preprocessing import normalize

from pushnyt.data import load_behaviors
from pushnyt.live import load_scorer
from pushnyt.objective import push_scores, send_decisions
from pushnyt.vectordb import ExactStore

SLOT_HOURS = (8, 13, 19)  # morning, afternoon, evening push times (New York time)
MIN_SUBCAT_ARTICLES = 20  # map only to MIND subcategories with enough articles for a stable centroid


def load_nyt(path: Path, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """NYT articles published in [start, end), New York time, oldest first."""
    df = pd.read_csv(path, usecols=["_id", "headline", "abstract", "pub_date", "document_type",
                                    "section_name", "subsection_name", "web_url"])
    df["published"] = (pd.to_datetime(df["pub_date"], utc=True).dt.tz_convert("America/New_York")
                       .dt.tz_localize(None))
    df = df[(df["document_type"] == "article") & (df["published"] >= start) & (df["published"] < end)]
    df = df[df["abstract"].fillna("").str.strip() != ""]
    title = df["headline"].map(lambda h: ast.literal_eval(h).get("main", "") if isinstance(h, str) else "")
    return pd.DataFrame({
        "news_id": "NYT-" + df["_id"].str.rsplit("/", n=1).str[-1],
        "title": title.to_numpy(), "abstract": df["abstract"].str.strip().to_numpy(),
        "section": df["section_name"].fillna("").to_numpy(), "subsection": df["subsection_name"].fillna("").to_numpy(),
        "url": df["web_url"].to_numpy(), "published": df["published"].to_numpy(),
    }).sort_values("published", kind="stable").reset_index(drop=True)


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


def slot_times(start: pd.Timestamp, days: int) -> list[pd.Timestamp]:
    return [start + pd.Timedelta(days=d, hours=h) for d in range(days) for h in SLOT_HOURS]


def simulate(run_dir: Path, n_users: int, start: pd.Timestamp, days: int, seed: int) -> dict:
    config = yaml.safe_load((run_dir / "config.yml").read_text())
    data_dir = Path(config["data"]["dir"])
    objective = yaml.safe_load(Path("configs/objective.yml").read_text())
    beh = load_behaviors(data_dir / config["data"]["behaviors"])
    log_end = beh["time"].max()
    print("building user state from the whole MIND log")
    scorer = load_scorer(run_dir, beh, log_end + pd.Timedelta(seconds=1))
    g = scorer.g

    # the week's NYT articles, plus the hours before the first slot (the first slot's candidates)
    slots = slot_times(start, days)
    nyt = load_nyt(data_dir / "nyt-recent.csv", slots[0] - pd.Timedelta(hours=13), slots[-1])
    print(f"{len(nyt):,} NYT articles from {nyt['published'].min()} to {nyt['published'].max()}")
    shift = (log_end.normalize() + pd.Timedelta(days=1)) - start  # NYT time -> just after the MIND log
    vec = embed(nyt["abstract"].tolist(), config)
    n_mind = len(g.news_ids)
    nyt["subcategory"], nyt["subcat_sim"] = map_subcategories(vec, g.abstract_vec[:n_mind], g.subcat[:n_mind],
                                                              g.subcat_names)
    scorer.add_articles(nyt, vec, (nyt["published"] + shift).to_numpy())
    g.store = ExactStore(g.news_ids, g.abstract_vec)  # MIND + NYT, searchable up to "now"

    test_users = beh.loc[beh["time"] >= pd.Timestamp(config["split"]["train_end"]), "user_id"].drop_duplicates()
    users = test_users.sample(min(n_users, len(test_users)), random_state=seed).tolist()
    print(f"simulating {len(users):,} users x {len(slots)} slots")

    scores, latency = [], []
    previous = slots[0] - pd.Timedelta(hours=13)
    for s, t in enumerate(slots):
        cand = nyt[(nyt["published"] > previous) & (nyt["published"] <= t)]
        previous = t
        if cand.empty:
            continue
        g.store.size = n_mind + int((nyt["published"] <= t).sum())  # articles published so far
        for i in range(0, len(users), 250):
            batch = users[i:i + 250]
            requests = [{"user_id": u, "time": t + shift, "news_ids": cand["news_id"].tolist(),
                         "impression_id": s * 1_000_000 + i + j} for j, u in enumerate(batch)]
            begin = time.perf_counter()
            rows = scorer.score_many(requests)
            latency.append((time.perf_counter() - begin) / len(batch))
            scores.append(rows.assign(slot_time=t)[
                ["impression_id", "user_id", "slot_time", "news_id", "p_click", "p_click_raw", "novelty",
                 "p_return", "llm_sim_click", "llm_sim_skip", "emb_sim_max", "subcat_affinity",
                 "article_age_hours"]])
        print(f"  {t:%a %b %d %H:%M}  {len(cand):3d} candidates")

    scores = pd.concat(scores, ignore_index=True)
    scores["push_score"] = push_scores(scores, scores["p_click"].to_numpy(), objective["friction"])
    top = scores.loc[scores.groupby("impression_id")["push_score"].idxmax()].merge(
        nyt[["news_id", "title", "section", "subsection", "subcategory", "published", "url"]], on="news_id")
    top["time"] = top["slot_time"]  # send policy caps pushes per user per calendar day
    send = objective["send"]
    top["sent"] = send_decisions(top, send["threshold"], send["risk_penalty"], send["daily_cap"])
    return scores, top.drop(columns="time"), summarize(scores, top, nyt, latency, objective)


def summarize(scores: pd.DataFrame, top: pd.DataFrame, nyt: pd.DataFrame, latency: list[float],
              objective: dict) -> dict:
    sent = top[top["sent"]]
    by_imp = scores.groupby("impression_id")
    return {
        "users": int(scores["user_id"].nunique()), "slots": int(scores["slot_time"].nunique()),
        "nyt_articles": len(nyt), "user_article_scores": len(scores), "decisions": len(top),
        "pushes_sent": len(sent), "send_rate": float(top["sent"].mean()),
        "pushes_per_user_per_day": float(len(sent) / scores["user_id"].nunique()
                                         / top["slot_time"].dt.normalize().nunique()),
        "friction": objective["friction"],
        "latency_ms_per_request": 1000 * float(np.mean(latency)),
        # proxy checks, since there are no clicks: is the pick closer to the user than the average candidate?
        "pick_vs_average_candidate": {
            col: {"pick": float(top[col].mean()), "average": float(by_imp[col].mean().mean())}
            for col in ("p_click", "llm_sim_click", "llm_sim_skip", "emb_sim_max", "novelty")},
        "distinct_sections_pushed_per_user": float(sent.groupby("user_id")["section"].nunique().mean()),
        "section_share_sent": sent["section"].value_counts(normalize=True).head(10).round(3).to_dict(),
        "section_share_published": nyt["section"].value_counts(normalize=True).head(10).round(3).to_dict(),
        "subcategory_mapping_similarity": {"median": float(nyt["subcat_sim"].median()),
                                           "p10": float(nyt["subcat_sim"].quantile(0.1))},
    }


def daily_report(scores: pd.DataFrame, pushes: pd.DataFrame) -> pd.DataFrame:
    """One row per simulated day: what was published, what was pushed, and to whom.

    expected_clicks sums the predicted P(click) of sent pushes; the model is not calibrated on
    NYT, so compare it across days rather than reading it as a click count.
    """
    day = lambda df: df["slot_time"].dt.normalize()
    sent = pushes[pushes["sent"]]
    by_sent = sent.groupby(day(sent))
    published = scores.groupby(day(scores))["news_id"].nunique()
    pushed = by_sent["news_id"].nunique()

    def top(col):
        def f(x):
            counts = x.value_counts()
            return counts.index[0], counts.iloc[0] / len(x)
        return by_sent[col].agg(f)

    top_section, top_title = top("section"), top("title")
    out = pd.DataFrame({
        "articles": published,
        "decisions": pushes.groupby(day(pushes)).size(),
        "sent": by_sent.size(),
        "users_reached": by_sent["user_id"].nunique(),
        "expected_clicks": by_sent["p_click"].sum(),
        "mean_p_click_sent": by_sent["p_click"].mean(),
        "mean_novelty_sent": by_sent["novelty"].mean(),
        "share_of_articles_pushed": pushed / published,
        "top_section": top_section.str[0],
        "top_section_share": top_section.str[1],
        "most_sent_title": top_title.str[0],
        "most_sent_users": by_sent["title"].agg(lambda x: x.value_counts().iloc[0]),
    }).fillna({"sent": 0, "users_reached": 0, "expected_clicks": 0, "share_of_articles_pushed": 0})
    out["send_rate"] = out["sent"] / out["decisions"]
    out.index = out.index.strftime("%Y-%m-%d")
    return out.rename_axis("day").reset_index()


def main() -> None:
    parser = argparse.ArgumentParser(description="Push NYT articles to MIND users, scored live")
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--users", type=int, default=1000)
    parser.add_argument("--start", default="2024-12-02", help="first day (NYT time); dense weeks: Dec 2022-May 2023, Dec 2023, Dec 2024")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path, default=Path("outputs/nyt-sim"))
    args = parser.parse_args()
    scores, pushes, summary = simulate(args.run_dir, args.users, pd.Timestamp(args.start), args.days, args.seed)
    args.out.mkdir(parents=True, exist_ok=True)
    scores.to_parquet(args.out / "scores.parquet", index=False)
    pushes.to_parquet(args.out / "pushes.parquet", index=False)
    daily = daily_report(scores, pushes)
    daily.to_csv(args.out / "daily.csv", index=False, float_format="%.4g")
    (args.out / "summary.json").write_text(json.dumps({"run": str(args.run_dir), **summary}, indent=2))
    print(json.dumps(summary, indent=2))
    print(daily[["day", "articles", "sent", "users_reached", "expected_clicks", "top_section"]].to_string(index=False))


if __name__ == "__main__":
    main()
