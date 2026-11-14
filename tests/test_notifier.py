import json
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
            con.execute(text("DROP TABLE IF EXISTS scores, readers, interactions, articles, meta, removed, devices"))
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
    friend_get, _ = phone(base)
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            me_post(f"/read/{first}")  # no reader cookie until a push is opened
        assert e.value.code == 403

        pin = me_get(f"/p/{first}")  # my first tap pins it and creates my reader id
        assert 'class="pin"' in pin and "About Story" in pin and 'Your <img class="pinmark"' in pin and "Your first name" in pin
        assert 'href="https://www.nytimes.com/' in pin and f'data-read="{first}"' in pin and "Crème" not in pin
        assert "🔖" not in pin and "data-kind" not in pin and "/saved" not in pin  # no Save any more
        assert "Nothing yet. Tap Read" in pin  # pinning isn't reading
        me_post("/name", b"Justin")
        me_post(f"/read/{first}"), me_post(f"/read/{first}")  # double tap: still one
        assert 'Read <span class="n">1</span>' in me_get("/me") and 'class="pin"' not in me_get("/me")

        friend_get(f"/p/{second}")  # the friend only opens the second push
        assert "Nothing yet. Tap Read" in friend_get(f"/p/{first}")  # my read isn't theirs

        for bad in ("/read/nope", "/save/" + first, "/heart/" + first):
            with pytest.raises(urllib.error.HTTPError) as e:
                me_post(bad)
            assert e.value.code == 404
        with pytest.raises(urllib.error.HTTPError) as e:
            me_get("/saved")
        assert e.value.code == 404
    finally:
        server.shutdown()

    readers = db("SELECT * FROM readers")
    assert len(readers) == 2 and sorted(readers["name"].fillna("")) == ["", "Justin"]
    me = readers.loc[readers["name"] == "Justin", "reader_id"].item()
    friend = readers.loc[readers["name"].isna(), "reader_id"].item()
    assert set(db("SELECT DISTINCT kind FROM interactions")["kind"]) == {"open", "read"}
    labeled = db("SELECT push_id, reader_id, label, read FROM labeled")
    rows = {(r.push_id, r.reader_id): (r.label, r.read) for r in labeled.itertuples()}
    assert rows[(first, me)] == (1, 1)
    assert rows[(first, friend)] == (1, 0)  # the friend's look at the first push opened it
    assert rows[(second, friend)] == (1, 0)
    assert pd.isna(rows[(second, me)][0])  # I haven't opened it, still inside the 24h window


def test_volume_choice_sets_the_daily_cap(setup):
    n, store, _sent, db = setup
    n.cfg["volume_caps"] = {"light": 1, "medium": 2, "heavy": 4}
    assert n.daily_cap() == 2  # nobody has chosen: medium
    server = cards.serve_cards(store, 0, caps=n.cfg["volume_caps"])
    get, post = phone(f"http://127.0.0.1:{server.server_address[1]}")
    try:
        start = get("/start")  # the welcome push's page creates the reader
        assert "How many pushes a day?" in start and 'data-volume="light"' in start and "up to 4" in start
        with pytest.raises(urllib.error.HTTPError) as e:
            post("/volume", b"lots")
        assert e.value.code == 404
        assert 'aria-pressed="true"' not in start and "You can always change it later." in start  # no default
        assert 'location.assign("/me")' in start  # choosing on the welcome page goes on to the profile
        post("/volume", b"light")
        assert 'aria-pressed="true"' not in get("/start")  # the welcome page always asks afresh
        me = get("/me")  # a newcomer's profile: empty, but there
        assert "Nothing yet. Tap Read on a push to start." in me and 'data-volume="light" aria-pressed="true"' in me
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



def test_profile_and_public_history(setup):
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
            post(f"/read/{p}")
        get(f"/p/{pushes[0]}")  # reopening doesn't count twice
        post(f"/read/{pushes[0]}")
        post("/name", b"Justin")
        me = get("/me")
        assert 'Read <span class="n">4</span>' in me and "articles read" not in me and 'Justin’s <img class="pinmark" src="/icon.png" alt="PushPin">' in me
        assert "Crème" not in me and 'class="star"' not in me and "data-fav" not in me  # no Crème any more
        with pytest.raises(urllib.error.HTTPError) as e:
            post(f"/favorite/{re.search(r'data-del="([^"]+)"', me).group(1)}")
        assert e.value.code == 404

        link = re.search(r'href="(http://[^"]+/u/([^"]+))"', me)
        public = stranger_get(f"/u/{link.group(2)}")
        assert 'Justin’s <img class="pinmark"' in public and 'class="readhead"><h2>Read <span class="n">' in public and public.count('class="list"') == 0 and 'id="papier-on"' not in public and "on PushPin" not in public
        assert public.count("<li>") == 0 and "articles read" not in public and "Started reading" not in public and "Crème" not in public
        assert "/p/" not in public  # links go to NYT: a visitor isn't counted as opening the push
        with pytest.raises(urllib.error.HTTPError) as e:
            stranger_post("/public", b"on")  # only the owner (by cookie) can change it
        assert e.value.code == 403

        post("/public", b"on")
        public = stranger_get(f"/u/{link.group(2)}")
        assert "Started reading on " in public and '<div class="stat">Read <span class="n">4</span></div>' in public and 'Read <span class="n">4</span>' in public and public.count("<li data-day=") == 4
        post("/public", b"off")
        private = stranger_get(f"/u/{link.group(2)}")  # just "Read 4": no titles, no dinosaurs
        assert 'Read <span class="n">4</span>' in private and private.count('class="list"') == 0 and 'id="papier-on"' not in private
        with pytest.raises(urllib.error.HTTPError) as e:
            stranger_get("/u/nobody")
        assert e.value.code == 404
    finally:
        server.shutdown()



def test_streak_counts_days_in_a_row():
    from datetime import date
    read = lambda *days: [{"read_at": f"2026-11-{d:02d}T09:00:00"} for d in days]
    today = date(2026, 11, 13)
    assert cards.streak(read(13, 12, 11, 9), today) == 3
    assert cards.streak(read(12, 11), today) == 2  # nothing yet today: still alive
    assert cards.streak(read(11), today) == 0
    assert cards.streak([], today) == 0


def test_public_profile_opens_with_a_summary():
    from datetime import date
    read = [{"news_id": "a", "title": "A", "url": "u", "read_at": "2026-11-13T09:00:00"},
            {"news_id": "b", "title": "B", "url": "u", "read_at": "2026-11-13T10:00:00"}]
    reader = {"name": "Justin", "history_public": 1}
    html = cards.profile_page(reader, read, owner=False, today=date(2026, 11, 13))
    assert 'class="splash"' in html and ('<div class="stat">Read <span class="n">2</span></div>'
                                    '<div class="stat">Daily streak <span class="n">1</span></div></div>') in html
    assert 's.classList.remove("gone")' in html  # a tap on the page brings the card back
    assert "day streak" not in html and 'Daily streak <span class="n">1</span>' in html  # box, then the header
    private = cards.profile_page({**reader, "history_public": 0}, read, owner=False, today=date(2026, 11, 13))
    assert 'class="splash"' in private and 'class="stat"' not in private  # the box keeps a private list's numbers out
    assert '<h2>Read <span class="n">2</span> <span class="streak">(Daily streak <span class="n">1</span>)</span></h2>' in private
    assert ">A</a>" not in private and 'id="papier-on"' not in private  # the header only: no titles, no dinosaurs
    own = cards.profile_page(reader, read, owner=True, today=date(2026, 11, 13))
    assert 'class="splash"' not in own and own.count("Daily streak <span") == 1
    assert '<h2>Read <span class="n">2</span> <span class="streak">(Daily streak <span class="n">1</span>)</span></h2>' in own
    lapsed = cards.profile_page(reader, read, owner=False, today=date(2026, 11, 20))
    assert '<div class="stat">Read <span class="n">2</span></div></div>' in lapsed and "day streak" not in lapsed and "Daily streak <span" not in lapsed  # a 0 streak isn't shown


def test_landing_lists_pinned_articles_not_yet_read(setup):
    n, store, sent, _db = setup
    n.run_slot(pd.Timestamp("2026-11-12 12:00"))
    n.run_slot(pd.Timestamp("2026-11-12 17:00"))
    first, second = (a[4].rsplit("/", 1)[1] for a in sent)
    server = cards.serve_cards(store, 0)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    get, post = phone(base)
    stranger_get, _ = phone(base)
    try:
        assert "<h2>Pinned" not in get(f"/p/{first}")  # the one on top isn't listed again
        landing = get(f"/p/{second}")
        pinned = landing.split("<h2>Pinned")[1].split("</section>")[0]
        assert '<span class="n">1</span>' in pinned and f'data-read="{first}"' in pinned and f'data-read="{second}"' not in pinned
        assert "Tap a headline to read it" in pinned and "Always private" in pinned
        me = get("/me")
        assert me.count('<li><div><a') == 2 and 'Pinned <span class="n">2</span>' in me
        post(f"/read/{first}")  # read: moves from Pinned to Read
        me = get("/me")
        assert 'Pinned <span class="n">1</span>' in me and f'data-read="{second}"' in me and 'Read <span class="n">1</span>' in me
        post("/public", b"on")
        slug = re.search(r"/u/([^\"]+)", me).group(1)
        assert "Pinned" not in stranger_get(f"/u/{slug}")  # pins stay private
        post(f"/read/{second}")
        assert "<h2>Pinned" not in get("/me")
    finally:
        server.shutdown()


def test_reader_can_delete_pinned_and_read_articles(setup):
    n, store, sent, _db = setup
    n.run_slot(pd.Timestamp("2026-11-12 12:00"))
    n.run_slot(pd.Timestamp("2026-11-12 17:00"))
    first, second = (a[4].rsplit("/", 1)[1] for a in sent)
    server = cards.serve_cards(store, 0)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    get, post = phone(base)
    _, stranger_post = phone(base)
    try:
        get(f"/p/{first}"), get(f"/p/{second}")
        news = [store.push(p)["news_id"] for p in (first, second)]
        post(f"/read/{first}")
        me = get("/me")
        assert f'data-del="{news[1]}"' in me and "<h2>Pinned" in me  # the second, pinned and unread
        post(f"/read/{second}")
        me = get("/me")
        assert f'data-del="{news[0]}"' in me and f'data-del="{news[1]}"' in me
        with pytest.raises(urllib.error.HTTPError):
            stranger_post(f"/remove/{news[1]}")  # not theirs
        post(f"/remove/{news[1]}")  # out of Read
        me = get("/me")
        assert "<h2>Pinned" not in me and f'data-del="{news[1]}"' not in me
        post(f"/remove/{news[0]}")  # out of Read, and the count
        me = get("/me")
        assert "Nothing yet. Tap Read" in me and not store.articles_read(store.reader_by_slug(
            re.search(r"/u/([^\"]+)", me).group(1))["reader_id"])
        get(f"/p/{second}")  # reloading the same push doesn't bring it back
        assert "<h2>Pinned" not in get("/me")
        assert store._rows("SELECT COUNT(*) AS n FROM interactions")[0]["n"] == 4  # labels kept for the model
    finally:
        server.shutdown()


def test_profile_volume_is_at_the_bottom_and_medium_by_default(setup):
    n, store, sent, _db = setup
    n.run_slot(pd.Timestamp("2026-11-12 12:00"))
    push_id = sent[0][4].rsplit("/", 1)[1]
    reader = store.new_reader(store.push(push_id)["sent_ts"])
    show = lambda: cards.profile_page(store.reader(reader), [], owner=True, pinned=store.push(push_id, reader))
    for volume in (None, "heavy"):  # before choosing (medium), and after
        if volume:
            store.set_volume(reader, volume)
        page = show()
        assert "How many pushes a day?" not in page and page.count('class="volume"') == 1
        assert f'data-volume="{volume or "medium"}" aria-pressed="true"' in page
        assert page.index('class="pin"') < page.index('class="toggle"') < page.index('class="volume"')  # at the bottom


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


def test_pinned_profile_escapes_html():
    pinned = {"push_id": 'x"</script>', "news_id": "N1", "title": "<b>T</b>", "section": "us",
              "url": "https://www.nytimes.com/a", "abstract": "a & b", "read": 0}
    page = cards.profile_page({"reader_id": "r", "name": "<i>x</i>", "volume": "light"}, [], owner=True,
                              pinned=pinned)
    assert "<b>T</b>" not in page and "a &amp; b" in page and "<i>x</i>" not in page
    assert 'data-read="x&quot;&lt;/script&gt;"' in page  # the id can't close a tag or script


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


def test_pushes_carry_the_icon_without_an_emoji_and_pages_serve_its_icons(setup):
    n, store, sent, _db = setup
    n.run_slot(pd.Timestamp("2026-11-12 12:00"))
    assert not sent[0][5].get("tags") and sent[0][5]["icon"] == "http://mac:8090/icon.png"
    server = cards.serve_cards(store, 0)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        for path in ("/favicon.ico", "/favicon.png", "/apple-touch-icon.png", "/icon.png"):
            with urllib.request.urlopen(base + path) as resp:
                assert resp.headers["content-type"] == "image/png" and resp.read(8) == b"\x89PNG\r\n\x1a\n"
        page = urllib.request.urlopen(base + "/start").read().decode()
        assert 'rel="icon" type="image/png" href="/favicon.png"' in page and 'rel="apple-touch-icon"' in page
    finally:
        server.shutdown()


def test_papier_mache_only_with_a_visible_read_list():
    read = [{"news_id": f"N{i}", "title": f"T{i}</script>", "url": "https://x", "read_at": f"2026-11-1{i}T09:00"}
            for i in range(7)]
    reader = {"name": "Jo", "history_public": 0}
    own = cards.profile_page(reader, read, owner=True)
    assert 'id="papier-on"' in own and "Each day’s pinned pushes make a picture: read them all to finish it · 7 finished" in own
    head = re.search(r'<div class="readhead">.*?</div>', own).group(0)
    assert 'id="papier-on"' in head and 'class="papier-hint" hidden' in own  # switch in the Read header; hint until on
    assert "Tap one to share it" in own and "async function sticker(day, kind, built = 1, past = false)" in own  # a finished one shares as a sticker
    assert "new ClipboardItem({'image/png': b})" in own and "textDino" not in own  # Copy copies the image; no text version
    assert "save.onclick" in own and "(pointer: coarse)" in own  # on a phone, Save goes through the share sheet (Save Image → Photos)
    assert "snowman(c)" in own and "Lightning bolt" in own  # common symbols, not just dinosaurs
    panel = own[own.index('<div class="readpanel">'):]
    assert panel.index('class="list"') < panel.index('id="papier"')  # one panel, no fold
    assert "<details" not in own and 'class="plus"' not in own
    data = re.search(r'id="papier-data"[^>]*>(.*?)</script>', own).group(1)
    assert data.startswith('[{"day": "2026-11-10", "pinned": 1, "read": 1, "titles": ["T0<\\/script>"], "abstracts": [""]}') and "</script>\"" not in data  # oldest first, can't close the tag
    assert 'id="papier-on"' not in cards.profile_page(reader, read, owner=False)  # private list: no dinosaurs
    assert 'id="papier-on"' in cards.profile_page({**reader, "history_public": 1}, read, owner=False)
    assert 'id="papier-on"' not in cards.profile_page(reader, [], owner=True)
    assert "read them all to finish it · 1 finished" in cards.profile_page(reader, read[:1], owner=True)
    half = [{"day": "2026-11-10", "pinned": 2, "read": 1, "titles": ["T0"]}]  # one of two pinned read: not finished
    assert "· 0 finished ·" in cards.profile_page(reader, read[:1], owner=True, days=half)


def test_a_days_picture_counts_what_was_pinned_and_is_finished_when_all_of_it_is_read(setup):
    n, store, sent, _db = setup
    n.cfg["daily_cap"] = 3
    for hour in (9, 12, 17):
        n.run_slot(pd.Timestamp(f"2026-11-12 {hour}:00"))
    first, second, third = (a[4].rsplit("/", 1)[1] for a in sent)
    server = cards.serve_cards(store, 0)
    get, post = phone(f"http://127.0.0.1:{server.server_address[1]}")
    try:
        get(f"/p/{first}")
        post(f"/read/{first}")
        rid = store._rows("SELECT reader_id FROM readers")[0]["reader_id"]
        title, abstract = store.push(first)["title"], store.push(first)["abstract"]
        day = store._rows("SELECT sent_at FROM scores WHERE push_id = :p", p=first)[0]["sent_at"][:10]  # when it was sent
        # the other two were sent but never tapped: they don't count, so 1 of 1 pinned is read
        assert store.days(rid) == [{"day": day, "pinned": 1, "read": 1, "titles": [title], "abstracts": [abstract]}]
        assert "· 1 finished ·" in get("/me")
        get(f"/p/{second}")  # pinned, not read: 1 of 2
        assert [(d["pinned"], d["read"]) for d in store.days(rid)] == [(2, 1)] and "· 0 finished ·" in get("/me")
        post(f"/read/{second}")
        assert [(d["pinned"], d["read"]) for d in store.days(rid)] == [(2, 2)] and "· 1 finished ·" in get("/me")
        get(f"/p/{third}")
        post(f"/remove/{store.push(third)['news_id']}")  # pinned, never read, deleted: it leaves the count
        assert [(d["pinned"], d["read"]) for d in store.days(rid)] == [(2, 2)]
        post(f"/remove/{store.push(second)['news_id']}")  # read, then deleted: it leaves the count too (out of 1)
        assert store.days(rid) == [{"day": day, "pinned": 1, "read": 1, "titles": [title], "abstracts": [abstract]}]
        assert store.days(None) == []
        stray = store.new_reader(None)  # no first push time: what they tapped still counts
        store.interact(first, stray, "open")
        assert [(d["pinned"], d["read"]) for d in store.days(stray)] == [(1, 0)]
    finally:
        server.shutdown()


def test_demo_page_shows_dinosaurs_and_stores_nothing(setup):
    _n, store, _sent, db = setup
    server = cards.serve_cards(store, 0)
    try:
        page = urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/demo").read().decode()
    finally:
        server.shutdown()
    assert 'id="papier-on"' in page and "14 finished" in page and "Justin’s" in page and "Crème" not in page
    data = json.loads(re.search(r'id="papier-data"[^>]*>(.*?)</script>', page).group(1))
    assert len(data) == 18 and {"day": "2026-10-23", "pinned": 2, "read": 0, "titles": [], "abstracts": []} in data  # pinned, nothing read
    assert 'data-today="2026-10-29"' in page  # a past day with nothing read shows its outline; today shows a nudge
    assert all(len(d["abstracts"]) == len(d["titles"]) and all(d["abstracts"]) for d in data)  # titles and abstracts
    assert "function dayText(d)" in page and "const seed = 'pushpin:' + d.day" in page  # seeded by the day, the same for everyone
    assert store.reader_count() == 0 and db("SELECT COUNT(*) AS n FROM interactions")["n"].item() == 0


def test_demo_owner_view_shows_what_the_reader_sees_and_stores_nothing(setup):
    _n, store, _sent, db = setup
    server = cards.serve_cards(store, 0)
    try:
        page = urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/demo/me").read().decode()
    finally:
        server.shutdown()
    assert 'class="pin"' in page and "The Lantern Makers of the River Festival" in page  # today's push, pinned on top
    assert 'id="public"' in page and "<h2>Pinned" in page and 'class="volume"' in page and 'id="papier-on"' in page
    assert 'Pinned <span class="n">5</span>' in page  # the pinned one on top isn't listed again
    assert '/remove/' not in page and '"/public"' not in page and '"/volume"' not in page  # buttons never reach the server
    assert store.reader_count() == 0 and db("SELECT COUNT(*) AS n FROM interactions")["n"].item() == 0


def test_demo_streak_differs_from_the_count():
    page = cards.demo_page()
    assert '<h2>Read <span class="n">63</span> <span class="streak">(Daily streak <span class="n">6</span>)</span></h2>' in page


def test_everyone_gets_the_same_shape_each_day():
    read = [{"news_id": f"N{i}", "title": f"T{i}", "url": "u", "section": None, "read_at": f"2026-11-0{1 + i % 9}"}
            for i in range(10)]
    a = cards.profile_page({"reader_id": "aaaa", "history_public": 0}, read, owner=True)
    b = cards.profile_page({"reader_id": "bbbb", "history_public": 0}, read, owner=True)
    data = re.compile(r'<script type="application/json" id="papier-data"[^>]*>')
    assert data.search(a).group(0) == data.search(b).group(0) and "data-seed" not in a  # nothing per reader
    assert "aaaa" not in a.split('id="papier-data"')[1]  # the reader's id (their cookie) isn't on the page
    assert "const kind = kindFor(d.day)" in a and "rng('pushpin:order')" in a  # the date picks the shape
