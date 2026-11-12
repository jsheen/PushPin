"""Notifier: push today's NYT articles to a real phone, and log every decision and click.

A long-running service, separate from the push job. At each slot hour (configs/notifier.yml) it:
  1. reads the NYT RSS feeds and adds articles it hasn't seen to the scorer's catalog
  2. scores the last `lookback_hours` of unpushed articles for one MIND user (the persona whose
     reading history stands in for yours) through the scorer service, with every model feature
  3. picks the top article with the push objective, gates it with the send policy
     (configs/objective.yml) and, if it passes, sends it through ntfy (https://ntfy.sh) to the
     phone subscribed to NTFY_TOPIC
  4. writes every candidate and its covariates to a SQLite database

It also serves a click tracker. The notification's tap action opens CLICK_BASE_URL/c/<push_id>,
which records the click and redirects to the article. The notification also has three buttons:
♥ (a favorite), 🔖 Save (for later) and 📚 Reading list (CLICK_BASE_URL/saved). Without
CLICK_BASE_URL, taps open the article directly, there are no buttons, and nothing is recorded.

Database (outputs/notifier/pushes.db):
  scores    one row per (slot, candidate article): article fields, every model feature,
            p_click / p_return / push_score, is_top, sent, push_id, sent_at, clicked_at
            (local times in the configured time zone) and sent_ts (Unix seconds)
  labeled   view over the sent pushes: label = 1 if clicked, 0 if not clicked within
            `click_window_hours`, NULL while still pending; hearted, saved (0/1)
  reactions one row per (push, kind), kind = heart or save: news_id, user_id, created_at,
            minutes_after_push
  articles  every article read from the feeds, with its mapped MIND subcategory
  meta      persona user_id and the time anchor

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
import sqlite3
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import pandas as pd
import yaml

from pushnyt.objective import push_scores, send_decisions
from pushnyt.push_job import HttpScorer

ARTICLE_COLUMNS = ["news_id", "title", "abstract", "section", "url", "published"]
REACTIONS = ("heart", "save")


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

    `actions` are ntfy action buttons, e.g. {"action": "view", "label": "Comment", "url": ...}."""
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
            self.db.execute("""CREATE TABLE IF NOT EXISTS reactions (push_id TEXT, kind TEXT, news_id TEXT,
                user_id TEXT, created_at TEXT, minutes_after_push REAL, PRIMARY KEY (push_id, kind))""")
            self._labeled_view()

    def _labeled_view(self) -> None:
        if not self._has_table("scores"):
            return
        self.db.execute("DROP VIEW IF EXISTS labeled")
        # Unix seconds, so the cutoff doesn't depend on the container's time zone
        window = int(self.click_window_hours * 3600)
        self.db.execute(f"""CREATE VIEW labeled AS SELECT *,
            CASE WHEN clicked_at IS NOT NULL THEN 1
                 WHEN sent_ts <= CAST(strftime('%s', 'now') AS INTEGER) - {window} THEN 0
            END AS label,
            EXISTS (SELECT 1 FROM reactions r WHERE r.push_id = scores.push_id AND r.kind = 'heart') AS hearted,
            EXISTS (SELECT 1 FROM reactions r WHERE r.push_id = scores.push_id AND r.kind = 'save') AS saved
            FROM scores WHERE sent = 1""")

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

    def click(self, push_id: str) -> str | None:
        """Record the first click on a push; returns the article URL (None if unknown)."""
        if not self._has_table("scores"):
            return None
        with self.lock, self.db:
            row = self.db.execute("SELECT url FROM scores WHERE push_id = ?", (push_id,)).fetchone()
            if row:
                self.db.execute("UPDATE scores SET clicked_at = ? WHERE push_id = ? AND clicked_at IS NULL",
                                (self.now(), push_id))
        return row[0] if row else None

    def push(self, push_id: str) -> dict | None:
        """The sent push with this id: news_id, user_id, title, url, sent_ts (None if unknown)."""
        if not self._has_table("scores"):
            return None
        with self.lock:
            row = self.db.execute("SELECT news_id, user_id, title, url, sent_ts FROM scores WHERE push_id = ?",
                                  (push_id,)).fetchone()
        return dict(zip(["news_id", "user_id", "title", "url", "sent_ts"], row)) if row else None

    def react(self, push_id: str, kind: str) -> bool:
        """Record a ♥ or a save on a sent push (once per kind); False if the push is unknown."""
        push = self.push(push_id)
        if push is None or kind not in REACTIONS:
            return False
        minutes = (time.time() - push["sent_ts"]) / 60 if push["sent_ts"] is not None else None
        with self.lock, self.db:
            self.db.execute("INSERT OR IGNORE INTO reactions VALUES (?, ?, ?, ?, ?, ?)",
                            (push_id, kind, push["news_id"], push["user_id"], self.now(), minutes))
        return True

    def reading_list(self) -> list[dict]:
        """Saved pushes, newest first, with whether each has been opened since."""
        if not self._has_table("scores"):
            return []
        with self.lock:
            rows = self.db.execute("""SELECT r.push_id, s.title, s.section, r.created_at, s.clicked_at IS NOT NULL
                FROM reactions r JOIN scores s USING (push_id) WHERE r.kind = 'save'
                ORDER BY r.created_at DESC""").fetchall()
        return [dict(zip(["push_id", "title", "section", "saved_at", "opened"], r)) for r in rows]


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>pushNYT</title>
<style>body{{font:17px -apple-system,system-ui,sans-serif;margin:0;padding:20px;max-width:640px;
background:#fff;color:#111}}h1{{font:600 22px Georgia,serif;margin:0 0 16px}}
a{{color:#111;text-decoration:none}}li{{list-style:none;padding:12px 0;border-bottom:1px solid #ddd}}
ul{{padding:0;margin:0}}.meta{{font-size:13px;color:#666;margin-top:4px}}.opened a{{color:#888}}
@media (prefers-color-scheme:dark){{body{{background:#121212;color:#eee}}a{{color:#eee}}
li{{border-color:#333}}.meta{{color:#999}}.opened a{{color:#777}}}}
</style></head><body>{body}</body></html>"""


def reading_list_page(items: list[dict]) -> str:
    if not items:
        return PAGE.format(body="<h1>Reading list</h1><p>Nothing saved yet. Tap 🔖 Save on a push.</p>")
    rows = "".join(
        f'<li class="{"opened" if it["opened"] else ""}"><a href="/c/{html.escape(it["push_id"])}">'
        f'{html.escape(it["title"])}</a><div class="meta">{html.escape(it["section"] or "")} · saved '
        f'{html.escape(it["saved_at"][:16].replace("T", " "))}{" · read" if it["opened"] else ""}</div></li>'
        for it in items)
    return PAGE.format(body=f"<h1>Reading list</h1><ul>{rows}</ul>")


def serve_clicks(store: Store, port: int) -> ThreadingHTTPServer:
    """The phone-facing endpoints.

      GET  /c/<push_id>             record the click, redirect to the article
      POST /heart/<push_id>         record a ♥ (ntfy http action, runs in the background)
      POST /save/<push_id>          save for later
      GET  /heart|save/<push_id>    the same, for `view` actions: records it and shows a confirmation
      GET  /saved                   reading list; its links go through /c/ so opening counts as a click
      GET  /health                  liveness
    """

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: str = "", content_type: str = "text/html; charset=utf-8") -> None:
            data = body.encode()
            self.send_response(status)
            self.send_header("content-type", content_type)
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _reaction(self) -> tuple[str, str] | None:
            kind, _, push_id = self.path.lstrip("/").partition("/")
            return (kind, push_id) if kind in REACTIONS and push_id else None

        def do_POST(self):
            r = self._reaction()
            if r is None or not store.react(r[1], r[0]):
                self._send(404, "unknown push", "text/plain")
                return
            print(json.dumps({"event": r[0], "push_id": r[1]}))
            self._send(200, "ok", "text/plain")

        def do_GET(self):
            if self.path == "/health":
                self._send(200, "ok", "text/plain")
                return
            if self.path == "/saved":
                self._send(200, reading_list_page(store.reading_list()))
                return
            if (r := self._reaction()) is not None:
                if not store.react(r[1], r[0]):
                    self._send(404, PAGE.format(body="<p>Unknown push.</p>"))
                    return
                print(json.dumps({"event": r[0], "push_id": r[1]}))
                done = "♥ Loved" if r[0] == "heart" else '🔖 Saved. <a href="/saved">Reading list</a>'
                self._send(200, PAGE.format(body=f"<h1>{done}</h1>"))
                return
            url = store.click(self.path[3:]) if self.path.startswith("/c/") else None
            if url is None:
                self._send(404, "not found", "text/plain")
                return
            print(json.dumps({"event": "click", "push_id": self.path[3:]}))
            self.send_response(302)
            self.send_header("location", url)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# --- the slot ------------------------------------------------------------------------------------

class Notifier:
    def __init__(self, scorer, store: Store, config: dict, objective: dict, topic: str | None,
                 click_base: str | None, dry_run: bool = False):
        self.scorer, self.store, self.cfg, self.objective = scorer, store, config, objective
        self.topic, self.click_base, self.dry_run = topic, (click_base or "").rstrip("/"), dry_run
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
            click = f"{self.click_base}/c/{push_id}" if self.click_base else article["url"]
            actions = self.actions(push_id) if self.click_base else None
            send_ntfy(self.cfg["ntfy_server"], self.topic, article["title"], article["abstract"], click,
                      tags=["newspaper"], actions=actions)

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
        out["clicked_at"] = None
        self.store.add_scores(out)

        result = {"slot": now.isoformat(timespec="minutes"), "candidates": len(cand), "title": article["title"],
                  "p_click": float(scores.at[top, "p_click"]), "passes_policy": passes, "under_cap": under_cap,
                  "sent": sent}
        print(json.dumps(result))
        return result

    def actions(self, push_id: str) -> list[dict]:
        """ntfy buttons. `http` buttons call the tracker in the background; `view` buttons open a
        confirmation page instead (for clients without http actions)."""
        mode, base = self.cfg.get("button_action", "http"), self.click_base
        def button(label, kind, clear):
            b = {"action": mode, "label": label, "url": f"{base}/{kind}/{push_id}"}
            return {**b, "method": "POST", "clear": clear} if mode == "http" else b
        return [button("♥", "heart", False), button("🔖 Save", "save", True),
                {"action": "view", "label": "📚 Reading list", "url": f"{base}/saved"}]

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
    notifier = Notifier(scorer, store, config, objective, topic, os.environ.get("CLICK_BASE_URL"), args.dry_run)
    print(f"persona {notifier.user_id}, scorer time shift {notifier.shift}")
    if args.once:
        notifier.run_slot()
        return
    serve_clicks(store, int(os.environ.get("PORT", config["click_port"])))
    notifier.run_forever()


if __name__ == "__main__":
    main()
