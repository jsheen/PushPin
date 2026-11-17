"""The uplift experiment's randomized design, shared by the notifier (live) and uplift.py (sim).

Core dependencies only, so the notifier's small image can use it. See docs/uplift.md.
"""

import hashlib

import numpy as np
import pandas as pd


def assignment_draw(push_id: str) -> float:
    """A uniform draw in [0, 1) fixed by the push id, so a holdout decision can be recomputed."""
    return int(hashlib.sha1(f"holdout:{push_id}".encode()).hexdigest(), 16) / 16 ** 40


def propensity(eligible: np.ndarray, holdout: float, explore: float) -> np.ndarray:
    """P(send): eligible picks are sent unless held out; ineligible ones are explored with `explore`."""
    return np.where(eligible, 1 - holdout, explore)


def pushes_today(df: pd.DataFrame, reader: str = "reader") -> np.ndarray:
    """Pushes the reader already got earlier the same day (a fatigue covariate, fixed before this slot)."""
    order = df.sort_values([reader, "slot_time"], kind="stable")
    day = order["slot_time"].dt.normalize()
    before = order.groupby([order[reader], day])["treated"].cumsum() - order["treated"]
    return before.reindex(df.index).to_numpy()
