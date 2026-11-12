"""Notifier: push today's NYT articles to a real phone, and log every decision and click.

A long-running service, separate from the push job. At each slot hour (configs/notifier.yml) it:
  1. reads the NYT RSS feeds and adds articles it hasn't seen to the scorer's catalog
  2. scores the last `lookback_hours` of unpushed articles for one MIND user (the persona whose
     reading history stands in for yours) through the scorer service, with every model feature
  3. picks the top article with the push objective, gates it with the send policy
     (configs/objective.yml) and, if it passes, sends it through ntfy (https://ntfy.sh) to the
     phone subscribed to NTFY_TOPIC
  4. writes every candidate and its covariates to a SQLite database

It also serves the push's card. Tapping the notification opens <base>/p/<push_id>, a small page
with the headline and abstract and three buttons: Read on NYT (a plain nytimes.com link, so iOS
opens the NYT app if it's installed), ♥ and 🔖 Save. The reading list is at <base>/saved.

Several people can subscribe to the same ntfy topic. ntfy can't tell them apart, so the card
does: the first card a phone opens sets a cookie with a random reader id (and can take a first
name), and every open, read, ♥ and save is stored per reader.

<base> is CLICK_BASE_URL if set, otherwise the public https address of a Cloudflare quick tunnel,
read from its metrics endpoint (TUNNEL_METRICS_URL, the compose `tunnel` service). Without
either, taps open the article directly and nothing is recorded.

Database (outputs/notifier/pushes.db):
  scores        one row per (slot, candidate article): article fields, every model feature,
                p_click / p_return / push_score, is_top, sent, push_id, sent_at (local time in
                the configured zone) and sent_ts (Unix seconds)
  readers       one row per phone that opened a card: reader_id, name, created_at, first_push_ts
  interactions  one row per (push, reader, kind), kind = open, read, heart or save:
                created_at, minutes_after_push. Unloving or unsaving deletes the row.
  labeled       view, one row per (sent push, reader who could have received it): the push's
                scores plus reader_id, reader_name, label (1 = opened, 0 = not opened within
                `click_window_hours`, NULL = pending), read, hearted, saved
  articles      every article read from the feeds, with its mapped MIND subcategory
  meta          persona user_id and the time anchor

Time: the scorer's user state ends with the MIND log (Nov 2019). As in the NYT sim, real time is
shifted so the notifier's first day maps to the day after the log; the shift is stored in `meta`
so it stays the same across restarts.

Needs only the core dependencies plus the standard library, so it runs in the objective image:
  docker compose up -d scorer notifier
  python -m pushnyt.notifier --once --dry-run    # one slot now, don't send
  python -m pushnyt.notifier --test              # send a test notification
"""

import argparse
import hashlib
import html
import json
import os
import secrets
import sqlite3
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import pandas as pd
import yaml

from pushnyt.objective import push_scores, send_decisions
from pushnyt.push_job import HttpScorer

ARTICLE_COLUMNS = ["news_id", "title", "abstract", "section", "url", "published"]
REACTIONS = ("heart", "save")      # toggles on the card
KINDS = ("open", "read", *REACTIONS)  # everything recorded per reader


# --- feeds ---------------------------------------------------------------------------------------

def parse_rss(xml: bytes, tz: ZoneInfo) -> pd.DataFrame:
    """Articles from an RSS 2.0 feed: news_id, title, abstract, section, url, published (local, naive)."""
    out = []
    for item in ET.fromstring(xml).iter("item"):
        text = lambda tag: (item.findtext(tag) or "").strip()
        url, abstract, pub = text("link"), text("description"), text("pubDate")
        if not url or not abstract or not pub:
            continue
        parts = urlparse(url).path.strip("/").split("/")  # /2026/11/12/<section>/...
        section = parts[3] if len(parts) > 4 and parts[0].isdigit() else ""
        guid = text("guid") or url
        out.append({"news_id": "NYT-" + hashlib.sha1(guid.encode()).hexdigest()[:16], "title": text("title"),
                    "abstract": abstract, "section": section, "url": url,
                    "published": parsedate_to_datetime(pub).astimezone(tz).replace(tzinfo=None)})
    return pd.DataFrame(out, columns=ARTICLE_COLUMNS)


def fetch_feeds(urls: list[str], tz: ZoneInfo) -> pd.DataFrame:
    frames = []
    for url in urls:
        req = urllib.request.Request(url, headers={"user-agent": "pushnyt-notifier"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                frames.append(parse_rss(resp.read(), tz))
        except Exception as e:  # one bad feed shouldn't stop the slot
            print(f"feed {url} failed: {e}")
    if not frames:
        return pd.DataFrame(columns=ARTICLE_COLUMNS)
    return pd.concat(frames).drop_duplicates("news_id").sort_values("published", kind="stable").reset_index(drop=True)


# --- delivery ------------------------------------------------------------------------------------

def send_ntfy(server: str, topic: str, title: str, message: str, click: str, tags: list[str] | None = None,
              actions: list[dict] | None = None) -> None:
    """Publish through ntfy's JSON API (body JSON, so non-ASCII titles are fine).

    `actions` are ntfy action buttons. The iOS app doesn't run `http` actions in the background,
    so the notifier puts its buttons on the card page instead."""
    body = {"topic": topic, "title": title, "message": message, "click": click, "tags": tags or [],
            **({"actions": actions} if actions else {})}
    req = urllib.request.Request(server.rstrip("/"), data=json.dumps(body).encode(),
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


# --- storage -------------------------------------------------------------------------------------

class Store:
    """The SQLite database. One connection, guarded by a lock (the click server is threaded)."""

    def __init__(self, path: Path, click_window_hours: float, tz: ZoneInfo):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.click_window_hours, self.tz = click_window_hours, tz
        with self.lock, self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
            self.db.execute("""CREATE TABLE IF NOT EXISTS articles (news_id TEXT PRIMARY KEY, title TEXT,
                abstract TEXT, section TEXT, url TEXT, published TEXT, subcategory TEXT, subcat_sim REAL)""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS readers (reader_id TEXT PRIMARY KEY, name TEXT,
                created_at TEXT, first_push_ts INTEGER)""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS interactions (push_id TEXT, reader_id TEXT, kind TEXT,
                created_at TEXT, minutes_after_push REAL, PRIMARY KEY (push_id, reader_id, kind))""")
            self._labeled_view()

    def _labeled_view(self) -> None:
        if not self._has_table("scores"):
            return
        self.db.execute("DROP VIEW IF EXISTS labeled")
        # Unix seconds, so the cutoff doesn't depend on the container's time zone
        window = int(self.click_window_hours * 3600)
        did = lambda kind: (f"EXISTS (SELECT 1 FROM interactions i WHERE i.push_id = s.push_id "
                            f"AND i.reader_id = r.reader_id AND i.kind = '{kind}')")
        # A reader is first seen when they open a card, so pushes sent before the first one they
        # opened are left out: we can't tell whether they were subscribed yet.
        self.db.execute(f"""CREATE VIEW labeled AS SELECT s.*, r.reader_id, r.name AS reader_name,
            CASE WHEN {did("open")} THEN 1
                 WHEN s.sent_ts <= CAST(strftime('%s', 'now') AS INTEGER) - {window} THEN 0
            END AS label,
            {did("read")} AS read, {did("heart")} AS hearted, {did("save")} AS saved
            FROM scores s JOIN readers r ON s.sent_ts >= r.first_push_ts
            WHERE s.sent = 1""")

    def now(self) -> str:
        """Current local time in the configured zone, as stored in the *_at columns."""
        return datetime.now(self.tz).replace(tzinfo=None).isoformat(timespec="seconds")

    def _has_table(self, name: str) -> bool:
        return self.db.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (name,)).fetchone() is not None

    def get(self, key: str) -> str | None:
        with self.lock:
            row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def put(self, key: str, value: str) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))

    def known_articles(self) -> set[str]:
        with self.lock:
            return {r[0] for r in self.db.execute("SELECT news_id FROM articles")}

    def add_articles(self, articles: pd.DataFrame) -> None:
        rows = articles.assign(published=articles["published"].map(pd.Timestamp.isoformat))
        with self.lock, self.db:
            self.db.executemany("INSERT OR IGNORE INTO articles VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                rows[ARTICLE_COLUMNS + ["subcategory", "subcat_sim"]].itertuples(index=False))

    def articles_since(self, since: datetime) -> pd.DataFrame:
        with self.lock:
            df = pd.read_sql("SELECT * FROM articles WHERE published > ? ORDER BY published",
                             self.db, params=(since.isoformat(),))
        return df.assign(published=pd.to_datetime(df["published"]))

    def pushed_ids(self) -> set[str]:
        if not self._has_table("scores"):
            return set()
        with self.lock:
            return {r[0] for r in self.db.execute("SELECT news_id FROM scores WHERE sent = 1")}

    def sent_on(self, day: str) -> int:
        if not self._has_table("scores"):
            return 0
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM scores WHERE sent = 1 AND substr(sent_at, 1, 10) = ?",
                                   (day,)).fetchone()[0]

    def add_scores(self, scores: pd.DataFrame) -> None:
        new = not self._has_table("scores")
        with self.lock, self.db:
            existing = {r[1] for r in self.db.execute("PRAGMA table_info(scores)")} if not new else set()
            for col in scores.columns:  # a retrained model can add features: widen the table
                if existing and col not in existing:
                    self.db.execute(f'ALTER TABLE scores ADD COLUMN "{col}"')
            scores.to_sql("scores", self.db, if_exists="append", index=False)
            if new:
                self.db.execute("CREATE INDEX IF NOT EXISTS scores_push ON scores (push_id)")
            if new:
                self._labeled_view()

    def push(self, push_id: str, reader_id: str | None = None) -> dict | None:
        """A sent push with its article, plus this reader's ♥ / save state (None if unknown)."""
        if not self._has_table("scores"):
            return None
        with self.lock:
            cur = self.db.execute("""SELECT s.push_id, s.news_id, s.title, s.section, s.url, s.sent_ts, a.abstract,
                    EXISTS (SELECT 1 FROM interactions i WHERE i.push_id = s.push_id AND i.reader_id = ?
                            AND i.kind = 'heart') AS hearted,
                    EXISTS (SELECT 1 FROM interactions i WHERE i.push_id = s.push_id AND i.reader_id = ?
                            AND i.kind = 'save') AS saved
                FROM scores s LEFT JOIN articles a USING (news_id) WHERE s.push_id = ? AND s.sent = 1""",
                                  (reader_id, reader_id, push_id))
            row = cur.fetchone()
            return dict(zip([d[0] for d in cur.description], row)) if row else None

    def reader(self, reader_id: str | None) -> dict | None:
        if not reader_id:
            return None
        with self.lock:
            row = self.db.execute("SELECT reader_id, name FROM readers WHERE reader_id = ?", (reader_id,)).fetchone()
        return {"reader_id": row[0], "name": row[1]} if row else None

    def new_reader(self, first_push_ts: int | None) -> str:
        reader_id = secrets.token_hex(8)
        with self.lock, self.db:
            self.db.execute("INSERT INTO readers VALUES (?, NULL, ?, ?)", (reader_id, self.now(), first_push_ts))
        return reader_id

    def set_name(self, reader_id: str, name: str) -> bool:
        name = " ".join(name.split())[:40] or None
        with self.lock, self.db:
            return self.db.execute("UPDATE readers SET name = ? WHERE reader_id = ?", (name, reader_id)).rowcount > 0

    def interact(self, push_id: str, reader_id: str, kind: str, on: bool = True) -> bool:
        """Record (or, for ♥ / save with on=False, clear) one reader's interaction with a sent push.
        Only the first open and first read are kept. False if the push, reader or kind is unknown."""
        push = self.push(push_id)
        if push is None or kind not in KINDS or self.reader(reader_id) is None:
            return False
        minutes = (time.time() - push["sent_ts"]) / 60 if push["sent_ts"] is not None else None
        with self.lock, self.db:
            if on:
                self.db.execute("INSERT OR IGNORE INTO interactions VALUES (?, ?, ?, ?, ?)",
                                (push_id, reader_id, kind, self.now(), minutes))
            elif kind in REACTIONS:
                self.db.execute("DELETE FROM interactions WHERE push_id = ? AND reader_id = ? AND kind = ?",
                                (push_id, reader_id, kind))
        return True

    def reading_list(self, reader_id: str | None) -> list[dict]:
        """This reader's saved pushes, newest first, with whether they've read each on NYT since."""
        if not reader_id or not self._has_table("scores"):
            return []
        with self.lock:
            rows = self.db.execute("""SELECT i.push_id, s.title, s.section, i.created_at,
                    EXISTS (SELECT 1 FROM interactions r WHERE r.push_id = i.push_id
                            AND r.reader_id = i.reader_id AND r.kind = 'read')
                FROM interactions i JOIN scores s ON s.push_id = i.push_id AND s.sent = 1
                WHERE i.kind = 'save' AND i.reader_id = ? ORDER BY i.created_at DESC""", (reader_id,)).fetchall()
        return [dict(zip(["push_id", "title", "section", "saved_at", "read"], r)) for r in rows]


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>pushNYT</title>
<style>
:root{--bg:#fff;--fg:#121212;--muted:#666;--line:#e2e2e2;--btn:#f1f1f1;--on:#d0021b;--link:#326891}
@media (prefers-color-scheme:dark){:root{--bg:#121212;--fg:#eee;--muted:#999;--line:#333;--btn:#262626;
--on:#ff5a6e;--link:#8ab4f8}}
body{font:17px/1.45 -apple-system,system-ui,sans-serif;margin:0;padding:24px 16px;background:var(--bg);color:var(--fg)}
main{max-width:600px;margin:0 auto}
.kicker{font-size:13px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
h1{font:700 26px/1.2 Georgia,"Times New Roman",serif;margin:6px 0 12px}
.abstract{color:var(--muted);margin:0 0 24px}
.read{display:block;text-align:center;background:var(--fg);color:var(--bg);text-decoration:none;
font-weight:600;padding:14px;border-radius:10px}
.row{display:flex;gap:10px;margin-top:10px}
.row button{flex:1;font:inherit;font-weight:600;padding:12px;border:0;border-radius:10px;
background:var(--btn);color:var(--fg)}
.row button[aria-pressed=true]{color:var(--on)}
.foot{margin-top:28px;font-size:15px;color:var(--muted)}.foot a,.list a{color:var(--link);text-decoration:none}
.who{display:flex;gap:8px;margin-top:8px}.who input{flex:1;font:inherit;padding:8px 10px;border:1px solid var(--line);
border-radius:8px;background:var(--bg);color:var(--fg)}.who button{font:inherit;padding:8px 12px;border:0;
border-radius:8px;background:var(--btn);color:var(--fg)}
.list{padding:0;margin:0}.list li{list-style:none;padding:12px 0;border-bottom:1px solid var(--line)}
.list .meta{font-size:13px;color:var(--muted);margin-top:2px}.list .done a{color:var(--muted)}
</style></head><body><main>%BODY%</main></body></html>"""

CARD_SCRIPT = """<script>
const id = %ID%;
document.querySelectorAll("button[data-kind]").forEach(b => b.addEventListener("click", async () => {
  const on = b.getAttribute("aria-pressed") !== "true";
  b.setAttribute("aria-pressed", on);
  b.textContent = b.dataset[on ? "on" : "off"];
  const r = await fetch(`/${b.dataset.kind}/${id}`, {method: "POST", body: on ? "on" : "off"}).catch(() => null);
  if (!r || !r.ok) { b.setAttribute("aria-pressed", !on); b.textContent = b.dataset[on ? "off" : "on"]; }
}));
document.querySelector(".read").addEventListener("click", () => navigator.sendBeacon(`/read/${id}`));
const who = document.querySelector(".who");
if (who) who.addEventListener("submit", async ev => {
  ev.preventDefault();
  const name = who.querySelector("input").value.trim();
  if (name && (await fetch("/name", {method: "POST", body: name}).catch(() => null))?.ok)
    who.outerHTML = `<span>Thanks, ${name.replace(/[<>&"]/g, "")}.</span>`;
});
</script>"""


def page(body: str) -> str:
    return PAGE.replace("%BODY%", body)


def card_page(push: dict, reader: dict | None = None) -> str:
    """The landing card a notification tap opens: Read on NYT, ♥, Save, and who's reading."""
    e = html.escape

    def toggle(kind, on, off, pressed):
        return (f'<button data-kind="{kind}" data-on="{e(on)}" data-off="{e(off)}" '
                f'aria-pressed="{"true" if pressed else "false"}">{e(on if pressed else off)}</button>')

    if reader and reader.get("name"):
        who = f'Reading as {e(reader["name"])} · <a href="/saved">Reading list →</a>'
    else:
        who = ('<a href="/saved">Reading list →</a><form class="who"><input name="name" maxlength="40" '
               'placeholder="Your first name (optional)" autocomplete="given-name"><button>Save</button></form>')
    # a plain link to nytimes.com (not a redirect) lets iOS open it in the NYT app when installed
    return page(f"""<div class="kicker">{e(push["section"] or "")}</div>
<h1>{e(push["title"])}</h1>
<p class="abstract">{e(push["abstract"] or "")}</p>
<a class="read" href="{e(push["url"])}">Read on NYT</a>
<div class="row">{toggle("heart", "♥ Loved", "♡ Love", push["hearted"])}{toggle("save", "🔖 Saved", "🔖 Save", push["saved"])}</div>
<div class="foot">{who}</div>
{CARD_SCRIPT.replace("%ID%", json.dumps(push["push_id"]).replace("<", "\\u003c"))}""")


def reading_list_page(items: list[dict]) -> str:
    if not items:
        return page("<h1>Reading list</h1><p>Nothing saved yet. Tap 🔖 Save on a push.</p>")
    e = html.escape
    rows = "".join(
        f'<li class="{"done" if it["read"] else ""}"><a href="/p/{e(it["push_id"])}">{e(it["title"])}</a>'
        f'<div class="meta">{e(it["section"] or "")} · saved {e(it["saved_at"][:16].replace("T", " "))}'
        f'{" · read" if it["read"] else ""}</div></li>' for it in items)
    return page(f'<h1>Reading list</h1><ul class="list">{rows}</ul>')


COOKIE = "pushnyt_reader"


def serve_clicks(store: Store, port: int) -> ThreadingHTTPServer:
    """The phone-facing endpoints. The reader is identified by the `pushnyt_reader` cookie.

      GET  /p/<push_id>        the push's card; records the open (and creates the reader on first visit)
      POST /read/<push_id>     'Read on NYT' was tapped (sent by the card as a beacon)
      POST /heart/<push_id>    body "on" or "off": set or clear the ♥
      POST /save/<push_id>     body "on" or "off": save for later, or unsave
      POST /name               body: the reader's first name
      GET  /saved              this reader's reading list; its links open the cards
      GET  /health             liveness
    """

    class Handler(BaseHTTPRequestHandler):
        def _reader_id(self) -> str | None:
            jar = SimpleCookie(self.headers.get("cookie") or "")
            rid = jar[COOKIE].value if COOKIE in jar else None
            return rid if store.reader(rid) else None

        def _send(self, status: int, body: str = "", content_type: str = "text/html; charset=utf-8",
                  set_reader: str | None = None) -> None:
            data = body.encode()
            self.send_response(status)
            self.send_header("content-type", content_type)
            self.send_header("content-length", str(len(data)))
            self.send_header("cache-control", "no-store")
            if set_reader:
                secure = "; Secure" if self.headers.get("x-forwarded-proto") == "https" else ""
                self.send_header("set-cookie", f"{COOKIE}={set_reader}; Max-Age=63072000; Path=/; "
                                               f"HttpOnly; SameSite=Lax{secure}")
            self.end_headers()
            self.wfile.write(data)

        def _route(self) -> tuple[str, str]:
            route, _, push_id = self.path.split("?")[0].lstrip("/").partition("/")
            return route, push_id

        def do_POST(self):
            route, push_id = self._route()
            body = self.rfile.read(min(int(self.headers.get("content-length") or 0), 200)).decode(errors="replace")
            reader_id = self._reader_id()
            if reader_id is None:
                self._send(403, "open a push first", "text/plain")
                return
            if route == "name":
                ok = store.set_name(reader_id, body)
            elif route == "read" or route in REACTIONS:
                ok = store.interact(push_id, reader_id, route, on=body.strip() != "off")
            else:
                ok = False
            if not ok:
                self._send(404, "unknown push", "text/plain")
                return
            print(json.dumps({"event": route, "push_id": push_id or None, "reader": reader_id,
                              **({"on": body.strip() != "off"} if route in REACTIONS else {})}))
            self._send(200, "ok", "text/plain")

        def do_GET(self):
            route, push_id = self._route()
            if route == "health":
                self._send(200, "ok", "text/plain")
            elif route == "saved":
                self._send(200, reading_list_page(store.reading_list(self._reader_id())))
            elif route == "p" and (push := store.push(push_id)) is not None:
                reader_id, new = self._reader_id(), None
                if reader_id is None:
                    reader_id = new = store.new_reader(push["sent_ts"])
                store.interact(push_id, reader_id, "open")
                print(json.dumps({"event": "open", "push_id": push_id, "reader": reader_id, "new_reader": bool(new)}))
                self._send(200, card_page(store.push(push_id, reader_id), store.reader(reader_id)), set_reader=new)
            else:
                self._send(404, page("<p>Unknown push.</p>"))

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def tunnel_url(metrics_url: str) -> str | None:
    """The public https address of a Cloudflare quick tunnel, from its metrics endpoint."""
    try:
        with urllib.request.urlopen(metrics_url.rstrip("/") + "/quicktunnel", timeout=5) as resp:
            host = json.loads(resp.read()).get("hostname")
        return f"https://{host}" if host else None
    except Exception as e:
        print(f"tunnel address unavailable: {e!r}")
        return None


# --- the slot ------------------------------------------------------------------------------------

class Notifier:
    def __init__(self, scorer, store: Store, config: dict, objective: dict, topic: str | None,
                 click_base: str | None, dry_run: bool = False, tunnel_metrics: str | None = None):
        self.scorer, self.store, self.cfg, self.objective = scorer, store, config, objective
        self.topic, self.click_base, self.dry_run = topic, (click_base or "").rstrip("/"), dry_run
        self.tunnel_metrics = tunnel_metrics
        self.tz = ZoneInfo(config["timezone"])
        self.in_scorer: set[str] = set()  # articles in the scorer's catalog (lost if it restarts)
        self.scorer_size = None
        info = scorer.info()
        if store.get("anchor") is None:  # first run: map today to the day after the MIND log
            store.put("anchor", self.now().normalize().isoformat())
            store.put("log_end", info["log_end"])
        log_end = pd.Timestamp(store.get("log_end"))
        self.shift = log_end.normalize() + pd.Timedelta(days=1) - pd.Timestamp(store.get("anchor"))
        self.user_id = config.get("user_id") or store.get("user_id") or scorer.users(1, config["seed"])[0]
        store.put("user_id", self.user_id)

    def now(self) -> pd.Timestamp:
        return pd.Timestamp(datetime.now(self.tz).replace(tzinfo=None))

    def base_url(self) -> str | None:
        """Where phones reach the card: CLICK_BASE_URL, else the quick tunnel's current address
        (looked up per push, since a quick tunnel gets a new address whenever it restarts)."""
        if self.click_base:
            return self.click_base
        return tunnel_url(self.tunnel_metrics) if self.tunnel_metrics else None

    def sync_articles(self, now: pd.Timestamp) -> None:
        """Read the feeds; make sure every recent article is in the scorer's catalog."""
        known = self.store.known_articles()
        fresh = fetch_feeds(self.cfg["feeds"], self.tz)
        fresh = fresh[~fresh["news_id"].isin(known) & (fresh["published"] <= now)]
        if self.scorer_size is not None and self.scorer.info()["articles"] != self.scorer_size:
            print("scorer restarted: re-adding recent articles")
            self.in_scorer = set()
        since = now - pd.Timedelta(hours=self.cfg["lookback_hours"])
        recent = pd.concat([self.store.articles_since(since)[ARTICLE_COLUMNS], fresh[fresh["published"] > since]])
        add = recent[~recent["news_id"].isin(self.in_scorer)].sort_values("published", kind="stable")
        if len(add):
            mapped = self.scorer.add_articles(add[["news_id", "title", "abstract"]]
                                              .assign(published=add["published"] + self.shift))
            self.in_scorer |= set(add["news_id"])
            new = fresh.merge(mapped, on="news_id")
            if len(new):
                self.store.add_articles(new)
        self.scorer_size = self.scorer.info()["articles"]
        print(f"{len(fresh)} new articles from the feeds, {len(add)} added to the scorer")

    def run_slot(self, now: pd.Timestamp | None = None) -> dict:
        now = now or self.now()
        self.sync_articles(now)
        since = now - pd.Timedelta(hours=self.cfg["lookback_hours"])
        cand = self.store.articles_since(since)
        cand = cand[~cand["news_id"].isin(self.store.pushed_ids()) & cand["news_id"].isin(self.in_scorer)]
        if cand.empty:
            print(f"{now:%a %H:%M}: no new candidates")
            return {"candidates": 0, "sent": False}

        slot_id = int(now.timestamp())
        request = {"user_id": self.user_id, "time": (now + self.shift).isoformat(),
                   "news_ids": cand["news_id"].tolist(), "impression_id": slot_id}
        scores = self.scorer.score([request], features=True)
        scores["push_score"] = push_scores(scores, scores["p_click"].to_numpy(), self.objective["friction"])
        top = scores["push_score"].idxmax()

        send = self.objective["send"]
        cap = self.cfg.get("daily_cap", send["daily_cap"])
        pick = scores.loc[[top]].assign(time=now)
        passes = bool(send_decisions(pick, send["threshold"], send["risk_penalty"], cap)[0])
        under_cap = self.store.sent_on(now.strftime("%Y-%m-%d")) < cap
        article = cand.set_index("news_id").loc[scores.at[top, "news_id"]]
        push_id = hashlib.sha1(f"{slot_id}{article.name}".encode()).hexdigest()[:12]

        sent = passes and under_cap and not self.dry_run
        if sent:
            # the tap opens the push's card (Read on NYT, ♥, Save); without a tracker, the article itself
            base = self.base_url()
            click = f"{base}/p/{push_id}" if base else article["url"]
            send_ntfy(self.cfg["ntfy_server"], self.topic, article["title"], article["abstract"], click,
                      tags=["newspaper"])

        out = scores.drop(columns=["impression_id"]).merge(
            cand[["news_id", "title", "section", "subcategory", "subcat_sim", "url", "published"]], on="news_id")
        out.insert(0, "slot_time", now.isoformat(timespec="seconds"))
        out.insert(1, "slot_id", slot_id)
        out["published"] = out["published"].map(pd.Timestamp.isoformat)
        out["is_top"] = out["news_id"] == article.name
        out["passes_policy"] = out["is_top"] & passes
        out["sent"] = out["is_top"] & sent
        out["dry_run"] = self.dry_run
        out["push_id"] = out["is_top"].map({True: push_id, False: None})
        out["sent_at"] = out["sent"].map({True: self.store.now(), False: None})
        out["sent_ts"] = out["sent"].map({True: int(time.time()), False: None})
        self.store.add_scores(out)

        result = {"slot": now.isoformat(timespec="minutes"), "candidates": len(cand), "title": article["title"],
                  "p_click": float(scores.at[top, "p_click"]), "passes_policy": passes, "under_cap": under_cap,
                  "sent": sent}
        print(json.dumps(result))
        return result

    def next_slot(self, now: pd.Timestamp) -> pd.Timestamp:
        hours = sorted(self.cfg["slot_hours"])
        today = [now.normalize() + pd.Timedelta(hours=h) for h in hours]
        later = [t for t in today if t > now]
        return later[0] if later else now.normalize() + pd.Timedelta(days=1, hours=hours[0])

    def run_forever(self) -> None:
        while True:
            now = self.now()
            slot = self.next_slot(now)
            print(f"next slot {slot:%a %b %d %H:%M} ({self.cfg['timezone']})")
            while (wait := (slot - self.now()).total_seconds()) > 0:
                time.sleep(min(wait, 300))  # wake periodically so laptop sleep doesn't skew the wait
            try:
                self.run_slot(slot)
            except Exception as e:  # a failed slot (scorer down, feed error) shouldn't kill the service
                print(f"slot {slot} failed: {e!r}")


def wait_for(scorer: HttpScorer, timeout: float = 900) -> None:
    start = time.time()
    while True:
        try:
            print(f"scorer ready: {scorer.info()}")
            return
        except Exception:
            if time.time() - start > timeout:
                raise
            time.sleep(10)


def main() -> None:
    parser = argparse.ArgumentParser(description="Push NYT articles to your phone and log clicks")
    parser.add_argument("--config", type=Path, default=Path("configs/notifier.yml"))
    parser.add_argument("--scorer", default=os.environ.get("PUSHNYT_SCORER_URL", "http://scorer:8000"))
    parser.add_argument("--once", action="store_true", help="run one slot now and exit")
    parser.add_argument("--dry-run", action="store_true", help="score and log, but don't send")
    parser.add_argument("--test", action="store_true", help="send a test notification and exit")
    args = parser.parse_args()

    config = yaml.safe_load(args.config.read_text())
    objective = yaml.safe_load(Path(config["objective"]).read_text())
    topic = os.environ.get("NTFY_TOPIC")
    if not topic and not args.dry_run:
        raise SystemExit("set NTFY_TOPIC (in .env) to the topic your phone subscribes to, or use --dry-run")
    if args.test:
        send_ntfy(config["ntfy_server"], topic, "pushNYT test", "If you can read this, delivery works.",
                  "https://www.nytimes.com", tags=["white_check_mark"])
        print("sent")
        return

    store = Store(Path(config["db"]), config["click_window_hours"], ZoneInfo(config["timezone"]))
    scorer = HttpScorer(args.scorer)
    wait_for(scorer)
    notifier = Notifier(scorer, store, config, objective, topic, os.environ.get("CLICK_BASE_URL"), args.dry_run,
                        tunnel_metrics=os.environ.get("TUNNEL_METRICS_URL"))
    print(f"persona {notifier.user_id}, scorer time shift {notifier.shift}")
    if args.once:
        notifier.run_slot()
        return
    serve_clicks(store, int(os.environ.get("PORT", config["click_port"])))
    notifier.run_forever()


if __name__ == "__main__":
    main()
