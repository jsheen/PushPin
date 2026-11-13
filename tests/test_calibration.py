import numpy as np
from scipy.special import expit

from pushpin.calibration import apply_platt, calibration_metrics, fit_platt


def test_platt_fixes_overconfidence_and_keeps_the_ranking():
    rng = np.random.default_rng(0)
    true_p = expit(rng.normal(-3, 1, 50_000))
    y = (rng.random(len(true_p)) < true_p).astype(float)
    raw = expit(2 * np.log(true_p / (1 - true_p)) + 2.5)  # overconfident and shifted
    calibrated = apply_platt(raw, fit_platt(raw, y))
    assert (np.argsort(raw, kind="stable") == np.argsort(calibrated, kind="stable")).all()
    assert calibration_metrics(y, calibrated)["ece"] < calibration_metrics(y, raw)["ece"] / 3
    assert abs(calibrated.mean() - y.mean()) < 0.005


def test_perfectly_calibrated_scores_have_small_ece():
    rng = np.random.default_rng(1)
    p = rng.uniform(0, 0.3, 100_000)
    y = (rng.random(len(p)) < p).astype(float)
    assert calibration_metrics(y, p)["ece"] < 0.005
