"""Fatigue: do users engage less after heavy recent exposure?

Compares each user against their own usual engagement, because heavy users differ from light
ones. For every impression, expected clicks = impression size x the global click rate for
impressions of that size (MIND only logs impressions with a click, which inflates small ones),
scaled by the user's own ratio of actual to expected clicks. The fatigue index for a bucket of
recent exposure is actual / expected clicks: 1.0 = as engaged as this user usually is, below 1
= fatigued. Two views: impressions in the previous 24 hours (short-term) and impressions
earlier in the log (cumulative over the week). 95% CIs resample users.

Observational and on feed impressions, which users start themselves; pushes are sent to users
unprompted, so push fatigue is likely stronger than anything visible here. Writes
outputs/analysis/fatigue.{csv,png}.
"""

import argparse
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from pushpin.data import load_behaviors
from pushpin.features import temporal_features
from pushpin.plots import plot_fatigue

VIEWS = {
    "impr_last_24h": ("Impressions in the previous 24 hours", [-1, 0, 1, 2, 4, np.inf], ["0", "1", "2", "3–4", "5+"]),
    "user_prior_impr": ("Impressions earlier in the week", [-1, 0, 2, 5, 10, np.inf], ["0", "1–2", "3–5", "6–10", "11+"]),
}


def impression_table(beh: pd.DataFrame, temporal: pd.DataFrame, min_impressions: int) -> pd.DataFrame:
    """Per impression: clicks, expected clicks for this user at this size, and exposure so far."""
    d = pd.DataFrame({"user": beh["user_id"].to_numpy(), "clicks": beh["impressions"].str.count("-1").to_numpy(),
                      "size": beh["impressions"].str.count(" ").to_numpy() + 1})
    d[list(VIEWS)] = temporal[list(VIEWS)].to_numpy()
    size_bin = pd.qcut(d["size"], 20, duplicates="drop")
    by_size = d.groupby(size_bin, observed=True)
    d["expected"] = d["size"] * (by_size["clicks"].transform("sum") / by_size["size"].transform("sum"))
    d = d[d.groupby("user")["user"].transform("size") >= min_impressions].copy()
    by_user = d.groupby("user")
    d["expected"] *= by_user["clicks"].transform("sum") / by_user["expected"].transform("sum")
    return d


def fatigue_index(d: pd.DataFrame, col: str, bins: list, labels: list[str], n_boot: int, seed: int) -> pd.DataFrame:
    """Actual / expected clicks per exposure bucket, with user-resampled 95% CIs."""
    bucket = pd.cut(d[col], bins, labels=labels)
    users, u = np.unique(d["user"], return_inverse=True)
    k = bucket.cat.codes.to_numpy()
    shape = (len(users), len(labels))
    clicks, expected = np.zeros(shape), np.zeros(shape)
    np.add.at(clicks, (u, k), d["clicks"].to_numpy())
    np.add.at(expected, (u, k), d["expected"].to_numpy())
    rng = np.random.default_rng(seed)
    draws = []
    for _ in range(n_boot):
        w = np.bincount(rng.integers(0, len(users), len(users)), minlength=len(users))
        draws.append((w @ clicks) / (w @ expected))
    lo, hi = np.percentile(draws, [2.5, 97.5], axis=0)
    return pd.DataFrame({"bucket": labels, "index": clicks.sum(0) / expected.sum(0), "lo": lo, "hi": hi,
                         "impressions": np.bincount(k, minlength=len(labels)),
                         "users": (expected > 0).sum(0)})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/train.yml")
    parser.add_argument("--min-impressions", type=int, default=5, help="users with at least this many impressions")
    parser.add_argument("--bootstrap", type=int, default=500)
    parser.add_argument("--output-dir", default="outputs/analysis")
    args = parser.parse_args()
    with open(args.config) as f:
        config = yaml.safe_load(f)
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    beh = load_behaviors(Path(config["data"]["dir"]) / config["data"]["behaviors"])
    temporal = temporal_features(beh, pd.Timestamp(config["split"]["fit_end"]), config["features"]["ctr_prior_strength"])
    d = impression_table(beh, temporal, args.min_impressions)
    table = pd.concat([fatigue_index(d, col, bins, labels, args.bootstrap, config["seed"]).assign(view=title)
                       for col, (title, bins, labels) in VIEWS.items()], ignore_index=True)
    print(table.round(3).to_string(index=False))
    print(f"{d['user'].nunique():,} users with {args.min_impressions}+ impressions, {len(d):,} impressions")
    table.to_csv(out / "fatigue.csv", index=False)
    plot_fatigue(table, out / "fatigue.png", d["user"].nunique(), args.min_impressions)


if __name__ == "__main__":
    main()
