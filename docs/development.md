# Running and developing PushPin

Setup, the batch API, tests and outputs. Short version in the [README](../README.md).

## Repository layout

```
configs/                # train.yml, objective.yml, notifier.yml
scripts/                # sync-data.sh, smoke-api.sh (CI), build-api-image.sh, deploy-gcp.sh
deploy/                 # cloudbuild.yaml, cloudrun/ (scorer, cards, notifier job), k8s/ (scorer at scale, KEDA)
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
outputs in `outputs/nyt-sim/`. See [docs/live-scoring.md](live-scoring.md) and
[docs/production.md](production.md).

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
| `objective/tradeoff.*` | Top-pick CTR and novelty at each friction, model vs. random vs. most recent |
| `objective/pushes.parquet` | The chosen push per impression with its `send` decision |
| `objective/volume.*`, `summary.json` | Send-policy volume vs. CTR by threshold; configured-friction summary |

Also `outputs/analysis/`, `outputs/nyt-sim/` and `outputs/notifier/pushes.db`.
