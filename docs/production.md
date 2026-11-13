# Running pushPin in production: goals and design

Status: **design notes, not built.** Written 2026-11-03 for later review. Companion to
[live-scoring.md](live-scoring.md) (how one request is scored from stored state), which this
document builds on.

## The job in one sentence

At each push slot, decide for every subscribed reader whether to send one push notification and
which article it should be, from the articles published since the last slot and everything we
know about that reader.

Pushes are naturally **batch** work: nothing has to be answered in milliseconds, decisions
happen at set times. So the core of production is a scheduled job, not an always-on server.

## What exists today (and what it stands in for)

| Today, in this repo | Stands in for |
|---|---|
| `pushpin-train` on the MIND log, run by hand (locally or in Docker) | Scheduled retraining in the cloud on our own click logs |
| `LiveScorer` (`live.py`): features from one user's state, identical to batch | The scoring step of the push job |
| `LiveScorer.add_articles`: embed, project text, map to a subcategory | Article ingestion from a news API |
| `pushpin-nyt-sim`: a week of NYT articles, 1,000 MIND users, 3 slots a day | The push job itself, minus real users and delivery |
| User state frozen during the simulation | The feedback loop (opens, clicks, opt-outs update state) |
| `daily.csv`, `summary.json` | Daily monitoring and editorial insight |
| Baked API image (`scripts/build-api-image.sh`) | A versioned, self-contained model artifact |
| `evaluation.min_auc` quality gate | The check that stops a bad model from shipping |

## Target architecture

```
every few minutes   news API (e.g. NYT Times Newswire) ──▶ ingest job
                       embed with MiniLM, project text, map subcategory, publish time
                       ──▶ article catalog + vector index

each push slot      Cloud Scheduler ──▶ push job (container)
  (8:00 13:00 19:00)   load model version + user state + articles since last slot
                       score_many(all subscribed users, in batches)
                       push objective (friction) + send policy (threshold, risk, daily cap)
                       ──▶ delivery service (Firebase Cloud Messaging / APNs) ──▶ phones
                       ──▶ log every decision (sent or held) with its features

continuously        opens, clicks, dismissals, opt-outs ──▶ event log
                       ──▶ user state updates (what LiveScorer.observe does)

daily               report job ──▶ daily.csv-style metrics + alerts (monitoring, editors)

weekly              retrain job on the new logs ──▶ quality gate ──▶ new model version
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

1. **Every reader, every slot.** Scale from 1,000 sampled users to all subscribers. `score_many`
   takes about 4 ms per user per slot, so 1M users is about an hour of CPU per slot; split it
   across parallel tasks (Cloud Run Jobs supports this) to finish in minutes.
2. **Close the feedback loop.** Real opens, clicks and opt-outs update user state before the
   next slot and become training data. Without it, the system never learns from its own pushes.
3. **Retrain on a schedule**, in the cloud, from logged data, with the quality gate deciding
   whether the new model ships, and a one-command rollback to the previous version.
4. **Daily insight and monitoring.** Per day: articles published, pushes sent, readers reached,
   share of articles never pushed, section mix, most-sent stories, mean predicted P(click) and
   novelty (the `daily.csv` columns), plus alerts when they move sharply.
5. **Reproducibility.** Any push can be traced to the model version, code commit, article set and
   user state that produced it.
6. **Cost proportional to use.** Nothing runs between jobs; no always-on VMs.

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

## Development vs. production

| | Development | Production |
|---|---|---|
| Where | One dev VM (or the Mac + Docker) | Scheduled container jobs + managed services |
| Lifetime | **Stop** the VM between sessions (disk and data kept, ~$5/month for 50 GB); delete when done | Created per run, deleted after |
| Data | Synced once from GCS; `gcloud storage rsync` copies only changes | Read from GCS each run (in-region: free, ~seconds for GBs) |
| Training | By hand | Scheduled, gated, versioned |

## Open questions

- **Feedback timing:** how quickly must an open or dismissal affect the next slot? Minutes
  (streaming state updates) or the next day (nightly batch)?
- **Send policy on thin days:** in the simulation, weekends had ~half the articles but the same
  number of sends, so the threshold effectively loosens when the pool shrinks. Should the
  threshold or cap depend on the day's volume?
- **Breaking news:** a fixed three slots can't react to a major story; add an editor-triggered
  slot, or score continuously and send when a story clears a high bar?
- **Opt-outs:** the return model is a proxy (does the user come back within 24 h). Real
  notification opt-outs should replace it once logged.
- **Cold start:** new subscribers have no history; today their features are missing, which
  XGBoost handles, but a dedicated policy (popular + diverse) may do better.
- **Domain shift:** the model is trained on MSN News 2019. On NYT it ranks sensibly but its
  P(click) isn't calibrated; real NYT clicks are needed both to retrain and to recalibrate.

## Rough costs (on-demand list prices; check current GCP pricing)

| Item | Approximate cost |
|---|---|
| Push and ingest jobs (Cloud Run Jobs) | Cents per day at class scale; scales with users × slots |
| Cloud Run API | Effectively free at demo traffic (free tier ~2M requests/month) |
| GCS (a few GB) | ~$0.02 per GB per month |
| Artifact Registry | First 0.5 GB free, then ~$0.10 per GB per month |
| Dev VM `e2-standard-8` | ~$0.27/hour running, ~$0.08 as Spot; disk only when stopped |

Set a billing budget alert; the usual surprise is a VM left running.
