"""Notifier: push today's NYT articles to phones, and log every decision.

At each slot hour (configs/notifier.yml) it:
  1. reads the NYT RSS feeds and adds articles it hasn't seen to the scorer's catalog
  2. scores the last `lookback_hours` of unpushed articles through the scorer service, with every
     model feature, for one user: a brand-new one by default (no reading history, no impressions;
     `user_id` in the config picks a MIND persona instead)
  3. picks the top article with the push objective, gates it with the send policy
     (configs/objective.yml) and, if it passes, sends it through ntfy (https://ntfy.sh) to
     everyone subscribed to NTFY_TOPIC
  4. writes every candidate and its covariates to the database (see store.py)

Tapping a push opens its card (cards.py), which records what each reader does with it. The card
address is CLICK_BASE_URL if set (e.g. the Cloud Run URL of the card service), otherwise the
public https address of a Cloudflare quick tunnel, read from its metrics endpoint
(TUNNEL_METRICS_URL, the compose `tunnel` service). Without either, taps open the article
directly and nothing is recorded.

Time: the scorer's user state ends with the MIND log (Nov 2019). As in the NYT sim, real time is
shifted so the notifier's first day maps to the day after the log; the shift is stored in `meta`
so it stays the same across restarts.

Modes:
  python -m pushnyt.notifier                    # loop: sleep until each slot, run it (local)
  python -m pushnyt.notifier --once             # run one slot now and exit (a Cloud Run job / CronJob)
  python -m pushnyt.notifier --once --dry-run   # score and log, send nothing
  python -m pushnyt.notifier --test             # send a test notification
"""

import argparse
import hashlib
import json
import os
import secrets
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import pandas as pd
import yaml

from pushnyt.cards import open_store
from pushnyt.objective import push_scores, send_decisions
from pushnyt.push_job import HttpScorer
from pushnyt.store import ARTICLE_COLUMNS, Store

NEW_USER_PREFIX = "new-"  # MIND ids look like U12345, so these never collide

# --- feeds ---------------------------------------------------------------------------------------

def parse_rss(xml: bytes, tz: ZoneInfo) -> pd.DataFrame:
    """Articles from an RSS 2.0 feed: news_id, title, abstract, section, url, published (local, naive)."""
    out = []
    for item in ET.fromstring(xml).iter("item"):
        def text(tag, item=item):
            return (item.findtext(tag) or "").strip()
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
        except Exception as e:  # noqa: BLE001 -- one bad feed shouldn't stop the slot
            print(f"feed {url} failed: {e}")
    if not frames:
        return pd.DataFrame(columns=ARTICLE_COLUMNS)
    return pd.concat(frames).drop_duplicates("news_id").sort_values("published", kind="stable").reset_index(drop=True)


# --- delivery ------------------------------------------------------------------------------------

def send_ntfy(server: str, topic: str, title: str, message: str, click: str, tags: list[str] | None = None,
              actions: list[dict] | None = None) -> None:
    """Publish through ntfy's JSON API (body JSON, so non-ASCII titles are fine).

    `actions` are ntfy action buttons. The notifier doesn't use them: an `http` action does run in
    the background (tested on iOS), but its request can't say which subscriber tapped it, so ♥ and
    Save live on the card, where the reader cookie identifies them."""
    body = {"topic": topic, "title": title, "message": message, "click": click, "tags": tags or [],
            **({"actions": actions} if actions else {})}
    req = urllib.request.Request(server.rstrip("/"), data=json.dumps(body).encode(),
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


def tunnel_url(metrics_url: str) -> str | None:
    """The public https address of a Cloudflare quick tunnel, from its metrics endpoint."""
    try:
        with urllib.request.urlopen(metrics_url.rstrip("/") + "/quicktunnel", timeout=5) as resp:
            host = json.loads(resp.read()).get("hostname")
        return f"https://{host}" if host else None
    except Exception as e:  # noqa: BLE001 -- no tunnel means no card link, not a failed push
        print(f"tunnel address unavailable: {e!r}")
        return None


def new_user_id(stored: str | None) -> str:
    """The id of the brand-new user the pushes are picked for: the stored one if it is already a
    new user, else a fresh one (the scorer has never seen it, so it starts cold). A stored MIND
    persona from an older setup is replaced."""
    if stored and stored.startswith(NEW_USER_PREFIX):
        return stored
    return NEW_USER_PREFIX + secrets.token_hex(4)


# --- the slot ------------------------------------------------------------------------------------

class Notifier:
    def __init__(self, scorer, store: Store, config: dict, objective: dict, topic: str | None,
                 click_base: str | None, dry_run: bool = False, tunnel_metrics: str | None = None):
        self.scorer, self.store, self.cfg, self.objective = scorer, store, config, objective
        self.topic, self.click_base, self.dry_run = topic, (click_base or "").rstrip("/"), dry_run
        self.tunnel_metrics = tunnel_metrics
        self.tz = ZoneInfo(config["timezone"])
        info = scorer.info()
        if store.get("anchor") is None:  # first run: map today to the day after the MIND log
            store.put("anchor", self.now().normalize().isoformat())
            store.put("log_end", info["log_end"])
        log_end = pd.Timestamp(store.get("log_end"))
        self.shift = log_end.normalize() + pd.Timedelta(days=1) - pd.Timestamp(store.get("anchor"))
        self.user_id = config.get("user_id") or new_user_id(store.get("user_id"))
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
        """Read the feeds; send articles the database hasn't seen to the scorer, which embeds them
        and maps them to MIND subcategories, and store them. Needs no memory between slots, so each
        slot can be a fresh process (a Cloud Run job): the scorer ignores articles it already has,
        and the candidates travel with every score request anyway (see run_slot)."""
        fresh = fetch_feeds(self.cfg["feeds"], self.tz)
        fresh = fresh[~fresh["news_id"].isin(self.store.known_articles()) & (fresh["published"] <= now)]
        if len(fresh):
            mapped = self.scorer.add_articles(fresh[["news_id", "title", "abstract"]]
                                              .assign(published=fresh["published"] + self.shift))
            self.store.add_articles(fresh.merge(mapped, on="news_id"))
        print(f"{len(fresh)} new articles from the feeds")

    def run_slot(self, now: pd.Timestamp | None = None) -> dict:
        now = now or self.now()
        self.sync_articles(now)
        since = now - pd.Timedelta(hours=self.cfg["lookback_hours"])
        cand = self.store.articles_since(since)
        cand = cand[~cand["news_id"].isin(self.store.pushed_ids())]
        if cand.empty:
            print(f"{now:%a %H:%M}: no new candidates")
            return {"candidates": 0, "sent": False}

        slot_id = int(now.timestamp())
        request = {"user_id": self.user_id, "time": (now + self.shift).isoformat(),
                   "news_ids": cand["news_id"].tolist(), "impression_id": slot_id}
        # the candidates go along with the request, so any scorer replica can score them (one that
        # restarted, or one behind a load balancer that never saw the earlier /articles call)
        inline = cand[["news_id", "title", "abstract", "published"]].assign(published=cand["published"] + self.shift)
        scores = self.scorer.score([request], features=True, articles=inline)
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
        out["sent_ts"] = pd.array([int(time.time()) if x else None for x in out["sent"]], dtype="Int64")
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
            except Exception as e:  # noqa: BLE001 -- a failed slot (scorer down, feed error) shouldn't kill the service
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

    store = open_store(config)
    scorer = HttpScorer(args.scorer)
    wait_for(scorer)
    notifier = Notifier(scorer, store, config, objective, topic, os.environ.get("CLICK_BASE_URL"), args.dry_run,
                        tunnel_metrics=os.environ.get("TUNNEL_METRICS_URL"))
    print(f"user {notifier.user_id}, scorer time shift {notifier.shift}")
    if args.once:
        notifier.run_slot()
        return
    notifier.run_forever()


if __name__ == "__main__":
    main()
