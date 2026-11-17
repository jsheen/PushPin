"""Uplift: estimate what a push *causes*, not who would click it.

The click model answers "will this reader open this push?". The send decision needs a different
question: does sending it change what the reader does, compared with not sending anything? In
potential outcomes, each (reader, slot) has Y(1) if pushed and Y(0) if not, and the target is the
conditional average treatment effect

    tau(x) = E[Y(1) - Y(0) | X = x]

Only one of Y(1), Y(0) is ever seen, so tau is identified from a randomized design: the notifier
holds out a share of eligible pushes (and can explore a share of ineligible ones), logging the
known probability e(x) of sending each. With that log, the DR-learner (Kennedy) estimates tau:

  1. Cross-fitting by reader: on each fold's complement, fit outcome models mu1(x) = E[Y | x, T=1]
     and mu0(x) = E[Y | x, T=0] (XGBoost); predict them on the held-out fold.
  2. Doubly robust pseudo-outcome, per row:
         phi = mu1 - mu0 + T (Y - mu1) / e - (1 - T) (Y - mu0) / (1 - e)
     E[phi | x] = tau(x) if either the mu's or e are right; e is known here, so phi is unbiased
     and the mu's only reduce its variance. mean(phi) is the ATE.
  3. Regress phi on x (a small, regularized XGBoost, also cross-fitted): tau_hat(x), the
     heterogeneous effect. GATES (DR effect within quintiles of tau_hat) checks it is real.

Two data sources:
  - sim (default): a semi-synthetic benchmark. Real features from the NYT simulation's top picks
    (outputs/nyt-sim/pushes.parquet), but outcomes drawn from a planted, known tau(x), with the
    same randomized design as the notifier. Because the truth is known, it checks that the
    estimator recovers tau and that policies chosen by tau_hat really cause more returns.
  - live (--live): the notifier's database, outcome = an app session within `outcome_hours`.

Writes <out_dir>/<sim|live>/: summary.json, gates.csv, policies.csv, uplift.png (sim), rows.parquet.
"""

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb
import yaml
from sklearn.model_selection import GroupKFold

from pushpin.plots import plot_uplift
from pushpin.uplift_design import propensity, pushes_today

FEATURES = ["p_click", "p_return", "novelty", "subcat_affinity", "emb_sim_max", "llm_sim_click", "llm_sim_skip",
            "article_age_hours", "hour", "pushes_today"]


# --- semi-synthetic benchmark ---------------------------------------------------------------------

def planted_effect(df: pd.DataFrame, planted: dict) -> tuple[np.ndarray, np.ndarray]:
    """Organic return probability y0(x) and the true push effect tau(x).

    y0 = organic * p_return: the reader comes back on their own as often as the return model says.
    tau = lift * (1 - p_return) * interest - fatigue * pushes_today: a push helps readers who would
    not come back anyway, more when the topic is theirs (interest rises with subcategory affinity
    from `interest_floor` to 1), and each earlier push that day costs `fatigue` (it can go negative).
    """
    affinity = df["subcat_affinity"].rank(pct=True).fillna(0.5).to_numpy()
    interest = planted["interest_floor"] + (1 - planted["interest_floor"]) * affinity
    y0 = np.clip(planted["organic"] * df["p_return"].to_numpy(), 0.01, 0.99)
    tau = planted["lift"] * (1 - df["p_return"].to_numpy()) * interest - planted["fatigue"] * df["pushes_today"].to_numpy()
    return y0, np.clip(y0 + tau, 0, 1) - y0  # tau as realized after keeping probabilities in [0, 1]


def simulate(picks: pd.DataFrame, cfg: dict, seed: int) -> pd.DataFrame:
    """Randomize and draw outcomes for `replicates` copies of the simulated readers.

    picks: the NYT simulation's top pick per (user, slot), with `sent` = eligible under the current
    click policy. Each replicate is a new set of readers with the same features but their own coin
    flips and outcomes; rows keep `user_id` so cross-fitting can keep all copies of a user together.
    """
    rng = np.random.default_rng(seed)
    design, out = cfg["design"], []
    for r in range(cfg["replicates"]):
        df = picks.assign(replicate=r, reader=f"{r}:" + picks["user_id"].astype(str),
                          eligible=picks["sent"].astype(bool), hour=picks["slot_time"].dt.hour).reset_index(drop=True)
        df["propensity"] = propensity(df["eligible"].to_numpy(), design["holdout"], design["explore"])
        df["treated"] = (rng.random(len(df)) < df["propensity"]).astype(int)
        df["pushes_today"] = pushes_today(df)
        df["y0"], df["tau"] = planted_effect(df, cfg["planted"])
        df["y"] = (rng.random(len(df)) < df["y0"] + df["treated"] * df["tau"]).astype(int)
        out.append(df)
    return pd.concat(out, ignore_index=True)


# --- estimation -----------------------------------------------------------------------------------

def dr_pseudo_outcome(y, t, e, mu0, mu1) -> np.ndarray:
    """phi: unbiased for tau(x) if either (mu0, mu1) or e is right. With mu0 = mu1 = 0 it is plain IPW."""
    return mu1 - mu0 + t * (y - mu1) / e - (1 - t) * (y - mu0) / (1 - e)


def cluster_mean(values: np.ndarray, clusters: np.ndarray) -> tuple[float, float]:
    """Mean and its standard error, clustered by reader (rows of one reader are not independent)."""
    values = np.asarray(values, float)
    mean = values.mean()
    sums = pd.Series(values - mean).groupby(np.asarray(clusters)).sum().to_numpy()
    g = len(sums)
    se = np.sqrt((sums ** 2).sum() * g / max(g - 1, 1)) / len(values)
    return float(mean), float(se)


def cross_fit(df: pd.DataFrame, cfg: dict, seed: int) -> pd.DataFrame:
    """Out-of-fold mu0, mu1, phi, the T-learner (mu1 - mu0) and the DR-learner tau_hat for every row.

    Folds group by `user_id`, so no reader (or simulated copy of one) is in both halves.
    """
    x, t, y, e = df[FEATURES], df["treated"].to_numpy(), df["y"].to_numpy(), df["propensity"].to_numpy()
    folds = list(GroupKFold(n_splits=cfg["folds"]).split(x, groups=df["user_id"]))
    mu0, mu1 = np.zeros(len(df)), np.zeros(len(df))
    for train, test in folds:
        for arm, mu in ((1, mu1), (0, mu0)):
            rows = train[t[train] == arm]
            model = xgb.XGBClassifier(**cfg["outcome_model"], random_state=seed, n_jobs=2)
            model.fit(x.iloc[rows], y[rows])
            mu[test] = model.predict_proba(x.iloc[test])[:, 1]
    phi = dr_pseudo_outcome(y, t, e, mu0, mu1)
    tau_dr = np.zeros(len(df))
    for train, test in folds:
        tau_dr[test] = effect_model(x.iloc[train], phi[train], cfg, seed).predict(x.iloc[test])
    return df.assign(mu0=mu0, mu1=mu1, phi=phi, tau_t=mu1 - mu0, tau_dr=tau_dr)


def effect_model(x: pd.DataFrame, phi: np.ndarray, cfg: dict, seed: int) -> xgb.XGBRegressor:
    """The DR-learner's second stage: regress the pseudo-outcome on the features."""
    model = xgb.XGBRegressor(**cfg["effect_model"], random_state=seed, n_jobs=2)
    return model.fit(x, phi)


def gates(df: pd.DataFrame, score: str = "tau_dr", groups: int = 5) -> pd.DataFrame:
    """Sorted group average treatment effects: the DR effect within quantiles of `score`.

    If tau_hat captures real heterogeneity, the estimated effect rises from the bottom group to
    the top one; if it only fits noise, the groups look alike.
    """
    group = pd.qcut(df[score].rank(method="first"), groups, labels=False).to_numpy()
    rows = []
    for g in range(groups):
        part = df[group == g]
        est, se = cluster_mean(part["phi"], part["reader"])
        row = {"group": g + 1, "rows": len(part), "mean_tau_hat": float(part[score].mean()),
               "dr_effect": est, "dr_se": se}
        if "tau" in part:
            row["true_effect"] = float(part["tau"].mean())
        rows.append(row)
    return pd.DataFrame(rows)


def capped_top(df: pd.DataFrame, score: np.ndarray, budget: int, cap: int) -> np.ndarray:
    """The `budget` highest-scoring rows, at most `cap` per reader per day."""
    order = np.argsort(-score, kind="stable")
    ranked = df.iloc[order]
    keep = ranked.groupby([ranked["reader"], ranked["slot_time"].dt.normalize()]).cumcount().to_numpy() < cap
    chosen = order[keep][:budget]
    mask = np.zeros(len(df), bool)
    mask[chosen] = True
    return mask


def policy_scores(df: pd.DataFrame, seed: int) -> dict[str, np.ndarray]:
    """Rankings to compare at the same push budget. `oracle` (the true tau) exists only in the sim."""
    out = {"random": np.random.default_rng(seed).random(len(df)),
           "click (p_click)": df["p_click"].to_numpy(),
           "structural prior": (df["p_click"] * (1 - df["p_return"])).to_numpy(),
           "T-learner": df["tau_t"].to_numpy(),
           "DR-learner": df["tau_dr"].to_numpy()}
    if "tau" in df:
        out["oracle"] = df["tau"].to_numpy()
    return out


def evaluate_policies(df: pd.DataFrame, cap: int, seed: int) -> pd.DataFrame:
    """Each policy at the current policy's budget: extra returns it causes per 1,000 pushes.

    `dr_estimate` is off-policy evaluation: the mean of phi over the rows the policy would push,
    which needs only the log (no truth). In the sim it is checked against `true_effect`. The
    current policy is the click-threshold rule itself (the eligible rows).
    """
    budget = int(df["eligible"].sum())
    choices = {"current (click threshold)": df["eligible"].to_numpy()}
    choices |= {name: capped_top(df, s, budget, cap) for name, s in policy_scores(df, seed).items()}
    rows = []
    for name, chosen in choices.items():
        part = df[chosen]
        est, se = cluster_mean(part["phi"], part["reader"])
        row = {"policy": name, "pushes": int(chosen.sum()), "mean_p_click": float(part["p_click"].mean()),
               "mean_p_return": float(part["p_return"].mean()),
               "dr_estimate_per_1000": 1000 * est, "dr_se_per_1000": 1000 * se}
        if "tau" in part:
            row["true_effect_per_1000"] = 1000 * float(part["tau"].mean())
        rows.append(row)
    return pd.DataFrame(rows)


def qini_curves(df: pd.DataFrame, seed: int, points: int = 41) -> pd.DataFrame:
    """Cumulative extra returns (per 1,000 decisions) when pushing the top share of rows by each ranking.

    `true` uses the planted tau (sim only); `dr` sums phi instead, which is what a real log allows.
    """
    rows = []
    shares = np.linspace(0, 1, points)
    for name, score in policy_scores(df, seed).items():
        order = np.argsort(-score, kind="stable")
        cum_phi = np.concatenate([[0], np.cumsum(df["phi"].to_numpy()[order])])
        cum_tau = np.concatenate([[0], np.cumsum(df["tau"].to_numpy()[order])]) if "tau" in df else None
        for s in shares:
            k = round(s * len(df))
            row = {"policy": name, "share_pushed": s, "dr": 1000 * cum_phi[k] / len(df)}
            if cum_tau is not None:
                row["true"] = 1000 * cum_tau[k] / len(df)
            rows.append(row)
    return pd.DataFrame(rows)


def average_effects(df: pd.DataFrame) -> dict:
    """The ATE three ways, to show why the estimator matters.

    naive: treated minus control means. Biased here: eligible picks are sent far more often, so
           the treated group is mostly readers who return anyway (confounding by design).
    ipw:   phi with mu = 0. Unbiased (e is known) but noisy.
    dr:    phi with the cross-fitted mu's. Unbiased and tighter.
    """
    t, y = df["treated"].to_numpy() == 1, df["y"].to_numpy()
    ipw = dr_pseudo_outcome(y, t.astype(int), df["propensity"].to_numpy(), 0.0, 0.0)
    out = {"naive_difference": float(y[t].mean() - y[~t].mean())}
    for name, values in (("ipw", ipw), ("dr", df["phi"].to_numpy())):
        est, se = cluster_mean(values, df["reader"])
        out[name] = {"estimate": est, "se": se, "ci95": [est - 1.96 * se, est + 1.96 * se]}
    if "tau" in df:
        out["true"] = float(df["tau"].mean())
    return out


# --- data sources ---------------------------------------------------------------------------------

def load_picks(sim_dir: Path) -> pd.DataFrame:
    """The NYT simulation's top pick per (user, slot); `sent` marks the current policy's choices."""
    return pd.read_parquet(sim_dir / "pushes.parquet").sort_values(["slot_time", "user_id"]).reset_index(drop=True)


def live_rows(db: str, outcome_hours: float) -> pd.DataFrame:
    """The randomized pushes in the notifier's database, with their outcome (see Store.uplift_rows)."""
    from zoneinfo import ZoneInfo

    from pushpin.store import Store  # imported here so the sim needs no database

    store = Store(db, click_window_hours=24, tz=ZoneInfo("America/New_York"))
    df = store.uplift_rows(outcome_hours)
    store.engine.dispose()
    if df.empty:
        return df
    df["slot_time"] = pd.to_datetime(df["slot_time"])
    df["reader"] = df["user_id"] = df["for_reader"]
    df["hour"] = df["slot_time"].dt.hour
    df["treated"] = df["treated"].astype(int)
    df["eligible"] = df["eligible"].astype(bool)
    df["pushes_today"] = pushes_today(df)
    return df


# --- runs -----------------------------------------------------------------------------------------

def run_sim(cfg: dict, out: Path) -> dict:
    picks = load_picks(Path(cfg["sim_dir"]))
    df = cross_fit(simulate(picks, cfg, cfg["seed"]), cfg, cfg["seed"])
    groups = gates(df)
    policies = evaluate_policies(df, cfg["daily_cap"], cfg["seed"])
    curves = qini_curves(df, cfg["seed"])
    summary = {
        "source": "semi-synthetic: NYT-simulation features, planted tau",
        "rows": len(df), "readers": int(df["reader"].nunique()), "replicates": cfg["replicates"],
        "design": cfg["design"], "planted": cfg["planted"],
        "treated_share": float(df["treated"].mean()), "eligible_share": float(df["eligible"].mean()),
        "average_effect": average_effects(df),
        "cate_recovery": {name: {"corr_with_true_tau": float(np.corrcoef(df[col], df["tau"])[0, 1]),
                                 "rmse": float(np.sqrt(np.mean((df[col] - df["tau"]) ** 2)))}
                          for name, col in (("T-learner", "tau_t"), ("DR-learner", "tau_dr"))},
        "current_policy_vs_all": {"p_return_sent": float(df.loc[df["eligible"], "p_return"].mean()),
                                  "p_return_all": float(df["p_return"].mean())},
    }
    write(out, df, summary, groups, policies)
    curves.to_csv(out / "qini.csv", index=False)
    plot_uplift(curves, groups, policies, out / "uplift.png")
    return summary


def run_live(cfg: dict, out: Path) -> dict:
    live = cfg["live"]
    df = live_rows(live["db"], live["outcome_hours"])
    if df.empty:
        raise SystemExit(f"no randomized pushes with a closed outcome window in {live['db']} yet")
    summary = {"source": f"live: {live['db']}", "rows": len(df), "readers": int(df["reader"].nunique()),
               "treated_share": float(df["treated"].mean()), "outcome_hours": live["outcome_hours"]}
    arms_ok = df["treated"].nunique() == 2 and df.groupby("treated")["y"].nunique().min() == 2
    if len(df) >= live["min_rows_dr"] and arms_ok and df["user_id"].nunique() >= cfg["folds"]:
        df = cross_fit(df, cfg, cfg["seed"])
        summary["average_effect"] = average_effects(df)
        if len(df) >= live["min_rows_cate"]:
            groups = gates(df)
            write(out, df, summary, groups, evaluate_policies(df, cfg["daily_cap"], cfg["seed"]))
            return summary
    else:  # too few rows (or readers) to fit outcome models: IPW only
        y, t, e = df["y"].to_numpy(), df["treated"].to_numpy(), df["propensity"].to_numpy()
        est, se = cluster_mean(dr_pseudo_outcome(y, t, e, 0.0, 0.0), df["reader"])
        summary["average_effect"] = {"ipw": {"estimate": est, "se": se, "ci95": [est - 1.96 * se, est + 1.96 * se]}}
    write(out, df, summary)
    return summary


def write(out: Path, df: pd.DataFrame, summary: dict, groups: pd.DataFrame | None = None,
          policies: pd.DataFrame | None = None) -> None:
    out.mkdir(parents=True, exist_ok=True)
    keep = [c for c in ["reader", "user_id", "slot_time", "news_id", *FEATURES, "eligible", "propensity", "treated",
                        "y", "y0", "tau", "mu0", "mu1", "phi", "tau_t", "tau_dr"] if c in df]
    df[keep].to_parquet(out / "rows.parquet", index=False)
    if groups is not None:
        groups.to_csv(out / "gates.csv", index=False)
        summary["gates"] = groups.to_dict("records")
    if policies is not None:
        policies.to_csv(out / "policies.csv", index=False)
        summary["policies"] = policies.to_dict("records")
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print(json.dumps(summary, indent=2, default=str))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--config", default="configs/uplift.yml")
    parser.add_argument("--live", action="store_true", help="fit on the notifier's database instead of the sim")
    parser.add_argument("--db", help="overrides live.db (a SQLite path or a Postgres URL)")
    args = parser.parse_args()
    with open(args.config) as f:
        cfg = yaml.safe_load(f)
    if args.db:
        cfg["live"]["db"] = args.db
    out = Path(cfg["out_dir"]) / ("live" if args.live else "sim")
    (run_live if args.live else run_sim)(cfg, out)
    print(f"written to {out}")


if __name__ == "__main__":
    main()
