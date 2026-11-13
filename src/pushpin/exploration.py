"""Exploration conversion: does clicking an unfamiliar subcategory turn into lasting interest?

For each (user, subcategory), take the first impression in the log that showed the user that
subcategory, note whether they clicked it and how unfamiliar it was (novelty at that moment), then
look at their later impressions: among users shown the subcategory again, what share click it?

Clickers are more engaged in general, so the useful comparison is the gap between "clicked the
first time" and "shown and skipped" within each familiarity bucket: if the gap is as large for
never-shown subcategories as for familiar ones, novel clicks stick as well as familiar ones.

Observational, on MIND feed clicks rather than pushes: it shows association, not that a push
causes interest. Writes outputs/analysis/exploration.{csv,png}.
"""

import argparse
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from pushpin.data import explode_impressions, load_behaviors, load_news
from pushpin.features import history_matrix, subcat_novelty
from pushpin.plots import plot_exploration

BUCKETS = ["Never shown", "Rarely shown", "Familiar"]


def familiarity(novelty: np.ndarray, rare: float) -> np.ndarray:
    """Never shown (novelty 1), rarely shown (under `rare` of the user's exposure), or familiar."""
    return np.select([novelty == 1, novelty >= 1 - rare], BUCKETS[:2], BUCKETS[2])


def exploration_events(rows: pd.DataFrame, times: np.ndarray, min_follow_up_hours: float) -> pd.DataFrame:
    """One row per (user, subcategory) first exposure, with what happened afterwards.

    `rows` has one row per shown candidate: user, subcat, row (impression index, in time order),
    label, novelty. `times` gives each impression's time. Events need at least
    `min_follow_up_hours` of log left after them.
    """
    g = (rows.groupby(["user", "subcat", "row"])
         .agg(clicked=("label", "max"), novelty=("novelty", "first")).reset_index())
    g["k"] = g.groupby(["user", "subcat"]).cumcount()  # rows are sorted by impression, i.e. by time
    first = g[g["k"] == 0].set_index(["user", "subcat"])
    later = g[g["k"] > 0].groupby(["user", "subcat"])["clicked"].max()
    first["reexposed"] = first.index.isin(later.index)
    first["clicked_later"] = later.reindex(first.index).to_numpy()
    first["time"] = times[first["row"]]
    end = times.max() - np.timedelta64(int(min_follow_up_hours * 3600), "s")
    return first[first["time"] <= end].reset_index()


def summarize(events: pd.DataFrame, rare: float) -> pd.DataFrame:
    """Later click rate among re-exposed users, by familiarity and first response, with 95% CIs."""
    events = events.assign(bucket=familiarity(events["novelty"].to_numpy(), rare),
                           first=np.where(events["clicked"] == 1, "Clicked the first time", "Shown and skipped"))
    re = events[events["reexposed"]]
    out = re.groupby(["bucket", "first"]).agg(n=("clicked_later", "size"), rate=("clicked_later", "mean"))
    out["ci"] = 1.96 * np.sqrt(out["rate"] * (1 - out["rate"]) / out["n"])
    out["events"] = events.groupby(["bucket", "first"]).size()
    out["reexposed_share"] = out["n"] / out["events"]
    out = out.reset_index()
    out["bucket"] = pd.Categorical(out["bucket"], BUCKETS, ordered=True)
    return out.sort_values(["bucket", "first"]).reset_index(drop=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/train.yml")
    parser.add_argument("--rare", type=float, default=0.1, help="'rarely shown' = under this share of exposure")
    parser.add_argument("--min-follow-up-hours", type=float, default=48)
    parser.add_argument("--output-dir", default="outputs/analysis")
    args = parser.parse_args()
    with open(args.config) as f:
        config = yaml.safe_load(f)
    data_dir = Path(config["data"]["dir"])
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    beh = load_behaviors(data_dir / config["data"]["behaviors"])
    news = load_news(data_dir / config["data"]["news"])
    news_idx = dict(zip(news["news_id"], range(len(news))))
    subcat, names = news["subcategory"].factorize()
    H, user_idx = history_matrix(beh, news_idx)
    rows = explode_impressions(beh, news_idx)
    rows["user"] = beh["user_id"].map(user_idx).to_numpy()[rows["row"]]
    rows["subcat"] = subcat[rows["news"]]
    rows["novelty"] = subcat_novelty(rows, H, subcat, len(names))

    events = exploration_events(rows, beh["time"].to_numpy(), args.min_follow_up_hours)
    summary = summarize(events, args.rare)
    print(summary.round(4).to_string(index=False))
    summary.to_csv(out / "exploration.csv", index=False)
    plot_exploration(summary, out / "exploration.png", args.rare, args.min_follow_up_hours)


if __name__ == "__main__":
    main()
