"""The push job: at each slot, pick and gate one push per user from newly published articles.

It never computes features itself. It talks to a *scorer*, either in the same process
(`nyt.LocalScorer`, used by `pushpin-nyt-sim`) or over HTTP (`HttpScorer`, which calls the
`pushpin-scorer` service; see scorer_api.py). Both answer the same four calls:

  info()                  -> {"log_end": ...}  end of the user-state log
  users(n, seed)          -> sample of users to push to
  add_articles(articles)  -> ingest new articles; returns their mapped subcategory
  score(requests)         -> per-candidate scores for a batch of (user, time, candidates)

This module needs only the core dependencies (pandas, numpy, pyyaml; HTTP via the standard
library), so it runs in the small objective image. With docker compose:

  docker compose up scorer -d          # loads the model and MIND user state, then serves :8000
  docker compose run --rm push-job     # calls http://scorer:8000 by service name

Writes <out>/: scores.parquet, pushes.parquet, daily.csv, summary.json (same as pushpin-nyt-sim).
"""

import argparse
import ast
import json
import time
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from pushpin.objective import push_scores, send_decisions
from pushpin.plots import plot_reach

SLOT_HOURS = (8, 13, 19)  # morning, afternoon, evening push times (New York time)
LOOKBACK_HOURS = 13       # the first slot's candidates: articles since the previous evening
USERS_PER_REQUEST = 250
SCORE_COLUMNS = ["impression_id", "user_id", "news_id", "p_click", "p_click_raw", "novelty", "p_return",
                 "llm_sim_click", "llm_sim_skip", "emb_sim_max", "subcat_affinity", "article_age_hours"]


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


def slot_times(start: pd.Timestamp, days: int) -> list[pd.Timestamp]:
    return [start + pd.Timedelta(days=d, hours=h) for d in range(days) for h in SLOT_HOURS]


def run_week(scorer, nyt_path: Path, n_users: int, start: pd.Timestamp, days: int, seed: int,
             objective: dict) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Ingest the week's articles, score every user at every slot, pick and gate pushes."""
    slots = slot_times(start, days)
    nyt = load_nyt(nyt_path, slots[0] - pd.Timedelta(hours=LOOKBACK_HOURS), slots[-1])
    print(f"{len(nyt):,} NYT articles from {nyt['published'].min()} to {nyt['published'].max()}")
    # shift NYT time to just after the user-state log, so gaps since each user's last visit stay realistic
    shift = (pd.Timestamp(scorer.info()["log_end"]).normalize() + pd.Timedelta(days=1)) - start
    mapped = scorer.add_articles(nyt[["news_id", "title", "abstract"]].assign(published=nyt["published"] + shift))
    nyt = nyt.merge(mapped, on="news_id")
    users = scorer.users(n_users, seed)
    print(f"simulating {len(users):,} users x {len(slots)} slots")

    scores, latency = [], []
    previous = slots[0] - pd.Timedelta(hours=LOOKBACK_HOURS)
    for s, t in enumerate(slots):
        cand = nyt[(nyt["published"] > previous) & (nyt["published"] <= t)]
        previous = t
        if cand.empty:
            continue
        for i in range(0, len(users), USERS_PER_REQUEST):
            batch = users[i:i + USERS_PER_REQUEST]
            requests = [{"user_id": u, "time": (t + shift).isoformat(), "news_ids": cand["news_id"].tolist(),
                         "impression_id": s * 1_000_000 + i + j} for j, u in enumerate(batch)]
            begin = time.perf_counter()
            rows = scorer.score(requests)
            latency.append((time.perf_counter() - begin) / len(batch))
            scores.append(rows.assign(slot_time=t))
        print(f"  {t:%a %b %d %H:%M}  {len(cand):3d} candidates")

    scores = pd.concat(scores, ignore_index=True)[["impression_id", "user_id", "slot_time", *SCORE_COLUMNS[2:]]]
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
        "section_share_sent": sent["section"].value_counts(normalize=True).round(4).to_dict(),
        "section_share_published": nyt["section"].value_counts(normalize=True).round(4).to_dict(),
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


def write_outputs(out: Path, scores: pd.DataFrame, pushes: pd.DataFrame, summary: dict) -> None:
    out.mkdir(parents=True, exist_ok=True)
    scores.to_parquet(out / "scores.parquet", index=False)
    pushes.to_parquet(out / "pushes.parquet", index=False)
    daily = daily_report(scores, pushes)
    daily.to_csv(out / "daily.csv", index=False, float_format="%.4g")
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    plot_reach(pushes, daily, summary["section_share_published"], summary["users"], summary["nyt_articles"],
               out / "reach.png")
    print(json.dumps(summary, indent=2))
    print(daily[["day", "articles", "sent", "users_reached", "expected_clicks", "top_section"]].to_string(index=False))


class HttpScorer:
    """The scorer interface over HTTP, against a running pushpin-scorer service."""

    def __init__(self, url: str, timeout: float = 600, auth: str | None = None):
        """auth="gcp": send a Google ID token (from the metadata server) for a private Cloud Run scorer."""
        self.url, self.timeout, self.auth = url.rstrip("/"), timeout, auth
        self._token, self._token_at = "", 0.0

    def _headers(self) -> dict:
        headers = {"content-type": "application/json"}
        if self.auth == "gcp":
            if time.time() - self._token_at > 30 * 60:   # tokens last an hour
                req = urllib.request.Request(
                    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/"
                    f"identity?audience={self.url}", headers={"Metadata-Flavor": "Google"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    self._token, self._token_at = resp.read().decode(), time.time()
            headers["authorization"] = f"Bearer {self._token}"
        return headers

    def _call(self, path: str, payload: dict | None = None):
        data = None if payload is None else json.dumps(payload).encode()
        req = urllib.request.Request(self.url + path, data=data, headers=self._headers())
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read())  # floats round-trip exactly; NaN is allowed (missing features)

    def info(self) -> dict:
        return self._call("/health")

    def users(self, n: int, seed: int) -> list[str]:
        return self._call(f"/users?n={n}&seed={seed}")["users"]

    def add_articles(self, articles: pd.DataFrame) -> pd.DataFrame:
        payload = articles.assign(published=articles["published"].map(pd.Timestamp.isoformat))
        return pd.DataFrame(self._call("/articles", {"articles": payload.to_dict("records")}))

    def score(self, requests: list[dict], features: bool = False, articles: pd.DataFrame | None = None) -> pd.DataFrame:
        """`articles` (news_id, title, abstract, published) are added first if the scorer lacks them."""
        payload = {"requests": requests, **({"features": True} if features else {})}
        if articles is not None:
            payload["articles"] = articles.assign(published=articles["published"].map(pd.Timestamp.isoformat)
                                                  ).to_dict("records")
        return pd.DataFrame(self._call("/score", payload))


def main() -> None:
    parser = argparse.ArgumentParser(description="Push job: score users through a pushpin-scorer service")
    parser.add_argument("--scorer", default="http://scorer:8000", help="scorer service URL")
    parser.add_argument("--nyt", type=Path, default=Path("data/nyt-recent.csv"), help="the news feed")
    parser.add_argument("--users", type=int, default=1000)
    parser.add_argument("--start", default="2024-12-02")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--objective", type=Path, default=Path("configs/objective.yml"))
    parser.add_argument("--out", type=Path, default=Path("outputs/nyt-sim-http"))
    args = parser.parse_args()
    scorer = HttpScorer(args.scorer)
    print(f"scorer at {args.scorer}: {scorer.info()}")
    objective = yaml.safe_load(args.objective.read_text())
    scores, pushes, summary = run_week(scorer, args.nyt, args.users, pd.Timestamp(args.start), args.days,
                                       args.seed, objective)
    write_outputs(args.out, scores, pushes, {"scorer": args.scorer, **summary})


if __name__ == "__main__":
    main()
