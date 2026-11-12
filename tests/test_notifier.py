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


def test_push_has_heart_save_and_list_buttons(setup):
    n, store, sent, db = setup
    n.run_slot(pd.Timestamp("2026-11-12 12:00"))
    push_id = sent[0][4].rsplit("/", 1)[1]
    heart, save, rlist = sent[0][5]["actions"]
    assert heart == {"action": "http", "label": "♥", "url": f"http://mac:8090/heart/{push_id}",
                     "method": "POST", "clear": False}
    assert save["url"] == f"http://mac:8090/save/{push_id}" and save["clear"]
    assert rlist == {"action": "view", "label": "📚 Reading list", "url": "http://mac:8090/saved"}

    n.cfg["button_action"] = "view"  # clients without background http actions
    assert n.actions("x")[0] == {"action": "view", "label": "♥", "url": "http://mac:8090/heart/x"}


def test_heart_save_and_reading_list(setup):
    n, store, sent, db = setup
    n.run_slot(pd.Timestamp("2026-11-12 12:00"))
    n.run_slot(pd.Timestamp("2026-11-12 17:00"))
    first, second = (a[4].rsplit("/", 1)[1] for a in sent)
    server = nt.serve_clicks(store, 0)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    post = lambda path: urllib.request.urlopen(urllib.request.Request(base + path, data=b"", method="POST"))
    try:
        assert urllib.request.urlopen(f"{base}/saved").read().decode().count("Nothing saved") == 1
        assert post(f"/heart/{first}").status == 200
        post(f"/heart/{first}")  # double tap: still one heart
        post(f"/save/{first}")
        assert "Saved" in urllib.request.urlopen(f"{base}/save/{second}").read().decode()  # view-action path
        with pytest.raises(urllib.error.HTTPError) as e:
            post("/heart/nope")
        assert e.value.code == 404
        page = urllib.request.urlopen(f"{base}/saved").read().decode()
        assert page.count("<li") == 2 and f'href="/c/{first}"' in page
    finally:
        server.shutdown()
    con = sqlite3.connect(db)
    reactions = pd.read_sql("SELECT push_id, kind, user_id, minutes_after_push FROM reactions", con)
    assert sorted(zip(reactions["push_id"], reactions["kind"])) == sorted(
        [(first, "heart"), (first, "save"), (second, "save")])
    assert (reactions["user_id"] == n.user_id).all() and (reactions["minutes_after_push"] < 1).all()
    labeled = pd.read_sql("SELECT push_id, hearted, saved, label FROM labeled", con).set_index("push_id")
    assert labeled.loc[first, ["hearted", "saved"]].tolist() == [1, 1]
    assert labeled.loc[second, ["hearted", "saved"]].tolist() == [0, 1]
    assert labeled["label"].isna().all()  # not tapped, still inside the 24h window


def test_unclicked_push_labeled_zero_after_window(setup):
    n, store, sent, db = setup
    n.run_slot(pd.Timestamp("2026-11-12 12:00"))
    con = sqlite3.connect(db)
    con.execute("UPDATE scores SET sent_ts = sent_ts - 25 * 3600 WHERE sent = 1")
    con.commit()
    assert pd.read_sql("SELECT label FROM labeled", con)["label"].tolist() == [0]
