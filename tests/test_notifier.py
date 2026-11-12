import sqlite3
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from pushnyt import notifier as nt
from tests.test_push_job import FakeScorer

TZ = ZoneInfo("America/New_York")
OBJECTIVE = {"friction": 0.2, "send": {"threshold": 0.0, "risk_penalty": 0.0, "daily_cap": 2}}


def rss(items):
    body = "".join(
        f"<item><title>{t}</title><link>https://www.nytimes.com/2026/11/12/{sec}/story-{i}.html</link>"
        f"<guid>g{i}</guid><description>About {t}</description><pubDate>{pub}</pubDate></item>"
        for i, (t, sec, pub) in enumerate(items))
    return f"<rss><channel>{body}</channel></rss>".encode()


ITEMS = [(f"Story {i}", sec, f"Thu, 12 Nov 2026 {9 + i:02d}:00:00 +0000") for i, sec in
         enumerate(["world", "us", "science", "arts"])]


class FeatureScorer(FakeScorer):
    def score(self, requests, features=False):
        rows = super().score(requests)
        return rows.assign(sim_text=0.5, user_history_len=7) if features else rows


@pytest.fixture
def setup(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(nt, "fetch_feeds", lambda urls, tz: nt.parse_rss(rss(ITEMS), tz))
    monkeypatch.setattr(nt, "send_ntfy", lambda *a, **k: sent.append((*a, k)))
    config = {"timezone": "America/New_York", "slot_hours": [8, 12, 17, 21], "lookback_hours": 24,
              "daily_cap": 2, "user_id": None, "seed": 42, "ntfy_server": "https://ntfy.sh", "feeds": []}
    store = nt.Store(tmp_path / "pushes.db", click_window_hours=24, tz=TZ)
    n = nt.Notifier(FeatureScorer(), store, config, OBJECTIVE, "topic", "http://mac:8090")
    return n, store, sent, tmp_path / "pushes.db"


def test_parse_rss():
    df = nt.parse_rss(rss(ITEMS), TZ)
    assert df["section"].tolist() == ["world", "us", "science", "arts"]
    assert df["published"].iloc[0] == pd.Timestamp("2026-11-12 04:00")  # 09:00 UTC in New York
    assert df["news_id"].str.startswith("NYT-").all() and df["news_id"].is_unique


def test_slot_logs_covariates_and_respects_cap(setup):
    n, store, sent, db = setup
    assert n.shift == pd.Timestamp("2019-11-15") - n.now().normalize()  # day after the MIND log
    noon = pd.Timestamp("2026-11-12 12:00")
    r = n.run_slot(noon)
    assert r["sent"] and r["candidates"] == 4 and len(sent) == 1
    assert sent[0][4].startswith("http://mac:8090/c/")  # tap goes through the click tracker

    scores = pd.read_sql("SELECT * FROM scores", sqlite3.connect(db))
    assert len(scores) == 4 and scores["is_top"].sum() == 1 and scores["sent"].sum() == 1
    assert {"sim_text", "user_history_len", "p_click", "push_score", "subcategory", "url"} <= set(scores.columns)

    # the pushed article isn't a candidate again; cap of 2 per day stops the third push
    assert n.run_slot(noon + pd.Timedelta(hours=5))["sent"]
    third = n.run_slot(noon + pd.Timedelta(hours=9))
    assert not third["sent"] and not third["under_cap"] and len(sent) == 2


def test_click_tracker_records_and_redirects(setup):
    n, store, sent, db = setup
    n.run_slot(pd.Timestamp("2026-11-12 12:00"))
    push_id = sent[0][4].rsplit("/", 1)[1]
    server = nt.serve_clicks(store, 0)
    port = server.server_address[1]
    try:
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        opener = urllib.request.build_opener(NoRedirect)
        with pytest.raises(urllib.error.HTTPError) as e:
            opener.open(f"http://127.0.0.1:{port}/c/{push_id}")
        assert e.value.code == 302 and e.value.headers["location"].startswith("https://www.nytimes.com/")
        with pytest.raises(urllib.error.HTTPError) as e:
            opener.open(f"http://127.0.0.1:{port}/c/nope")
        assert e.value.code == 404
    finally:
        server.shutdown()
    labeled = pd.read_sql("SELECT push_id, label FROM labeled", sqlite3.connect(db))
    assert labeled.set_index("push_id").at[push_id, "label"] == 1


def test_dry_run_sends_nothing(setup):
    n, store, sent, db = setup
    n.dry_run = True
    r = n.run_slot(pd.Timestamp("2026-11-12 12:00"))
    assert r["passes_policy"] and not r["sent"] and sent == []


def test_next_slot(setup):
    n = setup[0]
    assert n.next_slot(pd.Timestamp("2026-11-12 09:30")) == pd.Timestamp("2026-11-12 12:00")
    assert n.next_slot(pd.Timestamp("2026-11-12 21:00")) == pd.Timestamp("2026-11-13 08:00")


def test_push_has_comment_button(setup):
    n, store, sent, db = setup
    n.run_slot(pd.Timestamp("2026-11-12 12:00"))
    push_id = sent[0][4].rsplit("/", 1)[1]
    assert sent[0][5]["actions"][0]["url"] == f"http://mac:8090/comment/{push_id}"


def test_comment_form_and_storage(setup):
    import urllib.parse

    n, store, sent, db = setup
    n.run_slot(pd.Timestamp("2026-11-12 12:00"))
    push_id = sent[0][4].rsplit("/", 1)[1]
    server = nt.serve_clicks(store, 0)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        page = urllib.request.urlopen(f"{base}/comment/{push_id}").read().decode()
        assert "<textarea" in page and "Story" in page
        text = "Interesting <b>take</b> & more"
        data = urllib.parse.urlencode({"text": text}).encode()
        done = urllib.request.urlopen(f"{base}/comment/{push_id}", data=data).read().decode()
        assert "comment saved" in done
        for bad, code in ((f"{base}/comment/nope", 404), (f"{base}/comment/{push_id}", 400)):
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(bad, data=urllib.parse.urlencode({"text": "  "}).encode())
            assert e.value.code == code
    finally:
        server.shutdown()
    con = sqlite3.connect(db)
    comments = pd.read_sql("SELECT * FROM comments", con)
    assert comments["text"].tolist() == [text] and comments["user_id"].iloc[0] == n.user_id
    assert 0 <= comments["minutes_after_push"].iloc[0] < 1
    labeled = pd.read_sql("SELECT push_id, n_comments, label FROM labeled", con).set_index("push_id")
    assert labeled.at[push_id, "n_comments"] == 1
    assert pd.isna(labeled.at[push_id, "label"])  # not tapped, still inside the 24h window


def test_unclicked_push_labeled_zero_after_window(setup):
    n, store, sent, db = setup
    n.run_slot(pd.Timestamp("2026-11-12 12:00"))
    con = sqlite3.connect(db)
    con.execute("UPDATE scores SET sent_ts = sent_ts - 25 * 3600 WHERE sent = 1")
    con.commit()
    assert pd.read_sql("SELECT label FROM labeled", con)["label"].tolist() == [0]
