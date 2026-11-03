import numpy as np
import pandas as pd

from pushnyt.nyt import map_subcategories
from pushnyt.push_job import slot_times
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


def test_daily_report_counts_per_day():
    from pushnyt.push_job import daily_report
    t1, t2 = pd.Timestamp("2024-12-02 08:00"), pd.Timestamp("2024-12-03 08:00")
    scores = pd.DataFrame({"slot_time": [t1, t1, t1, t2, t2], "news_id": ["a", "b", "c", "d", "e"]})
    pushes = pd.DataFrame({
        "slot_time": [t1, t1, t1, t2], "user_id": ["u1", "u2", "u3", "u1"], "news_id": ["a", "a", "b", "d"],
        "title": ["A", "A", "B", "D"], "section": ["U.S.", "U.S.", "World", "Arts"],
        "p_click": [0.1, 0.2, 0.3, 0.4], "novelty": [1.0, 1.0, 0.5, 1.0], "sent": [True, True, False, False]})
    d = daily_report(scores, pushes).set_index("day")
    assert d.loc["2024-12-02", ["articles", "decisions", "sent", "users_reached"]].tolist() == [3, 3, 2, 2]
    assert d.loc["2024-12-02", "most_sent_title"] == "A" and d.loc["2024-12-02", "most_sent_users"] == 2
    assert abs(d.loc["2024-12-02", "share_of_articles_pushed"] - 1 / 3) < 1e-9
    assert d.loc["2024-12-03", "sent"] == 0 and d.loc["2024-12-03", "send_rate"] == 0
