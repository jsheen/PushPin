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
    assert sent[0][4].startswith("http://mac:8090/p/")  # tap opens the push's card

    scores = pd.read_sql("SELECT * FROM scores", sqlite3.connect(db))
    assert len(scores) == 4 and scores["is_top"].sum() == 1 and scores["sent"].sum() == 1
    assert {"sim_text", "user_history_len", "p_click", "push_score", "subcategory", "url"} <= set(scores.columns)

    # the pushed article isn't a candidate again; cap of 2 per day stops the third push
    assert n.run_slot(noon + pd.Timedelta(hours=5))["sent"]
    third = n.run_slot(noon + pd.Timedelta(hours=9))
    assert not third["sent"] and not third["under_cap"] and len(sent) == 2


def test_dry_run_sends_nothing(setup):
    n, store, sent, db = setup
    n.dry_run = True
    r = n.run_slot(pd.Timestamp("2026-11-12 12:00"))
    assert r["passes_policy"] and not r["sent"] and sent == []


def test_next_slot(setup):
    n = setup[0]
    assert n.next_slot(pd.Timestamp("2026-11-12 09:30")) == pd.Timestamp("2026-11-12 12:00")
    assert n.next_slot(pd.Timestamp("2026-11-12 21:00")) == pd.Timestamp("2026-11-13 08:00")


def test_card_read_heart_save_and_reading_list(setup):
    n, store, sent, db = setup
    n.run_slot(pd.Timestamp("2026-11-12 12:00"))
    n.run_slot(pd.Timestamp("2026-11-12 17:00"))
    first, second = (a[4].rsplit("/", 1)[1] for a in sent)
    assert all("actions" not in a[5] or not a[5]["actions"] for a in sent)
    server = nt.serve_clicks(store, 0)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    get = lambda path: urllib.request.urlopen(base + path).read().decode()
    post = lambda path, body=b"on": urllib.request.urlopen(urllib.request.Request(base + path, data=body, method="POST"))
    try:
        assert "Nothing saved" in get("/saved")
        card = get(f"/p/{first}")  # the tap
        assert "Read on NYT" in card and 'href="https://www.nytimes.com/' in card and "About Story" in card
        assert 'aria-pressed="false">♡ Love' in card
        post(f"/read/{first}")
        post(f"/heart/{first}"), post(f"/heart/{first}")  # double tap: still one
        post(f"/save/{first}"), post(f"/save/{second}")
        post(f"/save/{second}", b"off")  # unsave
        assert 'aria-pressed="true">♥ Loved' in get(f"/p/{first}")
        for bad in ("/heart/nope", "/read/nope", "/bogus/" + first):
            with pytest.raises(urllib.error.HTTPError) as e:
                post(bad)
            assert e.value.code == 404
        with pytest.raises(urllib.error.HTTPError) as e:
            get("/p/nope")
        assert e.value.code == 404
        listing = get("/saved")
        assert listing.count("<li") == 1 and f'href="/p/{first}"' in listing and "· read" in listing
    finally:
        server.shutdown()
    con = sqlite3.connect(db)
    reactions = pd.read_sql("SELECT push_id, kind, user_id, minutes_after_push FROM reactions", con)
    assert sorted(zip(reactions["push_id"], reactions["kind"])) == [(first, "heart"), (first, "save")]
    assert (reactions["user_id"] == n.user_id).all() and (reactions["minutes_after_push"] < 1).all()
    labeled = pd.read_sql("SELECT push_id, label, read, hearted, saved FROM labeled", con).set_index("push_id")
    assert labeled.loc[first].tolist() == [1, 1, 1, 1]  # tapped, read, loved, saved
    assert labeled.loc[second, ["read", "hearted", "saved"]].tolist() == [0, 0, 0]
    assert pd.isna(labeled.at[second, "label"])  # not tapped, still inside the 24h window


def test_card_escapes_html(setup):
    page = nt.card_page({"push_id": 'x"</script>', "title": "<b>T</b>", "section": "us", "url": "https://www.nytimes.com/a",
                         "abstract": "a & b", "hearted": 0, "saved": 0})
    assert "<b>T</b>" not in page and "a &amp; b" in page
    assert page.split("<script>")[1].count("</script>") == 1  # the id can't close the script early


def test_unclicked_push_labeled_zero_after_window(setup):
    n, store, sent, db = setup
    n.run_slot(pd.Timestamp("2026-11-12 12:00"))
    con = sqlite3.connect(db)
    con.execute("UPDATE scores SET sent_ts = sent_ts - 25 * 3600 WHERE sent = 1")
    con.commit()
    assert pd.read_sql("SELECT label FROM labeled", con)["label"].tolist() == [0]
