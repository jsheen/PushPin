import numpy as np
import pandas as pd

from pushpin.exploration import exploration_events, familiarity

T0 = np.datetime64("2019-11-09T00:00")


def test_first_exposure_and_what_happened_later():
    # user 0: subcat 5 shown and clicked at impression 0, shown again and clicked at impression 2;
    # subcat 6 shown and skipped at 0, never shown again; subcat 7 first shown at impression 3 (too late)
    rows = pd.DataFrame({"user": 0, "subcat": [5, 6, 5, 5, 7], "row": [0, 0, 1, 2, 3],
                         "label": [1, 0, 0, 1, 0], "novelty": [1.0, 0.5, 0.0, 0.0, 1.0]})
    times = T0 + np.array([0, 10, 20, 100], dtype="timedelta64[h]")
    ev = exploration_events(rows, times, min_follow_up_hours=48).set_index("subcat")
    assert ev.loc[5, "clicked"] == 1 and ev.loc[5, "novelty"] == 1.0  # novelty at the first exposure
    assert ev.loc[5, "reexposed"] and ev.loc[5, "clicked_later"] == 1  # any later click counts
    assert not ev.loc[6, "reexposed"]
    assert 7 not in ev.index  # under 48h of log left after it


def test_familiarity_buckets():
    assert familiarity(np.array([1.0, 0.95, 0.5]), rare=0.1).tolist() == [
        "Never shown", "Rarely shown", "Familiar"]
