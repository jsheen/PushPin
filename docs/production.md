# Running PushPin in production: live scoring and the push pipeline

Status: **design notes** for running at publisher scale (written 2026-11-03, updated 2026-11-19).
What's deployed for the demo, with costs, is in [deploy-gcp.md](deploy-gcp.md). The first part
below is how one request is scored live; the rest is the pipeline around it.

## Live scoring

Status (2026-11-19): **live scoring runs.** The scorer service (`scorer_api.py`, `POST /score`)
scores the notifier's requests at every slot, locally in Docker or as a private Cloud Run service
([deploy-gcp.md](deploy-gcp.md)). There's no separate online store: each request carries the
reader's earlier pushes and taps as events, and the notifier's database is the record, so the
scorer stays stateless ([notifier.md](notifier.md)). `pushpin-api` still serves the precomputed
test-day scores in `predictions.parquet`.

### How it works

The model is the easy part (`xgb_click.json` is 3 MB and predicts in microseconds). The hard part
is computing the ~30 features for one request exactly as batch training did, from state current
up to the request.

- `features.py` is split into `fit_globals` (shared state fitted on the log) and `features_for`
  (features for any set of events). Batch training calls both, so there is one implementation.
- `live.py`: `LiveScorer.score(user, time, candidates)` computes features from that user's own
  events plus the shared state, with the same `features_for`. `observe()` records an outcome.
  `add_articles` adds articles after training (text projected with the saved TF-IDF/SVD, a
  training subcategory, a publish time), and `score_many` scores many users in one pass (4 ms per
  request); `pushpin-nyt-sim` uses both.
- **Parity:** `pushpin-live-parity <run>` replays the test day request by request. On 100 users
  (6,740 rows), P(click), novelty and P(return) match batch exactly; median latency 112 ms per
  request. `tests/test_live.py` checks exact parity on synthetic data.
- Article first-seen time is the one piece of state shared across users, so a replay over a user
  sample must still observe every user's impressions.
- The return model is saved with the run (`xgb_return.json`). The other shared state (TF-IDF/SVD,
  complement matrix, prior rates) is refit deterministically from the log rather than saved.

### What each feature needs at request time

| Group | Features | State needed | Updated |
|-------|----------|--------------|---------|
| Time-slot engagement | `slot_prior_impr`, `slot_prior_clicks`, `slot_engaged_rate`, `slot_click_share` | Per user × slot: impression and click counts; per user: total clicks; global base rate | Every impression / click |
| Push fatigue | `slot_skipped_72h`, `slot_skip_rate_72h`, `impr_last_24h`, `hours_since_last_impr`, `hours_since_last_slot_impr` | Per user: timestamped impressions with shown/skipped counts for the last 72 h; last impression time overall and per slot | Every impression |
| User depth | `user_history_len`, `user_prior_impr` | Per user: history length, impression count | Every impression |
| Similarity | `sim_text`, `sim_entity`, `subcat_affinity` | Per user: history-profile vectors and subcategory weights; per article: text and entity vectors | Every click; per new article |
| Complementarity | `complement_score` | The fitted subcategory matrix `s` (frozen at training); per user: subcategory weights | Retrain; every click |
| Subcategory skips | `subcat_engaged_rate`, `subcat_prior_skips` | Per user × subcategory: shown and clicked counts; per-subcategory prior rates | Every impression |
| Embedding retrieval | `emb_sim_mean/max/top5`, `emb_n_close`, `emb_retrieval_rank` | Per user: clicked article IDs and the mean history embedding; the article vector index | Every click; per new article |
| Click/skip contrast | `emb_sim_clicked_max`, `emb_sim_skipped_max`, `emb_contrast` | Per user: clicked and shown-but-skipped article IDs; article vectors | Every impression |
| LLM reader profile | `llm_sim_click`, `llm_sim_skip`, `llm_contrast` | Per user: latest day's click/skip theme vectors | Daily batch job |
| Article recency | `article_age_hours` | Per article: first time it was shown (or its publish time) | First impression of each article |
| Serving extras | `novelty`, `p_return` | Per user × subcategory exposure; the return model and its features | Every impression; retrain |

Every one must use only events **strictly before** the request, exactly as in batch, or the live
model sees different inputs than it was trained on.

### Avoiding training/serving skew

Skew, two implementations of a feature that quietly disagree, is the main risk. It's handled by
the single `features_for` implementation and the parity test above. The remaining step is to
**log served features** next to each prediction (the notifier's `scores` table already does), so
drift between training and serving inputs can be monitored.

## The job in one sentence

At each push slot, decide for every subscribed reader whether to send one push notification and
which article it should be, from the day's unpushed articles and everything we know about that
reader.

Pushes are naturally **batch** work: nothing has to be answered in milliseconds, decisions
happen at set times. So the core of production is a scheduled job, not an always-on server.

## What exists today

Each production piece has a stand-in here: manual `pushpin-train` runs (gated by
`evaluation.min_auc`) for scheduled retraining, `LiveScorer` for the push job's scoring step, the
push job on a simulated week for the real one, the notifier's per-request events for the
feedback loop, the bandit simulation for a learned send decision, and `daily.csv` for monitoring.

## Target architecture

```
every few minutes   news API (e.g. NYT Times Newswire) ──▶ ingest job
                       embed with MiniLM, project text, map subcategory, publish time
                       ──▶ article catalog + vector index

each push slot      Cloud Scheduler ──▶ push job (container)
  (every two hours)    load model version + user state + the day's unpushed articles
                       score_many(all subscribed users, in batches)
                       push objective (friction) + send decision (fixed policy today; the
                       contextual bandit, refit nightly, once validated: docs/bandit.md)
                       ──▶ delivery service (Firebase Cloud Messaging / APNs) ──▶ phones
                       ──▶ log every decision (sent or held) with its features

continuously        opens, clicks, dismissals, opt-outs ──▶ event log
                       ──▶ user state updates (what LiveScorer.observe does)

daily               report job ──▶ daily.csv-style metrics + alerts (monitoring, editors)

nightly             bandit refit on all send decisions so far (sufficient statistics: cheap)

weekly or nightly   retrain job on the new logs ──▶ quality gate ──▶ new model version
                       ──▶ build serving image tagged with the version ──▶ deploy
```

### Components

| Component | Runs on (GCP) | Notes |
|---|---|---|
| Ingest job | Cloud Run Job, every few minutes | Idempotent: an article seen twice is added once |
| Push job | Cloud Run Job, triggered per slot by Cloud Scheduler | Starts, runs, exits; same code as `pushpin-nyt-sim`'s loop |
| Delivery | Firebase Cloud Messaging (Android, web), APNs (iOS) | We decide; they deliver |
| User state | Firestore or Memorystore (Redis), keyed by user, updated by an event-ingestion worker | Today the notifier sends each reader's events with the request; at scale, per-user state (counts, a 72-hour window, clicked/skipped IDs, a few vectors) must persist between job runs |
| Article catalog + vectors | GCS (or a database) + in-memory exact search | 50k articles ≈ 80 MB of vectors: exact numpy search takes ~2 ms, no vector DB needed below a few million articles; new articles must be embedded within minutes of publishing |
| LLM reader profiles | A daily batch job (Message Batches API, half price) | Output becomes per-user theme vectors in the user-state store |
| Event log | Pub/Sub → BigQuery or GCS | Source of truth for state updates, retraining and analysis |
| Training | Cloud Run Job or Vertex AI training job | Same `pushpin-train` container |
| Models and images | GCS (artifacts), Artifact Registry (images) | Every version tagged with git commit + data date |
| Optional HTTP API | Cloud Run service | For other systems asking one-off questions; the push job calls `LiveScorer` directly |

## Goals

1. **Every reader, every slot.** `score_many` takes about 4 ms per user per slot, so 1M users is
   about an hour of CPU per slot; split it across parallel Cloud Run Job tasks to finish in minutes.
2. **Close the feedback loop:** real opens, clicks and opt-outs update state before the next slot
   and become training data.
3. **Retrain on a schedule**, gated by the quality check, with one-command rollback.
4. **Daily monitoring** of the `daily.csv` columns (pushes, readers reached, section mix,
   most-sent stories, mean P(click)), with alerts when they move sharply.
5. **Reproducibility:** any push traces to its model version, commit, article set and user state.

## Principles

- **Machines are disposable; state lives in storage.** Jobs read data, models and images from
  GCS, Artifact Registry and git, so any machine can be thrown away. No long-lived VM that
  slowly drifts into a configuration nobody can recreate.
- **One feature implementation** for batch and live (above); keep it that way for every new
  feature.
- **Training happens in the cloud** even though it fits on a laptop: user data shouldn't leave
  managed storage, every model must be reproducible from recorded inputs, and retraining has to
  run unattended.
- **Log what was decided, not just what was sent.** Held pushes and their scores are needed to
  evaluate the send policy later.

## Open questions

- **Feedback timing:** how quickly must an open or dismissal affect the next slot? Minutes
  (streaming state updates) or the next day (nightly batch)?
- **Thin days:** in the simulation, weekends had about half the articles but the same number of
  sends. Should the bar depend on the day's volume? (The bandit's doesn't either.)
- **Breaking news:** fixed slots can't react at once; live, editorial alerts
  ([bandit.md](bandit.md#operating-it)) need an editor-triggered send.
- **Opt-outs and cold start:** the return model is a proxy until real opt-outs are logged, and
  new subscribers have no history (XGBoost handles the missing features; a popular-and-diverse
  default may do better).
- **Domain shift:** trained on MSN News 2019, the model ranks NYT articles sensibly but its
  P(click) isn't calibrated; real NYT clicks are needed to retrain and recalibrate.

## Costs

Current demo costs (about $1/month, all on Cloud Run with Neon's free Postgres) are in
[deploy-gcp.md](deploy-gcp.md). At scale, the push jobs scale with readers × slots, and the
usual surprise is a VM left running, so set a billing budget alert.
