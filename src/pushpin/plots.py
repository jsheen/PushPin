"""Plots saved alongside the training outputs."""

from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # no display inside the container
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.patches import Patch
from matplotlib.ticker import MultipleLocator, NullFormatter

# Feature groups from features.py, colored in fixed categorical order. The palette has eight
# slots, so embedding retrieval shares one with click/skip contrast (both compare the candidate's
# embedding with the user's history) and article recency shares one with user depth.
GROUPS = {
    "Similarity": ("#2a78d6", {"sim_text", "sim_entity", "subcat_affinity"}),
    "Slot engagement": ("#eb6834", {"slot", "hour", "slot_prior_impr", "slot_prior_clicks",
                                    "slot_engaged_rate", "slot_click_share"}),
    "Complementarity": ("#1baf7a", {"complement_score"}),
    "Push fatigue": ("#eda100", {"slot_skipped_72h", "slot_skip_rate_72h", "impr_last_24h",
                                 "hours_since_last_impr", "hours_since_last_slot_impr"}),
    "Embedding similarity": ("#e87ba4", {"emb_sim_mean", "emb_sim_max", "emb_sim_top5", "emb_n_close",
                                         "emb_retrieval_rank", "emb_sim_clicked_max",
                                         "emb_sim_skipped_max", "emb_contrast"}),
    "Recency & depth": ("#008300", {"article_age_hours", "user_history_len", "user_prior_impr"}),
    "Subcategory engagement": ("#6250d6", {"subcat_engaged_rate", "subcat_prior_skips"}),
    "LLM reader profile": ("#e34948", {"llm_sim_click", "llm_sim_skip", "llm_contrast"}),
}
SURFACE, TEXT, TEXT_MUTED, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
MODEL = "#2a78d6"  # categorical slot 1
SECOND = "#eb6834"  # categorical slot 2


def plot_importance(importance: pd.Series, path: Path,
                    title: str = "PushPin XGBoost click model: feature importance") -> None:
    """Horizontal bar chart of XGBoost gain per feature, colored by feature group."""
    color = {f: c for c, feats in GROUPS.values() for f in feats}
    imp = importance.sort_values()

    fig, ax = plt.subplots(figsize=(8, 1.4 + 0.3 * len(imp)), facecolor=SURFACE)
    ax.set_facecolor(SURFACE)
    y = range(len(imp))
    ax.barh(y, imp.to_numpy(), height=0.6, color=[color.get(f, TEXT_MUTED) for f in imp.index])
    for i, v in enumerate(imp.to_numpy()):
        ax.text(v + imp.max() * 0.012, i, f"{v:.1f}", va="center", color=TEXT_MUTED, fontsize=9)

    ax.set_yticks(list(y), imp.index, color=TEXT, fontfamily="monospace", fontsize=9)
    ax.set_xlabel("Average gain per split", color=TEXT_MUTED)
    ax.set_xlim(0, imp.max() * 1.12)
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.tick_params(axis="both", length=0, colors=TEXT_MUTED)
    for spine in ax.spines.values():
        spine.set_visible(False)

    ax.set_title(title, loc="left",
                 color=TEXT, fontsize=13, fontweight="bold", pad=28)
    ax.annotate("Gain = average loss reduction each time the feature is used in a split",
                xy=(0, 1), xycoords="axes fraction", xytext=(0, 8), textcoords="offset points",
                color=TEXT_MUTED, fontsize=9)
    ax.legend(handles=[Patch(color=c, label=g) for g, (c, feats) in GROUPS.items() if feats & set(imp.index)],
              loc="lower right", frameon=False, labelcolor=TEXT)

    fig.tight_layout()
    fig.savefig(path, dpi=200, facecolor=SURFACE)
    plt.close(fig)


def plot_tradeoff(curves: pd.DataFrame, path: Path, friction: float) -> None:
    """Top-pick CTR vs. share of never-shown subcategories, one point per friction.

    `curves` has a `policy` column: "model" (XGBoost engagement), "random" (engagement
    replaced by a random ranking) and optionally "most recent" (engagement replaced by article
    freshness), so the gaps are what the click model adds over chance and over recency.
    """
    model, rand, recent = (curves[curves["policy"] == p].sort_values("friction")
                           for p in ("model", "random", "most recent"))
    fig, ax = plt.subplots(figsize=(8, 5.6), facecolor=SURFACE)
    ax.set_facecolor(SURFACE)
    if len(recent):
        ax.plot(recent["new_subcat_at_1"], recent["ctr_at_1"], color=TEXT_MUTED, lw=2, ls=(0, (1, 2)),
                marker="s", ms=7, mfc=SURFACE, mew=2, label="Most recent article")
        ax.annotate("friction 0", (recent["new_subcat_at_1"].iloc[0], recent["ctr_at_1"].iloc[0]),
                    xytext=(-10, -4), textcoords="offset points", ha="right", color=TEXT_MUTED, fontsize=9)
    ax.plot(rand["new_subcat_at_1"], rand["ctr_at_1"], color=TEXT_MUTED, lw=2, ls=(0, (4, 3)),
            marker="o", ms=8, mfc=SURFACE, mew=2, label="Random engagement ranking")
    ax.plot(model["new_subcat_at_1"], model["ctr_at_1"], color=MODEL, lw=2,
            marker="o", ms=8, mec=SURFACE, mew=2, label="XGBoost engagement (PushPin)")

    # selective friction labels on the model curve; the configured friction is emphasized
    for _, r in model[model["friction"].isin({0, 0.1, 0.2, 0.5, 1.0, friction})].iterrows():
        chosen = r["friction"] == friction
        ax.annotate(f"friction {r['friction']:g}" + (" (config)" if chosen else ""),
                    (r["new_subcat_at_1"], r["ctr_at_1"]), xytext=(6, 7), textcoords="offset points",
                    color=TEXT if chosen else TEXT_MUTED, fontsize=9,
                    fontweight="bold" if chosen else "normal")
    ax.annotate("friction 0", (rand["new_subcat_at_1"].iloc[0], rand["ctr_at_1"].iloc[0]),
                xytext=(6, -14), textcoords="offset points", color=TEXT_MUTED, fontsize=9)

    ax.xaxis.set_major_formatter(lambda x, _: f"{x:.0%}")
    ax.yaxis.set_major_formatter(lambda y, _: f"{y:.0%}")
    ax.yaxis.set_major_locator(MultipleLocator(0.02))
    ax.set_ylim(0, model["ctr_at_1"].max() * 1.12)
    ax.set_xlim(0, 1)
    ax.set_xlabel("Top pushes from a subcategory the user was never shown", color=TEXT_MUTED)
    ax.set_ylabel("Click-through rate of the top push", color=TEXT_MUTED)
    ax.grid(color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.tick_params(axis="both", length=0, colors=TEXT_MUTED)
    for spine in ax.spines.values():
        spine.set_visible(False)

    ax.set_title("Push objective: engagement vs. novelty as friction rises", loc="left",
                 color=TEXT, fontsize=13, fontweight="bold", pad=28)
    ax.text(0, 1.035, "Top push per test-day impression; the gaps to the gray curves are what the click model adds",
            transform=ax.transAxes, color=TEXT_MUTED, fontsize=9)
    handles, labels = ax.get_legend_handles_labels()
    ax.legend(handles[::-1], labels[::-1], loc="upper right", frameon=False, labelcolor=TEXT, handlelength=4)

    fig.tight_layout()
    fig.savefig(path, dpi=200, facecolor=SURFACE)
    plt.close(fig)


def plot_tuning(trials: pd.DataFrame, space: dict, metric: str, path: Path) -> None:
    """Validation metric against each tuned parameter, one panel per parameter.

    A flat cloud means the parameter barely matters; a trend means it does. The config's own
    params (trial 0) and the chosen trial are marked in every panel; Bayesian trials are colored
    apart from the random ones that seeded the search.
    """
    col = f"valid_{metric}"
    names = list(space)
    ncols = 4
    nrows = -(-len(names) // ncols)
    fig, axes = plt.subplots(nrows, ncols, figsize=(3.2 * ncols, 2.8 * nrows + 0.8), facecolor=SURFACE,
                             sharey=True, squeeze=False)
    best = trials[trials["chosen"]].iloc[0] if "chosen" in trials else trials.loc[trials[col].idxmax()]
    config = trials[trials["trial"] == 0].iloc[0]
    lo, hi = trials[col].min(), trials[col].max()
    pad = max(hi - lo, 1e-4) * 0.1
    for ax, name in zip(axes.flat, names):
        ax.set_facecolor(SURFACE)
        for source, color, label in (("random", TEXT_MUTED, "Random trial"), ("bayes", MODEL, "Bayesian trial")):
            pts = trials[trials["source"] == source]
            if len(pts):
                ax.scatter(pts[name], pts[col], s=22, color=color, alpha=0.75, linewidths=0, label=label)
        ax.scatter([config[name]], [config[col]], s=46, facecolor=SURFACE, edgecolor=TEXT_MUTED, linewidths=1.6,
                   label="Config params (trial 0)", zorder=3)
        ax.scatter([best[name]], [best[col]], s=60, color=SECOND, marker="D", label="Chosen trial", zorder=4)
        if space[name][0] == "log":
            ax.set_xscale("log")
            ax.xaxis.set_minor_formatter(NullFormatter())  # short log ranges otherwise label every minor tick
        ax.set_ylim(lo - pad, hi + pad)
        ax.set_title(name, loc="left", color=TEXT, fontsize=10, fontfamily="monospace")
        ax.grid(color=GRID, linewidth=0.8)
        ax.set_axisbelow(True)
        ax.tick_params(axis="both", length=0, colors=TEXT_MUTED, labelsize=8)
        for spine in ax.spines.values():
            spine.set_visible(False)
    for ax in axes[:, 0]:
        ax.set_ylabel(f"Validation {metric.replace('_', ' ')}", color=TEXT_MUTED, fontsize=9)
    for ax in axes.flat[len(names):]:
        ax.axis("off")
    handles, labels = axes.flat[0].get_legend_handles_labels()
    (axes.flat[-1] if len(names) % ncols else fig).legend(handles, labels, loc="center", frameon=False,
                                                          labelcolor=TEXT, fontsize=9)

    fig.suptitle("PushPin XGBoost click model: hyperparameter tuning", x=0.01, ha="left",
                 color=TEXT, fontsize=13, fontweight="bold")
    fig.text(0.01, 1 - 0.55 / fig.get_figheight(),
             f"{len(trials) - 1} trials after the config params; a flat cloud means the parameter "
             "barely matters", color=TEXT_MUTED, fontsize=9)
    fig.tight_layout(rect=(0, 0, 1, 1 - 0.5 / fig.get_figheight()))
    fig.savefig(path, dpi=200, facecolor=SURFACE)
    plt.close(fig)


def _style(ax, title: str, subtitle: str) -> None:
    ax.grid(color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.tick_params(axis="both", length=0, colors=TEXT_MUTED)
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.set_title(title, loc="left", color=TEXT, fontsize=13, fontweight="bold", pad=28)
    ax.text(0, 1.035, subtitle, transform=ax.transAxes, color=TEXT_MUTED, fontsize=9)


def plot_calibration(y: np.ndarray, raw: np.ndarray, calibrated: np.ndarray, path: Path) -> None:
    """Reliability diagram: predicted vs. observed click rate in 20 equal-count bins."""
    from pushpin.calibration import reliability

    fig, ax = plt.subplots(figsize=(7, 6), facecolor=SURFACE)
    ax.set_facecolor(SURFACE)
    top = 0
    for p, color, ls, mfc, label in [(raw, TEXT_MUTED, (0, (4, 3)), SURFACE, "Raw XGBoost"),
                                      (calibrated, MODEL, "-", MODEL, "Platt-calibrated")]:
        pred, obs, _ = reliability(y, p)
        top = max(top, pred.max(), obs.max())
        ax.plot(pred, obs, color=color, lw=2, ls=ls, marker="o", ms=8, mfc=mfc,
                mec=SURFACE if mfc == MODEL else color, mew=2, label=label)
    top *= 1.08
    ax.plot([0, top], [0, top], color=GRID, lw=1.5, zorder=0)
    ax.annotate("perfect calibration", (top * 0.62, top * 0.62), rotation=45, color=TEXT_MUTED, fontsize=9,
                ha="center", va="bottom", rotation_mode="anchor", transform_rotates_text=True)
    ax.xaxis.set_major_locator(MultipleLocator(0.02))
    ax.yaxis.set_major_locator(MultipleLocator(0.02))
    ax.set_xlim(0, top)
    ax.set_ylim(0, top)
    ax.xaxis.set_major_formatter(lambda x, _: f"{x:.0%}")
    ax.yaxis.set_major_formatter(lambda v, _: f"{v:.0%}")
    ax.set_xlabel("Predicted click probability (bin mean)", color=TEXT_MUTED)
    ax.set_ylabel("Observed click rate", color=TEXT_MUTED)
    _style(ax, "Click model calibration", "Test day, 20 equal-count bins; on the diagonal = predictions mean what they say")
    ax.legend(loc="upper left", frameon=False, labelcolor=TEXT, handlelength=4)
    fig.tight_layout()
    fig.savefig(path, dpi=200, facecolor=SURFACE)
    plt.close(fig)


def plot_volume(volume: pd.DataFrame, path: Path, threshold: float, min_candidates: int) -> None:
    """Pushes per user-day vs. click rate of the pushes sent, one point per send threshold."""
    fig, ax = plt.subplots(figsize=(8, 5.6), facecolor=SURFACE)
    ax.set_facecolor(SURFACE)
    series = [("all", "All users", TEXT_MUTED, (0, (4, 3)), SURFACE),
              ("low risk", "Low-risk users (likely to return)", MODEL, "-", MODEL),
              ("high risk", "High-risk users (unlikely to return)", SECOND, "-", SECOND)]
    for group, label, color, ls, mfc in series:
        g = volume[volume["group"] == group].sort_values("threshold")
        ax.plot(g["pushes_per_user_day"], g["ctr_sent"], color=color, lw=2, ls=ls, marker="o", ms=8,
                mfc=mfc, mec=color if mfc == SURFACE else SURFACE, mew=2, label=label)
        chosen = g[g["threshold"] == threshold].iloc[0]
        if group == "all":
            ax.annotate(f"threshold {threshold:g} (config)", (chosen["pushes_per_user_day"], chosen["ctr_sent"]),
                        xytext=(8, 8), textcoords="offset points", color=TEXT, fontsize=9, fontweight="bold")
        for _, r in g[g["threshold"].isin({g["threshold"].min(), g["threshold"].max()})].iterrows():
            if group == "all":
                ax.annotate(f"threshold {r['threshold']:g}", (r["pushes_per_user_day"], r["ctr_sent"]),
                            xytext=(8, -14), textcoords="offset points", color=TEXT_MUTED, fontsize=9)
    ax.yaxis.set_major_locator(MultipleLocator(0.02))
    ax.yaxis.set_major_formatter(lambda v, _: f"{v:.0%}")
    ax.set_xlim(0, None)
    ax.set_ylim(0, volume["ctr_sent"].max() * 1.15)
    ax.set_xlabel("Pushes sent per active user per day", color=TEXT_MUTED)
    ax.set_ylabel("Click-through rate of pushes sent", color=TEXT_MUTED)
    _style(ax, "Send policy: fewer, better pushes as the threshold rises",
           f"Test day, impressions with {min_candidates}+ candidates; at-risk users face a higher threshold")
    ax.legend(loc="upper right", frameon=False, labelcolor=TEXT, handlelength=4)
    fig.tight_layout()
    fig.savefig(path, dpi=200, facecolor=SURFACE)
    plt.close(fig)


def plot_exploration(summary: pd.DataFrame, path: Path, rare: float, follow_up_hours: float) -> None:
    """Later click rate in a subcategory, by how familiar it was and the first response to it."""
    buckets = list(summary["bucket"].cat.categories)
    labels = {"Never shown": "Never shown\nbefore", "Rarely shown": f"Rarely shown\n(<{rare:.0%} of exposure)",
              "Familiar": "Familiar"}
    series = [("Clicked the first time", MODEL), ("Shown and skipped", TEXT_MUTED)]
    fig, ax = plt.subplots(figsize=(8, 5.6), facecolor=SURFACE)
    ax.set_facecolor(SURFACE)
    width = 0.36
    for i, (first, color) in enumerate(series):
        g = summary[summary["first"] == first].set_index("bucket").reindex(buckets)
        x = np.arange(len(buckets)) + (i - 0.5) * (width + 0.02)  # 2-unit gap between paired bars
        ax.bar(x, g["rate"], width, color=color, label=first, zorder=2)
        ax.errorbar(x, g["rate"], yerr=g["ci"], fmt="none", ecolor=TEXT, elinewidth=1, capsize=3, zorder=3)
        for xi, r, ci in zip(x, g["rate"], g["ci"]):
            ax.text(xi, r + ci + 0.006, f"{r:.0%}", ha="center", va="bottom", color=TEXT, fontsize=9)
    ax.set_xticks(np.arange(len(buckets)), [labels[b] for b in buckets], color=TEXT)
    ax.yaxis.set_major_locator(MultipleLocator(0.05))
    ax.yaxis.set_major_formatter(lambda v, _: f"{v:.0%}")
    ax.set_ylim(0, (summary["rate"] + summary["ci"]).max() * 1.2)
    ax.set_xlabel("Subcategory at the user's first exposure in the log", color=TEXT_MUTED)
    ax.set_ylabel("Click it again when shown it later", color=TEXT_MUTED)
    _style(ax, "Do clicks on unfamiliar subcategories stick?",
           f"MIND, Nov 9–14 2019; first exposures with {follow_up_hours:g}h+ of follow-up; "
           "users shown the subcategory again; 95% CIs")
    ax.grid(axis="x", visible=False)
    ax.legend(loc="upper right", frameon=False, labelcolor=TEXT)
    fig.tight_layout()
    fig.savefig(path, dpi=200, facecolor=SURFACE)
    plt.close(fig)


def plot_fatigue(table: pd.DataFrame, path: Path, n_users: int, min_impressions: int) -> None:
    """Fatigue index by recent and cumulative exposure, as two panels on one y-scale."""
    views = list(dict.fromkeys(table["view"]))
    fig, axes = plt.subplots(1, len(views), figsize=(10, 5.2), sharey=True, facecolor=SURFACE)
    lo, hi = table["lo"].min(), table["hi"].max()
    pad = (hi - lo) * 0.25
    for ax, view in zip(axes, views):
        g = table[table["view"] == view]
        x = np.arange(len(g))
        ax.set_facecolor(SURFACE)
        ax.axhline(1.0, color=TEXT_MUTED, lw=1, ls=(0, (4, 3)), zorder=1)
        ax.fill_between(x, g["lo"], g["hi"], color=MODEL, alpha=0.15, lw=0, zorder=2)
        ax.plot(x, g["index"], color=MODEL, lw=2, marker="o", ms=8, mec=SURFACE, mew=2, zorder=3)
        ax.set_xticks(x, [f"{b}\n{n:,} users" for b, n in zip(g["bucket"], g["users"])], color=TEXT, fontsize=9)
        ax.set_xlabel(view, color=TEXT_MUTED)
        ax.set_xlim(-0.6, len(g) - 0.4)
        ax.set_ylim(lo - pad, hi + pad)
        ax.yaxis.set_major_formatter(lambda v, _: f"{v:.2f}")
        ax.grid(axis="y", color=GRID, linewidth=0.8)
        ax.set_axisbelow(True)
        ax.tick_params(axis="both", length=0, colors=TEXT_MUTED)
        for spine in ax.spines.values():
            spine.set_visible(False)
    axes[0].set_ylabel("Clicks vs. the user's usual (dashed 1.00 = usual)", color=TEXT_MUTED)
    fig.suptitle("Fatigue: engagement after recent and cumulative exposure", x=0.07, ha="left",
                 color=TEXT, fontsize=13, fontweight="bold", y=0.96)
    fig.text(0.07, 0.885, f"MIND, Nov 9–14 2019; {n_users:,} users with {min_impressions}+ impressions; "
             "adjusted for impression size; 95% CIs resample users", color=TEXT_MUTED, fontsize=9)
    fig.subplots_adjust(left=0.08, right=0.98, top=0.84, bottom=0.17, wspace=0.1)
    fig.savefig(path, dpi=200, facecolor=SURFACE)
    plt.close(fig)


def _clean(ax) -> None:
    ax.set_facecolor(SURFACE)
    ax.set_axisbelow(True)
    ax.tick_params(axis="both", length=0, colors=TEXT_MUTED)
    for spine in ax.spines.values():
        spine.set_visible(False)


def _panel_title(ax, title: str, subtitle: str) -> None:
    ax.set_title(title, loc="left", color=TEXT, fontsize=11, fontweight="bold", pad=22)
    ax.text(0, 1.03, subtitle, transform=ax.transAxes, color=TEXT_MUTED, fontsize=8.5)


def plot_reach(pushes: pd.DataFrame, daily: pd.DataFrame, published_share: dict, n_users: int,
               n_articles: int, path: Path) -> None:
    """Who and what the simulated pushes reach: section mix vs. what was published, daily reach,
    concentration of pushes on a few articles, and section diversity per user."""
    sent = pushes[pushes["sent"]]
    fig, axes = plt.subplots(2, 2, figsize=(12, 9.4), facecolor=SURFACE)
    (a_sec, a_day), (a_conc, a_div) = axes
    for ax in axes.flat:
        _clean(ax)

    # 1. section mix: share of pushes sent vs. share of articles published
    sent_share = sent["section"].value_counts(normalize=True)
    pub = pd.Series(published_share, dtype=float)
    top = (sent_share.add(pub, fill_value=0)).sort_values(ascending=False).index[:10]
    rows = pd.DataFrame({"sent": sent_share.reindex(top).fillna(0), "pub": pub.reindex(top).fillna(0)})
    rows = rows.sort_values("sent")
    y, h = np.arange(len(rows)), 0.38
    a_sec.barh(y + h / 2, rows["sent"], height=h, color=MODEL, label="Share of pushes sent")
    a_sec.barh(y - h / 2, rows["pub"], height=h, color=GRID, label="Share of articles published")
    for i, (s, p) in enumerate(zip(rows["sent"], rows["pub"])):
        a_sec.text(s + 0.004, i + h / 2, f"{s:.0%}", va="center", color=TEXT, fontsize=8)
        a_sec.text(p + 0.004, i - h / 2, f"{p:.0%}", va="center", color=TEXT_MUTED, fontsize=8)
    a_sec.set_yticks(y, rows.index, color=TEXT, fontsize=9)
    a_sec.xaxis.set_major_formatter(lambda v, _: f"{v:.0%}")
    a_sec.set_xlim(0, rows.to_numpy().max() * 1.18)
    a_sec.grid(axis="x", color=GRID, linewidth=0.8)
    a_sec.legend(loc="lower right", frameon=False, labelcolor=TEXT, fontsize=8.5)
    _panel_title(a_sec, "Section mix: pushed vs. published", "Top 10 sections; bars past grey = over-pushed")

    # 2. daily reach: users who got at least one push
    days = pd.to_datetime(daily["day"])
    reach = daily["users_reached"] / n_users
    x = np.arange(len(daily))
    a_day.bar(x, reach, width=0.6, color=MODEL)
    for i, r in enumerate(reach):
        a_day.text(i, r + 0.01, f"{r:.0%}", ha="center", color=TEXT, fontsize=8.5)
    a_day.set_xticks(x, [d.strftime("%a\n%b %d") for d in days], color=TEXT, fontsize=8.5)
    a_day.yaxis.set_major_formatter(lambda v, _: f"{v:.0%}")
    a_day.set_ylim(0, min(1.0, reach.max() * 1.2) if reach.max() > 0 else 1.0)
    a_day.grid(axis="y", color=GRID, linewidth=0.8)
    a_day.set_ylabel(f"Users reached (of {n_users:,})", color=TEXT_MUTED)
    _panel_title(a_day, "Daily reach", "Share of simulated users sent at least one push that day")

    # 3. concentration: cumulative share of pushes vs. share of published articles, most-pushed first
    per_article = np.sort(sent["news_id"].value_counts().to_numpy())[::-1]
    counts = np.concatenate([per_article, np.zeros(max(n_articles - len(per_article), 0))])
    xs = np.arange(len(counts) + 1) / len(counts)
    ys = np.concatenate([[0], np.cumsum(counts) / counts.sum()])
    a_conc.plot([0, 1], [0, 1], color=TEXT_MUTED, lw=1, ls=(0, (4, 3)))
    a_conc.text(0.62, 0.53, "every article\npushed equally", color=TEXT_MUTED, fontsize=8.5, ha="left", va="top")
    a_conc.plot(xs, ys, color=MODEL, lw=2)
    k = max(1, round(0.1 * len(counts)))
    a_conc.plot([xs[k]], [ys[k]], "o", ms=8, color=MODEL, mec=SURFACE, mew=2)
    a_conc.annotate(f"top 10% of articles\n= {ys[k]:.0%} of pushes", (xs[k], ys[k]), xytext=(12, -2),
                    textcoords="offset points", ha="left", va="top", color=TEXT, fontsize=9, fontweight="bold")
    never = 1 - len(per_article) / len(counts)
    a_conc.plot([1 - never], [1], "o", ms=8, color=MODEL, mec=SURFACE, mew=2)
    a_conc.annotate(f"{never:.0%} of articles\nnever pushed", (1 - never, 1), xytext=(8, -8),
                    textcoords="offset points", ha="left", va="top", color=TEXT_MUTED, fontsize=8.5)
    a_conc.set_xlim(0, 1)
    a_conc.set_ylim(0, 1.02)
    a_conc.xaxis.set_major_formatter(lambda v, _: f"{v:.0%}")
    a_conc.yaxis.set_major_formatter(lambda v, _: f"{v:.0%}")
    a_conc.grid(color=GRID, linewidth=0.8)
    a_conc.set_xlabel(f"Published articles, most-pushed first ({n_articles:,})", color=TEXT_MUTED)
    a_conc.set_ylabel("Cumulative share of pushes sent", color=TEXT_MUTED)
    _panel_title(a_conc, "Concentration", "How much of the push volume a few stories take")

    # 4. diversity: distinct sections each user was pushed over the run
    per_user = sent.groupby("user_id")["section"].nunique()
    none = n_users - len(per_user)
    dist = per_user.value_counts().sort_index()
    dist = pd.concat([pd.Series({0: none}), dist])
    a_div.bar(dist.index, dist.to_numpy() / n_users, width=0.65, color=[GRID] + [MODEL] * (len(dist) - 1))
    for v, n in dist.items():
        share = n / n_users
        a_div.text(v, share + 0.006, f"{share:.0%}" if share >= 0.01 else "<1%", ha="center", color=TEXT, fontsize=8.5)
    a_div.set_xticks(dist.index, [str(v) for v in dist.index], color=TEXT, fontsize=9)
    a_div.yaxis.set_major_formatter(lambda v, _: f"{v:.0%}")
    a_div.grid(axis="y", color=GRID, linewidth=0.8)
    a_div.set_xlabel("Distinct sections pushed to the user (grey = no pushes)", color=TEXT_MUTED)
    a_div.set_ylabel("Share of users", color=TEXT_MUTED)
    _panel_title(a_div, "Diversity per user",
                 f"Mean {per_user.mean():.1f} sections among users pushed at least once")

    start, end = pd.to_datetime(daily["day"]).min(), pd.to_datetime(daily["day"]).max()
    fig.suptitle("NYT simulation: who and what the pushes reach", x=0.06, ha="left",
                 color=TEXT, fontsize=14, fontweight="bold", y=0.985)
    fig.text(0.06, 0.945, f"{n_users:,} MIND users, {n_articles:,} NYT articles, {start:%b %d} – {end:%b %d %Y}; "
             f"{len(sent):,} pushes sent. Simulated decisions, no real clicks.", color=TEXT_MUTED, fontsize=9)
    fig.subplots_adjust(left=0.13, right=0.98, top=0.86, bottom=0.08, hspace=0.42, wspace=0.3)
    fig.savefig(path, dpi=200, facecolor=SURFACE)
    plt.close(fig)


UPLIFT_STYLE = {  # policy -> (color, linestyle); categorical slots in fixed order, references in ink
    "DR-learner": (MODEL, "-"), "click (p_click)": (SECOND, "-"), "structural prior": ("#1baf7a", "-"),
    "T-learner": ("#eda100", "-"), "random": (TEXT_MUTED, ":"), "oracle": (TEXT, "--"),
}


def plot_uplift(curves: pd.DataFrame, gates: pd.DataFrame, policies: pd.DataFrame, path: Path) -> None:
    """Three panels: uplift (Qini) curves, GATES by tau_hat quintile, and each policy at the current budget."""
    fig, axes = plt.subplots(1, 3, figsize=(16, 5.2), facecolor=SURFACE, gridspec_kw={"width_ratios": [1.25, 1, 1.15]})
    for ax in axes:
        ax.set_facecolor(SURFACE)

    ax = axes[0]
    # share of decisions the current policy pushes
    budget = policies.loc[policies["policy"] == "current (click threshold)", "pushes"].item() / gates["rows"].sum()
    for name, (color, ls) in UPLIFT_STYLE.items():
        c = curves[curves["policy"] == name]
        if c.empty:
            continue
        ax.plot(c["share_pushed"], c["true"], color=color, linestyle=ls, linewidth=2, label=name)
    dr = curves[curves["policy"] == "DR-learner"]
    ax.plot(dr["share_pushed"], dr["dr"], color=MODEL, linewidth=1, alpha=0.6, linestyle="--",
            label="DR-learner, estimated from the log")
    ax.axvline(budget, color=GRID, linewidth=1.5, zorder=0)
    ax.annotate("current budget", (budget, ax.get_ylim()[1]), xytext=(4, -12), textcoords="offset points",
                color=TEXT_MUTED, fontsize=8)
    _style(ax, "Uplift curves", "Extra returns per 1,000 decisions, pushing the top share by each ranking")
    ax.set_xlabel("Share of decisions pushed", color=TEXT_MUTED)
    ax.legend(frameon=False, fontsize=8, labelcolor=TEXT, loc="lower right")

    ax = axes[1]
    x = gates["group"].to_numpy()
    ax.errorbar(x - 0.08, gates["dr_effect"], yerr=1.96 * gates["dr_se"], fmt="o", color=MODEL, markersize=7,
                elinewidth=2, capsize=0, label="DR estimate, 95% CI")
    ax.scatter(x + 0.08, gates["true_effect"], marker="D", s=40, color=TEXT, label="True effect", zorder=3)
    ax.axhline(0, color=TEXT_MUTED, linewidth=1)
    ax.set_xticks(x, [f"Q{g}" for g in x])
    _style(ax, "GATES", "Effect within quintiles of the DR-learner's tau_hat")
    ax.set_xlabel("tau_hat quintile (Q5 = largest predicted effect)", color=TEXT_MUTED)
    ax.set_ylabel("Push effect on return probability", color=TEXT_MUTED)
    ax.legend(frameon=False, fontsize=8, labelcolor=TEXT, loc="upper left")

    ax = axes[2]
    p = policies.iloc[::-1].reset_index(drop=True)
    y = np.arange(len(p))
    ax.barh(y, p["true_effect_per_1000"], height=0.55, color=GRID, label="True")
    ax.errorbar(p["dr_estimate_per_1000"], y, xerr=1.96 * p["dr_se_per_1000"], fmt="o", color=MODEL, markersize=6,
                elinewidth=2, capsize=0, label="DR off-policy estimate, 95% CI")
    for i, v in enumerate(p["true_effect_per_1000"]):
        ax.text(max(v, 0) + 2, i + 0.3, f"{v:.0f}", color=TEXT_MUTED, fontsize=8, va="center")
    ax.set_yticks(y, p["policy"], color=TEXT, fontsize=9)
    ax.axvline(0, color=TEXT_MUTED, linewidth=1)
    ax.set_xlim(right=1.45 * (p["dr_estimate_per_1000"] + 1.96 * p["dr_se_per_1000"]).max())
    _style(ax, "Policies at the current budget", "Extra returns per 1,000 pushes (same number of pushes)")
    ax.legend(frameon=False, fontsize=8, labelcolor=TEXT, loc="upper right")

    fig.suptitle("PushPin uplift: doubly robust estimates on the semi-synthetic NYT simulation", x=0.01, ha="left",
                 color=TEXT, fontsize=13, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    fig.savefig(path, dpi=200, facecolor=SURFACE)
    plt.close(fig)


BANDIT_COLORS = [MODEL, SECOND, "#1baf7a"]  # bandit, breaking, best threshold (categorical order); never in ink


def plot_bandit(weekly: pd.DataFrame, hours: pd.DataFrame, coefficients: pd.DataFrame, table: pd.DataFrame,
                shown: list[str], lam: float, V: float, path: Path) -> None:
    """Three panels: reward vs never pushing by week, pushes by hour of day, the learned effect weights."""
    fig, axes = plt.subplots(1, 3, figsize=(17, 5.4), facecolor=SURFACE, gridspec_kw={"width_ratios": [1.2, 1, 1]})
    styles = {name: (BANDIT_COLORS[i], "-") for i, name in enumerate(shown[:3])} | {shown[3]: (TEXT_MUTED, "--")}

    ax = axes[0]
    _clean(ax)
    # breaking is far below the others, so it's left out of this panel and given in the subtitle
    never = weekly[weekly["policy"] == "never"].set_index("week")["reward_per_reader_day"]
    for name in [shown[0], shown[2], shown[3]]:
        w = weekly[weekly["policy"] == name].set_index("week")["reward_per_reader_day"] - never
        color, ls = styles[name]
        ax.plot(w.index + 1, w.to_numpy(), color=color, linestyle=ls, linewidth=2, marker="o" if ls == "-" else None,
                markersize=4, label=name + (" (best fixed threshold)" if name == shown[2] else ""))
    ax.grid(color=GRID, linewidth=0.8)
    ax.set_xlabel("Week", color=TEXT_MUTED)
    ax.set_ylabel("Reward per reader-day, minus never pushing", color=TEXT_MUTED)
    breaking = table.loc[table["policy"] == shown[1], "reward_vs_never"].item()
    _panel_title(ax, "Reward by week, minus never pushing",
                 f"sessions − {lam:g}·pushes − {V:g}·opt-outs; breaking ({breaking:+.2f}) is off the scale")
    ax.legend(frameon=False, fontsize=8, labelcolor=TEXT, loc="lower right")

    ax = axes[1]
    _clean(ax)
    for name in shown[:3]:
        color, _ = styles[name]
        ax.plot(hours["hour"], hours[name], color=color, linewidth=2, marker="o", markersize=5, label=name)
    ax.grid(color=GRID, linewidth=0.8)
    ax.set_xticks(hours["hour"], [str(h) for h in hours["hour"]])
    ax.set_xlabel("Hour of day (New York)", color=TEXT_MUTED)
    ax.set_ylim(bottom=0)
    ax.set_ylabel("Pushes per reader-day at this hour", color=TEXT_MUTED)
    _panel_title(ax, "When each policy pushes", "Breaking sends at every slot")
    ax.legend(frameon=False, fontsize=8, labelcolor=TEXT, loc="center right")

    ax = axes[2]
    _clean(ax)
    c = coefficients.iloc[::-1].reset_index(drop=True)
    y = np.arange(len(c))
    ax.errorbar(c["theta_mean"], y, xerr=1.96 * c["theta_sd"], fmt="o", color=MODEL, markersize=6, elinewidth=2,
                capsize=0)
    ax.axvline(0, color=TEXT_MUTED, linewidth=1)
    ax.set_yticks(y, c["feature"], color=TEXT, fontsize=9)
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    ax.set_xlabel("Weight (mean, 95% interval)", color=TEXT_MUTED)
    _panel_title(ax, "What the bandit learned", "Effect of sending; send when the total is > 0")

    best = table.loc[table["policy"] == shown[0]].iloc[0]
    fig.suptitle(f"PushPin contextual bandit on the NYT simulation: {best['pushes_per_reader_day']:.2f} pushes/reader-day, "
                 f"{100 * best['opted_out_share']:.1f}% opted out", x=0.01, ha="left", color=TEXT, fontsize=13,
                 fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    fig.savefig(path, dpi=200, facecolor=SURFACE)
    plt.close(fig)


V_SHADES = ["#9ec5f4", "#2a78d6", "#0d3f7f"]  # one hue, light to dark: V is ordered (small to large opt-out price)


def plot_bandit_grid(grid: pd.DataFrame, path: Path) -> None:
    """The comparison at every (lambda, V): reward vs never pushing, and pushes per reader-day."""
    bandit = grid[grid["policy"] == "contextual bandit"]
    best = grid[grid["best_threshold"]]
    breaking = grid[grid["policy"] == "breaking"]["reward_vs_never"]
    lams = sorted(grid["lambda"].unique())
    x = np.arange(len(lams))
    fig, axes = plt.subplots(1, 2, figsize=(13, 5), facecolor=SURFACE)
    for ax, col, title, sub in (
            (axes[0], "reward_vs_never", "Reward vs never pushing",
             f"Per reader-day. Breaking is off the scale: {breaking.max():+.2f} to {breaking.min():+.2f}"),
            (axes[1], "pushes_per_reader_day", "How much the bandit pushes", "Pushes per reader-day")):
        _clean(ax)
        for color, V in zip(V_SHADES, sorted(grid["V"].unique()), strict=True):
            b = bandit[bandit["V"] == V].set_index("lambda").loc[lams, col]
            ax.plot(x, b.to_numpy(), color=color, linewidth=2, marker="o", markersize=7, label=f"bandit, V = {V:g}")
        if col == "reward_vs_never":
            t = best.groupby("lambda")[col].max().loc[lams]
            ax.plot(x, t.to_numpy(), color=TEXT_MUTED, linestyle="--", linewidth=2, label="best fixed threshold (any V)")
            ax.axhline(0, color=TEXT, linewidth=1)
        ax.set_xticks(x, [f"λ = {lam:g}" for lam in lams])
        ax.grid(color=GRID, linewidth=0.8)
        ax.set_xlabel("Cost of one push, in sessions", color=TEXT_MUTED)
        _panel_title(ax, title, sub)
        ax.legend(frameon=False, fontsize=8, labelcolor=TEXT, loc="upper right")
    fig.suptitle("Contextual bandit across the two prices (V = cost of a reader turning notifications off)",
                 x=0.01, ha="left", color=TEXT, fontsize=13, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    fig.savefig(path, dpi=200, facecolor=SURFACE)
    plt.close(fig)
