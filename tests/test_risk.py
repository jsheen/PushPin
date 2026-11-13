import numpy as np
import pandas as pd

from pushpin.risk import return_labels

T0 = pd.Timestamp("2019-11-09")


def test_return_labels_and_censoring():
    beh = pd.DataFrame({"user_id": ["a", "a", "b", "a", "c"],
                        "time": T0 + pd.to_timedelta([0, 10, 12, 60, 70], unit="h")})
    y = return_labels(beh, horizon_hours=24)
    # a@0 returns at 10h; a@10 next at 60h (> 24h) and 10+24 <= 70 is observable -> 0;
    # b@12 never returns, 12+24 <= 70 -> 0; a@60 and c@70: horizon runs past the log -> censored
    assert y[:3].tolist() == [1.0, 0.0, 0.0]
    assert np.isnan(y[3]) and np.isnan(y[4])


def test_return_inside_the_horizon_is_known_even_near_the_end():
    beh = pd.DataFrame({"user_id": ["a", "a"], "time": [T0, T0 + pd.Timedelta(hours=2)]})
    assert return_labels(beh, horizon_hours=24)[0] == 1.0
