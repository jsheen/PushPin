"""Contextual bandit for the send decision: is this pick worth sending to this reader right now?

XGBoost answers "which article?" (p_click, p_return). The bandit answers "send it now or not?"
for the top pick at every slot, from context XGBoost doesn't model: how recently the reader was
pushed, how many pushes they ignored this week, how long since they last opened one, their own
open rate on our pushes, and whether this is an hour they tend to come back (relative to their
own average, so it's separate from how often they come back at all). It maximizes

    reward = sessions - lambda * pushes - V * opt-outs        (lambda, V set by hand, in sessions)

Model: per decision, r = beta . [z, a * z] with a = 1 if sent, Bayesian linear regression with a
Gaussian prior. theta (the a * z half) is the effect of sending; Thompson sampling draws beta from
the posterior once per reader-day and sends when theta~ . z > 0. The posterior is refit every
simulated night from all decisions so far (sufficient statistics), so it is a batched bandit.

Data: the NYT simulation's candidates (outputs/nyt-sim-8slot/scores.parquet: each reader's top 20
per slot plus the slot's newest article), replayed for `weeks` weeks. Outcomes come from a
planted world (configs/bandit.yml): organic sessions, the push effect, opens, a decaying
annoyance per reader that lowers both and drives opt-outs, per-reader tolerance and preferred
time of day. Every policy sees the same random draws (common random numbers).

Editorial alerts (`editorial` in the config, a list of news_ids, empty by default) are pushed
to every subscribed reader at the slot where they are newest, bypassing every policy: an editor's
must-send news is never held back by the bandit.

Policies compared: the bandit; "breaking" (the newest article to everyone at every slot);
"send if p_click >= c" for each c in `thresholds` (the best one in hindsight is a reference a
non-contextual policy could at most reach); never push.

Writes <out_dir>/: summary.json, policies.csv, weekly.csv, hours.csv, coefficients.csv, bandit.png;
with --grid, grid.csv (every policy at each (lambda, V)) and bandit_grid.png.
"""

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

FEATURES = ["intercept", "p_click", "risk", "p_click x risk", "recent pushes", "ignored this week",
            "log hours since open", "own open rate", "is this their hour"]


# --- data -----------------------------------------------------------------------------------------

def load_candidates(sim_dir: Path, interest_floor: float) -> dict:
    """Arrays indexed [slot, reader, k] from the sim's kept candidates (best push_score first),
    plus the newest article per slot. Articles get integer ids; interest = floor + (1 - floor) *
    rank percentile of subcategory affinity (as in uplift.planted_effect)."""
    s = pd.read_parquet(sim_dir / "scores.parquet",
                        columns=["user_id", "slot_time", "news_id", "p_click", "p_return", "subcat_affinity",
                                 "push_score", "is_newest"])
    s["interest"] = interest_floor + (1 - interest_floor) * s["subcat_affinity"].rank(pct=True).fillna(0.5)
    slots = np.sort(s["slot_time"].unique())
    users = np.sort(s["user_id"].unique())
    articles = pd.Index(np.sort(s["news_id"].unique()))
    s["slot"] = np.searchsorted(slots, s["slot_time"].to_numpy())
    s["reader"] = np.searchsorted(users, s["user_id"].to_numpy())
    s["article"] = articles.get_indexer(s["news_id"])
    s = s.sort_values(["slot", "reader", "push_score"], ascending=[True, True, False], kind="stable")
    s["k"] = s.groupby(["slot", "reader"]).cumcount()
    n_slots, n, k = len(slots), len(users), int(s["k"].max()) + 1
    out = {"n_slots": n_slots, "n": n, "users": users, "slot_times": pd.to_datetime(slots), "n_articles": len(articles),
           "news_ids": articles}
    for col, fill, dtype in (("article", -1, np.int32), ("p_click", 0.0, np.float32), ("interest", 0.0, np.float32)):
        arr = np.full((n_slots, n, k), fill, dtype=dtype)
        arr[s["slot"], s["reader"], s["k"]] = s[col].to_numpy()
        out[col] = arr
    p_return = np.zeros((n_slots, n), np.float32)
    p_return[s["slot"], s["reader"]] = s["p_return"].to_numpy()
    out["p_return"] = p_return
    newest = s[s["is_newest"]]
    for col in ("article", "p_click", "interest"):
        arr = np.zeros((n_slots, n), np.float32 if col != "article" else np.int32)
        arr[newest["slot"], newest["reader"]] = newest[col].to_numpy()
        out[f"newest_{col}"] = arr
    out["hours"] = out["slot_times"].hour.to_numpy()
    out["mean_p_click"] = s[s["k"] == 0].groupby("reader")["p_click"].mean().reindex(range(n)).to_numpy()
    return out


def reader_traits(data: dict, planted: dict, seed: int) -> dict:
    """Planted per-reader traits: tolerance for ignored pushes and preference by hour (mean 1)."""
    rng = np.random.default_rng([seed, 1])
    n = data["n"]
    rel = data["mean_p_click"] / np.nanmean(data["mean_p_click"])
    tolerance = (planted["tolerance_median"] * np.exp(planted["tolerance_spread"] * rng.standard_normal(n))
                 * rel ** planted["tolerance_open_elasticity"])
    peak = rng.choice(planted["peaks"], n) + rng.uniform(-1, 1, n)
    day_hours = np.sort(np.unique(data["hours"]))
    gap = np.abs(day_hours[None, :] - peak[:, None])
    pref = planted["pref_floor"] + (1 - planted["pref_floor"]) * np.exp(-gap ** 2 / (2 * planted["pref_width_hours"] ** 2))
    pref /= pref.mean(axis=1, keepdims=True)
    return {"tolerance": tolerance, "peak": peak, "pref": pref, "day_hours": day_hours}


# --- the bandit -----------------------------------------------------------------------------------

class LinearThompson:
    """Bayesian linear regression r = beta . phi, phi = [z, a * z], Gaussian prior and noise."""

    def __init__(self, d: int, prior_sd: float, noise_sd: float, seed: int):
        self.d, self.noise_var = d, noise_sd ** 2
        self.precision = np.eye(2 * d) / prior_sd ** 2
        self.b = np.zeros(2 * d)
        self.rng = np.random.default_rng([seed, 2])
        self.refit()

    def add(self, z: np.ndarray, a: np.ndarray, r: np.ndarray) -> None:
        phi = np.hstack([z, z * a[:, None]])
        self.precision += phi.T @ phi / self.noise_var
        self.b += phi.T @ r / self.noise_var

    def refit(self) -> None:
        self.chol = np.linalg.cholesky(self.precision)
        self.mean = np.linalg.solve(self.precision, self.b)

    def sample_theta(self, m: int) -> np.ndarray:
        """m posterior draws of theta (the effect-of-sending half), shape (m, d)."""
        eps = self.rng.standard_normal((2 * self.d, m))
        draws = self.mean[:, None] + np.linalg.solve(self.chol.T, eps)
        return draws[self.d:].T

    def theta_mean_sd(self) -> tuple[np.ndarray, np.ndarray]:
        cov = np.linalg.inv(self.precision)
        return self.mean[self.d:], np.sqrt(np.diag(cov)[self.d:])


# --- the planted world, one policy over `weeks` -----------------------------------------------------

def simulate(data: dict, traits: dict, cfg: dict, policy: str, lam: float, V: float, threshold: float | None = None,
             trace: list | None = None):
    """Run one policy. Returns per-slot totals (sessions, pushes, opens, opt-outs, reward) and the bandit.
    With `trace` (a list), appends a per-decision frame for subscribed readers (diagnostics)."""
    p, seed, n = cfg["planted"], cfg["seed"], data["n"]
    slots_per_day = len(traits["day_hours"])
    days = 7 * cfg["weeks"]
    decay = 0.5 ** (1 / p["annoyance_half_life_days"])
    tol, pref_all = traits["tolerance"], traits["pref"]
    readers = np.arange(n)
    risk_mean = float(1 - data["p_return"].mean())
    # editorial alerts: articles an editor marks as must-send. At the slot where one is newest, it
    # goes to every subscribed reader who hasn't had it, whatever the policy says (it bypasses the
    # bandit), and it counts as a push: cost, fatigue, annoyance and opt-out risk all apply.
    editorial_ids = data["news_ids"].get_indexer(pd.Index(cfg.get("editorial") or []))
    editorial_ids = set(editorial_ids[editorial_ids >= 0].tolist())

    active = np.ones(n, bool)
    annoy = np.zeros(n)
    pushed = np.zeros((n, data["n_articles"]), bool)
    last_push = np.full((n, 3), -1e9)           # absolute hours of the last 3 pushes
    last_open = np.full(n, -1e9)
    pushes_n, opens_n = np.zeros(n), np.zeros(n)
    sess_h, seen_h = np.zeros((n, slots_per_day)), np.zeros((n, slots_per_day))
    ignored_by_day = np.zeros((n, 7))
    bandit = LinearThompson(len(FEATURES), cfg["bandit"]["prior_sd"], cfg["bandit"]["noise_sd"], seed) \
        if policy == "bandit" else None
    rows = []

    for day in range(days):
        if day % 7 == 0:
            pushed[:] = False                    # the replayed week's articles are new again
        ignored_by_day[:, day % 7] = 0
        theta = bandit.sample_theta(n) if bandit else None   # one Thompson draw per reader-day
        for j in range(slots_per_day):
            s = (day % 7) * slots_per_day + j
            hour = data["hours"][s]
            now = day * 24 + hour
            rng = np.random.default_rng([seed, 3, day, j])   # same draws for every policy
            u_session, u_open, u_opt = rng.random(n), rng.random(n), rng.random(n)
            pr, pref = data["p_return"][s].astype(float), pref_all[:, j]

            # the policy's article: the slot's newest for "breaking", else the best not yet pushed
            if policy == "breaking":
                art = data["newest_article"][s]
                pc, interest = data["newest_p_click"][s].astype(float), data["newest_interest"][s].astype(float)
                avail = ~pushed[readers, art]
            else:
                cand = data["article"][s]
                ok = (cand >= 0) & ~pushed[readers[:, None], np.maximum(cand, 0)]
                k = ok.argmax(axis=1)
                avail = ok[readers, k]
                art = cand[readers, k]
                pc = data["p_click"][s][readers, k].astype(float)
                interest = data["interest"][s][readers, k].astype(float)
            forced = np.zeros(n, bool)
            if editorial_ids and int(data["newest_article"][s][0]) in editorial_ids:
                alert = data["newest_article"][s]
                forced = active & ~pushed[readers, alert]
                art = np.where(forced, alert, art)
                pc = np.where(forced, data["newest_p_click"][s], pc)
                interest = np.where(forced, data["newest_interest"][s], interest)
                avail = avail | forced

            recent = (now - last_push < cfg["fatigue_hours"]).sum(axis=1)
            open_rate = (opens_n + 0.1) / (pushes_n + 2)
            # "is this their hour?": the reader's session rate at this hour relative to their own
            # average, from slots without a push (so our pushes don't feed back into it). It's
            # separate from how often they come back at all, which risk already covers.
            rate_here = (sess_h[:, j] + 0.3) / (seen_h[:, j] + 2)
            rate_all = (sess_h.sum(axis=1) + 0.3 * slots_per_day) / (seen_h.sum(axis=1) + 2 * slots_per_day)
            their_hour = rate_here / rate_all - 1
            z = np.column_stack([
                np.ones(n), 10 * pc, 1 - pr, 10 * pc * (1 - pr), recent, ignored_by_day.sum(axis=1) / 10,
                np.log1p(np.minimum(now - last_open, 168)) / 5, 10 * open_rate, 2 * their_hour])

            can = active & avail
            if policy == "never":
                send = np.zeros(n, bool)
            elif policy == "breaking":
                send = can.copy()
            elif policy == "threshold":
                send = can & (pc >= threshold)
            else:
                send = can & ((theta * z).sum(axis=1) > 0)
            send |= forced  # an editorial alert overrides the policy (the bandit still learns from it)

            factor = np.exp(-annoy / tol)
            y0 = np.clip(p["organic"] * pr * pref, 0, 0.95)
            tau = p["lift"] * (1 - pr) * interest * pref * factor - p["fatigue"] * recent
            session = u_session < np.clip(y0 + send * tau, 0, 1)
            opened = send & (u_open < np.clip(pc * pref * factor, 0, 0.95))
            ignored = send & ~opened
            annoy += ignored
            # at-risk readers (low p_return) quit more easily when annoyed: the old send rule's worry
            risk_mult = ((1 - pr) / risk_mean) ** p["optout_risk_elasticity"]
            optout = ignored & (u_opt < p["optout_hazard"] * risk_mult * annoy / tol)
            reward = session - lam * send - V * optout

            if trace is not None:
                trace.append(pd.DataFrame({"day": day, "hour": int(hour), "reader": readers[active],
                                           "their_hour": their_hour[active], "pref": pref[active],
                                           "risk": 1 - pr[active], "send": send[active], "tau": tau[active],
                                           "session": session[active], "reward": reward[active]}))
            if bandit is not None:  # learn from the decisions of readers still subscribed
                # subtract the reader's own organic session rate at this hour (estimated from slots
                # without a push), so the model only explains what a push adds: a misfit baseline
                # would otherwise leak into the effect weights, since sends aren't random
                bandit.add(z[active], send[active].astype(float), (reward - rate_here)[active])
            pushed[readers[send], art[send]] = True
            last_push[send] = np.column_stack([last_push[send, 1:], np.full(send.sum(), now)])
            last_open[opened] = now
            pushes_n += send
            opens_n += opened
            sess_h[:, j] += session & ~send   # organic sessions only (see their_hour)
            seen_h[:, j] += ~send
            ignored_by_day[:, day % 7] += ignored
            active &= ~optout
            rows.append({"day": day, "week": day // 7, "hour": int(hour), "sessions": int(session.sum()),
                         "pushes": int(send.sum()), "editorial_pushes": int(forced.sum()),
                         "opens": int(opened.sum()), "optouts": int(optout.sum()),
                         "true_effect": float((send * tau).sum()), "active": int(active.sum())})
        annoy *= decay
        if bandit is not None:
            bandit.refit()
    return pd.DataFrame(rows), bandit


def score(log: pd.DataFrame, n: int, lam: float, V: float) -> dict:
    """Per reader-day: sessions, pushes, opens, reward; opt-outs as a share of readers."""
    reader_days = n * log["day"].nunique()
    sessions, pushes, optouts = log["sessions"].sum(), log["pushes"].sum(), log["optouts"].sum()
    return {"reward_per_reader_day": (sessions - lam * pushes - V * optouts) / reader_days,
            "sessions_per_reader_day": sessions / reader_days, "pushes_per_reader_day": pushes / reader_days,
            "editorial_per_reader_day": log["editorial_pushes"].sum() / reader_days,
            "open_rate": log["opens"].sum() / max(pushes, 1), "opted_out_share": optouts / n,
            "caused_sessions_per_reader_day": log["true_effect"].sum() / reader_days}


# --- runs -----------------------------------------------------------------------------------------

def compare(data: dict, traits: dict, cfg: dict, lam: float, V: float, baselines: dict | None = None):
    """Every policy at one (lambda, V). Baselines don't depend on the prices (only their scoring
    does), so a dict of their logs can be passed in and reused."""
    n = data["n"]
    if baselines is None:
        baselines = {"never": simulate(data, traits, cfg, "never", lam, V)[0],
                     "breaking": simulate(data, traits, cfg, "breaking", lam, V)[0]}
        for c in cfg["thresholds"]:
            baselines[f"p_click >= {c:g}"] = simulate(data, traits, cfg, "threshold", lam, V, c)[0]
    log_bandit, bandit = simulate(data, traits, cfg, "bandit", lam, V)
    logs = {"contextual bandit": log_bandit, **baselines}
    table = pd.DataFrame([{"policy": name, **score(log, n, lam, V)} for name, log in logs.items()])
    thresholds = table[table["policy"].str.startswith("p_click >=")]
    best = thresholds.loc[thresholds["reward_per_reader_day"].idxmax(), "policy"]
    table["best_threshold"] = table["policy"] == best
    never = table.loc[table["policy"] == "never", "reward_per_reader_day"].item()
    table["reward_vs_never"] = table["reward_per_reader_day"] - never
    return table, logs, bandit, baselines


def run(cfg: dict, out: Path, grid: bool) -> dict:
    data = load_candidates(Path(cfg["sim_dir"]), cfg["planted"]["interest_floor"])
    traits = reader_traits(data, cfg["planted"], cfg["seed"])
    lam, V = cfg["lambda"], cfg["V"]
    table, logs, bandit, baselines = compare(data, traits, cfg, lam, V)
    best = table.loc[table["best_threshold"], "policy"].item()
    shown = ["contextual bandit", "breaking", best, "never"]

    weekly = pd.concat([logs[p].groupby("week").agg(sessions=("sessions", "sum"), pushes=("pushes", "sum"),
                                                     optouts=("optouts", "sum"), active=("active", "last"))
                        .assign(policy=p) for p in shown]).reset_index()
    weekly["reward_per_reader_day"] = (weekly["sessions"] - lam * weekly["pushes"] - V * weekly["optouts"]) / (7 * data["n"])
    hours = pd.concat([logs[p].groupby("hour")["pushes"].sum().div(data["n"] * 7 * cfg["weeks"]).rename(p)
                       for p in shown], axis=1).reset_index()
    mean, sd = bandit.theta_mean_sd()
    coefficients = pd.DataFrame({"feature": FEATURES, "theta_mean": mean, "theta_sd": sd})

    out.mkdir(parents=True, exist_ok=True)
    table.to_csv(out / "policies.csv", index=False, float_format="%.5g")
    weekly.to_csv(out / "weekly.csv", index=False, float_format="%.5g")
    hours.to_csv(out / "hours.csv", index=False, float_format="%.5g")
    coefficients.to_csv(out / "coefficients.csv", index=False, float_format="%.5g")
    from pushpin.plots import plot_bandit
    plot_bandit(weekly, hours, coefficients, table, shown, lam, V, out / "bandit.png")

    summary = {"source": f"planted world on {cfg['sim_dir']} candidates", "readers": data["n"],
               "days": 7 * cfg["weeks"], "slots_per_day": len(traits["day_hours"]), "lambda": lam, "V": V,
               "best_threshold": best, "policies": table.to_dict("records"),
               "theta": coefficients.to_dict("records"), "planted": cfg["planted"]}
    if grid:
        rows = []
        for g_lam in cfg["grid"]["lambda"]:
            for g_V in cfg["grid"]["V"]:
                t, *_ = compare(data, traits, cfg, g_lam, g_V, baselines)
                rows.append(t.assign(**{"lambda": g_lam, "V": g_V}))
                print(f"grid lambda={g_lam} V={g_V} done")
        grid_table = pd.concat(rows, ignore_index=True)
        grid_table.to_csv(out / "grid.csv", index=False, float_format="%.5g")
        from pushpin.plots import plot_bandit_grid
        plot_bandit_grid(grid_table, out / "bandit_grid.png")
        summary["grid"] = (grid_table[grid_table["policy"].isin(["contextual bandit", "breaking"]) | grid_table["best_threshold"]]
                           [["lambda", "V", "policy", "reward_vs_never", "pushes_per_reader_day", "opted_out_share"]]
                           .to_dict("records"))
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=float))
    print(table.round(4).to_string(index=False))
    print(coefficients.round(3).to_string(index=False))
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--config", default="configs/bandit.yml")
    parser.add_argument("--grid", action="store_true", help="also compare at every (lambda, V) in the grid")
    args = parser.parse_args()
    cfg = yaml.safe_load(Path(args.config).read_text())
    run(cfg, Path(cfg["out_dir"]), args.grid)
    print(f"written to {cfg['out_dir']}")


if __name__ == "__main__":
    main()
