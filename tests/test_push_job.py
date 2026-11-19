import socket
import threading
import time
import zlib

import numpy as np
import pandas as pd
import pytest
import uvicorn
from fastapi.testclient import TestClient

from pushpin.push_job import SCORE_COLUMNS, HttpScorer, run_week
from pushpin.scorer_api import create_app

OBJECTIVE = {"friction": 0.2, "send": {"threshold": 0.3, "risk_penalty": 0.04, "daily_cap": 2}}


class FakeScorer:
    """Deterministic stand-in for LocalScorer, with a missing feature to exercise NaN."""

    def __init__(self):
        self.articles = pd.DataFrame()

    def info(self):
        return {"log_end": "2019-11-14T23:59:00", "articles": len(self.articles), "users": 6}

    def users(self, n, seed):
        return [f"U{i}" for i in range(min(n, 6))]

    def add_articles(self, articles):
        self.articles = pd.concat([self.articles, articles])
        return pd.DataFrame({"news_id": articles["news_id"], "subcategory": "news", "subcat_sim": 0.5})

    def score(self, requests):
        rows = []
        for r in requests:
            for n in r["news_ids"]:
                h = zlib.crc32(f"{r['user_id']}{n}".encode()) / 2**32
                rows.append({"impression_id": r["impression_id"], "user_id": r["user_id"], "news_id": n,
                             "p_click": h, "p_click_raw": h, "novelty": 1 - h, "p_return": 0.9,
                             "llm_sim_click": h / 2, "llm_sim_skip": np.nan, "emb_sim_max": h,
                             "subcat_affinity": 0.1, "article_age_hours": 1.0})
        return pd.DataFrame(rows)[SCORE_COLUMNS]


@pytest.fixture
def nyt_csv(tmp_path):
    times = pd.date_range("2024-12-02 05:00", periods=12, freq="80min", tz="America/New_York").tz_convert("UTC")
    path = tmp_path / "nyt.csv"
    pd.DataFrame({
        "_id": [f"nyt://article/{i}" for i in range(12)],
        "headline": [str({"main": f"Story {i}"}) for i in range(12)],
        "abstract": [f"Abstract {i}" for i in range(12)],
        "pub_date": times.astype(str), "document_type": "article",
        "section_name": ["U.S.", "World", "Arts"] * 4, "subsection_name": "", "web_url": "https://example.com",
    }).to_csv(path, index=False)
    return path


@pytest.fixture
def server():
    """The scorer service on a free local port, in a background thread."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(create_app(FakeScorer()), host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(timeout=5)


def test_push_job_over_http_matches_in_process(nyt_csv, server):
    start = pd.Timestamp("2024-12-02")
    local = run_week(FakeScorer(), nyt_csv, 6, start, 1, 0, OBJECTIVE)
    remote = run_week(HttpScorer(server), nyt_csv, 6, start, 1, 0, OBJECTIVE)
    pd.testing.assert_frame_equal(local[0], remote[0])  # every score, NaN included, bit for bit
    pd.testing.assert_frame_equal(local[1], remote[1])  # same pushes and send decisions
    assert remote[0]["llm_sim_skip"].isna().all()
    assert remote[1]["sent"].any()


def test_slot_pool_matches_the_batch_send_policy(nyt_csv):
    """Deciding slot by slot gives the same sends as the batch rule (earliest first, cap per day)."""
    from pushpin.objective import send_decisions

    scores, pushes, _, _ = run_week(FakeScorer(), nyt_csv, 6, pd.Timestamp("2024-12-02"), 1, 0, OBJECTIVE)
    best = scores.loc[scores.groupby("impression_id")["push_score"].idxmax(), "news_id"].to_numpy()
    assert (pushes["news_id"].to_numpy() == best).all()
    send = OBJECTIVE["send"]
    batch = send_decisions(pushes.assign(time=pushes["slot_time"]), send["threshold"], send["risk_penalty"],
                           send["daily_cap"])
    assert (pushes["sent"].to_numpy() == batch).all()


class OldestFirstScorer(FakeScorer):
    """Every user likes older articles more: the earliest article of the day always ranks first."""

    def score(self, requests):
        rows = super().score(requests)
        rows["p_click"] = 1 / (1 + rows["news_id"].str.extract(r"(\d+)$")[0].astype(int))
        return rows


def day_pool_run(nyt_csv, threshold, scorer=None):
    objective = {"friction": 0.0, "send": {"threshold": threshold, "risk_penalty": 0.0, "daily_cap": 3}}
    return run_week(scorer or OldestFirstScorer(), nyt_csv, 6, pd.Timestamp("2024-12-02"), 1, 0, objective,
                    (8, 13, 19), day_pool=True, keep_top=3)


def test_day_pool_keeps_an_unsent_article_for_later_slots(nyt_csv):
    scores, pushes, summary, articles = day_pool_run(nyt_csv, threshold=2.0)  # nothing clears it: nothing sent
    pools = articles.groupby("slot_time")["news_id"].apply(set).to_list()
    assert pools[0] < pools[1] < pools[2]  # each slot's pool holds the earlier slots' articles too
    assert not pushes["sent"].any()
    assert pushes["news_id"].nunique() == 1  # the 05:00 article loses no slot to newer ones: it is the pick all day
    assert scores.groupby("impression_id").size().max() <= 4  # the best 3, plus the newest if it isn't one
    assert scores.groupby("impression_id")["is_newest"].sum().eq(1).all()
    newest = scores[scores["is_newest"]].merge(articles, on=["slot_time", "news_id"])
    # the latest published by each slot: 07:40 by 08:00, 13:00 by 13:00, 18:20 by 19:00
    assert newest.groupby("slot_time")["news_id"].first().tolist() == ["NYT-2", "NYT-6", "NYT-10"]
    assert summary["day_pool"] and summary["user_article_scores"] > len(scores)  # counts every candidate scored


def test_day_pool_never_pushes_an_article_twice(nyt_csv):
    _, pushes, _, _ = day_pool_run(nyt_csv, threshold=0.0)  # everything clears it, up to the cap of 3
    assert pushes["sent"].all()
    assert not pushes.duplicated(["user_id", "news_id"]).any()
    assert pushes.groupby("user_id")["news_id"].apply(list).iloc[0] == ["NYT-0", "NYT-1", "NYT-2"]  # oldest unpushed


def test_scorer_api_endpoints():
    client = TestClient(create_app(FakeScorer()))
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/users", params={"n": 3}).json()["users"] == ["U0", "U1", "U2"]
    mapped = client.post("/articles", json={"articles": [
        {"news_id": "A", "title": "t", "abstract": "a", "published": "2019-11-15T08:00:00"}]}).json()
    assert mapped["subcategory"] == ["news"]
    out = client.post("/score", json={"requests": [
        {"user_id": "U0", "time": "2019-11-15T09:00:00", "news_ids": ["A"], "impression_id": 7}]})
    assert out.status_code == 200 and "NaN" in out.text  # missing features travel as NaN


def test_local_scorer_implements_the_scorer_interface():
    from pushpin.nyt import LocalScorer
    from pushpin.push_job import HttpScorer

    for method in ("info", "users", "add_articles", "score"):
        assert callable(getattr(LocalScorer, method, None)), method
        assert callable(getattr(HttpScorer, method, None)), method


def test_http_scorer_sends_a_cached_gcp_id_token(monkeypatch):
    """auth="gcp": a private Cloud Run scorer gets a Bearer token from the metadata server, fetched once."""
    import io

    from pushpin import push_job

    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req)
        body = b"tok-123" if "metadata.google.internal" in req.full_url else b'{"ok": true}'
        return io.BytesIO(body)

    monkeypatch.setattr(push_job.urllib.request, "urlopen", fake_urlopen)
    scorer = push_job.HttpScorer("https://scorer.example/", auth="gcp")
    assert scorer.info() == {"ok": True}
    assert scorer.info() == {"ok": True}
    meta = [r for r in calls if "metadata.google.internal" in r.full_url]
    assert len(meta) == 1 and "audience=https://scorer.example" in meta[0].full_url
    assert meta[0].get_header("Metadata-flavor") == "Google"
    assert all(r.get_header("Authorization") == "Bearer tok-123" for r in calls if r not in meta)


def test_http_scorer_without_auth_sends_no_token(monkeypatch):
    import io

    from pushpin import push_job

    calls = []
    monkeypatch.setattr(push_job.urllib.request, "urlopen",
                        lambda req, timeout=None: calls.append(req) or io.BytesIO(b"{}"))
    push_job.HttpScorer("http://scorer:8000").info()
    assert len(calls) == 1 and calls[0].get_header("Authorization") is None
