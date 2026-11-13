"""End to end: train on the synthetic data, then run the objective on its predictions."""

import json
import sys

import pandas as pd
import yaml

from pushpin import objective
from pushpin.train import train


def test_train_then_objective(train_config, tmp_path, monkeypatch):
    train(train_config)
    run = tmp_path / "run"
    for name in ("xgb_click.json", "xgb_return.json", "metrics.json", "feature_importance.csv", "feature_importance.png",
                 "config.yml", "predictions.parquet"):
        assert (run / name).exists(), name
    metrics = json.loads((run / "metrics.json").read_text())
    assert 0 <= metrics["auc"] <= 1 and "ci" in metrics
    preds = pd.read_parquet(run / "predictions.parquet")
    assert {"impression_id", "user_id", "news_id", "label", "p_click", "p_click_raw", "novelty",
            "p_return"} <= set(preds.columns)
    assert {"calibration", "return_model"} <= set(metrics)
    assert (run / "calibration.png").exists()

    cfg = tmp_path / "objective.yml"
    send = {"threshold": 0.05, "risk_penalty": 0.04, "daily_cap": 2, "sweep": [0, 0.05, 0.1],
            "eval_min_candidates": 1}
    cfg.write_text(yaml.safe_dump({"run_dir": str(run), "friction": 0.2, "sweep": [0, 0.5, 1], "seed": 0,
                                   "send": send}))
    monkeypatch.setattr(sys, "argv", ["pushpin-objective", "--config", str(cfg)])
    objective.main()
    out = run / "objective"
    curve = pd.read_csv(out / "tradeoff.csv")
    assert set(curve["policy"]) == {"model", "random"}
    pushes = pd.read_parquet(out / "pushes.parquet")
    assert len(pushes) == preds["impression_id"].nunique()
    assert "send" in pushes
    assert (out / "tradeoff.png").exists() and (out / "summary.json").exists()
    assert (out / "volume.png").exists() and set(pd.read_csv(out / "volume.csv")["group"]) == {
        "all", "low risk", "high risk"}
