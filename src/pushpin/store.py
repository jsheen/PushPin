"""The notifier's database: pushes, their covariates, readers and what each reader did.

One class, two backends: a SQLite file locally (outputs/notifier/pushes.db) or Postgres in the
cloud (Cloud SQL), chosen by the location passed in: a path, or a URL like
postgresql+psycopg://user:pass@host/db. The SQL sticks to what both support
(ON CONFLICT ... DO NOTHING / DO UPDATE, EXISTS, booleans compared by truth); the one difference,
the current Unix time in the `labeled` view, is chosen per dialect.

Tables:
  scores        one row per (slot, candidate article): article fields, every model feature,
                p_click / p_return / push_score, is_top, sent, push_id, sent_at (local time in
                the configured zone) and sent_ts (Unix seconds)
  readers       one row per phone that opened a card or the start page: reader_id, name,
                created_at, first_push_ts, volume (light / medium / heavy: how many pushes a day
                they asked for, NULL = not chosen), volume_at (when they chose it), slug (the
                random id in their public profile link, /u/<slug>; never the reader_id, which is
                their login cookie) and history_public (1 = others see what they've read)
  interactions  one row per (push, reader, kind), kind = open (the card was opened: the click)
                or save: created_at, minutes_after_push. Unsaving deletes the row.
  favorites     a reader's top article (their Crème; MAX_FAVORITES of them): reader_id, news_id, created_at
  labeled       view, one row per (sent push, reader who could have received it): the push's
                scores plus reader_id, reader_name, reader_volume, label (1 = opened, 0 = not
                opened within `click_window_hours`, NULL = pending), saved
  articles      every article read from the feeds, with its mapped MIND subcategory
  meta          the user_id pushes are scored for, and the time anchor
"""

import secrets
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import BigInteger, Boolean, Text, create_engine, inspect, text

ARTICLE_COLUMNS = ["news_id", "title", "abstract", "section", "url", "published"]
REACTIONS = ("save",)              # toggles on the card
KINDS = ("open", *REACTIONS)  # everything recorded per reader
VOLUMES = ("light", "medium", "heavy")  # push volume a reader can ask for (caps in configs/notifier.yml)
MAX_FAVORITES = 1  # the Crème: a reader's one top article
# reader columns added after the first release, created on older databases
READER_COLUMNS = {"volume": "TEXT", "volume_at": "TEXT", "slug": "TEXT", "history_public": "INTEGER DEFAULT 0"}

# columns whose type pandas can't infer from a batch where they're all empty
SCORE_TYPES = {"slot_id": BigInteger(), "sent_ts": BigInteger(), "push_id": Text(), "sent_at": Text(),
               "is_top": Boolean(), "passes_policy": Boolean(), "sent": Boolean(), "dry_run": Boolean()}

SCHEMA = [
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)",
    """CREATE TABLE IF NOT EXISTS articles (news_id TEXT PRIMARY KEY, title TEXT, abstract TEXT, section TEXT,
        url TEXT, published TEXT, subcategory TEXT, subcat_sim DOUBLE PRECISION)""",
    """CREATE TABLE IF NOT EXISTS readers (reader_id TEXT PRIMARY KEY, name TEXT, created_at TEXT, first_push_ts BIGINT,
        volume TEXT, volume_at TEXT, slug TEXT, history_public INTEGER DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS interactions (push_id TEXT, reader_id TEXT, kind TEXT, created_at TEXT,
        minutes_after_push DOUBLE PRECISION, PRIMARY KEY (push_id, reader_id, kind))""",
    """CREATE TABLE IF NOT EXISTS favorites (reader_id TEXT, news_id TEXT, created_at TEXT,
        PRIMARY KEY (reader_id, news_id))""",
]


def database_url(location: str | Path) -> str:
    """A SQLAlchemy URL: URLs pass through, paths become SQLite files."""
    location = str(location)
    if "://" in location:
        return location
    Path(location).parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{location}"


class Store:
    """Thread-safe: the card server is threaded. SQLite gets one writer at a time through a lock;
    Postgres handles concurrency itself, so the lock only matters locally."""

    def __init__(self, location: str | Path, click_window_hours: float, tz: ZoneInfo):
        url = database_url(location)
        sqlite = url.startswith("sqlite")
        self.engine = create_engine(url, **({"connect_args": {"check_same_thread": False}} if sqlite
                                            else {"pool_pre_ping": True}))
        self.lock = threading.Lock() if sqlite else _NoLock()
        self.click_window_hours, self.tz = click_window_hours, tz
        for statement in SCHEMA:
            self._exec(statement)
        with self.lock:
            reader_cols = {c["name"] for c in inspect(self.engine).get_columns("readers")}
        for col, sql_type in READER_COLUMNS.items():  # databases made before these columns
            if col not in reader_cols:
                self._exec(f"ALTER TABLE readers ADD COLUMN {col} {sql_type}")
        self._exec("CREATE UNIQUE INDEX IF NOT EXISTS readers_slug ON readers (slug)")
        self._labeled_view()

    # --- plumbing

    def _exec(self, sql: str, params=None):
        with self.lock, self.engine.begin() as con:
            return con.execute(text(sql), params or {})

    def _rows(self, sql: str, **params) -> list[dict]:
        with self.lock, self.engine.connect() as con:
            return [dict(r) for r in con.execute(text(sql), params).mappings()]

    def _has_scores(self) -> bool:
        with self.lock:
            return inspect(self.engine).has_table("scores")

    def _now_epoch(self) -> str:
        if self.engine.dialect.name == "postgresql":
            return "CAST(EXTRACT(EPOCH FROM now()) AS BIGINT)"
        return "CAST(strftime('%s', 'now') AS INTEGER)"

    def _labeled_view(self) -> None:
        if not self._has_scores():
            return
        window = int(self.click_window_hours * 3600)  # Unix seconds: independent of the server's time zone
        did = lambda kind: (f"EXISTS (SELECT 1 FROM interactions i WHERE i.push_id = s.push_id "
                            f"AND i.reader_id = r.reader_id AND i.kind = '{kind}')")
        # A reader is first seen when they open a card, so pushes sent before the first one they
        # opened are left out: we can't tell whether they were subscribed yet.
        self._exec("DROP VIEW IF EXISTS labeled")
        self._exec(f"""CREATE VIEW labeled AS SELECT s.*, r.reader_id, r.name AS reader_name,
            r.volume AS reader_volume,
            CASE WHEN {did("open")} THEN 1 WHEN s.sent_ts <= {self._now_epoch()} - {window} THEN 0 END AS label,
            {did("save")} AS saved
            FROM scores s JOIN readers r ON s.sent_ts >= r.first_push_ts
            WHERE s.sent""")

    def now(self) -> str:
        """Current local time in the configured zone, as stored in the *_at columns."""
        return datetime.now(self.tz).replace(tzinfo=None).isoformat(timespec="seconds")

    # --- settings

    def get(self, key: str) -> str | None:
        rows = self._rows("SELECT value FROM meta WHERE key = :key", key=key)
        return rows[0]["value"] if rows else None

    def put(self, key: str, value: str) -> None:
        self._exec("INSERT INTO meta VALUES (:key, :value) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                   {"key": key, "value": value})

    # --- articles and pushes (written by the notifier)

    def known_articles(self) -> set[str]:
        return {r["news_id"] for r in self._rows("SELECT news_id FROM articles")}

    def add_articles(self, articles: pd.DataFrame) -> None:
        rows = articles.assign(published=articles["published"].map(pd.Timestamp.isoformat))
        self._exec("""INSERT INTO articles VALUES (:news_id, :title, :abstract, :section, :url, :published,
                          :subcategory, :subcat_sim) ON CONFLICT DO NOTHING""",
                   rows[ARTICLE_COLUMNS + ["subcategory", "subcat_sim"]].to_dict("records"))

    def articles_since(self, since: datetime) -> pd.DataFrame:
        with self.lock, self.engine.connect() as con:
            df = pd.read_sql(text("SELECT * FROM articles WHERE published > :since ORDER BY published"),
                             con, params={"since": since.isoformat()})
        return df.assign(published=pd.to_datetime(df["published"]))

    def pushed_ids(self) -> set[str]:
        if not self._has_scores():
            return set()
        return {r["news_id"] for r in self._rows("SELECT news_id FROM scores WHERE sent")}

    def sent_on(self, day: str) -> int:
        if not self._has_scores():
            return 0
        return self._rows("SELECT COUNT(*) AS n FROM scores WHERE sent AND substr(slot_time, 1, 10) = :day",
                          day=day)[0]["n"]

    def push_events(self, user_id: str) -> pd.DataFrame:
        """Every push sent for `user_id`, oldest first, with its article and whether anyone opened
        it so far: slot_id, slot_time, news_id, title, abstract, published, tapped (0/1). A push
        still inside the click window counts as not tapped until it is."""
        if not self._has_scores():
            return pd.DataFrame(columns=["slot_id", "slot_time", "news_id", "title", "abstract", "published", "tapped"])
        with self.lock, self.engine.connect() as con:
            return pd.read_sql(text("""SELECT s.slot_id, s.slot_time, s.news_id, a.title, a.abstract, a.published,
                    CASE WHEN EXISTS (SELECT 1 FROM interactions i WHERE i.push_id = s.push_id AND i.kind = 'open')
                         THEN 1 ELSE 0 END AS tapped
                FROM scores s JOIN articles a ON a.news_id = s.news_id
                WHERE s.sent AND s.user_id = :u ORDER BY s.slot_time"""), con, params={"u": user_id})

    def add_scores(self, scores: pd.DataFrame) -> None:
        new = not self._has_scores()
        with self.lock:
            existing = set() if new else {c["name"] for c in inspect(self.engine).get_columns("scores")}
        added = [c for c in scores.columns if existing and c not in existing]
        for col in added:  # a retrained model can add features: widen the table
            kind = scores[col].dtype.kind
            sql_type = {"f": "DOUBLE PRECISION", "i": "BIGINT", "u": "BIGINT", "b": "BOOLEAN"}.get(kind, "TEXT")
            self._exec(f'ALTER TABLE scores ADD COLUMN "{col}" {sql_type}')
        with self.lock, self.engine.begin() as con:
            scores.to_sql("scores", con, if_exists="append", index=False,
                          dtype={c: t for c, t in SCORE_TYPES.items() if c in scores.columns})
        if new:
            self._exec("CREATE INDEX IF NOT EXISTS scores_push ON scores (push_id)")
        if new or added:
            self._labeled_view()  # Postgres views fix their column list at creation

    # --- readers and interactions (written by the card server)

    def push(self, push_id: str, reader_id: str | None = None) -> dict | None:
        """A sent push with its article, plus whether this reader saved it (None if unknown)."""
        if not self._has_scores():
            return None
        mark = lambda kind: (f"EXISTS (SELECT 1 FROM interactions i WHERE i.push_id = s.push_id "
                             f"AND i.reader_id = :reader AND i.kind = '{kind}')")
        rows = self._rows(f"""SELECT s.push_id, s.news_id, s.title, s.section, s.url, s.sent_ts, a.abstract,
                {mark("save")} AS saved
            FROM scores s LEFT JOIN articles a ON a.news_id = s.news_id
            WHERE s.push_id = :push AND s.sent""", push=push_id, reader=reader_id)
        return rows[0] if rows else None

    def reader(self, reader_id: str | None) -> dict | None:
        if not reader_id:
            return None
        rows = self._rows("SELECT reader_id, name, volume, slug, history_public FROM readers WHERE reader_id = :r",
                          r=reader_id)
        return rows[0] if rows else None

    def new_reader(self, first_push_ts: int | None) -> str:
        reader_id = secrets.token_hex(8)
        self._exec("INSERT INTO readers (reader_id, name, created_at, first_push_ts) VALUES (:r, NULL, :at, :ts)",
                   {"r": reader_id, "at": self.now(), "ts": first_push_ts})
        return reader_id

    def set_name(self, reader_id: str, name: str) -> bool:
        name = " ".join(name.split())[:40] or None
        return self._exec("UPDATE readers SET name = :n WHERE reader_id = :r", {"n": name, "r": reader_id}).rowcount > 0

    def set_volume(self, reader_id: str, volume: str) -> bool:
        if volume not in VOLUMES:
            return False
        return self._exec("UPDATE readers SET volume = :v, volume_at = :at WHERE reader_id = :r",
                          {"v": volume, "at": self.now(), "r": reader_id}).rowcount > 0

    def volume(self) -> str | None:
        """The most recently chosen push volume across readers (the demo has one reader; with one
        ntfy topic, everyone subscribed gets the same pushes), or None if nobody has chosen."""
        rows = self._rows("SELECT volume FROM readers WHERE volume IS NOT NULL ORDER BY volume_at DESC LIMIT 1")
        return rows[0]["volume"] if rows else None

    def interact(self, push_id: str, reader_id: str, kind: str, on: bool = True) -> bool:
        """Record (or, for save with on=False, clear) one reader's interaction with a sent push.
        Only the first open is kept. False if the push, reader or kind is unknown."""
        push = self.push(push_id)
        if push is None or kind not in KINDS or self.reader(reader_id) is None:
            return False
        params = {"p": push_id, "r": reader_id, "k": kind}
        if on:
            minutes = (time.time() - push["sent_ts"]) / 60 if push["sent_ts"] is not None else None
            self._exec("INSERT INTO interactions VALUES (:p, :r, :k, :at, :m) ON CONFLICT DO NOTHING",
                       {**params, "at": self.now(), "m": minutes})
        elif kind in REACTIONS:
            self._exec("DELETE FROM interactions WHERE push_id = :p AND reader_id = :r AND kind = :k", params)
        return True

    def reader_count(self) -> int:
        return self._rows("SELECT COUNT(*) AS n FROM readers")[0]["n"]

    def reading_list(self, reader_id: str | None) -> list[dict]:
        """This reader's saved pushes, newest first."""
        if not reader_id or not self._has_scores():
            return []
        return self._rows("""SELECT i.push_id, s.title, s.section, i.created_at AS saved_at
            FROM interactions i JOIN scores s ON s.push_id = i.push_id AND s.sent
            WHERE i.kind = 'save' AND i.reader_id = :r ORDER BY i.created_at DESC""", r=reader_id)


    # --- profile: articles read, favorites, public history

    def articles_read(self, reader_id: str | None) -> list[dict]:
        """Every article this reader opened a push card for, most recent first: news_id, title,
        section, url, read_at. Opening the card is what's recorded, not reading on NYT."""
        if not reader_id or not self._has_scores():
            return []
        return self._rows("""SELECT s.news_id, MAX(s.title) AS title, MAX(s.section) AS section, MAX(s.url) AS url,
                MIN(i.created_at) AS read_at
            FROM interactions i JOIN scores s ON s.push_id = i.push_id AND s.sent
            WHERE i.kind = 'open' AND i.reader_id = :r
            GROUP BY s.news_id ORDER BY read_at DESC""", r=reader_id)

    def favorites(self, reader_id: str | None) -> list[str]:
        """This reader's favorite news_ids, in the order they were picked."""
        if not reader_id:
            return []
        return [r["news_id"] for r in self._rows(
            "SELECT news_id FROM favorites WHERE reader_id = :r ORDER BY created_at, news_id", r=reader_id)]

    def set_favorite(self, reader_id: str, news_id: str, on: bool = True) -> str:
        """Pick (or drop) an article the reader has read as a favorite. Past MAX_FAVORITES, the
        oldest pick makes room. Returns "ok", or "unknown" (not an article they've read)."""
        params = {"r": reader_id, "n": news_id}
        if not on:
            self._exec("DELETE FROM favorites WHERE reader_id = :r AND news_id = :n", params)
            return "ok"
        if news_id not in {a["news_id"] for a in self.articles_read(reader_id)}:
            return "unknown"
        current = self.favorites(reader_id)
        if news_id in current:
            return "ok"
        for old in current[:len(current) - MAX_FAVORITES + 1]:  # replace the oldest pick(s)
            self._exec("DELETE FROM favorites WHERE reader_id = :r AND news_id = :n", {"r": reader_id, "n": old})
        self._exec("INSERT INTO favorites VALUES (:r, :n, :at) ON CONFLICT DO NOTHING", {**params, "at": self.now()})
        return "ok"

    def set_history_public(self, reader_id: str, public: bool) -> bool:
        return self._exec("UPDATE readers SET history_public = :p WHERE reader_id = :r",
                          {"p": int(public), "r": reader_id}).rowcount > 0

    def slug(self, reader_id: str) -> str:
        """The reader's public profile id, made on first use."""
        self._exec("UPDATE readers SET slug = :s WHERE reader_id = :r AND slug IS NULL",
                   {"s": secrets.token_urlsafe(6), "r": reader_id})
        return self._rows("SELECT slug FROM readers WHERE reader_id = :r", r=reader_id)[0]["slug"]

    def reader_by_slug(self, slug: str) -> dict | None:
        if not slug:
            return None
        rows = self._rows("SELECT reader_id, name, slug, history_public FROM readers WHERE slug = :s", s=slug)
        return rows[0] if rows else None


class _NoLock:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False
