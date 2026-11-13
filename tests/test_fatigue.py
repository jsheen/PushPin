import numpy as np
import pandas as pd

from pushpin.fatigue import fatigue_index

BINS, LABELS = [-1, 0, 2, np.inf], ["0", "1–2", "3+"]


def _table(fatigue: float, seed: int = 0) -> pd.DataFrame:
    """Users with different baseline engagement; clicks drop by `fatigue` per earlier exposure."""
    rng = np.random.default_rng(seed)
    rows = []
    for u in range(300):
        base = rng.uniform(0.5, 2.0)  # heavy vs. light clickers
        for exposure in range(6):
            expected = base * 2.0
            rows.append({"user": u, "exposure": exposure, "expected": expected,
                         "clicks": rng.poisson(expected * (1 - fatigue * exposure))})
    d = pd.DataFrame(rows)
    d["expected"] *= d.groupby("user")["clicks"].transform("sum") / d.groupby("user")["expected"].transform("sum")
    return d


def test_index_falls_with_exposure_when_there_is_fatigue():
    idx = fatigue_index(_table(fatigue=0.08), "exposure", BINS, LABELS, n_boot=50, seed=0)
    assert idx["index"].is_monotonic_decreasing
    assert idx["hi"].iloc[-1] < 1 < idx["lo"].iloc[0]


def test_index_is_flat_without_fatigue_despite_heavy_and_light_users():
    idx = fatigue_index(_table(fatigue=0.0), "exposure", BINS, LABELS, n_boot=50, seed=0)
    assert ((idx["lo"] < 1) & (idx["hi"] > 1)).all()
