# Real pushes to your phone

The `notifier` service runs the model on today's NYT news and sends real push notifications to
your phone through your ntfy topic. Every candidate it scores is logged with all its covariates,
and so is what you did with the push. That gives you labeled push data, which MIND can't provide.

```
NYT RSS feeds ──▶ notifier ──HTTP──▶ scorer  (model + MIND user state, from the NYT sim)
                   │
   ntfy.sh ◀───────┤          cards :8090 ◀── tunnel (https://….trycloudflare.com) ◀── a reader taps
   (push to phones)│            │                                                    Save / pushes a day
                   ▼            ▼
        outputs/notifier/pushes.db, or Postgres via DATABASE_URL
        (candidates, covariates, sends, and per reader: opens, saves, push volume)
```

## The schedule

The notifier schedules itself; nothing external triggers it. Its main loop:

1. Works out the next slot from `slot_hours` in `configs/notifier.yml` (8, 12, 17, 21 by default)
   and the current time in `timezone` (New York).
2. Sleeps until that slot, waking at least every 5 minutes to recheck the clock. That way a
   Mac that was asleep notices it's past due as soon as it wakes.
3. Runs the slot, then goes back to step 1.

ntfy has no part in the timing; it only forwards what the notifier sends. Docker just keeps the
process alive (`restart: unless-stopped`).

Each slot:

1. Reads the NYT RSS feeds and adds new articles to the scorer, which embeds them and maps them
   to MIND subcategories as in the simulation.
2. Scores every article from the last 12 hours that hasn't already been pushed. Every earlier
   push goes along with the request as an impression of one article, labeled 1 if it was opened
   (see below).
3. Picks the top article with the push objective (friction from `configs/objective.yml`).
4. Sends it through [ntfy](https://ntfy.sh) if it passes the send policy and the reader's daily
   cap (see [Push volume](#push-volume)): 1, 2 or 4 a day for light, medium or heavy, and 3
   until they choose.
5. Writes every candidate to the database.

## The card

Tapping a notification opens that push's **card**, a small page served by the `cards` service
(`src/pushnyt/cards.py`) and made public by the `tunnel` service. It shows the headline, the abstract and:

- **Read on NYT** is a plain nytimes.com link, so iOS opens the NYT app if it's installed and
  Safari otherwise. Reading needs the reader's own NYT login there, as usual. Tapping it isn't
  tracked.
- **🔖 Save** adds it to your reading list at `/saved`. Tap again to unsave.
- **Pushes a day**: Light, Medium or Heavy (see below).

The first card a phone opens sets a cookie with a random reader ID, and offers an optional
first-name box. Only two things are stored per push under that reader: whether they opened it
(the click) and whether they saved it. ntfy's
own notification buttons aren't used: their requests carry no reader identity, and the card
keeps the buttons, the reading list and the tracking in one place.

## Push volume

Each reader picks how many pushes a day they want: **Light** (1), **Medium** (up to 2) or
**Heavy** (up to 4, every slot), set by `volume_caps` in `configs/notifier.yml`. Until they
choose, the cap is `daily_cap` (3). The cap is a ceiling: a slot only sends if its top article
clears the send policy, so a quiet news day sends fewer.

- **At the start:** `python -m pushnyt.notifier --welcome` sends a welcome push that opens
  `/start`, a page with the three buttons. Opening it creates the reader, so pushes from then on
  count for them.
- **On any card:** until the reader has chosen, the buttons sit above the headline with the
  question "How many pushes a day?"; afterwards they move to the bottom, to change any time.

The choice is stored on the reader (`readers.volume`, `volume_at`) and each slot reads it. All
subscribers to the ntfy topic get the same pushes, so with several readers the most recent
choice wins; per-reader volume would need a topic per reader.

## Setup (once)

1. **Topic.** Install **ntfy** from the App Store and allow notifications. Tap **+** and
   subscribe to a topic on `ntfy.sh`. Anyone who knows the topic name can read it, so make it
   hard to guess (e.g. `pushnyt-` plus the output of `openssl rand -hex 6`).
2. **`.env`.** Add `NTFY_TOPIC=<your topic>`. Leave `CLICK_BASE_URL` unset to use the tunnel.
   Only set it if you host the cards somewhere else, e.g. a fixed domain.
3. **Images.** The scorer runs in the training image, which needs the `api` extra (FastAPI).
   Images built before that was added won't start the scorer, so rebuild, then remove the old
   image:
   ```bash
   docker compose build scorer notifier
   docker image prune -f
   ```
4. **Memory.** The scorer is capped at 2.5 GB of Docker's 4 GB VM, so stop anything else heavy
   first, e.g. `docker compose stop api`.

## Run

```bash
docker compose up -d scorer        # loads the model and MIND state; about 2-5 min
docker compose ps                  # wait until scorer shows (healthy)

docker compose run --rm notifier "python -m pushnyt.notifier --test"            # a test push should reach your phone
docker compose run --rm notifier "python -m pushnyt.notifier --once --dry-run"  # score one slot now, log it, send nothing

docker compose up -d notifier cards tunnel   # the schedule, the card server, its public address
docker compose run --rm notifier "python -m pushnyt.notifier --welcome"   # pick light / medium / heavy
docker compose logs -f notifier cards        # one JSON line per slot, and per open / save / volume
```

The first run creates the new user's id (`new-` plus random hex) and fixes the time shift, and
both are stored in the database. To see the pushes on your Mac, open ntfy's web app (ntfy.sh/app, subscribe to the same
topic) or use iPhone Mirroring.

Stop it with `docker compose stop notifier cards tunnel scorer`.

**Postgres locally.** The database is SQLite by default. Set `DATABASE_URL` to use Postgres, the
same engine as Cloud SQL. The `db` service runs one on your Mac:

```bash
docker compose --profile postgres up -d db
echo 'DATABASE_URL=postgresql+psycopg://pushnyt:pushnyt@db:5432/pushnyt' >> .env   # then restart notifier and cards
PUSHNYT_TEST_DATABASE_URL=postgresql+psycopg://pushnyt:pushnyt@localhost:5433/pushnyt uv run pytest tests/test_notifier.py
```

## Keeping it running

The containers run inside Docker Desktop on your Mac, so pushes go out and cards load only while
the Mac is awake and Docker is running:

- **Sleep.** Containers pause while the Mac sleeps. On wake, the notifier sends the slot it
  was waiting for, late, and skips any others it missed.
- **Reboot or Docker restart.** Turn on Docker Desktop → Settings → General → *Start Docker
  Desktop when you sign in*. The notifier, cards and tunnel have `restart: unless-stopped` and
  come back by themselves. The scorer doesn't, so it doesn't hold 2.5 GB all the time; start it again
  with `docker compose up -d scorer`. Until you do, slots fail and are logged, without stopping
  the notifier.
- **Tunnel address.** A quick tunnel gets a new `trycloudflare.com` address whenever it
  restarts. New pushes use the new address, but cards of older pushes stop loading. A
  Cloudflare named tunnel with your own domain keeps one address.
- **Always on.** Pushing with the laptop closed means running these services in the cloud;
  see [Deploying to Google Cloud](deploy-gcp.md).

## The data

`outputs/notifier/pushes.db` contains:

| Table | One row per | Columns |
|-------|-------------|---------|
| `scores` | slot × candidate article | `slot_time`, article (`title`, `section`, `subcategory`, `url`, `published`), every model feature, `p_click`, `p_return`, `novelty`, `push_score`, `is_top`, `passes_policy`, `sent`, `push_id`, `sent_at`, `sent_ts` (Unix seconds) |
| `readers` | phone that opened a card or `/start` | `reader_id` (the cookie), `name` (optional), `created_at`, `first_push_ts`, `volume` (`light` / `medium` / `heavy`, NULL = not chosen), `volume_at` |
| `interactions` | push × reader × kind | `kind` (`open` = the click, or `save`), `created_at`, `minutes_after_push`; unsaving deletes the row |
| `labeled` (view) | sent push × reader | the push's `scores` row plus `reader_id`, `reader_name`, `reader_volume`, `label` (1 = opened, 0 = not opened within 24 h, NULL = pending), `saved` |
| `articles` | article read from the feeds | text, URL, publish time, mapped MIND subcategory |
| `meta` | setting | `user_id` the pushes are scored for, time anchor |

A reader is first seen when they open a card or the start page. So `labeled` pairs each reader
only with pushes sent from then on; before that, there's no way to know whether they were
subscribed. Subscribers who never open a card don't appear at all.

Example queries:

```bash
sqlite3 -header -column outputs/notifier/pushes.db \
  "SELECT slot_time, reader_name, round(p_click, 3) AS p_click, label, saved, title FROM labeled ORDER BY slot_time DESC LIMIT 20"

# per reader: pushes, opens, open rate, saves
sqlite3 -header -column outputs/notifier/pushes.db \
  "SELECT coalesce(reader_name, reader_id) AS reader, count(*) AS pushes, sum(label = 1) AS opens, round(avg(label), 3) AS open_rate, sum(saved) AS saves FROM labeled GROUP BY reader_id"

# open rate by predicted P(click) bucket
sqlite3 outputs/notifier/pushes.db \
  "SELECT round(p_click, 2) AS bucket, count(*) AS n, avg(label) AS open_rate FROM labeled WHERE label IS NOT NULL GROUP BY bucket"
```

The non-pushed candidates are kept too (`is_top = 0`), so the table can also serve as training or
off-policy data later. In pandas: `pd.read_sql("SELECT * FROM labeled", sqlite3.connect(path))`.

## Settings and limitations

`configs/notifier.yml` holds the slot hours, time zone, lookback, daily cap, `user_id`,
feeds, ntfy server and click window. The threshold, risk penalty and friction come from
`configs/objective.yml`.

- **Pushes are picked for a brand-new user.** By default the scorer sees a user it has never
  met: no click history and no impressions, like a MIND user on their first visit. The
  personal features (similarity to history, subcategory affinity, LLM themes) are missing, so
  the pick rests on the article (age, its subcategory's overall click rate) and the time of
  day. Every subscriber gets the same pushes. Setting `user_id` to a MIND id (e.g. `U12345`)
  scores for that reader's history instead.
- **Pushes feed back as impressions.** Each slot sends the user's earlier pushes to the scorer
  as one-article impressions, labeled 1 if any reader opened the card (a push still inside the
  24 h window counts as 0 until it's opened). The scorer uses them exactly as training used a
  MIND user's earlier impressions, so the per-user features move: slot and subcategory
  engagement, skips, the click/skip embedding contrast, and exposure (`impr_last_24h`,
  `hours_since_last_impr`, which now count pushes). The scorer keeps no state for this; the
  database is the record, so a restarted or second scorer replica gives the same scores.
- **The pre-period history doesn't grow.** In MIND that history is fixed before the log starts,
  and the model learned in-log clicks only through the features above. So taps don't enter it:
  `user_history_len` stays 0 and the history-similarity features (`sim_text`,
  `subcat_affinity`, embedding retrieval, LLM themes) stay missing for the new user.
- **Only opens are fed back.** Saves are stored but not used: an open means "curious", a save
  means "interested, not now". MIND has only clicks, so the model can't use saves until it's
  retrained on push data.
- **Exposure features learned from feed visits.** MIND impressions are visits users chose to
  make; pushes aren't. The model's exposure effect is small (about 2–3% lower engagement after
  5+ impressions a day; see analysis.md), so it won't do much to stop over-pushing on its own.
- **Cookies are per browser.** A reader who clears cookies, or opens a card in another browser,
  becomes a new reader. Saves live in this database, not in the reader's NYT account; NYT
  has no public API for writing to its saved list.
- **The feeds are RSS.** RSS needs no API key, but it only lists what editors put in each
  section's feed. The NYT Times Wire API would cover every article in one call.
- **P(click) is a ranking signal here, not a rate.** The model was trained on another outlet
  and era, so the 0.07 send threshold is in MIND feed units. Logged opens are what would
  recalibrate it.
