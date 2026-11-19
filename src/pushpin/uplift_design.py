"""The uplift experiment's randomized design (uplift.py's semi-synthetic benchmark). See docs/uplift.md.

Core dependencies only.
"""

import numpy as np
import pandas as pd


def propensity(eligible: np.ndarray, holdout: float, explore: float) -> np.ndarray:
    """P(send): eligible picks are sent unless held out; ineligible ones are explored with `explore`."""
    return np.where(eligible, 1 - holdout, explore)


def pushes_today(df: pd.DataFrame, reader: str = "reader") -> np.ndarray:
    """Pushes the reader already got earlier the same day (a fatigue covariate, fixed before this slot)."""
    order = df.sort_values([reader, "slot_time"], kind="stable")
    day = order["slot_time"].dt.normalize()
    before = order.groupby([order[reader], day])["treated"].cumsum() - order["treated"]
    return before.reindex(df.index).to_numpy()
