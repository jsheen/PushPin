import numpy as np
import pandas as pd

from pushnyt.nyt import map_subcategories, slot_times
from pushnyt.vectordb import ExactStore


def test_exact_store_returns_nearest_in_order_and_respects_size():
    vecs = np.eye(4, dtype=np.float32)
    store = ExactStore(["a", "b", "c", "d"], vecs)
    q = np.array([[0.1, 0.9, 0.0, 0.5]], np.float32)
    assert store.nearest(q, 3) == [["b", "d", "a"]]
    store.size = 2  # only the first two are "published"
    assert store.nearest(q, 3) == [["b", "a"]]


def test_map_subcategories_picks_nearest_centroid(monkeypatch):
    monkeypatch.setattr("pushnyt.nyt.MIN_SUBCAT_ARTICLES", 1)
    mind = np.array([[1, 0], [1, 0.1], [0, 1]], np.float32)
    names, sims = map_subcategories(np.array([[0.2, 1.0], [1.0, 0.0]], np.float32), mind,
                                    np.array([0, 0, 1]), np.array(["sports", "food"]))
    assert names.tolist() == ["food", "sports"]
    assert (sims > 0.9).all()


def test_slot_times_three_per_day():
    slots = slot_times(pd.Timestamp("2024-12-02"), 2)
    assert [t.hour for t in slots] == [8, 13, 19, 8, 13, 19]
    assert slots[-1].day == 3
