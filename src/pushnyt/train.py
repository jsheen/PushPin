"""Train an XGBoost click model on MIND: P(click | user, article, time slot)."""

import argparse
import json
import random
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb
import yaml
from sklearn.metrics import log_loss, roc_auc_score

from pushnyt.calibration import apply_platt, calibration_metrics, fit_platt
from pushnyt.data import SLOTS, load_behaviors, load_entity_embeddings, load_news
from pushnyt.features import (
    FEATURES,
    LLM_FEATURES,
    VECTOR_FEATURES,
    build_features,
    temporal_features,
)
from pushnyt.plots import plot_calibration, plot_importance
from pushnyt.profile import load_profiles
from pushnyt.risk import RETURN_FEATURES, fit_return_model
from pushnyt.vectordb import ArticleStore


def set_seed(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)


def impression_aucs(df: pd.DataFrame, scores: np.ndarray) -> pd.Series:
    """AUC within each impression that has both a click and a non-click; the mean is group AUC."""
    return pd.Series({i: roc_auc_score(g["label"], g["score"])
                      for i, g in df.assign(score=scores).groupby("impression_id") if g["label"].nunique() == 2})


def _weighted_auc(rank: np.ndarray, y: np.ndarray, w: np.ndarray) -> float:
    """AUC with row weights; `rank` is the dense rank of each score, so ties count as half."""
    pos, neg = np.bincount(rank, w * y), np.bincount(rank, w * (1 - y))
    below = np.cumsum(neg) - neg
    return float((pos * (below + 0.5 * neg)).sum() / (pos.sum() * neg.sum()))


def bootstrap_ci(df: pd.DataFrame, scores: np.ndarray, imp_auc: pd.Series,
                 n: int, level: float, seed: int) -> dict:
    """Percentile CIs for AUC, group AUC, and AUC by slot.

    Resamples whole impressions, since candidates shown together are correlated; each
    replicate reweights rows by how often their impression was drawn.
    """
    rng = np.random.default_rng(seed)
    imp, imp_ids = df["impression_id"].factorize()
    imp_auc_pos = imp_ids.get_indexer(imp_auc.index)
    rank = np.unique(scores, return_inverse=True)[1]
    y = df["label"].to_numpy(float)
    slots = {SLOTS[s]: df["slot"].to_numpy() == s for s in np.unique(df["slot"])}
    draws = {"auc": [], "group_auc": [], **{s: [] for s in slots}}
    for _ in range(n):
        counts = np.bincount(rng.integers(0, len(imp_ids), len(imp_ids)), minlength=len(imp_ids))
        w = counts[imp].astype(float)
        draws["auc"].append(_weighted_auc(rank, y, w))
        draws["group_auc"].append(float(np.average(imp_auc.to_numpy(), weights=counts[imp_auc_pos])))
        for s, m in slots.items():
            draws[s].append(_weighted_auc(rank[m], y[m], w[m]))
    tail = 100 * (1 - level) / 2
    ci = {k: [float(x) for x in np.percentile(v, [tail, 100 - tail])] for k, v in draws.items()}
    return {"level": level, "samples": n, "auc": ci.pop("auc"), "group_auc": ci.pop("group_auc"),
            "auc_by_slot": ci}


def evaluate(df: pd.DataFrame, scores: np.ndarray, cfg: dict, seed: int) -> dict:
    imp_auc = impression_aucs(df, scores)
    metrics = {
        "rows": len(df),
        "ctr": float(df["label"].mean()),
        "auc": float(roc_auc_score(df["label"], scores)),
        "logloss": float(log_loss(df["label"], scores)),
        "group_auc": float(imp_auc.mean()),
    }
    metrics["auc_by_slot"] = {SLOTS[s]: float(roc_auc_score(g["label"], scores[g.index]))
                              for s, g in df.reset_index(drop=True).groupby("slot")}
    if cfg.get("bootstrap_samples", 0) > 0:
        metrics["ci"] = bootstrap_ci(df, scores, imp_auc, cfg["bootstrap_samples"], cfg["ci_level"], seed)
    return metrics


def check_quality(metrics: dict, cfg: dict) -> None:
    """Quality gate: fail the run if test AUC is below evaluation.min_auc (unset = no gate)."""
    min_auc = cfg.get("min_auc")
    if min_auc is not None and metrics["auc"] < min_auc:
        raise SystemExit(f"quality gate failed: test AUC {metrics['auc']:.4f} < min_auc {min_auc}")


def train(config: dict) -> None:
    seed = config["seed"]
    set_seed(seed)
    data_dir = Path(config["data"]["dir"])
    output_dir = Path(config["output"]["dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "config.yml").write_text(yaml.safe_dump(config, sort_keys=False))
    features = [f for f in FEATURES if f not in config["features"].get("exclude", [])]

    print("loading data")
    beh = load_behaviors(data_dir / config["data"]["behaviors"])
    news = load_news(data_dir / config["data"]["news"])
    emb = load_entity_embeddings(data_dir / config["data"]["entity_embeddings"])
    store = abstract_vec = None
    if set(VECTOR_FEATURES) & set(features):  # the vector database is only needed for these
        store = ArticleStore(data_dir / config["embeddings"]["chroma_dir"], config["embeddings"]["collection"])
        abstract_vec = store.vectors(news["news_id"].tolist())
    profiles = load_profiles(data_dir, config) if set(LLM_FEATURES) & set(features) else None

    print("building features")
    split = config["split"]
    rows = build_features(beh, news, emb, abstract_vec, store, pd.Timestamp(split["fit_end"]),
                          config["features"], seed, profiles)
    del profiles, abstract_vec  # only feature building uses these; free them before training
    train_end = pd.Timestamp(split["train_end"])
    train_df = rows[rows["time"] < train_end]
    test_df = rows[rows["time"] >= train_end].reset_index(drop=True)
    # latest slice of the training period is held out for early stopping
    cut = train_df["time"].quantile(1 - config["model"]["valid_fraction"])
    fit_df, valid_df = train_df[train_df["time"] < cut], train_df[train_df["time"] >= cut]
    print(f"rows: fit={len(fit_df):,} valid={len(valid_df):,} test={len(test_df):,}")

    params = {k: v for k, v in config["model"].items() if k != "valid_fraction"}
    model = xgb.XGBClassifier(**params, tree_method="hist", eval_metric="auc",
                              random_state=seed, n_jobs=-1)
    model.fit(fit_df[features], fit_df["label"],
              eval_set=[(valid_df[features], valid_df["label"])], verbose=100)

    # Platt-calibrate on the early-stopping slice; monotone, so rankings and AUC are unchanged
    platt = fit_platt(model.predict_proba(valid_df[features])[:, 1], valid_df["label"].to_numpy())
    raw = model.predict_proba(test_df[features])[:, 1]
    scores = apply_platt(raw, platt)
    metrics = evaluate(test_df, scores, config.get("evaluation", {}), seed)
    y = test_df["label"].to_numpy()
    metrics["calibration"] = {"platt": platt, "raw": calibration_metrics(y, raw),
                              "calibrated": calibration_metrics(y, scores)}

    print("fitting return model")
    temporal = temporal_features(beh, pd.Timestamp(split["fit_end"]), config["features"]["ctr_prior_strength"])
    p_return, metrics["return_model"], return_model = fit_return_model(
        beh, temporal, pd.Timestamp(split["fit_end"]), train_end, config["risk"], seed)
    p_return = pd.Series(p_return, index=beh["impression_id"])
    metrics["best_iteration"] = int(model.best_iteration)
    print(json.dumps(metrics, indent=2))

    importance = pd.Series(model.get_booster().get_score(importance_type="gain"))
    importance = importance.reindex(features).fillna(0).sort_values(ascending=False)
    print(importance.round(1).to_string())
    return_importance = pd.Series(return_model.get_booster().get_score(importance_type="gain"))
    return_importance = return_importance.reindex(RETURN_FEATURES).fillna(0).sort_values(ascending=False)

    model.save_model(output_dir / "xgb_click.json")
    return_model.save_model(output_dir / "xgb_return.json")  # needed to score P(return) live
    (output_dir / "metrics.json").write_text(json.dumps(metrics, indent=2))
    importance.rename("gain").to_csv(output_dir / "feature_importance.csv")
    return_importance.rename("gain").to_csv(output_dir / "return_feature_importance.csv")
    # handoff to the push objective (pushnyt-objective), which runs in its own container
    test_df.assign(news_id=news["news_id"].to_numpy()[test_df["news"]], p_click=scores, p_click_raw=raw,
                   p_return=p_return.reindex(test_df["impression_id"]).to_numpy())[
        ["impression_id", "user_id", "time", "slot", "news_id", "label", "p_click", "p_click_raw",
         "novelty", "p_return"]
    ].to_parquet(output_dir / "predictions.parquet", index=False)
    check_quality(metrics, config.get("evaluation", {}))  # after saving, so a failed run can be inspected
    plot_importance(importance, output_dir / "feature_importance.png")
    plot_importance(return_importance, output_dir / "return_feature_importance.png",
                    title="pushNYT XGBoost return model: feature importance")
    plot_calibration(y, raw, scores, output_dir / "calibration.png")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/train.yml")
    parser.add_argument("--exclude", nargs="*", default=[], help="features to leave out (ablations)")
    parser.add_argument("--output-dir", help="overrides output.dir in the config")
    args = parser.parse_args()
    with open(args.config) as f:
        config = yaml.safe_load(f)
    config["features"]["exclude"] = config["features"].get("exclude", []) + args.exclude
    if args.output_dir:
        config["output"]["dir"] = args.output_dir
    train(config)


if __name__ == "__main__":
    main()
