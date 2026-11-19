# PushPin

[![CI](https://github.com/jsheen/PushPin/actions/workflows/ci.yml/badge.svg)](https://github.com/jsheen/PushPin/actions/workflows/ci.yml)

Aims to push articles users want to engage with. Currently pushes New York Times articles. Balances engagement, discovery, and opt-out risk. Built as final project for AC215: MLOps and LLMOps: Production AI Systems at Harvard. Utilized Anthropic's Claude Code Pro.

## How it works

An XGBoost model trained on the [Microsoft News Dataset (MIND)](https://msnews.github.io/)
predicts `P(click | user, article, time slot)`. Features include past engagement, article
embeddings in a [Chroma](https://www.trychroma.com/) vector database, and Claude-written reader
profiles. A push objective then trades engagement against novelty, and a send decision chooses
whether to push the pick now: a fixed rule today, a contextual bandit in simulation. How many
pushes a reader gets is the policy's call, not a setting.

### Two models, two jobs

A **contextual bandit** learns the send decision instead of the fixed rule (simulation only so
far; live still uses the fixed rule). It doesn't redo XGBoost's job:

| | XGBoost | Contextual bandit |
|---|---|---|
| **Question** | Which article would this reader open? | Is this pick worth sending to this reader *now*? |
| **Learns from** | MIND feed logs (supervised, offline) | Its own send decisions and their outcomes (online) |
| **Target** | P(click), P(return) | return visits − λ·pushes − V·opt-outs |
| **Inputs** | Reader × article: history similarity, embeddings, Claude profiles, article age, slot engagement | XGBoost's outputs plus our push history: recent and ignored pushes, the reader's open rate, their best hours |

The reward counts return visits, not opens: an open by a reader who'd have come back anyway adds
nothing. λ (the cost of a push) and V (the cost of a reader turning notifications off) are prices
set by hand, and how much one push really moves visits isn't known, so the simulation plants it.
Both models can be updated every night. Why the split, rather than one bandit on all the
features, and the rest: [docs/bandit.md](docs/bandit.md).

## Results

On a held-out day (1.22M candidates):

- **Click model:** AUC 0.703. Claude reader profiles add about +0.0006, a borderline gain.
- **Targeting:** the top pick is clicked 16.7% of the time vs. 9.1% for a random pick.
- **Novelty:** friction 0.2 triples the share of never-shown subcategories for about 3 points of CTR.
- **Send policy:** the default sends 0.41 pushes per active user-day at 7.9% CTR, vs. 5.3% for
  sending everything (predicted levels run high on large impressions; see
  [docs/analysis.md](docs/analysis.md#send-policy)).

- **Uplift (causal):** ranking pushes by their doubly robust estimated effect, not by P(click), causes
  2.3× as many extra returns as the current send policy for the same number of pushes, on a
  semi-synthetic NYT simulation with a planted, known effect. The click policy pushes readers who
  would come back anyway. See [docs/uplift.md](docs/uplift.md).
- **Contextual bandit (when to push):** on the same simulation, pushing the latest breaking story
  at all 8 slots loses 0.27–0.71 visits per reader-day vs never pushing, and 59% of readers opt
  out. The bandit gains +0.002 to +0.016 when a push costs λ = 0.02–0.04 visits, where any fixed
  threshold gets ~0, and sends readers at risk of leaving fewer, better pushes. The gains may be
  conservative: only negative carryover (annoyance) is planted, not positive spillover from opened
  pushes. See [docs/bandit.md](docs/bandit.md).

Details: [docs/model.md](docs/model.md) and [docs/analysis.md](docs/analysis.md).

## Real pushes

The notifier scores live NYT RSS articles every two hours (7 to 21) and pushes to phones through
[ntfy](https://ntfy.sh) when the top article clears the send policy (the fixed rule).
Tapping a push opens the reader's profile, where the day's first 3 reads become a shareable
papier-mâché word picture made of their titles ([docs/demo-screens.md](docs/demo-screens.md)).
Every push and tap is logged as labeled data that MIND lacks, for scheduled retraining. It can
also deploy to GCP for about $1/month.

## Proposed A/B test

| Arm | Pushes | Papier-mâché |
|-----|--------|-----------|
| A | NYT-style: many a day, same for everyone | — |
| B | Selective: model-picked, sent when the send rule says so* | Off |
| C | Selective: model-picked, sent when the send rule says so* | On |

\* The fixed rule today, the bandit once validated. Readers are assigned by a hash of their id;
the primary outcome is return visits per reader-day, with clicks and opt-outs alongside, compared
per reader. Design: [docs/ab-test.md](docs/ab-test.md).

## Quick start

```bash
./scripts/sync-data.sh                          # MIND from gs://pushpin-data
docker compose run --rm train "pushpin-embed --config configs/train.yml"  # once
docker compose run --rm train "pushpin-profile" # Claude reader profiles (ANTHROPIC_API_KEY in .env)
docker compose run --rm train                   # train
docker compose run --rm objective               # push objective + send policy
docker compose up -d api                        # http://localhost:8000/docs
docker compose up -d scorer notifier cards tunnel   # real pushes (NTFY_TOPIC in .env)
docker compose run --rm uplift                  # causal ML benchmark (needs outputs/nyt-sim)
uv run pushpin-bandit --grid                    # contextual bandit (needs outputs/nyt-sim-8slot; docs/bandit.md)
```

## Docs

| Doc | Covers |
|-----|--------|
| [model.md](docs/model.md) | Pipeline, features, evaluation, objective and send policy |
| [analysis.md](docs/analysis.md) | Calibration, return model, send policy, the bandit's objective, fatigue, tuning |
| [development.md](docs/development.md) | Setup, API, tests, CI, outputs |
| [notifier.md](docs/notifier.md) | Real pushes, cards, how many pushes, database |
| [ab-test.md](docs/ab-test.md) | The proposed A/B test and its analysis |
| [bandit.md](docs/bandit.md) | Contextual bandit for the send decision: reward, planted world, results |
| [uplift.md](docs/uplift.md) | Causal ML: doubly robust estimates of what a push causes |
| [deploy-gcp.md](docs/deploy-gcp.md), [production.md](docs/production.md) | Cloud deployment |
| [live-scoring.md](docs/live-scoring.md) | Request-time scoring |
| [demo-screens.md](docs/demo-screens.md) | What the phone shows: push, tapped push, demo profile, sharing a picture |

## Status and contact

These programs are a work in progress, as we work to improve usability, error-catching, and speed of analysis. If you find errors, please contact Justin Sheen at justinksheen (at) gmail (dot) com

Last Update: November 19, 2026
