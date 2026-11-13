import os
import re
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from sqlalchemy import text

from pushpin import cards
from pushpin import notifier as nt
from pushpin.store import Store
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
    def __init__(self):
        super().__init__()
        self.requests = []

    def score(self, requests, features=False, articles=None):
        self.requests += requests
        if articles is not None:  # inline candidates and past pushes must cover every article
            pushed = {e["impressions"][:-2] for r in requests for e in r.get("events", [])}
            assert set(articles["news_id"]) >= {n for r in requests for n in r["news_ids"]} | pushed
        rows = super().score(requests)
        return rows.assign(sim_text=0.5, user_history_len=7) if features else rows


PG_URL = os.environ.get("PUSHPIN_TEST_DATABASE_URL")  # e.g. postgresql+psycopg://pushpin:pushpin@localhost:5433/pushpin


class DB:
    """Run SQL against the store's database, whichever backend it is."""

    def __init__(self, store):
        self.engine = store.engine

    def __call__(self, sql, **params):
        with self.engine.connect() as con:
            return pd.read_sql(text(sql), con, params=params)

    def run(self, sql, **params):
        with self.engine.begin() as con:
            con.execute(text(sql), params)


def make_store(location):
    if str(location).startswith("postgresql"):  # start from empty tables
        from sqlalchemy import create_engine
        engine = create_engine(location)
        with engine.begin() as con:
            con.execute(text("DROP VIEW IF EXISTS labeled"))
            con.execute(text("DROP TABLE IF EXISTS scores, readers, interactions, articles, meta, favorites"))
        engine.dispose()
    return Store(location, click_window_hours=24, tz=TZ)


@pytest.fixture(params=["sqlite", "postgres"])
def setup(request, tmp_path, monkeypatch):
    if request.param == "postgres" and not PG_URL:
        pytest.skip("set PUSHPIN_TEST_DATABASE_URL to test against Postgres")
    sent = []
    monkeypatch.setattr(nt, "fetch_feeds", lambda urls, tz: nt.parse_rss(rss(ITEMS), tz))
    monkeypatch.setattr(nt, "send_ntfy", lambda *a, **k: sent.append((*a, k)))
    config = {"timezone": "America/New_York", "slot_hours": [8, 12, 17, 21], "lookback_hours": 24,
              "daily_cap": 2, "user_id": None, "seed": 42, "ntfy_server": "https://ntfy.sh", "feeds": []}
    store = make_store(tmp_path / "pushes.db" if request.param == "sqlite" else PG_URL)
    n = nt.Notifier(FeatureScorer(), store, config, OBJECTIVE, "topic", "http://mac:8090")
    yield n, store, sent, DB(store)
    store.engine.dispose()


def test_parse_rss():
    df = nt.parse_rss(rss(ITEMS), TZ)
    assert df["section"].tolist() == ["world", "us", "science", "arts"]
    assert df["published"].iloc[0] == pd.Timestamp("2026-11-12 04:00")  # 09:00 UTC in New York
    assert df["news_id"].str.startswith("NYT-").all() and df["news_id"].is_unique


def test_slot_logs_covariates_and_respects_cap(setup):
    n, _store, sent, db = setup
    assert n.shift == pd.Timestamp("2019-11-15") - n.now().normalize()  # day after the MIND log
    noon = pd.Timestamp("2026-11-12 12:00")
    r = n.run_slot(noon)
    assert r["sent"] and r["candidates"] == 4 and len(sent) == 1
    assert sent[0][4].startswith("http://mac:8090/p/")  # tap opens the push's card

    scores = db("SELECT * FROM scores")
    assert len(scores) == 4 and scores["is_top"].sum() == 1 and scores["sent"].sum() == 1
    assert {"sim_text", "user_history_len", "p_click", "push_score", "subcategory", "url"} <= set(scores.columns)

    # the pushed article isn't a candidate again; cap of 2 per day stops the third push
    assert n.run_slot(noon + pd.Timedelta(hours=5))["sent"]
    third = n.run_slot(noon + pd.Timedelta(hours=9))
    assert not third["sent"] and not third["under_cap"] and len(sent) == 2


def test_past_pushes_go_to_the_scorer_with_tap_labels(setup):
    n, store, sent, _db = setup
    noon = pd.Timestamp("2026-11-12 12:00")
    n.run_slot(noon)
    assert n.scorer.requests[-1]["events"] == []  # nothing pushed yet
    first = sent[0][4].rsplit("/", 1)[1]
    store.interact(first, store.new_reader(store.push(first)["sent_ts"]), "open")
    n.run_slot(noon + pd.Timedelta(hours=5))
    n.run_slot(noon + pd.Timedelta(hours=9))  # over the cap: scored, not sent
    events = n.scorer.requests[-1]["events"]
    pushed = store.push_events(n.user_id)
    assert len(events) == 2 and pushed["tapped"].tolist() == [1, 0]
    assert [e["impressions"] for e in events] == [f"{pushed['news_id'][0]}-1", f"{pushed['news_id'][1]}-0"]
    assert events[0]["time"] == (noon + n.shift).isoformat()  # in the scorer's (shifted) time
    assert events[0]["impression_id"] == int(noon.timestamp())


def test_dry_run_sends_nothing(setup):
    n, _store, sent, _db = setup
    n.dry_run = True
    r = n.run_slot(pd.Timestamp("2026-11-12 12:00"))
    assert r["passes_policy"] and not r["sent"] and sent == []


def test_next_slot(setup):
    n = setup[0]
    assert n.next_slot(pd.Timestamp("2026-11-12 09:30")) == pd.Timestamp("2026-11-12 12:00")
    assert n.next_slot(pd.Timestamp("2026-11-12 21:00")) == pd.Timestamp("2026-11-13 08:00")


def phone(base):
    """A client with its own cookie jar, like one subscriber's phone."""
    import http.cookiejar

    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    get = lambda path: opener.open(base + path).read().decode()
    post = lambda path, body=b"on": opener.open(urllib.request.Request(base + path, data=body, method="POST"))
    return get, post


def test_readers_tracked_separately(setup):
    n, store, sent, db = setup
    n.run_slot(pd.Timestamp("2026-11-12 12:00"))
    n.run_slot(pd.Timestamp("2026-11-12 17:00"))
    first, second = (a[4].rsplit("/", 1)[1] for a in sent)
    server = cards.serve_cards(store, 0)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    me_get, me_post = phone(base)
    friend_get, friend_post = phone(base)
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            me_post(f"/save/{first}")  # no reader cookie until a card is opened
        assert e.value.code == 403

        card = me_get(f"/p/{first}")  # my first tap creates my reader id
        assert "Read on NYT" in card and 'href="https://www.nytimes.com/' in card and "About Story" in card
        assert 'aria-pressed="false">🔖 Save' in card and "Your first name" in card
        assert "Love" not in card and "/read/" not in card  # only opens and saves are recorded
        me_post("/name", b"Justin")
        me_post(f"/save/{first}"), me_post(f"/save/{first}")  # double tap: still one
        assert 'aria-pressed="true">🔖 Saved' in me_get(f"/p/{first}") and "Reading as Justin" in me_get(f"/p/{first}")

        friend_get(f"/p/{second}")  # the friend only opens the second push
        assert 'aria-pressed="false">🔖 Save' in friend_get(f"/p/{first}")  # my save isn't theirs
        friend_post(f"/save/{second}"), friend_post(f"/save/{second}", b"off")  # save, then unsave

        for bad in ("/save/nope", "/heart/" + first, "/read/" + first):
            with pytest.raises(urllib.error.HTTPError) as e:
                me_post(bad)
            assert e.value.code == 404
        mine = me_get("/saved")
        assert mine.count("<li") == 1 and f'href="/p/{first}"' in mine
        assert "Nothing saved" in friend_get("/saved")
    finally:
        server.shutdown()

    readers = db("SELECT * FROM readers")
    assert len(readers) == 2 and sorted(readers["name"].fillna("")) == ["", "Justin"]
    me = readers.loc[readers["name"] == "Justin", "reader_id"].item()
    friend = readers.loc[readers["name"].isna(), "reader_id"].item()
    assert set(db("SELECT DISTINCT kind FROM interactions")["kind"]) == {"open", "save"}
    labeled = db("SELECT push_id, reader_id, label, saved FROM labeled")
    rows = {(r.push_id, r.reader_id): (r.label, r.saved) for r in labeled.itertuples()}
    assert rows[(first, me)] == (1, 1)
    assert rows[(first, friend)] == (1, 0)  # the friend's check of the first card opened it
    assert rows[(second, friend)] == (1, 0)
    assert pd.isna(rows[(second, me)][0])  # I haven't opened it, still inside the 24h window


def test_volume_choice_sets_the_daily_cap(setup):
    n, store, _sent, db = setup
    n.cfg["volume_caps"] = {"light": 1, "medium": 2, "heavy": 4}
    assert n.daily_cap() == 2  # nobody has chosen: the config's daily_cap
    server = cards.serve_cards(store, 0, caps=n.cfg["volume_caps"])
    get, post = phone(f"http://127.0.0.1:{server.server_address[1]}")
    try:
        start = get("/start")  # the welcome push's page creates the reader
        assert "How many pushes a day?" in start and 'data-volume="light"' in start and "up to 4" in start
        with pytest.raises(urllib.error.HTTPError) as e:
            post("/volume", b"lots")
        assert e.value.code == 404
        post("/volume", b"light")
        assert 'data-volume="light" aria-pressed="true"' in get("/start")
    finally:
        server.shutdown()
    assert store.volume() == "light" and n.daily_cap() == 1
    noon = pd.Timestamp("2026-11-12 12:00")
    assert n.run_slot(noon)["sent"]
    assert not n.run_slot(noon + pd.Timedelta(hours=5))["under_cap"]  # light: one a day
    reader = db("SELECT reader_id, first_push_ts FROM readers")
    assert len(reader) == 1 and reader["first_push_ts"].notna().all()  # pushes after /start count for them
    assert db("SELECT reader_volume FROM labeled")["reader_volume"].tolist() == ["light"]
    store.set_volume(reader["reader_id"].item(), "heavy")
    assert n.daily_cap() == 4


def test_store_adds_volume_columns_to_an_older_database(tmp_path):
    from sqlalchemy import create_engine
    path = tmp_path / "old.db"
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as con:
        con.execute(text("CREATE TABLE readers (reader_id TEXT PRIMARY KEY, name TEXT, created_at TEXT, "
                         "first_push_ts BIGINT)"))
        con.execute(text("INSERT INTO readers VALUES ('r1', NULL, '2026-11-12T12:00:00', 0)"))
    engine.dispose()
    store = Store(path, click_window_hours=24, tz=TZ)
    assert store.reader("r1")["volume"] is None and store.set_volume("r1", "heavy") and store.volume() == "heavy"



def test_profile_favorites_and_public_history(setup):
    n, store, sent, _db = setup
    for hour in (8, 12, 17, 21):
        n.cfg["daily_cap"] = 4
        n.run_slot(pd.Timestamp(f"2026-11-12 {hour}:00"))
    pushes = [a[4].rsplit("/", 1)[1] for a in sent]
    assert len(pushes) == 4
    server = cards.serve_cards(store, 0)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    get, post = phone(base)
    stranger_get, stranger_post = phone(base)
    try:
        assert "Open a push first" in get("/me")
        for p in pushes:
            get(f"/p/{p}")
        get(f"/p/{pushes[0]}")  # reopening doesn't count twice
        post("/name", b"Justin")
        me = get("/me")
        assert "<strong>4</strong> articles read" in me and "Justin’s PushPin" in me and me.count('class="star"') == 4
        ids = re.findall(r'data-fav="([^"]+)"', me)
        for i in ids[:3]:
            post(f"/favorite/{i}")
        with pytest.raises(urllib.error.HTTPError) as e:
            post(f"/favorite/{ids[3]}")  # a fourth
        assert e.value.code == 409
        with pytest.raises(urllib.error.HTTPError) as e:
            post("/favorite/NYT-never-read")
        assert e.value.code == 404
        post(f"/favorite/{ids[0]}", b"off")
        post(f"/favorite/{ids[3]}")
        me = get("/me")
        assert me.count('aria-pressed="true"') == 6  # three favorites, each starred on top and in the list

        link = re.search(r'href="(http://[^"]+/u/([^"]+))"', me)
        public = stranger_get(f"/u/{link.group(2)}")
        assert "Justin’s PushPin" in public and "Reading history is private" in public and "on PushPin" not in public
        assert public.count("<li>") == 3 and "articles read" not in public and 'class="star"' not in public
        assert "/p/" not in public  # links go to NYT: a visitor isn't counted as opening the push
        with pytest.raises(urllib.error.HTTPError) as e:
            stranger_post("/public", b"on")  # only the owner (by cookie) can change it
        assert e.value.code == 403

        post("/public", b"on")
        public = stranger_get(f"/u/{link.group(2)}")
        assert "<strong>4</strong> articles read" in public and public.count("<li>") == 3 + 4
        post("/public", b"off")
        assert "Reading history is private" in stranger_get(f"/u/{link.group(2)}")
        with pytest.raises(urllib.error.HTTPError) as e:
            stranger_get("/u/nobody")
        assert e.value.code == 404
    finally:
        server.shutdown()


def test_card_asks_for_volume_until_chosen(setup):
    n, store, sent, _db = setup
    n.run_slot(pd.Timestamp("2026-11-12 12:00"))
    push_id = sent[0][4].rsplit("/", 1)[1]
    reader = store.new_reader(store.push(push_id)["sent_ts"])
    ask = cards.card_page(store.push(push_id, reader), store.reader(reader))
    assert "How many pushes a day?" in ask
    store.set_volume(reader, "medium")
    chosen = cards.card_page(store.push(push_id, reader), store.reader(reader))
    assert "How many pushes a day?" not in chosen and 'data-volume="medium" aria-pressed="true"' in chosen


def test_reader_not_labeled_for_pushes_before_they_joined(setup):
    n, store, sent, db = setup
    n.run_slot(pd.Timestamp("2026-11-12 12:00"))
    n.run_slot(pd.Timestamp("2026-11-12 17:00"))
    first, second = (a[4].rsplit("/", 1)[1] for a in sent)
    db.run("UPDATE scores SET sent_ts = sent_ts - 3600 WHERE push_id = :p", p=first)  # the earlier push
    reader = store.new_reader(store.push(second)["sent_ts"])  # first seen on the later push
    store.interact(second, reader, "open")
    assert db("SELECT push_id FROM labeled")["push_id"].tolist() == [second]


def test_base_url_prefers_env_then_tunnel(setup, monkeypatch):
    n = setup[0]
    assert n.base_url() == "http://mac:8090"
    n.click_base, n.tunnel_metrics = "", "http://tunnel:2000"
    monkeypatch.setattr(nt, "tunnel_url", lambda m: "https://abc.trycloudflare.com")
    assert n.base_url() == "https://abc.trycloudflare.com"
    n.tunnel_metrics = None
    assert n.base_url() is None


def test_tunnel_url_reads_quicktunnel_endpoint():
    import json as _json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = _json.dumps({"hostname": "abc.trycloudflare.com"} if self.path == "/quicktunnel" else {}).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert nt.tunnel_url(f"http://127.0.0.1:{srv.server_address[1]}") == "https://abc.trycloudflare.com"
        assert nt.tunnel_url("http://127.0.0.1:1") is None  # unreachable: no card link, not a crash
    finally:
        srv.shutdown()


def test_card_escapes_html(setup):
    page = cards.card_page({"push_id": 'x"</script>', "title": "<b>T</b>", "section": "us", "url": "https://www.nytimes.com/a",
                         "abstract": "a & b", "hearted": 0, "saved": 0}, {"reader_id": "r", "name": "<i>x</i>"})
    assert "<b>T</b>" not in page and "a &amp; b" in page and "<i>x</i>" not in page
    assert page.split("<script>")[1].count("</script>") == 1  # the id can't close the script early


def test_unclicked_push_labeled_zero_after_window(setup):
    n, store, _sent, db = setup
    n.run_slot(pd.Timestamp("2026-11-12 12:00"))
    store.new_reader(0)  # a subscriber who never opens it
    db.run("UPDATE scores SET sent_ts = sent_ts - 25 * 3600 WHERE sent")
    assert db("SELECT label FROM labeled")["label"].tolist() == [0]


def test_stats_needs_token(setup, monkeypatch):
    n, store, sent, _db = setup
    n.run_slot(pd.Timestamp("2026-11-12 12:00"))
    store.interact(sent[0][4].rsplit("/", 1)[1], store.new_reader(0), "open")
    for token, header, code in ((None, "x", 404), ("s3cret", "wrong", 404), ("s3cret", "s3cret", 200)):
        if token:
            monkeypatch.setenv("CARDS_STATS_TOKEN", token)
        server = cards.serve_cards(store, 0)
        url = f"http://127.0.0.1:{server.server_address[1]}/stats"
        try:
            req = urllib.request.Request(url, headers={"x-api-key": header})
            if code == 200:
                assert urllib.request.urlopen(req).read() == b'{"readers": 1}'
            else:
                with pytest.raises(urllib.error.HTTPError) as e:
                    urllib.request.urlopen(req)
                assert e.value.code == 404
        finally:
            server.shutdown()


def test_scores_for_a_new_user_kept_across_restarts(setup, tmp_path):
    n, store, _, _ = setup
    assert n.user_id.startswith(nt.NEW_USER_PREFIX)
    again = nt.Notifier(FeatureScorer(), store, n.cfg, OBJECTIVE, "topic", None)
    assert again.user_id == n.user_id
    n.run_slot(pd.Timestamp("2026-11-12 12:00"))
    assert set(DB(store)("SELECT DISTINCT user_id FROM scores")["user_id"]) == {n.user_id}


def test_new_user_replaces_a_stored_mind_persona_unless_configured(setup):
    n, store, _, _ = setup
    store.put("user_id", "U123")
    assert nt.Notifier(FeatureScorer(), store, n.cfg, OBJECTIVE, "topic", None).user_id.startswith("new-")
    persona = nt.Notifier(FeatureScorer(), store, {**n.cfg, "user_id": "U9"}, OBJECTIVE, "topic", None)
    assert persona.user_id == "U9"
