# PushPin

[![CI](https://github.com/jsheen/PushPin/actions/workflows/ci.yml/badge.svg)](https://github.com/jsheen/PushPin/actions/workflows/ci.yml)

Push notification engine that could be utilized by The New York Times product. Balances engagement, discovery, and opt-out risk. Built as final project for AC215: MLOps and LLMOps: Production AI Systems at Harvard. Utilized Anthropic's Claude Code Pro.

## Overview

PushPin predicts whether a reader will click on a given article if it is pushed to them in a
given time slot, `P(click | user, article, time slot)`. Slots are:

| Slot      | Hours (local) |
|-----------|---------------|
| morning   | 05:00–11:59   |
| afternoon | 12:00–16:59   |
| evening   | 17:00–04:59   |

The model is an XGBoost
classifier trained on the [Microsoft News Dataset (MIND)](https://msnews.github.io/), with
article abstracts embedded into a local [Chroma](https://www.trychroma.com/) vector database for
retrieval features and Claude-written reader profiles. A push objective and send policy on top
decide what to send and whether to send it. It can also send real pushes to your phone; see
[docs/notifier.md](docs/notifier.md).

| Doc | Covers |
|-----|--------|
| [docs/analysis.md](docs/analysis.md) | Push objective trade-off, calibration, return model, send policy, exploration and fatigue analyses, LLM profile ablation |
| [docs/notifier.md](docs/notifier.md) | Real pushes from live NYT RSS via ntfy, the card server, the labeled push database |
| [docs/deploy-gcp.md](docs/deploy-gcp.md) | Mixed GKE + Cloud Run deployment, `scripts/deploy-gcp.sh`, cost |
| [docs/live-scoring.md](docs/live-scoring.md) | What request-time scoring would still need |
| [docs/production.md](docs/production.md) | How this would run in production end to end |

## Pipeline

```
gs://pushpin-data ──sync-data.sh──▶ data/
                                     │
          news.tsv ──pushpin-embed──▶ data/chroma   (MiniLM abstract embeddings)
                                     │
 behaviors.tsv + chroma ──pushpin-profile──▶ data/llm + chroma  (Claude click/skip themes per user-day)
                                     │
 behaviors.tsv + news.tsv + entity embeddings + chroma + profiles
                                     │
                              pushpin-train            (train container)
                                     │
                                     ▼
          outputs/<run>/  (click model + calibration, return model, metrics, predictions.parquet)
                                     │
                            pushpin-objective          (objective container)
                                     │
                                     ▼
          outputs/<run>/objective/  (engagement-vs-novelty tradeoff, send policy, chosen pushes)

          outputs/<run>/predictions.parquet ──▶ pushpin-api  (api container, :8000)
```

1. **Sync** — `scripts/sync-data.sh` pulls MIND from the project's GCS bucket.
2. **Embed** — `pushpin-embed` encodes each abstract (title if empty) with `all-MiniLM-L6-v2`
   into Chroma (`data/chroma`, collection `abstracts`).
3. **Profile** — `pushpin-profile` (RAG + LLM): for each user-day, retrieves recent clicks and the
   skipped articles nearest to each, and has Claude Haiku name 2-4 themes the user clicks and
   2-4 they skip (Message Batches API). Themes are embedded into a `profiles` collection.
4. **Train** — `pushpin-train` builds features, trains XGBoost with early stopping, calibrates it,
   fits a return model (churn proxy), and evaluates on a held-out day.
5. **Objective** — `pushpin-objective` picks one push per impression (engagement vs. novelty) and
   decides whether to send it. Its own lightweight image; no retraining needed to change it.
6. **Serve** — `pushpin-api` exposes scores and push decisions over HTTP.
7. **Analyze** — `pushpin-exploration` and `pushpin-fatigue` study behavior across the whole log.

**Time-based split.** Impressions before `split.fit_end` only fit complement statistics and user
history; `[fit_end, train_end)` trains (latest `model.valid_fraction` held out for early
stopping); `train_end` onward is test. Every per-user feature uses only strictly earlier impressions.

### Features

Defined in `src/pushpin/features.py`:

| Group | Features | Idea |
|-------|----------|------|
| Time-slot engagement | `slot`, `slot_prior_impr`, `slot_prior_clicks`, `slot_engaged_rate`, `slot_click_share` | Past engagement in this slot, shrunk toward the global rate |
| Similarity | `sim_text`, `sim_entity`, `subcat_affinity` | Candidate vs. history: TF-IDF/SVD text, TransE entities, subcategory mix |
| Complementarity | `complement_score` | Alibaba-style `s_ab = p_ab − q_ab` over subcategories ([background](https://eugeneyan.com/writing/push/)): what users read *next* |
| Push fatigue | `slot_skipped_72h`, `slot_skip_rate_72h`, `impr_last_24h`, `hours_since_last_impr`, `hours_since_last_slot_impr` | Recent skips and volume |
| Embedding retrieval | `emb_sim_mean`, `emb_sim_max`, `emb_sim_top5`, `emb_n_close`, `emb_retrieval_rank` | Candidate vs. history in abstract-embedding space |
| Article recency | `article_age_hours` | Hours since first shown (MIND lacks publish time) |
| User depth | `user_history_len`, `user_prior_impr` | How much we know about the user |
| Subcategory skips | `subcat_engaged_rate`, `subcat_prior_skips` | The user's shrunk click rate and skip count in this subcategory |
| Click/skip contrast | `emb_sim_clicked_max`, `emb_sim_skipped_max`, `emb_contrast` | Closest clicked vs. closest skipped article |
| LLM reader profile | `llm_sim_click`, `llm_sim_skip`, `llm_contrast` | Best-matching Claude click theme vs. skip theme |

### Evaluation and results

`metrics.json` reports overall AUC, log loss, CTR, **group AUC** (mean AUC within impressions
that contain both a click and a non-click — MIND's standard metric), and AUC by time slot.

Bootstrap confidence intervals resample whole impressions, since candidates shown together are
correlated (replicates and level under `evaluation` in `configs/train.yml`; `bootstrap_samples: 0`
skips them). User- and article-level correlation isn't resampled, so the intervals are probably a
little narrow.

**Quality gate.** If test AUC falls below `evaluation.min_auc` (0.644, the lower bound of the
baseline's 95% CI), `pushpin-train` exits with an error after saving its artifacts, so a regression
can't pass silently but the failed run can still be inspected.

Test day 2019-11-14 (1.22M candidates, 30,270 impressions), 95% CIs:

| Run | AUC | Group AUC |
|-----|-----|-----------|
| `baseline-no-emb` | 0.646 (0.644–0.649) | 0.617 (0.614–0.620) |
| `recency-depth-no-emb` (+ recency, user depth) | 0.656 (0.653–0.659) | 0.622 (0.619–0.625) |
| `ablation-no-llm` (all features but LLM) | 0.7026 (0.700–0.705) | 0.6757 (0.673–0.679) |
| `ablation-with-llm` (all features) | **0.7032** (0.701–0.706) | **0.6766** (0.673–0.680) |

Adding recency and user depth lifts AUC by 0.01; `article_age_hours` ranks third by gain. For the
LLM features, `pushpin-compare` scores both runs on the same resampled impressions, so its CI is
for the difference (1,000 paired replicates):

| Metric | With − without LLM | 95% CI | P(no gain) |
|--------|--------------------|--------|------------|
| AUC | +0.0006 | −0.0000 to +0.0013 | 0.027 |
| Group AUC | +0.0009 | −0.0003 to +0.0021 | 0.082 |

The LLM themes add at most about a thousandth of AUC: borderline for AUC, not significant for
group AUC. They rank 23rd, 24th and 27th of 30 by gain, well behind `emb_sim_clicked_max` and
`emb_contrast`, which already use the same clicked/skipped evidence. Details in
[docs/analysis.md](docs/analysis.md#llm-reader-profiles).

Feature importance for the full model, colored by feature group:

![Click model feature importance](docs/feature_importance.png)

### Push objective and send policy

```
score = (1 − friction) · engagement + friction · novelty,     friction ∈ [0, 1]
```

Engagement is `P(click)`; novelty is 1 − the user's prior exposure share in the candidate's
subcategory. Both are percentile-ranked within the impression so friction is a true weight. The
top pick is sent only if

```
calibrated P(click) ≥ threshold + risk_penalty · (1 − P(return))
```

and the user is under `daily_cap` (defaults 0.07, 0.04, 2 in `configs/objective.yml`).

**Trade-off.** Clicks are rare: 3.8% of candidates are clicked, a random pick per impression
clicks 9.1% (the fair baseline, since MIND only logs impressions with a click), and the model's
top pick clicks 16.7%. Sweeping friction (`recency-depth-no-emb`; dashed = random engagement):

![Push objective trade-off](docs/tradeoff.png)

| Friction | CTR of top pick | …with random engagement | Top pick from a never-shown subcategory |
|----------|-----------------|-------------------------|-----------------------------------------|
| 0 | 16.7% | 9.1% | 11% |
| 0.1 | 15.4% | 8.8% | 23% |
| 0.2 (default) | 13.9% | 8.5% | 37% |
| 0.3 | 12.6% | 8.2% | 48% |
| 0.5 | 11.0% | 8.0% | 64% |
| 1 | 8.1% | 8.1% | 87% |

Low friction buys novelty cheaply: 0 → 0.2 more than triples the never-shown share for about 3
points of CTR.

**Send policy.** On impressions with 50+ candidates:

| Threshold | Pushes per active user-day | CTR of pushes sent | Predicted CTR |
|-----------|----------------------------|--------------------|---------------|
| 0 (send all, cap 2) | 1.15 | 5.3% | 8.1% |
| 0.05 | 0.72 | 6.4% | 9.4% |
| **0.07 (default)** | **0.41** | **7.9%** | 10.9% |
| 0.10 | 0.14 | 10.3% | 13.9% |

Raising the threshold trades volume for quality as intended; the model over-predicts the level on
this slice (an artifact of MIND's impression sizes, explained in the analysis doc). At the
default, low-risk users get 0.82 pushes/day at 9.3% CTR and high-risk users 0.15/day at 5.3%.

![Send policy](docs/volume.png)

Other findings:

- **Calibration.** Platt scaling fixes overconfidence at the top (ECE 0.35 → 0.24 pts; top bin
  13.2% → 11.1% predicted vs. 10.9% observed) without changing rankings. The top is where the
  send threshold operates, so that's where it matters.
- **Return model.** P(another impression within 24 h), AUC 0.743, stands in for opt-out risk; it
  sets *how much* to send each user, not *what*.
- **Exploration.** A first click on a never-shown subcategory goes with a 2.4× higher re-click rate.
- **Fatigue.** Engagement is ~2% below a user's norm after 5+ impressions in a day, with no
  cumulative weekly effect, supporting a daily cap.

Plots, tables, caveats and what real opt-out logging would add: [docs/analysis.md](docs/analysis.md).

## Repository layout

```
configs/                # train.yml, objective.yml, notifier.yml
scripts/                # sync-data.sh, smoke-api.sh (CI), build-api-image.sh, deploy-gcp.sh
deploy/                 # cloudbuild.yaml, cloudrun/ (cards, notifier job), k8s/ (scorer, KEDA)
src/pushpin/
  data.py embed.py vectordb.py      # MIND loaders, slots; abstract embeddings; Chroma wrapper
  profile.py                        # RAG + Claude reader profiles
  features.py train.py compare.py   # features; training; paired bootstrap of two runs
  calibration.py risk.py            # Platt scaling, ECE; return model
  objective.py                      # push objective + send policy
  exploration.py fatigue.py         # behavior analyses
  api.py live.py                    # FastAPI app; request-time scoring + parity replay
  nyt.py notifier.py cards.py store.py  # NYT sim; real pushes; card server; SQLite/Postgres store
  plots.py                          # all charts
tests/                  # pytest suite on synthetic MIND-style data
Dockerfile, Dockerfile.objective, Dockerfile.api   # train / objective / API images
docker-compose.yml, docker-shell.sh
docs/                   # design notes and README images
data/, outputs/         # git-ignored
```

## Getting started

Needs Docker (Compose v2) and the [Google Cloud CLI](https://cloud.google.com/sdk/docs/install)
with access to `gs://pushpin-data` (`gcloud auth login`).

```bash
./scripts/sync-data.sh                                            # pull data into ./data
docker compose run --rm train "pushpin-embed --config configs/train.yml"   # once, or when news.tsv changes
docker compose run --rm train "pushpin-profile --estimate"        # LLM profiles: cost estimate only
docker compose run --rm train "pushpin-profile"                   # all user-days (ANTHROPIC_API_KEY in .env)
docker compose run --rm train                                     # train with configs/train.yml
docker compose run --rm objective                                 # push objective with configs/objective.yml
docker compose up -d api                                          # serve on http://localhost:8000
docker compose run --rm shell                                     # interactive shell (or ./docker-shell.sh)
```

There are three images: `train` (heavy stack, `.[train]`), `objective` (core deps only) and `api`
(core + FastAPI). They share runs through the mounted `outputs/` folder. `src/`, `configs/`,
`data/` and `outputs/` are bind-mounted, so only `pyproject.toml` changes need a rebuild.
Training fails with `run pushpin-embed first` / `run pushpin-profile first` if those are missing.

**Memory.** On an 8 GB Mac, Docker's VM has 4 GB; full training peaks at ~3.7 GB (exit code 137
means it was killed; raise Settings → Resources → Memory).

**Options.** Drop features for an ablation, or re-rank at another friction without retraining:

```bash
pushpin-train --config configs/train.yml \
  --exclude emb_sim_mean emb_sim_max emb_sim_top5 emb_n_close emb_retrieval_rank \
  --output-dir outputs/recency-depth-no-emb          # no vector database needed
pushpin-compare outputs/ablation-no-llm outputs/ablation-with-llm   # paired bootstrap of two runs
docker compose run --rm objective "pushpin-objective --run-dir outputs/recency-depth-no-emb --friction 0.3"
docker compose run --rm train "pushpin-exploration"   # outputs/analysis/exploration.*
docker compose run --rm train "pushpin-fatigue"       # outputs/analysis/fatigue.*
```

## API

```bash
docker compose up -d api                                  # serves outputs/recency-depth-no-emb
PUSHPIN_RUN_DIR=outputs/full docker compose up -d api     # another run
curl -X POST localhost:8000/push -H 'content-type: application/json' \
  -d '{"impression_id": 152636, "friction": 0.2}'
```

Docs at <http://localhost:8000/docs>.

| Endpoint | Does |
|----------|------|
| `GET /health` | Liveness and loaded run |
| `GET /model` | Run name, excluded features, test metrics |
| `GET /users/{user_id}/impressions` | A user's impressions, to find ids to score |
| `POST /score` | `P(click)` and novelty for an impression's candidates |
| `POST /push` | The objective's pick at `friction`, its ranks, the `send` decision, and the runners-up |
| `GET /metrics` | Prometheus: requests, errors, latency, send rate, `p_click`/novelty histograms, live model |

`POST` endpoints accept an optional `news_ids` subset. Friction outside [0, 1] returns 422;
unknown impressions or articles return 404. The send decision uses `configs/objective.yml`; the
caller enforces the daily cap.

**Monitoring.**

| Question | How |
|----------|-----|
| Is it up? | Docker `HEALTHCHECK` on `/health` every 30s; Compose restarts the process if it exits |
| Erroring or slow? | `pushpin_requests_total{endpoint,status}` and `pushpin_request_seconds` on `/metrics` |
| Model behaving? | Send/hold counts, `p_click` and novelty histograms, `pushpin_model_info{run}` on `/metrics` |
| What happened on a request? | One JSON log line per request (`docker compose logs api`) |

A shift in send rate or `p_click` with no change in traffic is the early warning that something
upstream changed. Accuracy needs real push outcomes logged and joined back.

**Production image.** `scripts/build-api-image.sh ablation-with-llm` bakes one run (~20 MB) into
`pushpin-api:ablation-with-llm` and smoke-tests it; deploy by tag, roll back by redeploying the
previous tag.

**Request-time scoring.** The API serves precomputed test-day scores. `pushpin-live-parity
outputs/<run> --users 100` rescores impressions from each user's earlier events only
(`src/pushpin/live.py`) and matches batch exactly at ~110 ms per request. `pushpin-nyt-sim
outputs/ablation-with-llm` uses it to push a week of real NYT articles (`data/nyt-recent.csv`) to
1,000 MIND users: 918 articles, 4,411 pushes sent (0.63 per user-day), 4.3 ms per request;
outputs in `outputs/nyt-sim/`. See [docs/live-scoring.md](docs/live-scoring.md) and
[docs/production.md](docs/production.md).

## Real pushes and cloud deployment

The `notifier` service scores today's NYT RSS news with the model and pushes the top pick to your
phone through [ntfy](https://ntfy.sh) at 8, 12, 17 and 21 New York time. Tapping opens a card
(Read on NYT / Save / pushes a day, plus a profile page with favorites and articles read) served through a Cloudflare quick tunnel, and every candidate and reader
interaction is logged, which yields the labeled push data MIND lacks.

```
NYT RSS feeds ──▶ notifier ──HTTP──▶ scorer  (model + MIND user state, from the NYT sim)
                   │
   ntfy.sh ◀───────┤          cards :8090 ◀── tunnel (https://….trycloudflare.com) ◀── a reader taps
   (push to phones)│            │                                                    Save / pushes a day
                   ▼            ▼
        outputs/notifier/pushes.db, or Postgres via DATABASE_URL
```

With `NTFY_TOPIC=<your topic>` in `.env` and the ntfy app subscribed to it:

```bash
docker compose up -d scorer                  # loads the model and MIND state; about 2-5 min
docker compose run --rm notifier "python -m pushpin.notifier --test"            # test push
docker compose run --rm notifier "python -m pushpin.notifier --once --dry-run"  # score one slot, send nothing
docker compose up -d notifier cards tunnel   # the schedule, the card server, its public address
```

Pushes are scored for a brand-new user (no history, a cold start; `user_id` in
`configs/notifier.yml` picks a MIND persona instead), and interactions are stored but not yet
fed back. Setup, the database tables and example queries are in
[docs/notifier.md](docs/notifier.md).

The same services deploy to GCP, each where it fits:

| Piece | Runs on |
|-------|---------|
| Scorer | GKE Autopilot behind an internal load balancer, scaled by KEDA |
| Card server | Cloud Run service (public HTTPS replaces the tunnel) |
| Notifier slot | Cloud Run job triggered by Cloud Scheduler |
| Database | Cloud SQL Postgres |
| Images | Artifact Registry, built by Cloud Build |

`scripts/deploy-gcp.sh {setup,images,deploy,status,pause,resume,down}` is a dry run unless
`--apply`. About $50–60/month while running; not yet applied to a real project. See
[docs/deploy-gcp.md](docs/deploy-gcp.md).

## Development

```bash
uv sync --extra train --extra api --extra dev && ruff check src tests && uv run pytest
```

The suite runs on a tiny synthetic MIND-style dataset (`tests/conftest.py`), with no real data,
and covers metrics and the quality gate, calibration, return labels, the objective and send
policy, no-lookahead guarantees, the analyses, an end-to-end train → objective run, every API
endpoint, and the notifier store on SQLite and Postgres
(`PUSHPIN_TEST_DATABASE_URL`).

- **CI** (`.github/workflows/ci.yml`, every push and PR): `ruff`, `pytest`, builds of all three
  images, and `scripts/smoke-api.sh` against a fixture run.
- **Release** (`release.yml`, on a version tag like `git tag v0.2.0 && git push origin v0.2.0`):
  publishes `ghcr.io/jsheen/pushpin-{train,objective,api}`.

Training on real data isn't in CI; the quality gate guards it.

### Outputs

Each run writes to `outputs/<run>/` (`output.dir` or `--output-dir`, default `outputs/full`):

| File | Contents |
|------|----------|
| `xgb_click.json` | Trained XGBoost click model |
| `xgb_return.json` | Return model (P(return within 24 hours)) |
| `metrics.json` | Test metrics, bootstrap CIs, calibration (Platt parameters, ECE, Brier), return-model AUC, best iteration |
| `predictions.parquet` | Test-set candidates: impression, user, slot, article, label, `p_click` (calibrated), `p_click_raw`, `novelty`, `p_return` |
| `feature_importance.*`, `return_feature_importance.*` | Gain per feature and charts |
| `calibration.png` | Reliability diagram, raw vs. calibrated |
| `config.yml` | Resolved config for the run |
| `objective/tradeoff.*` | Top-pick CTR and novelty at each friction, model vs. random |
| `objective/pushes.parquet` | The chosen push per impression with its `send` decision |
| `objective/volume.*`, `summary.json` | Send-policy volume vs. CTR by threshold; configured-friction summary |

Also `outputs/analysis/`, `outputs/nyt-sim/` and `outputs/notifier/pushes.db`.

## Status and contact

These programs are a work in progress, as we work to improve usability, error-catching, and speed of analysis. If you find errors, please contact Justin Sheen at justinksheen (at) gmail (dot) com

Last Update: November 12, 2026
