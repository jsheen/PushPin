# Running PushPin in production: goals and design

Status: **design notes** for running at publisher scale (written 2026-11-03, updated 2026-11-19).
What's deployed for the demo, with costs, is in [deploy-gcp.md](deploy-gcp.md); how one request
is scored is in [live-scoring.md](live-scoring.md).

## The job in one sentence

At each push slot, decide for every subscribed reader whether to send one push notification and
which article it should be, from the day's unpushed articles and everything we know about that
reader.

Pushes are naturally **batch** work: nothing has to be answered in milliseconds, decisions
happen at set times. So the core of production is a scheduled job, not an always-on server.

## What exists today (and what it stands in for)

| Today, in this repo | Stands in for |
|---|---|
| `pushpin-train` on the MIND log, run by hand (locally or in Docker) | Scheduled retraining in the cloud on our own click logs |
| `LiveScorer` (`live.py`): features from one user's state, identical to batch | The scoring step of the push job |
| `LiveScorer.add_articles`: embed, project text, map to a subcategory | Article ingestion from a news API |
| `pushpin-nyt-sim` / the push job: a week of NYT articles, 1,000 MIND users, 3 or 8 slots a day | The push job itself, minus real users and delivery |
| The notifier sending each reader's past pushes and taps with every request | The feedback loop (opens, clicks, opt-outs update state) |
| The contextual bandit on the simulation (`bandit.py`) | A learned send decision |
| `daily.csv`, `summary.json` | Daily monitoring and editorial insight |
| Baked API image (`scripts/build-api-image.sh`) | A versioned, self-contained model artifact |
| `evaluation.min_auc` quality gate | The check that stops a bad model from shipping |

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
| User state | Firestore or Memorystore (Redis), keyed by user | Today it's in memory (`LiveScorer.log`); must persist between job runs |
| Article catalog + vectors | GCS (or a database) + in-memory exact search | 50k articles ≈ 80 MB of vectors: exact numpy search takes ~2 ms, no vector DB needed at this size |
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
- **One feature implementation.** Batch training and live scoring call the same
  `features_for`; the parity test (`pushpin-live-parity`) guards it. Keep it that way for every
  new feature.
- **Training happens in the cloud** even though it fits on a laptop: user data shouldn't leave
  managed storage, every model must be reproducible from recorded inputs, and retraining has to
  run unattended.
- **Log what was decided, not just what was sent.** Held pushes and their scores are needed to
  evaluate the send policy later.

## Open questions

- **Feedback timing:** how quickly must an open or dismissal affect the next slot? Minutes
  (streaming state updates) or the next day (nightly batch)?
- **Send policy on thin days:** in the simulation, weekends had ~half the articles but the same
  number of sends, so the threshold effectively loosens when the pool shrinks. Should the
  threshold or cap depend on the day's volume? (The bandit's threshold depends on the pick and
  the reader's state, not on the pool's size, so this question carries over to it.)
- **Breaking news:** fixed slots can't react to a major story at once. The bandit sim has
  editorial alerts that bypass the policy ([bandit.md](bandit.md#operating-it)); live, that
  needs an editor-triggered send, or continuous scoring with a high bar.
- **Opt-outs:** the return model is a proxy (does the user come back within 24 h). Real
  notification opt-outs should replace it once logged.
- **Cold start:** new subscribers have no history; today their features are missing, which
  XGBoost handles, but a dedicated policy (popular + diverse) may do better.
- **Domain shift:** the model is trained on MSN News 2019. On NYT it ranks sensibly but its
  P(click) isn't calibrated; real NYT clicks are needed both to retrain and to recalibrate.

## Costs

Current demo costs (about $1/month, all on Cloud Run with Neon's free Postgres) are in
[deploy-gcp.md](deploy-gcp.md). At scale, the push jobs scale with readers × slots, and the
usual surprise is a VM left running, so set a billing budget alert.
