"""Paired bootstrap comparison of two training runs on the same test set (e.g. an ablation).

Separate per-run CIs overlap even when one model is reliably better, because most of their
width is shared test-set noise. Here each replicate resamples whole impressions once and
scores both runs on that same sample, so the CI is for the difference itself.

Usage: pushpin-compare outputs/ablation-no-llm outputs/ablation-with-llm
Writes <b>/compare_vs_<a name>.json.
"""

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

from pushpin.train import _weighted_auc, impression_aucs

KEYS = ["impression_id", "news_id"]


def load_pair(a: Path, b: Path) -> tuple[pd.DataFrame, np.ndarray, np.ndarray]:
    """Test rows and both runs' scores, aligned on (impression, article)."""
    pa, pb = (pd.read_parquet(d / "predictions.parquet") for d in (a, b))
    if len(pa) != len(pb) or pa.duplicated(KEYS).any():
        raise ValueError("runs must share the same test rows, one per (impression_id, news_id)")
    df = pa[KEYS + ["label"]].merge(pb[KEYS + ["label", "p_click"]], on=KEYS, suffixes=("", "_b"))
    if len(df) != len(pa) or not (df["label"] == df["label_b"]).all():
        raise ValueError("runs do not share the same test rows and labels")
    score_a = pa.set_index(KEYS)["p_click"].reindex(pd.MultiIndex.from_frame(df[KEYS])).to_numpy()
    return df[KEYS + ["label"]], score_a, df["p_click"].to_numpy()


def paired_bootstrap(df: pd.DataFrame, score_a: np.ndarray, score_b: np.ndarray,
                     n: int, level: float, seed: int) -> dict:
    """Point estimates, CIs and one-sided p-values for b - a in AUC and group AUC."""
    y = df["label"].to_numpy(float)
    imp, imp_ids = df["impression_id"].factorize()
    rank_a = np.unique(score_a, return_inverse=True)[1]
    rank_b = np.unique(score_b, return_inverse=True)[1]
    auc_a, auc_b = impression_aucs(df, score_a), impression_aucs(df, score_b)
    both = auc_a.index.intersection(auc_b.index)
    gdiff = (auc_b[both] - auc_a[both]).to_numpy()
    gpos = imp_ids.get_indexer(both)

    rng = np.random.default_rng(seed)
    draws = {"auc": [], "group_auc": []}
    for _ in range(n):
        counts = np.bincount(rng.integers(0, len(imp_ids), len(imp_ids)), minlength=len(imp_ids))
        w = counts[imp].astype(float)
        draws["auc"].append(_weighted_auc(rank_b, y, w) - _weighted_auc(rank_a, y, w))
        draws["group_auc"].append(float(np.average(gdiff, weights=counts[gpos])))

    point = {"auc": float(roc_auc_score(y, score_b) - roc_auc_score(y, score_a)),
             "group_auc": float(gdiff.mean())}
    tail = 100 * (1 - level) / 2
    out = {"level": level, "samples": n, "impressions": len(imp_ids)}
    for k, v in draws.items():
        v = np.asarray(v)
        out[k] = {"a": float(roc_auc_score(y, score_a)) if k == "auc" else float(auc_a.mean()),
                  "b": float(roc_auc_score(y, score_b)) if k == "auc" else float(auc_b.mean()),
                  "diff": point[k], "ci": [float(x) for x in np.percentile(v, [tail, 100 - tail])],
                  "p_b_not_better": float((v <= 0).mean())}
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("a", type=Path, help="baseline run directory")
    parser.add_argument("b", type=Path, help="run compared against the baseline")
    parser.add_argument("--samples", type=int, default=1000)
    parser.add_argument("--level", type=float, default=0.95)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    df, score_a, score_b = load_pair(args.a, args.b)
    result = {"a": str(args.a), "b": str(args.b),
              **paired_bootstrap(df, score_a, score_b, args.samples, args.level, args.seed)}
    path = args.b / f"compare_vs_{args.a.name}.json"
    path.write_text(json.dumps(result, indent=2))
    print(f"{args.b.name} vs {args.a.name}: {result['impressions']:,} impressions, "
          f"{args.samples} paired replicates, {args.level:.0%} CI")
    for k in ("auc", "group_auc"):
        r = result[k]
        print(f"  {k:10s} {r['a']:.4f} -> {r['b']:.4f}  diff {r['diff']:+.4f}  "
              f"CI [{r['ci'][0]:+.4f}, {r['ci'][1]:+.4f}]  P(diff <= 0) {r['p_b_not_better']:.3f}")
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
