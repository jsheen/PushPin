# PushPin

[![CI](https://github.com/jsheen/PushPin/actions/workflows/ci.yml/badge.svg)](https://github.com/jsheen/PushPin/actions/workflows/ci.yml)

Product to promote engagement with push notifications. Currently pushes New York Times article headlines. Balances engagement, discovery, and opt-out risk. Built as final project for AC215: MLOps and LLMOps: Production AI Systems at Harvard. Utilized Anthropic's Claude Code Pro.

## How it works

An XGBoost model trained on the [Microsoft News Dataset (MIND)](https://msnews.github.io/)
predicts `P(click | user, article, time slot)`. Features include past engagement, article
embeddings in a [Chroma](https://www.trychroma.com/) vector database, and Claude-written reader
profiles. A push objective then trades engagement against novelty, and a send policy holds back
pushes for readers at risk of opting out.

Personalization works at two levels. The XGBoost model chooses which article each reader gets,
and the reader chooses how many pushes they get a day (light, medium or heavy).

## Results

On a held-out day (1.22M candidates):

- **Click model:** AUC 0.703. Claude reader profiles add about +0.0006, a borderline gain.
- **Targeting:** the top pick is clicked 16.7% of the time vs. 9.1% for a random pick.
- **Novelty:** friction 0.2 triples the share of never-shown subcategories for about 3 points of CTR.
- **Send policy:** the default sends 0.41 pushes per active user-day at 7.9% CTR, vs. 5.3% for sending everything.

Details: [docs/model.md](docs/model.md) and [docs/analysis.md](docs/analysis.md).

## Real pushes

The notifier scores live NYT RSS articles and pushes to phones through [ntfy](https://ntfy.sh)
or the iOS app. Readers choose a volume: light (2 a day: morning, night), medium (4) or heavy (8).
Tapping a push opens the reader's profile, where reads can become papier-mâché dinosaurs. Every
push and tap is logged as labeled data that MIND lacks. It can also deploy to GCP for about
$1/month.

## Proposed A/B test

| Arm | Pushes | Dinosaurs |
|-----|--------|-----------|
| A | NYT-style: many a day, same for everyone | — |
| B | Selective: reader-chosen volume, model-picked | Off |
| C | Selective: reader-chosen volume, model-picked | On |

A vs. B tests selective pushing; B vs. C tests the dinosaurs. Each reader is assigned to an arm
by a fixed rule (a hash of their id), and the outcomes are clicks per reader-day, return rate and
opt-outs. Per-push CTR alone would favor sending less. Pushes from the same reader aren't
independent, so arms are compared on per-reader numbers. Design: [docs/ab-test.md](docs/ab-test.md).

## Quick start

```bash
./scripts/sync-data.sh                          # MIND from gs://pushpin-data
docker compose run --rm train "pushpin-embed --config configs/train.yml"  # once
docker compose run --rm train "pushpin-profile" # Claude reader profiles (ANTHROPIC_API_KEY in .env)
docker compose run --rm train                   # train
docker compose run --rm objective               # push objective + send policy
docker compose up -d api                        # http://localhost:8000/docs
docker compose up -d scorer notifier cards tunnel   # real pushes (NTFY_TOPIC in .env)
```

## Docs

| Doc | Covers |
|-----|--------|
| [model.md](docs/model.md) | Pipeline, features, evaluation, objective and send policy |
| [analysis.md](docs/analysis.md) | Calibration, return model, exploration, fatigue, LLM ablation |
| [development.md](docs/development.md) | Setup, API, tests, CI, outputs |
| [notifier.md](docs/notifier.md) | Real pushes, cards, push volume, database |
| [ab-test.md](docs/ab-test.md) | The proposed A/B test and its analysis |
| [deploy-gcp.md](docs/deploy-gcp.md), [production.md](docs/production.md) | Cloud deployment |
| [ios-app.md](docs/ios-app.md), [live-scoring.md](docs/live-scoring.md) | iOS backend; request-time scoring |

## Status and contact

These programs are a work in progress, as we work to improve usability, error-catching, and speed of analysis. If you find errors, please contact Justin Sheen at justinksheen (at) gmail (dot) com

Last Update: November 13, 2026
