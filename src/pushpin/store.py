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
  interactions  one row per (push, reader, kind): created_at, minutes_after_push. kind = open
                (the push was tapped: the click, which "pins" the article; not shown on the
                profile) or read (Read was tapped on the pinned article: what the profile counts).
                Rows of kind save are from before saves were dropped.
  favorites     a reader's top article (their Crème; MAX_FAVORITES of them), any article they
                pinned: reader_id, news_id, created_at
  removed       articles a reader deleted from their profile (out of Pinned, Read and the Crème):
                reader_id, news_id, created_at. Their open and read rows stay, so the model's
                labels don't change; a later push of the article, if tapped, brings it back
  labeled       view, one row per (sent push, reader who could have received it): the push's
                scores plus reader_id, reader_name, reader_volume, label (1 = opened, 0 = not
                opened within `click_window_hours`, NULL = pending), read (1 = tapped Read)
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
KINDS = ("open", "read")  # recorded per reader: the push tapped (pinned), then Read tapped
VOLUMES = ("light", "medium", "heavy")  # push volume a reader can ask for (caps in configs/notifier.yml)
DEFAULT_VOLUME = "medium"  # until a reader chooses (readers.volume stays NULL: not chosen)
MAX_FAVORITES = 1  # the Crème: a reader's one top article
# reader columns added after the first release, created on older databases
READER_COLUMNS = {"volume": "TEXT", "volume_at": "TEXT", "slug": "TEXT", "history_public": "INTEGER DEFAULT 0",
                  "apple_sub": "TEXT", "scorer_user": "TEXT"}

# columns whose type pandas can't infer from a batch where they're all empty
SCORE_TYPES = {"slot_id": BigInteger(), "sent_ts": BigInteger(), "push_id": Text(), "sent_at": Text(),
               "is_top": Boolean(), "passes_policy": Boolean(), "sent": Boolean(), "dry_run": Boolean(),
               "for_reader": Text()}

SCHEMA = [
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)",
    """CREATE TABLE IF NOT EXISTS articles (news_id TEXT PRIMARY KEY, title TEXT, abstract TEXT, section TEXT,
        url TEXT, published TEXT, subcategory TEXT, subcat_sim DOUBLE PRECISION)""",
    """CREATE TABLE IF NOT EXISTS readers (reader_id TEXT PRIMARY KEY, name TEXT, created_at TEXT, first_push_ts BIGINT,
        volume TEXT, volume_at TEXT, slug TEXT, history_public INTEGER DEFAULT 0, apple_sub TEXT,
        scorer_user TEXT)""",
    """CREATE TABLE IF NOT EXISTS interactions (push_id TEXT, reader_id TEXT, kind TEXT, created_at TEXT,
        minutes_after_push DOUBLE PRECISION, PRIMARY KEY (push_id, reader_id, kind))""",
    """CREATE TABLE IF NOT EXISTS favorites (reader_id TEXT, news_id TEXT, created_at TEXT,
        PRIMARY KEY (reader_id, news_id))""",
    """CREATE TABLE IF NOT EXISTS removed (reader_id TEXT, news_id TEXT, created_at TEXT,
        PRIMARY KEY (reader_id, news_id))""",
    # the iOS app's push addresses (APNs device tokens); one reader can have several devices
    """CREATE TABLE IF NOT EXISTS devices (token TEXT PRIMARY KEY, reader_id TEXT, sandbox INTEGER DEFAULT 0,
        created_at TEXT)""",
]
# profile lists leave out what the reader deleted (s = the scores row, :r = the reader)
KEPT = "NOT EXISTS (SELECT 1 FROM removed x WHERE x.reader_id = :r AND x.news_id = s.news_id)"


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
        self._exec("CREATE UNIQUE INDEX IF NOT EXISTS readers_apple ON readers (apple_sub)")
        if self._has_scores():  # databases from before per-reader pushes
            with self.lock:
                score_cols = {c["name"] for c in inspect(self.engine).get_columns("scores")}
            if "for_reader" not in score_cols:
                self._exec("ALTER TABLE scores ADD COLUMN for_reader TEXT")
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
        # Two audiences. Broadcast pushes (for_reader NULL: the ntfy topic) count for every web reader
        # from when they were first seen (they open a card), since we can't tell whether they were
        # subscribed before. A push picked for one app reader (for_reader set) counts only for them.
        self._exec("DROP VIEW IF EXISTS labeled")
        self._exec(f"""CREATE VIEW labeled AS SELECT s.*, r.reader_id, r.name AS reader_name,
            r.volume AS reader_volume,
            CASE WHEN {did("open")} THEN 1 WHEN s.sent_ts <= {self._now_epoch()} - {window} THEN 0 END AS label,
            {did("read")} AS read
            FROM scores s JOIN readers r ON (s.for_reader IS NULL AND r.apple_sub IS NULL
                                             AND s.sent_ts >= r.first_push_ts) OR s.for_reader = r.reader_id
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

    @staticmethod
    def _audience(reader_id: str | None) -> tuple[str, dict]:
        """SQL picking one audience's pushes: the broadcast (None) or one app reader's."""
        return ("for_reader IS NULL", {}) if reader_id is None else ("for_reader = :aud", {"aud": reader_id})

    def pushed_ids(self, reader_id: str | None = None) -> set[str]:
        """Articles already pushed to this audience (the broadcast, or one app reader)."""
        if not self._has_scores():
            return set()
        where, params = self._audience(reader_id)
        return {r["news_id"] for r in self._rows(f"SELECT news_id FROM scores WHERE sent AND {where}", **params)}

    def sent_on(self, day: str, reader_id: str | None = None) -> int:
        if not self._has_scores():
            return 0
        where, params = self._audience(reader_id)
        return self._rows(f"SELECT COUNT(*) AS n FROM scores WHERE sent AND substr(slot_time, 1, 10) = :day "
                          f"AND {where}", day=day, **params)[0]["n"]

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
        """A sent push with its article, plus whether this reader read it (None if unknown)."""
        if not self._has_scores():
            return None
        mark = lambda kind: (f"EXISTS (SELECT 1 FROM interactions i WHERE i.push_id = s.push_id "
                             f"AND i.reader_id = :reader AND i.kind = '{kind}')")
        rows = self._rows(f"""SELECT s.push_id, s.news_id, s.title, s.section, s.url, s.sent_ts, a.abstract,
                s.for_reader, {mark("read")} AS read
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

    def reader_for_apple(self, apple_sub: str, first_push_ts: int | None) -> tuple[str, bool]:
        """The reader signed in with this Apple id, made on first sign-in: (reader_id, new)."""
        rows = self._rows("SELECT reader_id FROM readers WHERE apple_sub = :a", a=apple_sub)
        if rows:
            return rows[0]["reader_id"], False
        reader_id = self.new_reader(first_push_ts)
        self._exec("UPDATE readers SET apple_sub = :a WHERE reader_id = :r", {"a": apple_sub, "r": reader_id})
        return reader_id, True

    def add_device(self, reader_id: str, token: str, sandbox: bool = False) -> bool:
        """Register (or move to this reader) an APNs device token."""
        token = token.strip().lower()
        if not token or len(token) > 200 or any(c not in "0123456789abcdef" for c in token):
            return False
        self._exec("DELETE FROM devices WHERE token = :t", {"t": token})
        self._exec("INSERT INTO devices VALUES (:t, :r, :s, :at)",
                   {"t": token, "r": reader_id, "s": int(sandbox), "at": self.now()})
        return True

    def remove_device(self, token: str, reader_id: str | None = None) -> bool:
        """Drop a device token (signed out, or APNs says it's dead). With a reader, only theirs."""
        where, params = "token = :t", {"t": token.strip().lower()}
        if reader_id is not None:
            where, params = where + " AND reader_id = :r", {**params, "r": reader_id}
        return self._exec(f"DELETE FROM devices WHERE {where}", params).rowcount > 0

    def devices(self, reader_id: str | None = None) -> list[dict]:
        """Device tokens: all of them, or one reader's."""
        if reader_id is None:
            return self._rows("SELECT token, reader_id, sandbox FROM devices ORDER BY created_at")
        return self._rows("SELECT token, reader_id, sandbox FROM devices WHERE reader_id = :r ORDER BY created_at",
                          r=reader_id)

    def app_readers(self, new_user_id) -> list[dict]:
        """App readers with somewhere to push (a device): reader_id, volume, scorer_user. Each gets
        their own pushes, scored as their own user; `new_user_id()` names a reader's scorer user the
        first time (a cold start for the scorer)."""
        rows = self._rows("""SELECT reader_id, volume, scorer_user FROM readers r
            WHERE EXISTS (SELECT 1 FROM devices d WHERE d.reader_id = r.reader_id) ORDER BY created_at, reader_id""")
        for row in rows:
            if row["scorer_user"] is None:
                row["scorer_user"] = new_user_id()
                self._exec("UPDATE readers SET scorer_user = :u WHERE reader_id = :r",
                           {"u": row["scorer_user"], "r": row["reader_id"]})
        return rows

    def delete_reader(self, reader_id: str) -> bool:
        """Delete an account and everything stored under it (App Store rule for apps with
        accounts). The pushes themselves stay: they were sent to everyone, not to this reader."""
        if self.reader(reader_id) is None:
            return False
        for table in ("devices", "favorites", "removed", "interactions", "readers"):
            self._exec(f"DELETE FROM {table} WHERE reader_id = :r", {"r": reader_id})
        return True

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
        ntfy topic, everyone subscribed gets the same pushes), or None if nobody has chosen. App
        readers choose for themselves (`app_readers`), so they don't count here."""
        rows = self._rows("SELECT volume FROM readers WHERE volume IS NOT NULL AND apple_sub IS NULL "
                          "ORDER BY volume_at DESC LIMIT 1")
        return rows[0]["volume"] if rows else None

    def interact(self, push_id: str, reader_id: str, kind: str) -> bool:
        """Record one reader's open or read of a sent push; only the first of each is kept.
        False if the push, reader or kind is unknown."""
        push = self.push(push_id)
        if push is None or kind not in KINDS or self.reader(reader_id) is None:
            return False
        if push["for_reader"] not in (None, reader_id):  # picked for another app reader
            return False
        minutes = (time.time() - push["sent_ts"]) / 60 if push["sent_ts"] is not None else None
        new = self._exec("INSERT INTO interactions VALUES (:p, :r, :k, :at, :m) ON CONFLICT DO NOTHING",
                         {"p": push_id, "r": reader_id, "k": kind, "at": self.now(), "m": minutes}).rowcount > 0
        if new and kind == "open":  # a new push of a deleted article was tapped: it's pinned again
            self._exec("DELETE FROM removed WHERE reader_id = :r AND news_id = :n",
                       {"r": reader_id, "n": push["news_id"]})
        return True

    def reader_count(self) -> int:
        return self._rows("SELECT COUNT(*) AS n FROM readers")[0]["n"]

    # --- profile: articles read, favorites, public history

    def articles_read(self, reader_id: str | None) -> list[dict]:
        """Every article this reader tapped Read on, most recent first: news_id, title, section,
        url, read_at. The tap on Read is what's recorded, not reading on NYT."""
        if not reader_id or not self._has_scores():
            return []
        return self._rows(f"""SELECT s.news_id, MAX(s.title) AS title, MAX(s.section) AS section, MAX(s.url) AS url,
                MIN(i.created_at) AS read_at
            FROM interactions i JOIN scores s ON s.push_id = i.push_id AND s.sent
            WHERE i.kind = 'read' AND i.reader_id = :r AND {KEPT}
            GROUP BY s.news_id ORDER BY read_at DESC""", r=reader_id)

    def pinned_unread(self, reader_id: str | None) -> list[dict]:
        """Articles this reader tapped a push for but hasn't tapped Read on yet, most recently
        pinned first: news_id, title, url, push_id (a push of it, for recording the read)."""
        if not reader_id or not self._has_scores():
            return []
        return self._rows(f"""SELECT s.news_id, MAX(s.title) AS title, MAX(s.url) AS url, MAX(i.push_id) AS push_id,
                MAX(i.created_at) AS pinned_at
            FROM interactions i JOIN scores s ON s.push_id = i.push_id AND s.sent
            WHERE i.kind = 'open' AND i.reader_id = :r AND {KEPT} AND s.news_id NOT IN (
                SELECT s2.news_id FROM interactions i2 JOIN scores s2 ON s2.push_id = i2.push_id AND s2.sent
                WHERE i2.kind = 'read' AND i2.reader_id = :r)
            GROUP BY s.news_id ORDER BY pinned_at DESC""", r=reader_id)

    def pinned(self, reader_id: str, news_id: str) -> bool:
        """Whether this reader tapped (or read) a push of this article."""
        if not self._has_scores():
            return False
        return bool(self._rows("""SELECT 1 FROM interactions i JOIN scores s ON s.push_id = i.push_id AND s.sent
            WHERE i.reader_id = :r AND s.news_id = :n LIMIT 1""", r=reader_id, n=news_id))

    def has_read(self, reader_id: str, news_id: str) -> bool:
        """Whether this reader tapped Read on a push of this article."""
        if not self._has_scores():
            return False
        return bool(self._rows("""SELECT 1 FROM interactions i JOIN scores s ON s.push_id = i.push_id AND s.sent
            WHERE i.reader_id = :r AND s.news_id = :n AND i.kind = 'read' LIMIT 1""", r=reader_id, n=news_id))

    def favorites(self, reader_id: str | None) -> list[dict]:
        """This reader's favorites (the Crème), in the order they were picked: news_id, title, url."""
        if not reader_id or not self._has_scores():
            return []
        return self._rows(f"""SELECT f.news_id, MAX(s.title) AS title, MAX(s.url) AS url
            FROM favorites f JOIN scores s ON s.news_id = f.news_id AND s.sent
            WHERE f.reader_id = :r AND {KEPT} GROUP BY f.news_id, f.created_at ORDER BY f.created_at, f.news_id""", r=reader_id)

    def set_favorite(self, reader_id: str, news_id: str, on: bool = True) -> str:
        """Pick (or drop) an article the reader has read as a favorite. Past MAX_FAVORITES, the
        oldest pick makes room. Returns "ok", "unknown" (not an article they pinned) or "unread"
        (pinned but not read yet: only read articles go in the Crème)."""
        params = {"r": reader_id, "n": news_id}
        if not on:
            self._exec("DELETE FROM favorites WHERE reader_id = :r AND news_id = :n", params)
            return "ok"
        if not self.pinned(reader_id, news_id):
            return "unknown"
        if not self.has_read(reader_id, news_id):
            return "unread"
        current = [f["news_id"] for f in self.favorites(reader_id)]
        if news_id in current:
            return "ok"
        for old in current[:len(current) - MAX_FAVORITES + 1]:  # replace the oldest pick(s)
            self._exec("DELETE FROM favorites WHERE reader_id = :r AND news_id = :n", {"r": reader_id, "n": old})
        self._exec("INSERT INTO favorites VALUES (:r, :n, :at) ON CONFLICT DO NOTHING", {**params, "at": self.now()})
        return "ok"

    def remove(self, reader_id: str, news_id: str) -> bool:
        """Delete an article the reader pinned or read from their profile: it leaves Pinned, Read
        and the Crème. False if it isn't one of theirs."""
        if not self.pinned(reader_id, news_id):
            return False
        params = {"r": reader_id, "n": news_id}
        self._exec("DELETE FROM favorites WHERE reader_id = :r AND news_id = :n", params)
        self._exec("INSERT INTO removed VALUES (:r, :n, :at) ON CONFLICT DO NOTHING", {**params, "at": self.now()})
        return True

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
