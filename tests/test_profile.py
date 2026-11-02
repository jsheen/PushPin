import json
from types import SimpleNamespace

import numpy as np
import pandas as pd

from pushnyt.features import llm_features
from pushnyt.profile import build_prompt, retrieve, run_batches, snapshot_days

T0 = pd.Timestamp("2019-11-12")


def _beh(impressions: list[tuple[str, pd.Timestamp, str]], history: list[str] = ()) -> pd.DataFrame:
    return pd.DataFrame({"user_id": [u for u, _, _ in impressions], "time": [t for _, t, _ in impressions],
                         "history": [list(history)] * len(impressions),
                         "impressions": [i for _, _, i in impressions]})


def test_retrieve_uses_only_earlier_days_and_nearest_skips():
    news_idx = {f"N{i}": i for i in range(5)}
    vec = np.array([[1, 0], [0.9, 0.44], [0, 1], [0.95, 0.31], [0.7, 0.71]], dtype=np.float32)
    beh = _beh([("a", T0 + pd.Timedelta(hours=9), "N0-1 N1-0 N2-0"),        # day 1: click 0, skip 1, 2
                ("a", T0 + pd.Timedelta(days=1, hours=9), "N3-0 N4-1"),      # day 2
                ("a", T0 + pd.Timedelta(days=2, hours=9), "N1-1 N2-0")])     # day 3
    days = snapshot_days(beh, T0 + pd.Timedelta(days=1))
    assert days == [T0 + pd.Timedelta(days=1), T0 + pd.Timedelta(days=2)]
    out = retrieve(beh, news_idx, vec, days, max_clicks=10, skips_per_click=1).set_index("day")

    day2 = out.loc[days[0]]  # sees only day 1
    assert day2["clicks"] == [0] and day2["near_skips"] == [[1]]  # 1 is closer to 0 than 2 is
    day3 = out.loc[days[1]]  # sees days 1-2; 4 was clicked, 3 skipped
    assert day3["clicks"] == [0, 4]
    assert day3["near_skips"] == [[3], [1]]  # nearest of the skips {1, 2, 3} to each click
    assert day3["custom_id"] == "20191114-a"


def test_retrieve_drops_skips_clicked_later_and_users_without_clicks():
    news_idx = {f"N{i}": i for i in range(3)}
    vec = np.eye(3, dtype=np.float32)
    beh = _beh([("a", T0, "N1-0 N0-1"), ("a", T0 + pd.Timedelta(hours=1), "N1-1 N2-0"),
                ("a", T0 + pd.Timedelta(days=1), "N0-0 N2-1"),
                ("b", T0 + pd.Timedelta(days=1), "N0-0 N1-1")])  # b: no history, no earlier clicks
    out = retrieve(beh, news_idx, vec, [T0 + pd.Timedelta(days=1)], 10, 2)
    assert out["user_id"].tolist() == ["a"]
    assert out.iloc[0]["clicks"] == [0, 1]
    assert all(s == [2] for s in out.iloc[0]["near_skips"])  # 1 was skipped, then clicked


def test_retrieve_keeps_most_recent_clicks():
    news_idx = {f"N{i}": i for i in range(4)}
    beh = _beh([("a", T0, "N3-1"), ("a", T0 + pd.Timedelta(days=1), "N0-1")], history=["N0", "N1", "N2"])
    out = retrieve(beh, news_idx, np.eye(4, dtype=np.float32), [T0 + pd.Timedelta(days=1)], 2, 2)
    assert out.iloc[0]["clicks"] == [2, 3]  # history is oldest first; in-log clicks come after it
    assert out.iloc[0]["near_skips"] == [[], []]


def test_build_prompt_lists_each_skip_once():
    text = ["A", "B", "C"]
    prompt = build_prompt([0, 1], [[2], [2]], text)
    assert prompt.count("SKIPPED: C") == 1 and "CLICKED: A" in prompt and "CLICKED: B" in prompt
    assert "(No skipped articles.)" in build_prompt([0], [[]], text)


class FakeBatches:
    def __init__(self, results):
        self.results_by_id, self.created = results, []

    def create(self, requests):
        self.created.append(requests)
        return SimpleNamespace(id=f"b{len(self.created)}")

    def retrieve(self, batch_id):
        return SimpleNamespace(processing_status="ended")

    def results(self, batch_id):
        return self.results_by_id


def _ok(cid, payload, stop="end_turn"):
    msg = SimpleNamespace(stop_reason=stop, usage=SimpleNamespace(input_tokens=100, output_tokens=20),
                          content=[SimpleNamespace(type="text", text=json.dumps(payload))])
    return SimpleNamespace(custom_id=cid, result=SimpleNamespace(type="succeeded", message=msg))


def test_run_batches_parses_results_and_resumes(tmp_path):
    summary = {"click_summary": "NFL recaps", "skip_summary": "Fantasy football"}
    batches = FakeBatches([_ok("x", summary), _ok("y", summary, stop="max_tokens"),
                           SimpleNamespace(custom_id="z", result=SimpleNamespace(type="errored"))])
    client = SimpleNamespace(messages=SimpleNamespace(batches=batches))
    state = tmp_path / "batches.json"
    requests = {c: {"model": "m"} for c in "xyz"}

    summaries, report = run_batches(client, requests, state, "v1", poll_seconds=0)
    assert summaries == {"x": summary}
    assert report["failed"] == {"y": "max_tokens", "z": "errored"}
    assert report["usage"] == {"input_tokens": 200, "output_tokens": 40}
    assert len(batches.created) == 1

    run_batches(client, requests, state, "v1", poll_seconds=0)  # same version: no resubmission
    assert len(batches.created) == 1
    run_batches(client, requests, state, "v2", poll_seconds=0)  # new prompt version: resubmits
    assert len(batches.created) == 2


def test_llm_features_use_the_profile_for_the_impression_day():
    keys = pd.DataFrame({"day": [T0, T0 + pd.Timedelta(days=1)], "user_id": ["a", "a"]})
    click = np.array([[1, 0], [0, 1]], dtype=np.float32)
    skip = np.array([[0, 1], [np.nan, np.nan]], dtype=np.float32)  # no skip summary on day 2
    rows = pd.DataFrame({"time": [T0 + pd.Timedelta(hours=5), T0 + pd.Timedelta(days=1, hours=5),
                                  T0 - pd.Timedelta(hours=1), T0 + pd.Timedelta(hours=5)],
                         "user_id": ["a", "a", "a", "b"], "news": [0, 0, 0, 0]})
    out = llm_features(rows, np.array([[1, 0]], dtype=np.float32), (keys, click, skip))
    assert out.loc[0].tolist() == [1.0, 0.0, 1.0]
    assert out.loc[1, "llm_sim_click"] == 0 and out.loc[1, ["llm_sim_skip", "llm_contrast"]].isna().all()
    assert out.loc[2].isna().all() and out.loc[3].isna().all()  # before any profile; no profile
