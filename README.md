# PushPin

[![CI](https://github.com/jsheen/PushPin/actions/workflows/ci.yml/badge.svg)](https://github.com/jsheen/PushPin/actions/workflows/ci.yml)

Aims to push articles users want to engage with. Currently pushes New York Times articles. Balances engagement, discovery, and opt-out risk. Built as final project for AC215: MLOps and LLMOps: Production AI Systems at Harvard. Utilized Anthropic's Claude Code Pro.

## How it works

An XGBoost model trained on the [Microsoft News Dataset (MIND)](https://msnews.github.io/)
predicts `P(click | user, article, time slot)`. Features include past engagement, article
embeddings in a [Chroma](https://www.trychroma.com/) vector database, and Claude-written reader
profiles. A push objective then trades engagement against novelty, and a send policy holds back
pushes for readers at risk of opting out.

The XGBoost model chooses which article each reader gets, and the send policy decides whether
to push it, so how many pushes a reader gets a day is the policy's call, not a setting.

## Results

On a held-out day (1.22M candidates):

- **Click model:** AUC 0.703. Claude reader profiles add about +0.0006, a borderline gain.
- **Targeting:** the top pick is clicked 16.7% of the time vs. 9.1% for a random pick.
- **Novelty:** friction 0.2 triples the share of never-shown subcategories for about 3 points of CTR.
- **Send policy:** the default sends 0.41 pushes per active user-day at 7.9% CTR, vs. 5.3% for sending everything.
  The model over-predicts here (8.1% predicted vs. 5.3% actual when sending everything) because
  this is evaluated on impressions with 50+ candidates, and calibration was fit across all
  candidates at population scale, not per slice. MIND only logs impressions with a click, so small
  impressions click far more and large ones far less than the average the model learned.
  Raising the threshold still sends fewer, better pushes (actual CTR rises from 5.3% to 10.3%);
  only the predicted levels are too high.

- **Uplift (causal):** ranking pushes by their doubly robust estimated effect, not by P(click), causes
  2.3× as many extra returns as the current send policy for the same number of pushes, on a
  semi-synthetic NYT simulation with a planted, known effect. The click policy pushes readers who
  would come back anyway. See [docs/uplift.md](docs/uplift.md).

Details: [docs/model.md](docs/model.md) and [docs/analysis.md](docs/analysis.md).

## Real pushes

The notifier scores live NYT RSS articles every two hours (7 to 21) and pushes to phones through
[ntfy](https://ntfy.sh) when the top article clears the send policy.
Tapping a push opens the reader's profile. The first 3 articles they read each day make a small
papier-mâché word art (a snowman, a heart, a dinosaur...) out of the articles' titles and
abstracts. Tapping one turns it into a sticker to share in any chat. Everyone gets the same shape
on a given day; the words they read make it theirs. Every push and tap is logged as labeled data
that MIND lacks. The plan is to retrain the model each week on these logs. It can also deploy to
GCP for about $1/month.

## Proposed A/B test

| Arm | Pushes | Papier-mâché |
|-----|--------|-----------|
| A | NYT-style: many a day, same for everyone | — |
| B | Selective: model-picked, sent only when it clears the send policy | Off |
| C | Selective: model-picked, sent only when it clears the send policy | On |

A vs. B tests selective pushing; B vs. C tests the papier-mâché pictures. Each reader is assigned to an arm
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
| [analysis.md](docs/analysis.md) | Calibration, return model, exploration, fatigue, LLM ablation, hyperparameter tuning |
| [development.md](docs/development.md) | Setup, API, tests, CI, outputs |
| [notifier.md](docs/notifier.md) | Real pushes, cards, how many pushes, database |
| [ab-test.md](docs/ab-test.md) | The proposed A/B test and its analysis |
| [uplift.md](docs/uplift.md) | Causal ML: doubly robust estimates of what a push causes, per reader (potential outcomes, randomized holdout, DR-learner, GATES, off-policy evaluation) |
| [deploy-gcp.md](docs/deploy-gcp.md), [production.md](docs/production.md) | Cloud deployment |
| [live-scoring.md](docs/live-scoring.md) | Request-time scoring |
| [demo-screens.md](docs/demo-screens.md) | What the phone shows: push, tapped push, demo profile, sharing a picture |

## Status and contact

These programs are a work in progress, as we work to improve usability, error-catching, and speed of analysis. If you find errors, please contact Justin Sheen at justinksheen (at) gmail (dot) com

Last Update: November 13, 2026
