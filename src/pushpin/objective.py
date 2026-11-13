"""Push objective: trade predicted engagement off against subcategory novelty.

    score = (1 - friction) * engagement + friction * novelty,    friction in [0, 1]

The two halves are on very different scales: P(click) from the XGBoost model sits in a narrow
band near the base CTR (~0.04), while novelty spans [0, 1]. Adding them raw would let novelty
dominate at almost any friction. So each half is replaced by its percentile rank among the
candidates in the same impression (the set we are choosing one push from): both then mean
"share of the other candidates this one beats", and friction is a true weight between the
two rankings. friction = 0 is the pure click model; friction = 1 pushes the least-seen subcategory.

Then a send policy decides whether to send that push at all: only if its calibrated P(click)
clears a threshold that rises with the user's risk (1 - P(return), from the return model), and
at most `daily_cap` pushes per user per day. Friction picks the content; the send policy sets
the volume.

Runs separately from training (its own image): it reads a run's predictions.parquet, so friction
and the send policy can be changed without retraining.
"""

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from pushpin.plots import plot_tradeoff, plot_volume


def _rank(values, groups: np.ndarray) -> np.ndarray:
    """Percentile rank within each group, in (0, 1]; ties share their average rank."""
    return pd.Series(np.asarray(values)).groupby(groups).rank(pct=True).to_numpy()


def objective_parts(df: pd.DataFrame, p_click: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Engagement and novelty, each as a percentile rank within its impression."""
    imp = df["impression_id"].to_numpy()
    return _rank(p_click, imp), _rank(df["novelty"], imp)


def push_scores(df: pd.DataFrame, p_click: np.ndarray, friction: float) -> np.ndarray:
    """Objective score for each candidate; push the highest one in each impression."""
    if not 0 <= friction <= 1:
        raise ValueError(f"friction must be in [0, 1], got {friction}")
    engage, novel = objective_parts(df, p_click)
    return (1 - friction) * engage + friction * novel


def tradeoff(df: pd.DataFrame, p_click: np.ndarray, frictions: list[float]) -> pd.DataFrame:
    """What the top-ranked push per impression looks like at each friction.

    ctr_at_1: share of top picks the user actually clicked; novelty_at_1: mean novelty of
    the top pick; new_subcat_at_1: share of top picks from a subcategory never shown before.
    """
    imp = df["impression_id"].to_numpy()
    engage, novel = objective_parts(df, p_click)
    out = []
    for f in frictions:
        score = pd.Series((1 - f) * engage + f * novel)
        top = df.iloc[score.groupby(imp).idxmax().to_numpy()]
        out.append({"friction": f, "ctr_at_1": top["label"].mean(), "novelty_at_1": top["novelty"].mean(),
                    "new_subcat_at_1": (top["novelty"] == 1).mean()})
    return pd.DataFrame(out)


def random_tradeoff(df: pd.DataFrame, frictions: list[float], seed: int, draws: int = 5) -> pd.DataFrame:
    """tradeoff() with engagement replaced by a random ranking, averaged over draws."""
    rng = np.random.default_rng(seed)
    return pd.concat([tradeoff(df, rng.random(len(df)), frictions) for _ in range(draws)]
                     ).groupby("friction", as_index=False).mean()


def send_decisions(pushes: pd.DataFrame, threshold: float, risk_penalty: float, daily_cap: int) -> np.ndarray:
    """Whether to send each top pick (one row per impression).

    Send if p_click >= threshold + risk_penalty * (1 - p_return), at most daily_cap per user per
    calendar day, earliest first: the policy cannot see a user's later impressions.
    """
    ok = pushes["p_click"].to_numpy() >= threshold + risk_penalty * (1 - pushes["p_return"].to_numpy())
    df = pd.DataFrame({"user": pushes["user_id"].to_numpy(), "day": pushes["time"].dt.normalize().to_numpy(),
                       "time": pushes["time"].to_numpy(), "ok": ok}).sort_values("time", kind="stable")
    sent_so_far = df.groupby(["user", "day"])["ok"].cumsum()
    return (df["ok"] & (sent_so_far <= daily_cap)).sort_index().to_numpy()


def risk_groups(pushes: pd.DataFrame) -> np.ndarray:
    """Each user's risk tercile on the day (mean 1 - p_return over their impressions)."""
    risk = 1 - pushes.groupby("user_id")["p_return"].transform("mean")
    return pd.qcut(risk.rank(method="first"), 3, labels=["low risk", "mid risk", "high risk"]).to_numpy()


def volume_curve(pushes: pd.DataFrame, thresholds: list[float], risk_penalty: float, daily_cap: int) -> pd.DataFrame:
    """Pushes sent per active user-day vs. their click rate, at each send threshold, by risk group."""
    group = risk_groups(pushes)
    user_day = pushes["user_id"] + "|" + pushes["time"].dt.date.astype(str)
    label, p_click = pushes["label"].to_numpy(), pushes["p_click"].to_numpy()
    out = []
    for t in thresholds:
        send = send_decisions(pushes, t, risk_penalty, daily_cap)
        for name in ("all", "low risk", "high risk"):
            m = np.ones(len(pushes), bool) if name == "all" else group == name
            s = send & m
            user_days = user_day[m].nunique()
            out.append({"threshold": t, "group": name, "pushes_per_user_day": s.sum() / user_days,
                        "ctr_sent": label[s].mean() if s.any() else np.nan,
                        "pred_ctr_sent": p_click[s].mean() if s.any() else np.nan,
                        "clicks_per_user_day": label[s].sum() / user_days, "sent_share": s.sum() / m.sum()})
    return pd.DataFrame(out)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/objective.yml")
    parser.add_argument("--run-dir", help="overrides run_dir in the config")
    parser.add_argument("--friction", type=float, help="overrides friction in the config")
    args = parser.parse_args()
    with open(args.config) as f:
        config = yaml.safe_load(f)
    run_dir = Path(args.run_dir or config["run_dir"])
    friction = config["friction"] if args.friction is None else args.friction

    preds = pd.read_parquet(run_dir / "predictions.parquet")
    if "p_return" not in preds:  # runs from before the return model: treat everyone as zero risk
        preds["p_return"] = 1.0
    preds["n_candidates"] = preds.groupby("impression_id")["news_id"].transform("size")
    p_click = preds["p_click"].to_numpy()
    frictions = sorted(set(config["sweep"]) | {friction})
    curve = pd.concat([tradeoff(preds, p_click, frictions).assign(policy="model"),
                       random_tradeoff(preds, frictions, config["seed"]).assign(policy="random")])
    print(curve.round(4).to_string(index=False))

    out = run_dir / "objective"
    out.mkdir(exist_ok=True)
    curve.to_csv(out / "tradeoff.csv", index=False)
    plot_tradeoff(curve, out / "tradeoff.png", friction)
    score = pd.Series(push_scores(preds, p_click, friction))
    pushes = preds.iloc[score.groupby(preds["impression_id"].to_numpy()).idxmax().to_numpy()]
    pushes = pushes.assign(score=score[pushes.index].to_numpy()).reset_index(drop=True)

    send = config["send"]
    pushes["send"] = send_decisions(pushes, send["threshold"], send["risk_penalty"], send["daily_cap"])
    pushes.to_parquet(out / "pushes.parquet", index=False)
    # evaluate volume on large impressions: MIND logs only impressions with a click, which
    # inflates click rates in small ones (see docs/analysis.md, "Calibration")
    large = pushes[pushes["n_candidates"] >= send["eval_min_candidates"]].reset_index(drop=True)
    thresholds = sorted(set(send["sweep"]) | {send["threshold"]})
    volume = volume_curve(large, thresholds, send["risk_penalty"], send["daily_cap"])
    volume.to_csv(out / "volume.csv", index=False)
    plot_volume(volume, out / "volume.png", send["threshold"], send["eval_min_candidates"])
    print(volume[volume["group"] == "all"].round(4).to_string(index=False))

    summary = {"friction": curve[(curve["policy"] == "model") & (curve["friction"] == friction)].iloc[0].to_dict(),
               "send_policy": send | {"sent_share_all_impressions": float(pushes["send"].mean())},
               "send_eval_large_impressions": volume[volume["threshold"] == send["threshold"]]
               .set_index("group").drop(columns="threshold").to_dict("index")}
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print(f"friction={friction}: {len(pushes):,} top picks, {pushes['send'].sum():,} sent, written to {out}")


if __name__ == "__main__":
    main()
