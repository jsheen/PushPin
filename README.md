# pushNYT

[![CI](https://github.com/jsheen/pushNYT/actions/workflows/ci.yml/badge.svg)](https://github.com/jsheen/pushNYT/actions/workflows/ci.yml)

Push notification engine that could be utilized by The New York Times product. Balances engagement, discovery, and opt-out risk. Built as final project for AC215: MLOps and LLMOps: Production AI Systems at Harvard. Utilized Anthropic's Claude Code Pro.

## Overview

pushNYT predicts whether a reader will click on a given article if it is pushed to them in a
given time slot, `P(click | user, article, time slot)`. Slots are:

| Slot      | Hours (local) |
|-----------|---------------|
| morning   | 05:00–11:59   |
| afternoon | 12:00–16:59   |
| evening   | 17:00–04:59   |

The model is an XGBoost classifier trained on the
[Microsoft News Dataset (MIND)](https://msnews.github.io/), with article abstracts embedded into
a local [Chroma](https://www.trychroma.com/) vector database to power retrieval-based features.
To get its pushes on your own phone, see [Real pushes to your phone](#real-pushes-to-your-phone).

## Pipeline

```
gs://pushnyt-data ──sync-data.sh──▶ data/
                                     │
          news.tsv ──pushnyt-embed──▶ data/chroma   (MiniLM abstract embeddings)
                                     │
 behaviors.tsv + chroma ──pushnyt-profile──▶ data/llm + chroma  (Claude click/skip themes per user-day)
                                     │
 behaviors.tsv + news.tsv + entity embeddings + chroma + profiles
                                     │
                              pushnyt-train            (train container)
                                     │
                                     ▼
          outputs/<run>/  (click model + calibration, return model, metrics, predictions.parquet)
                                     │
                            pushnyt-objective          (objective container)
                                     │
                                     ▼
          outputs/<run>/objective/  (engagement-vs-novelty tradeoff, send policy, chosen pushes)

          outputs/<run>/predictions.parquet ──▶ pushnyt-api  (api container, :8000)
```

1. **Sync data** — `scripts/sync-data.sh` pulls the MIND files from the project's GCS bucket.
2. **Embed** — `pushnyt-embed` encodes every article abstract (falling back to the title when
   the abstract is empty) with `sentence-transformers/all-MiniLM-L6-v2` and stores the vectors
   in Chroma (`data/chroma`, collection `abstracts`).
3. **Profile** — `pushnyt-profile` is the RAG + LLM step. For each user active on a train or test
   day, it retrieves their most recent clicks and, for each one, the skipped articles nearest to it
   in embedding space (from that user's earlier impressions only). Claude Haiku then names 2-4
   themes of what the user clicks and 2-4 of what they skip, through the Message Batches API. Each
   theme is embedded on its own with the same MiniLM model into a `profiles` Chroma collection,
   and also saved as `data/llm/theme_vectors_{click,skip}.npy` for training to read. See
   [LLM reader profiles](#llm-reader-profiles).
4. **Train** — `pushnyt-train` builds features, trains XGBoost with early stopping, calibrates its
   probabilities, fits a return model (a churn proxy), evaluates on a held-out day, and writes
   artifacts to `outputs/<run>/`, including per-candidate test-set predictions in `predictions.parquet`.
5. **Objective** — `pushnyt-objective` picks one push per impression with the push objective below
   (engagement vs. novelty, set by a friction parameter), then decides whether to send it (a
   calibrated, risk-adjusted threshold and a daily cap). It runs in its own lightweight image, so
   both can be changed in seconds without retraining.
6. **Serve** — `pushnyt-api` exposes scores and push decisions over HTTP (see [API](#api)).
7. **Analyze** — `pushnyt-exploration` and `pushnyt-fatigue` look at user behavior over the whole
   log (do novel clicks stick? does heavy exposure tire users?) and write to `outputs/analysis/`.

### Time-based split

All splits are by time so the model never sees the future:

- impressions before `split.fit_end` are used only to fit complement statistics and user history;
- `[fit_end, train_end)` trains the model (the latest `model.valid_fraction` is held out for early stopping);
- impressions at or after `split.train_end` form the test set.

Every per-user temporal feature uses only impressions strictly before the current one.

### Features

Defined in `src/pushnyt/features.py`, in ten groups:

| Group | Features | Idea |
|-------|----------|------|
| Time-slot engagement | `slot`, `slot_prior_impr`, `slot_prior_clicks`, `slot_engaged_rate`, `slot_click_share` | How the user has engaged in this slot before; rates are shrunk toward the global rate |
| Similarity | `sim_text`, `sim_entity`, `subcat_affinity` | Candidate vs. reading history via TF-IDF/SVD text vectors, TransE entity vectors, and subcategory mix |
| Complementarity | `complement_score` | Alibaba-style `s_ab = p_ab − q_ab` over subcategories ([background](https://eugeneyan.com/writing/push/)) — what users read *next*, not what is merely similar |
| Push fatigue | `slot_skipped_72h`, `slot_skip_rate_72h`, `impr_last_24h`, `hours_since_last_impr`, `hours_since_last_slot_impr` | Recent skips and overall notification volume |
| Embedding retrieval | `emb_sim_mean`, `emb_sim_max`, `emb_sim_top5`, `emb_n_close`, `emb_retrieval_rank` | Candidate vs. history in abstract-embedding space, plus its rank among the user's nearest neighbors in Chroma |
| Article recency | `article_age_hours` | Hours since the article was first shown in any impression — a proxy for publish time, which MIND lacks. Articles in a pre-period click history are capped at the start of the log, so their age is a lower bound |
| User depth | `user_history_len`, `user_prior_impr` | How much we know about the user: clicks in their pre-period history (0 = cold start, long-time readers are high) and impressions logged so far across all slots |
| Subcategory skips | `subcat_engaged_rate`, `subcat_prior_skips` | The user's click rate in the candidate's subcategory over earlier impressions, shrunk toward that subcategory's global rate, and how often they have skipped it |
| Click/skip contrast | `emb_sim_clicked_max`, `emb_sim_skipped_max`, `emb_contrast` | The candidate's closest match among what the user clicked (history plus earlier in-log clicks) and among what they were shown and skipped; contrast = clicked − skipped, positive when it looks more like what they read |
| LLM reader profile | `llm_sim_click`, `llm_sim_skip`, `llm_contrast` | The candidate's similarity to its best-matching Claude theme of what the user clicks and of what they skip, from the profile for the impression's day; contrast = click − skip |

### Evaluation

`metrics.json` reports overall AUC, log loss, CTR, **group AUC** (mean AUC within impressions
that contain both a click and a non-click — MIND's standard metric), and AUC broken out by
time slot.

It also reports bootstrap confidence intervals (`ci`) for AUC, group AUC, and AUC by slot.
Whole impressions are resampled, since the candidates shown together are correlated, and each
replicate reweights the rows by how often its impression was drawn. The number of replicates and
the interval level are set under `evaluation` in `configs/train.yml` (`bootstrap_samples: 0` skips this).

The bootstrap only measures uncertainty on the test-day predictions; training and calibration
treat each row as independent. A user's impressions, and one article's rows across lists, are
also correlated, so the intervals are probably still a little narrow. Resampling users would be
the next step.

**Quality gate.** If test AUC falls below `evaluation.min_auc` (0.644, the lower bound of the
baseline's 95% CI), `pushnyt-train` exits with an error after saving its artifacts, so a regression
can't pass silently but the failed run can still be inspected. Remove the setting to disable it.

### Results

Test day (2019-11-14, 1.22M candidate rows), embedding retrieval excluded from both runs, 95%
impression-level bootstrap CIs (1,000 replicates):

| Metric | Baseline (`baseline-no-emb`) | + recency & user depth (`recency-depth-no-emb`) |
|--------|------------------------------|-------------------------------------------------|
| AUC | 0.646 (0.644–0.649) | **0.656** (0.653–0.659) |
| Group AUC | 0.617 (0.614–0.620) | **0.622** (0.619–0.625) |
| Log loss (before calibration) | 0.1571 | **0.1566** |
| AUC, morning | 0.654 (0.651–0.658) | **0.669** (0.665–0.673) |
| AUC, afternoon | 0.634 (0.629–0.640) | **0.639** (0.633–0.644) |
| AUC, evening | 0.642 (0.635–0.648) | **0.645** (0.638–0.651) |

`article_age_hours` ranks third by gain, behind `subcat_affinity` and `sim_text`.

**LLM reader profiles.** Same test day, all features (including embedding retrieval and the
click/skip contrast group), with and without the three `llm_*` features:

| Metric | Without LLM (`ablation-no-llm`) | With LLM (`ablation-with-llm`) |
|--------|---------------------------------|--------------------------------|
| AUC | 0.7026 (0.700–0.705) | 0.7032 (0.701–0.706) |
| Group AUC | 0.6757 (0.673–0.679) | 0.6766 (0.673–0.680) |
| Log loss (calibrated) | 0.1520 | 0.1520 |
| AUC, morning | 0.706 (0.703–0.710) | 0.708 (0.704–0.712) |
| AUC, afternoon | 0.700 (0.695–0.705) | 0.699 (0.694–0.703) |
| AUC, evening | 0.697 (0.691–0.703) | 0.697 (0.691–0.703) |

Separate CIs overlap almost entirely, but most of their width is test-set noise the two runs
share. `pushnyt-compare` resamples impressions once per replicate and scores both runs on the same
sample, so its CI is for the difference (1,000 paired replicates, 30,270 impressions):

| Metric | With − without | 95% CI | P(no gain) |
|--------|----------------|--------|------------|
| AUC | +0.0006 | −0.0000 to +0.0013 | 0.027 |
| Group AUC | +0.0009 | −0.0003 to +0.0021 | 0.082 |

The LLM themes add at most about a thousandth of AUC on top of the embedding and contrast
features; the AUC gain is borderline and the group-AUC gain is not significant. The three
features rank 23rd, 24th and 27th of 30 by gain (about 2% each), well behind
`emb_sim_clicked_max` and `emb_contrast`, which already use the same clicked/skipped evidence.
Both runs are one training seed each, so the CI covers test-set sampling but not run-to-run
model variance.

Feature importance for the full model (`ablation-with-llm`), colored by feature group. The user's
own subcategory click rate (`subcat_engaged_rate`), embedding similarity to their clicked articles
and their subcategory share lead; the LLM features sit near the bottom:

![Click model feature importance](docs/feature_importance.png)

### Push objective

The click model answers "will they click?"; the push objective decides what to actually send.
For each candidate in an impression:

```
score = (1 − friction) · engagement + friction · novelty,     friction ∈ [0, 1]
```

- **engagement** — the XGBoost `P(click)`.
- **novelty** — `1 − share of the user's earlier exposure in the candidate's subcategory`, where
  exposure is their pre-period history clicks plus every article shown in earlier impressions.
  1 = a subcategory they have never been shown; near 0 = what they are usually shown.

**Same scale.** The raw values aren't comparable. `P(click)` sits near the base CTR (~0.04),
while novelty spans 0–1, so adding them raw would let novelty swamp engagement. Each half is
therefore converted to its **percentile rank among the candidates in the same impression**. That
makes friction a literal weight between two rankings, and it isn't affected by recalibrating
`P(click)`.

**Baseline click rates.** Clicks are rare. On the test day (30,270 impressions, 1.22M candidates):

| What | Click rate |
|------|------------|
| Any single candidate shown | 3.8% |
| One candidate picked at random from each impression | 9.1% |
| The model's top pick per impression (friction 0) | 16.7% |

A random pick beats the per-candidate rate because MIND only logs impressions with a click, and
short impressions have a high click share. So 9.1% is the fair random baseline, and the model
nearly doubles it. These are feed impressions, so real push click rates would be lower.

**The trade-off.** `pushnyt-objective` sweeps friction and plots, for the single top push per
impression, the click rate against the share of pushes from a never-shown subcategory. The dashed
curve repeats the same objective with the click model swapped for a random ranking, so the gap
between the curves is what the model adds at each level of novelty (`recency-depth-no-emb` model):

![Push objective trade-off](docs/tradeoff.png)

| Friction | CTR of top pick | …with random engagement | Top pick from a never-shown subcategory |
|----------|-----------------|-------------------------|-----------------------------------------|
| 0 | 16.7% | 9.1% | 11% |
| 0.05 | 16.3% | 9.0% | 16% |
| 0.1 | 15.4% | 8.8% | 23% |
| 0.15 | 14.7% | 8.7% | 31% |
| 0.2 (default) | 13.9% | 8.5% | 37% |
| 0.3 | 12.6% | 8.2% | 48% |
| 0.4 | 11.8% | 8.0% | 56% |
| 0.5 | 11.0% | 8.0% | 64% |
| 0.7 | 10.0% | 8.0% | 76% |
| 1 | 8.1% | 8.1% | 87% |

Low friction buys novelty cheaply: going from 0 to 0.2 more than triples the never-shown share
for about 3 points of CTR. Above about 0.5 the model keeps less and less of its edge. CTR only
credits logged clicks, so it likely understates how novel pushes would do.

### Calibration

A ranking only needs scores in the right order. A **send/don't-send decision** needs them to be
right in absolute terms: of all pushes scored 0.10, about 10% should be clicked. That property is
calibration, and models trained for ranking often lack it (gradient boosting with early stopping
tends to be over- or under-confident in the tails).

`pushnyt-train` applies **Platt scaling**: it fits `p = sigmoid(a · logit(raw) + b)` on the
held-out early-stopping slice and applies it to the test day. The mapping is strictly increasing,
so rankings, AUC, and the push objective's choices are unchanged; only the probability scale
moves. (Isotonic regression is the more flexible alternative, but it creates ties, which would
change the objective's rankings.)

![Calibration](docs/calibration.png)

| | Raw XGBoost | Calibrated |
|---|---|---|
| Expected calibration error (ECE) | 0.35 pts | **0.24 pts** |
| Brier score | 0.03635 | **0.03630** |
| Top bin: predicted → observed | 13.2% → 10.9% | **11.1% → 10.9%** |

On average the raw model was already close; the fix matters at the top, where it was
**overconfident**, and the top is exactly where a send threshold operates. ECE is the
count-weighted mean gap between predicted and observed rates over 20 equal-count bins.

**MIND caveats.** Calibration is per *candidate*, not per chosen push, and MIND only logs
impressions that contain a click. Small impressions therefore have inflated click rates (18.6%
with ≤10 candidates vs 1.9% with >100), and the model can't see impression size. The labels also
mean "picked from this list", not "wants this article": candidates shown together compete for
about one click. So the 0.07 send threshold is in feed units (a top pick from about 37
candidates), not the chance of opening a lone notification. The send policy is evaluated on
impressions with 50+ candidates, where the artifact is weakest. Within those, top picks still
click more as the prediction rises (3.6% → 8.5% across quintiles), but the model over-predicts the
level. Two possible fixes are an impression-size feature fixed at a reference value at push time,
or weighting each impression once. Real push logs are the proper source (see
[Production](#production-plugging-in-real-opt-out-data) and
[Real pushes to your phone](#real-pushes-to-your-phone)).

### Return model (churn proxy)

The real cost of a bad push is a reader turning notifications off. MIND has no opt-out events, so
`pushnyt-train` fits a proxy: **P(the user has another impression within 24 hours)**, an XGBoost
model on user depth and fatigue features (`src/pushnyt/risk.py`). Risk = 1 − P(return).

- Labels are censored near the end of the log, so it trains on impressions before `fit_end`,
  is evaluated on `[fit_end, train_end)` (**AUC 0.743**, 54% return rate), and only scores the test day.
- It is a **user-level** signal: it mostly measures how active someone is, not what a push did to
  them. Subtracting it from the per-article score would barely change which article wins (it is
  nearly constant within an impression), so it sets **how much** to send each user, not **what**.
- The feature importances confirm it: a split on `user_history_len` (pre-log click count) reduces
  the loss about 7× as much as a split on any other feature, followed by recent activity
  (`impr_last_24h`, `user_prior_impr`, `hours_since_last_impr`). The engagement and skip-rate
  features add little.

![Return model feature importance](docs/return_importance.png)

### Send policy

For each impression's top pick (at the configured friction), `pushnyt-objective` sends it only if

```
calibrated P(click) ≥ threshold + risk_penalty · (1 − P(return))
```

and the user has had fewer than `daily_cap` pushes that day (earliest first; the policy can't see
later impressions). Defaults in `configs/objective.yml`: threshold 0.07, risk penalty 0.04, cap 2.
At-risk users therefore only get pushes the model is more confident about.

![Send policy](docs/volume.png)

On impressions with 50+ candidates:

| Threshold | Pushes per active user-day | CTR of pushes sent | Predicted CTR |
|-----------|----------------------------|--------------------|---------------|
| 0 (send all, cap 2) | 1.15 | 5.3% | 8.1% |
| 0.05 | 0.72 | 6.4% | 9.4% |
| **0.07 (default)** | **0.41** | **7.9%** | 10.9% |
| 0.10 | 0.14 | 10.3% | 13.9% |
| 0.15 | 0.02 | 17.4% | 19.1% |

Raising the threshold trades volume for quality as intended. At the default, low-risk users get
0.82 pushes/day at 9.3% CTR and high-risk users 0.15/day at 5.3%. The model over-predicts
on this slice, most for high-risk users, which is the impression-size caveat above plus a sign
that a production calibrator should be fit per user segment.

### Production: plugging in real opt-out data

In production the return proxy should be replaced by a model of actual notification opt-outs.
What NYT would need to log:

| Event | Fields | Used for |
|-------|--------|----------|
| Push sent | user, article, time, slot, model scores, **send probability** | Training data; off-policy evaluation of new policies |
| Push opened | user, article, time | The click label (replaces MIND impressions) |
| Push dismissed | user, article, time | Article-level negative feedback |
| Notifications disabled / app uninstalled | user, time, pushes in the prior 7 days | The opt-out model's label |

A user-level opt-out model drops straight into the send policy in place of `p_return`. Dismissals
and opt-outs shortly after a specific push are what an **article-level** risk term would need,
added to the score as `(1 − f) · engagement + f · novelty − λ · risk`; MIND has nothing comparable,
so that term is not built here.

### Do clicks on unfamiliar subcategories stick?

The novelty term pays a click cost now on the bet that new interests last. MIND can partly test
that bet. `pushnyt-exploration` takes each user's **first exposure** in the log to each
subcategory, notes whether they clicked it and how unfamiliar it was at that moment, and then asks:
among users shown that subcategory again later, what share click it?

![Exploration conversion](docs/exploration.png)

| Subcategory at first exposure | Clicked the first time | Shown and skipped | Ratio |
|-------------------------------|------------------------|-------------------|-------|
| Never shown before | 17.6% | 7.4% | **2.4×** |
| Rarely shown (<10% of exposure) | 32.9% | 22.7% | 1.5× |
| Familiar | 51.7% | 36.4% | 1.4× |

(First exposures with at least 48h of log left; 20k–380k re-exposed users per bar; 95% CIs ≤ ±1 point.)

Users come back to familiar subcategories far more often, as expected. But a first click on a
**never-shown** subcategory goes with a 2.4× higher chance of clicking it again, the biggest
relative gap of the three: an unfamiliar click that lands is followed by real, repeated interest,
not just a one-off. That supports the novelty term's premise.

**Caveats.** This is observational and on feed clicks, not pushes: users who click unfamiliar
topics may simply be more curious, so it shows association, not that a push *creates* interest.
The follow-up is at most a few days, and a user can only re-click what the feed showed them again
(hence conditioning on re-exposure). The causal version is an experiment: randomize friction and
measure later organic reading of the new subcategories.

```bash
docker compose run --rm train "pushnyt-exploration"   # writes outputs/analysis/exploration.{csv,png}
```

### Fatigue: do users engage less after heavy exposure?

The send policy's daily cap assumes more pushes means less attention to each. `pushnyt-fatigue`
checks what MIND shows. Heavy users differ from light ones, so each user is compared with **their
own** usual engagement: for every impression, expected clicks = impression size × the click rate
for impressions of that size (MIND's logging inflates small ones), scaled by the user's own
actual-to-expected ratio. The index is actual ÷ expected clicks: 1.00 = usual, below = fatigued.
Users with 5+ impressions (9,725); 95% CIs resample users.

![Fatigue](docs/fatigue.png)

| Impressions in the previous 24h | 0 | 1 | 2 | 3–4 | 5+ |
|---|---|---|---|---|---|
| Engagement vs. usual | **1.019** | 0.994 | 0.997 | 0.991 | **0.983** |

| Impressions earlier in the week | 0 | 1–2 | 3–5 | 6–10 | 11+ |
|---|---|---|---|---|---|
| Engagement vs. usual | 1.016 | 0.994 | 0.997 | 1.000 | 1.008 |

- **Short-term fatigue is real but small.** Users engage about 2% above their norm when they have
  not been seen in the past day and about 2% below it after 5+ impressions; the intervals at the
  two ends don't overlap. Freshness matters: the first impression after a gap does best.
- **No cumulative fatigue over the week.** Engagement does not keep falling with total exposure; the
  heaviest users are back at their norm, likely because people who keep coming back are the
  engaged ones.
- **What it supports:** a *daily* cap and spacing pushes out (both in the send policy and the
  `hours_since_last_impr` feature), rather than a weekly budget.

**Caveats.** Feed impressions are started by the user, while pushes arrive unprompted, so push
fatigue is very likely stronger than this, and MIND can't show the outcome that matters most:
users turning notifications off. Six days is also short for "long term." The production version is
the volume experiment in the roadmap: randomize daily caps for several weeks and track CTR and
opt-outs over time.

```bash
docker compose run --rm train "pushnyt-fatigue"       # writes outputs/analysis/fatigue.{csv,png}
```

## Repository layout

```
configs/train.yml       # all data, split, feature, embedding, and model settings
configs/objective.yml   # push objective: which run to re-rank, friction, sweep
configs/notifier.yml    # real pushes: slot hours, feeds, persona, ntfy, click window
scripts/sync-data.sh    # pull gs://pushnyt-data into ./data
scripts/smoke-api.sh    # start the API image on a fixture run and check it responds (used by CI)
scripts/build-api-image.sh  # build + smoke-test a production API image with one run baked in
scripts/deploy-gcp.sh   # deploy the push pipeline to GCP (GKE + Cloud Run); dry run unless --apply
deploy/                 # cloudbuild.yaml, cloudrun/ (cards service, notifier job), k8s/ (scorer, KEDA)
src/pushnyt/
  data.py               # MIND loaders, time-slot bucketing
  embed.py              # pushnyt-embed: abstract embeddings -> Chroma
  profile.py            # pushnyt-profile: RAG + Claude click/skip themes per user-day -> Chroma
  vectordb.py           # thin Chroma wrapper (add / fetch vectors / nearest neighbors)
  features.py           # feature engineering
  train.py              # pushnyt-train: fit, evaluate, save artifacts
  compare.py            # pushnyt-compare: paired bootstrap of two runs (ablations)
  live.py               # request-time scoring from per-user state; pushnyt-live-parity replay
  nyt.py                # pushnyt-nyt-sim: push a week of NYT articles to MIND users, scored live
  notifier.py           # real pushes to phones (ntfy) from live NYT RSS: the slot loop / one-slot job
  cards.py              # the push card server: Read on NYT, ♥, Save, reading list, per-reader cookie
  store.py              # the notifier's database (SQLite locally, Postgres in the cloud)
  calibration.py        # Platt scaling, reliability bins, ECE
  risk.py               # return model (churn proxy)
  exploration.py        # pushnyt-exploration: do clicks on unfamiliar subcategories stick?
  fatigue.py            # pushnyt-fatigue: engagement after recent and cumulative exposure
  objective.py          # pushnyt-objective: engagement vs. novelty re-ranking + send policy
  api.py                # pushnyt-api: FastAPI serving app
  plots.py              # all charts (importance, calibration, trade-off, send policy, analyses)
tests/                  # pytest suite on synthetic MIND-style data
.github/workflows/      # ci.yml (lint, test, build images), release.yml (publish images)
Dockerfile              # training image (torch, xgboost, chroma: the `train` extra)
Dockerfile.objective    # objective image (numpy, pandas, pyarrow, matplotlib)
Dockerfile.api          # API image (core deps + the `api` extra: fastapi, uvicorn); `baked` target = production
Dockerfile.api.dockerignore  # build-context rules for API builds (lets a run's serving files in)
docker-compose.yml, docker-shell.sh
docs/                   # images used by this README, live-scoring.md and production.md (design notes)
data/                   # git-ignored; populated by sync-data.sh and pushnyt-embed
outputs/                # git-ignored; one folder of artifacts per run
```

## Getting started

### Prerequisites

- Docker (with Compose v2)
- [Google Cloud CLI](https://cloud.google.com/sdk/docs/install), authenticated with access to
  `gs://pushnyt-data` (`gcloud auth login`)

### Containers

```bash
./scripts/sync-data.sh            # pull gs://pushnyt-data into ./data (needs gcloud auth)
./docker-shell.sh                 # build image + interactive shell
docker compose run --rm train     # build + run training with configs/train.yml
docker compose run --rm objective # build + run the push objective with configs/objective.yml
docker compose up -d api          # build + start the serving API on http://localhost:8000
docker compose run --rm shell     # interactive shell via compose (training image)
docker compose up -d scorer notifier tunnel  # real pushes to phones (see that section)
```

There are three images. `pushnyt-train` holds the heavy training stack (`pip install .[train]`);
`pushnyt-objective` holds only the core dependencies and mounts just `src/`, `configs/`, and
`outputs/`; `pushnyt-api` adds FastAPI to the core dependencies and mounts `outputs/` read-only.
Batch steps pass a run's `predictions.parquet` through the shared `outputs/` folder. Only the
live services talk over HTTP: `scorer` (training image) serves `push-job` and `notifier`, which
both run in the objective image.

`src/`, `configs/`, `data/`, and `outputs/` are bind-mounted, so code and config edits
take effect without rebuilding. Rebuild after changing `pyproject.toml`.

**Memory.** On macOS, all containers share Docker Desktop's VM (4 GB on an 8 GB Mac), and a
process over the limit exits with code 137. Full training peaks at about 3.7 GB. It stays under
the limit by building features in fixed-size slices and memory-mapping the LLM theme vectors. If
runs are killed, raise Settings → Resources → Memory.

### Running the pipeline

Build the vector database once (and again whenever `news.tsv` or the embedding model changes),
then train:

```bash
docker compose run --rm train "pushnyt-embed --config configs/train.yml"
docker compose run --rm train
```

Or, from inside a container shell:

```bash
pushnyt-embed --config configs/train.yml
pushnyt-train --config configs/train.yml
```

The LLM profile features also need `pushnyt-profile`, which runs after embedding and needs an
Anthropic API key in `.env` (`ANTHROPIC_API_KEY=...`; the train service loads it):

```bash
docker compose run --rm train "pushnyt-profile --estimate"     # token count + cost; submits nothing
docker compose run --rm train "pushnyt-profile --sample 200"   # small trial batch
docker compose run --rm train "pushnyt-profile"                # all user-days
```

Training fails with `run pushnyt-embed first` if the Chroma collection is missing, or with
`run pushnyt-profile first` if the profiles are. The vector
database is only needed for the embedding retrieval features, so training without them skips it
(see below).

### Training options

```bash
# drop features for an ablation and write artifacts to their own folder
pushnyt-train --config configs/train.yml \
  --exclude sim_text emb_retrieval_rank \
  --output-dir outputs/ablation-no-text

# train without the embedding retrieval group (no vector database needed)
pushnyt-train --config configs/train.yml \
  --exclude emb_sim_mean emb_sim_max emb_sim_top5 emb_n_close emb_retrieval_rank \
  --output-dir outputs/recency-depth-no-emb
```

Hyperparameters, split dates, and feature settings all live in `configs/train.yml`. The resolved
config is copied into the output directory with each run.

### LLM reader profiles

The click/skip contrast features compare a candidate with the user's own clicked and skipped
articles. The LLM profile asks whether an LLM, given the same evidence, captures *why* a reader
clicks one article and passes over a similar one better than the raw nearest neighbors do.

- **Retrieve.** For day D: the user's 10 most recent clicks before D, and for each, the 2
  nearest skipped articles from their earlier impressions. Skips they later clicked are dropped.
  Skips close to a click show within-topic preferences that subcategory counts can't.
- **Generate.** One request per user-day to `claude-haiku-5-5` through the Message Batches API
  (half price). The structured output is `{click_themes, skip_themes}`, 2-4 each. Themes read
  like headlines ("NFL game recaps, injuries and trade rumors"), so they embed near abstracts.
  Each set describes only itself, because MiniLM ignores "rather than"; the comparison is left to
  `llm_contrast`.
- **Embed and score.** Each theme is its own vector in the `profiles` Chroma collection, and a
  candidate is scored by its best-matching theme. Users with no clicks or no skips have missing
  features, which XGBoost handles.

Profiles only use data from before their day. `data/llm/batches.json` stores the batch ids for
each prompt version, so reruns resume and prompt changes resubmit. `data/llm/report.json` has
token usage and cost. MIND-small has about 41k user-days. For training, the theme vectors are
memory-mapped from `data/llm/theme_vectors_{click,skip}.npy` rather than loaded from Chroma
(275k vectors); training rebuilds them if they are stale.

To measure the uplift, train with and without the three `llm_*` features, then compare the two
runs on the same test impressions (results under [Results](#results)):

```bash
docker compose run --rm train "pushnyt-train --exclude llm_sim_click llm_sim_skip llm_contrast --output-dir outputs/ablation-no-llm"
docker compose run --rm train "pushnyt-train --output-dir outputs/ablation-with-llm"
docker compose run --rm train "pushnyt-compare outputs/ablation-no-llm outputs/ablation-with-llm"
```

`pushnyt-compare` prints the paired difference and writes it to
`outputs/ablation-with-llm/compare_vs_ablation-no-llm.json`.

### Objective options

```bash
# re-rank a run's predictions at a different friction (no retraining)
docker compose run --rm objective \
  "pushnyt-objective --run-dir outputs/recency-depth-no-emb --friction 0.3"
```

`run_dir`, `friction`, the friction `sweep`, and the random baseline's `seed` default from
`configs/objective.yml`.

### API

```bash
docker compose up -d api      # serves outputs/recency-depth-no-emb by default
PUSHNYT_RUN_DIR=outputs/full docker compose up -d api   # serve a different run
docker compose down           # stop it
```

Interactive docs are at <http://localhost:8000/docs>.

| Endpoint | Does |
|----------|------|
| `GET /health` | Liveness, plus which run is loaded |
| `GET /model` | Run name, excluded features, test metrics |
| `GET /users/{user_id}/impressions` | A user's impressions in the run, to find ids to score |
| `POST /score` | `P(click)` and novelty for an impression's candidates, highest first |
| `GET /metrics` | Prometheus metrics: requests, errors, latency, and push behavior (see Monitoring) |
| `POST /push` | The push the objective picks at a given `friction` (default 0.2), with its engagement and novelty ranks, whether the send policy would send it (`send`: decision, risk-adjusted threshold, risk), and the next four candidates |

```bash
curl localhost:8000/users/U86598/impressions
curl -X POST localhost:8000/push -H 'content-type: application/json' \
  -d '{"impression_id": 152636, "friction": 0.2}'
```

Both `POST` endpoints accept an optional `news_ids` list to restrict the choice to a subset of the
impression's candidates. Friction outside [0, 1] returns 422; unknown impressions or articles return 404.

The send decision uses the threshold and risk penalty from `configs/objective.yml`. The daily cap
needs to know what the user was already sent today, so the caller enforces it.

#### Production image

The compose service reads a run from the mounted `outputs/` folder. For deployment, build an
image with one run baked in, so it serves with no mounts and the image tag names the model:

```bash
scripts/build-api-image.sh ablation-with-llm   # builds pushnyt-api:ablation-with-llm, then smoke-tests it
docker run -p 8000:8000 pushnyt-api:ablation-with-llm
```

The `baked` target copies the run's four serving files (about 20 MB) into the image. The script
then starts it without volumes and checks `/health` and `/push`. To deploy a model, build a new
tag; to roll back, redeploy the previous one.

The API serves **precomputed** scores: it looks up the batch predictions for impressions in the
run and can't score new users or articles. Request-time scoring exists as an offline replay:
`pushnyt-live-parity outputs/<run> --users 100` scores each test-day impression from that
user's earlier events only (`src/pushnyt/live.py`, same feature code as training) and checks the
result against batch: on 100 users, P(click), novelty and P(return) match exactly, at about
110 ms per request. What a live endpoint would still need (an online state store, event
ingestion, daily jobs) is in [docs/live-scoring.md](docs/live-scoring.md).

#### Simulation: pushing NYT articles live

`pushnyt-nyt-sim outputs/ablation-with-llm` runs the live scorer on articles the model never saw:
NYT articles from `data/nyt-recent.csv` for one week (default Dec 2–8, 2024), pushed to 1,000
MIND test-day users at 8:00, 13:00 and 19:00 New York time. At each slot a user's candidates are
the articles published since the previous slot; the push objective and send policy from
`configs/objective.yml` pick one and decide whether to send it.

Users keep their MIND state as of the end of the log, frozen for the week. Articles are embedded
with the training MiniLM model and mapped to the nearest MIND subcategory centroid (median cosine
0.40). They have no entities. The week is shifted to start the day after the log, so gaps since
each user's last visit stay realistic.

| | |
|---|---|
| NYT articles / push slots | 918 / 21 |
| User-article scores / push decisions | 918,000 / 21,000 |
| Pushes sent | 4,411 (21%; 0.63 per user per day, daily cap 2) |
| Latency | 4.3 ms per request, scoring 250 users per pass |

There are no labels, so these are sanity checks, not accuracy. The chosen push is closer to the
user than the average candidate on every signal (P(click) 0.071 vs 0.034, LLM click-theme
similarity 0.224 vs 0.156). Sends lean toward U.S. news (35% of sends vs 17% of articles), and
users get 3.3 distinct sections over the week. Weekends had half the articles but as many sends,
so a fixed threshold loosens when the pool shrinks. Outputs in `outputs/nyt-sim/`:
`scores.parquet`, `pushes.parquet`, `daily.csv` (per-day volume, reach, top section and story)
and `summary.json`.

How this would run in production (scheduled push jobs, article ingestion, delivery, the
feedback loop from real clicks, scheduled retraining, daily monitoring) is written up in
[docs/production.md](docs/production.md).

### Monitoring

| Layer | How |
|-------|-----|
| **Is it up?** | The API image has a Docker `HEALTHCHECK` on `/health` every 30s; `docker ps` shows `(healthy)` / `(unhealthy)`. Compose restarts the container if the process exits. |
| **Is it erroring or slow?** | `GET /metrics` (Prometheus format): `pushnyt_requests_total{endpoint,status}` and the `pushnyt_request_seconds` latency histogram. |
| **Is the model behaving sensibly?** | Also on `/metrics`: `pushnyt_push_decisions_total{decision="send"/"hold"}` (send rate), histograms of the chosen push's `p_click` and `novelty`, the friction callers ask for, and `pushnyt_model_info{run}` / `pushnyt_model_test_auc` for which model is live. |
| **What happened on a request?** | One JSON log line per request (`docker compose logs api`): endpoint, status, latency, and for `/push` the impression, friction, chosen article, `p_click`, and send decision. |

A sudden change in the send rate or the `p_click` distribution, with no change in traffic, is the
early warning that something upstream changed: a new training run, a feature bug, or a shift in
the data. Prometheus can scrape `/metrics` directly (or Google Cloud Managed Service for
Prometheus on GCP), and the JSON logs work as-is with Cloud Logging. What this can't watch is
**accuracy**: comparing predicted to actual clicks needs the outcomes of sent pushes logged and
joined back (see "Production: plugging in real opt-out data").

**Limitation:** the API serves a run's precomputed test-day predictions, so it only knows the
impressions in that run's `predictions.parquet`. Scoring live requests would need each user's
features (history, recent skips, exposure) computed online from a feature store.

## Real pushes to your phone

The `notifier` service runs the model on today's NYT news and sends real push notifications to
everyone subscribed to your ntfy topic: you, and any friends you share the topic with. Every
candidate it scores is logged with all its covariates, and so is what each reader did with the
push. That gives you labeled push data, which MIND can't provide.

```
NYT RSS feeds ──▶ notifier ──HTTP──▶ scorer  (model + MIND user state, from the NYT sim)
                   │
   ntfy.sh ◀───────┤          cards :8090 ◀── tunnel (https://….trycloudflare.com) ◀── a reader taps
   (push to phones)│            │                                                    Read / ♥ / Save
                   ▼            ▼
        outputs/notifier/pushes.db, or Postgres via DATABASE_URL
        (candidates, covariates, sends, and per reader: opens, reads, ♥, saves)
```

### The schedule

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
2. Scores every article from the last 12 hours that hasn't already been pushed.
3. Picks the top article with the push objective (friction from `configs/objective.yml`).
4. Sends it through [ntfy](https://ntfy.sh) if it passes the send policy, up to 3 a day.
5. Writes every candidate to the database.

### The card

Tapping a notification opens that push's **card**, a small page served by the `cards` service
(`src/pushnyt/cards.py`) and made public by the `tunnel` service. It shows the headline, the abstract and three buttons:

- **Read on NYT** is a plain nytimes.com link, so iOS opens the NYT app if it's installed and
  Safari otherwise. Reading needs the reader's own NYT login there, as usual.
- **♡ Love** marks the article as a favorite. Tap again to undo.
- **🔖 Save** adds it to the reader's reading list at `/saved`. Tap again to unsave.

ntfy can't tell subscribers apart, so the card does. The first card a phone opens sets a cookie
with a random reader ID and offers an optional first-name box. From then on, each open, read,
♥ and save is stored for that reader, and each reader has their own reading list. ntfy's own
notification buttons aren't used. They do work in the background on iOS, but a button's request
can't say which subscriber tapped it, so it couldn't tell your ♥ from a friend's.

### Setup (once)

1. **Topic.** Install **ntfy** from the App Store and allow notifications. Tap **+** and
   subscribe to a topic on `ntfy.sh`. Anyone who knows the topic name can read it, so make it
   hard to guess (e.g. `pushnyt-` plus the output of `openssl rand -hex 6`), and only share it
   with friends you're happy to push to.
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

### Run

```bash
docker compose up -d scorer        # loads the model and MIND state; about 2-5 min
docker compose ps                  # wait until scorer shows (healthy)

docker compose run --rm notifier "python -m pushnyt.notifier --test"            # a test push should reach your phone
docker compose run --rm notifier "python -m pushnyt.notifier --once --dry-run"  # score one slot now, log it, send nothing

docker compose up -d notifier cards tunnel   # the schedule, the card server, its public address
docker compose logs -f notifier cards        # one JSON line per slot, and per open / read / ♥ / save
```

The first run picks the MIND persona and fixes the time shift, and both are stored in the
database. To see the pushes on your Mac, open ntfy's web app (ntfy.sh/app, subscribe to the same
topic) or use iPhone Mirroring.

**Friends:** have them install ntfy and subscribe to the same topic. Tell them their taps, ♥ and
saves are recorded for a class project.

Stop it with `docker compose stop notifier cards tunnel scorer`.

**Postgres locally.** The database is SQLite by default. Set `DATABASE_URL` to use Postgres, the
same engine as Cloud SQL. The `db` service runs one on your Mac:

```bash
docker compose --profile postgres up -d db
echo 'DATABASE_URL=postgresql+psycopg://pushnyt:pushnyt@db:5432/pushnyt' >> .env   # then restart notifier and cards
PUSHNYT_TEST_DATABASE_URL=postgresql+psycopg://pushnyt:pushnyt@localhost:5433/pushnyt uv run pytest tests/test_notifier.py
```

### Keeping it running

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
  see [Deploying to Google Cloud](#deploying-to-google-cloud).

### The data

`outputs/notifier/pushes.db` contains:

| Table | One row per | Columns |
|-------|-------------|---------|
| `scores` | slot × candidate article | `slot_time`, article (`title`, `section`, `subcategory`, `url`, `published`), every model feature, `p_click`, `p_return`, `novelty`, `push_score`, `is_top`, `passes_policy`, `sent`, `push_id`, `sent_at`, `sent_ts` (Unix seconds) |
| `readers` | phone that opened a card | `reader_id` (the cookie), `name` (optional), `created_at`, `first_push_ts` |
| `interactions` | push × reader × kind | `kind` (`open`, `read`, `heart`, `save`), `created_at`, `minutes_after_push`; unloving or unsaving deletes the row |
| `labeled` (view) | sent push × reader | the push's `scores` row plus `reader_id`, `reader_name`, `label` (1 = opened, 0 = not opened within 24 h, NULL = pending), `read`, `hearted`, `saved` |
| `articles` | article read from the feeds | text, URL, publish time, mapped MIND subcategory |
| `meta` | setting | persona `user_id`, time anchor |

A reader is first seen when they open a card. So `labeled` pairs each reader only with pushes
sent from their first opened push onward; before that, there's no way to know whether they were
subscribed. Subscribers who never open a card don't appear at all.

Example queries:

```bash
sqlite3 -header -column outputs/notifier/pushes.db \
  "SELECT slot_time, reader_name, round(p_click, 3) AS p_click, label, read, hearted, saved, title FROM labeled ORDER BY slot_time DESC LIMIT 20"

# per reader: pushes, opens, reads, hearts, saves
sqlite3 -header -column outputs/notifier/pushes.db \
  "SELECT coalesce(reader_name, reader_id) AS reader, count(*) AS pushes, sum(label = 1) AS opens, sum(read) AS reads, sum(hearted) AS hearts, sum(saved) AS saves FROM labeled GROUP BY reader_id"

# open rate by predicted P(click) bucket
sqlite3 outputs/notifier/pushes.db \
  "SELECT round(p_click, 2) AS bucket, count(*) AS n, avg(label) AS open_rate FROM labeled WHERE label IS NOT NULL GROUP BY bucket"
```

The non-pushed candidates are kept too (`is_top = 0`), so the table can also serve as training or
off-policy data later. In pandas: `pd.read_sql("SELECT * FROM labeled", sqlite3.connect(path))`.

### Settings and limitations

`configs/notifier.yml` holds the slot hours, time zone, lookback, daily cap, persona `user_id`,
feeds, ntfy server and click window. The threshold, risk penalty and friction come from
`configs/objective.yml`.

- **Everyone gets the same push, picked for a stand-in.** The scorer only knows MIND users, so
  one MIND reader (sampled, or set with `user_id`) stands in for all subscribers. Per-reader
  pushes would need a topic per reader and a profile built from each reader's own opens, reads,
  ♥ and saves. The reader IDs are already in place for that.
- **Interactions aren't fed back yet.** They're stored, but they don't update any profile in the
  scorer. They're signals of different strength: an open means "curious", a read means "read
  it", ♥ means "more like this", and a save means "interested, not now".
- **Cookies are per browser.** A reader who clears cookies, or opens a card in another browser,
  becomes a new reader. ♥ and saves live in this database, not in the reader's NYT account; NYT
  has no public API for writing to its saved list.
- **The feeds are RSS.** RSS needs no API key, but it only lists what editors put in each
  section's feed. The NYT Times Wire API would cover every article in one call.
- **P(click) is a ranking signal here, not a rate.** The model was trained on another outlet
  and era, so the 0.07 send threshold is in MIND feed units. Logged opens are what would
  recalibrate it.

## Deploying to Google Cloud

The same pipeline, deployed in a mixed setup. Each piece goes where it fits:

| Piece | Runs on | Why there |
|-------|---------|-----------|
| Scorer | **GKE** Autopilot (`deploy/k8s/`) | Big (~3 GB) and slow to start, so it stays up. It sits behind an internal load balancer, and **KEDA** scales it to one replica per 10 readers. |
| Card server | **Cloud Run** service (`deploy/cloudrun/cards.yaml`) | Bursty: scales with taps, down to zero. Its HTTPS address replaces the tunnel. |
| Notifier slot | **Cloud Run job** (`deploy/cloudrun/notifier-job.yaml`) + **Cloud Scheduler** | Runs four times a day and exits. Scheduler replaces the local sleep loop. |
| Database | **Cloud SQL** Postgres | Shared by the card server and the job; replicas can't share a SQLite file. |
| Images | **Artifact Registry**, built by **Cloud Build** (`deploy/cloudbuild.yaml`) | x86 images from the ARM Mac, without building locally. |

```
Cloud Scheduler ─(8, 12, 17, 21)─▶ notifier job (Cloud Run) ──VPC──▶ scorer (GKE, internal LB) ◀── KEDA
                                        │          │                                               │
                                     ntfy.sh    Cloud SQL ◀── cards (Cloud Run, public HTTPS) ──/stats┘
                                        ▼                         ▲
                                      phones ────── tap ──────────┘
```

**How the code supports it:**
- **Stateless slots.** Each slot can be a fresh process. The scorer ignores articles it already
  has, and the notifier sends each slot's candidates with its `/score` request. So any scorer
  replica can answer, including one that just started.
- **Two separate entry points.** The card server (`python -m pushnyt.cards`) and a single slot
  (`python -m pushnyt.notifier --once`) run on their own.
- **One database layer for both backends.** `src/pushnyt/store.py` uses SQL that works on both
  SQLite and Postgres, and the tests run on both.
- **A reader count for the autoscaler.** The card server's `/stats` endpoint returns the number
  of readers, protected by a token, for KEDA to read.

`scripts/deploy-gcp.sh` does the deployment. **It's a dry run by default:** it prints every
command and runs nothing until you add `--apply`.

```bash
scripts/deploy-gcp.sh setup     # once: APIs, registry, service account, Cloud SQL, secrets, GKE cluster, KEDA, data to the bucket
scripts/deploy-gcp.sh images    # build both images on Cloud Build
scripts/deploy-gcp.sh deploy    # cards → scorer → notifier job → schedule
scripts/deploy-gcp.sh status    # what's running (read-only)
scripts/deploy-gcp.sh pause     # stop what bills hourly: scorer to 0 replicas, schedule paused, Cloud SQL off
scripts/deploy-gcp.sh resume
scripts/deploy-gcp.sh down      # delete everything it created (keeps the bucket)
```

It uses gcloud's current project (`PROJECT`), `us-east1` (`REGION`), the `pushnyt-data` bucket
(`BUCKET`), the `ablation-with-llm` run (`RUN`), and the current commit as the image tag (`TAG`).
`NTFY_TOPIC` comes from `.env`. The ntfy topic, the database URL and the `/stats` token go into
Secret Manager and are masked in the printed commands.

**Cost while running** (rough estimates; check the GCP pricing calculator):

| | Monthly |
|---|---|
| Card server, notifier job, Scheduler (3 jobs free), Cloud Build (2,500 min free) | ~$0 at this traffic |
| Scorer on GKE Autopilot, 1 replica (1 vCPU, 3 GiB); the free tier covers one cluster's management fee | ~$40–50 |
| Cloud SQL `db-f1-micro` | ~$10 |
| Artifact Registry (~5 GB of images) and the bucket (~2 GB) | ~$1 |

So about $50–60/month while it's running. `pause` drops it to roughly storage only, and `down`
removes it. New GCP accounts get $300 in credits; set a budget alert in Billing either way.

**Not yet run against a real project.** The code paths are tested locally, including Postgres.
The manifests parse, and the script's dry runs print the expected commands. The first real
`--apply` is where cloud-side surprises would show up:
- **Organization policies.** If the project sits under an organization that blocks public
  (`allUsers`) access, the card service can't be made public.
- **Permissions and quotas** for Cloud SQL and GKE.
- **Scorer startup time** with the data copied from the bucket.

## Development

### Tests

```bash
docker compose run --rm -v "$PWD/tests:/app/tests" train "uv sync --extra train --extra api --extra dev && ruff check src tests && pytest"
# or locally: uv sync --extra train --extra api --extra dev && uv run pytest
```

The suite builds a tiny synthetic MIND-style dataset (`tests/conftest.py`), so it needs no real data
or vector database. It covers the AUC/bootstrap math and quality gate, calibration (Platt keeps
rankings, fixes overconfidence), return labels and censoring, the objective (friction bounds and
endpoints, scale-invariance), the send policy (risk-adjusted threshold, daily cap order), the
no-lookahead guarantees of the recency and novelty
features, the exploration and fatigue analyses (on planted effects), an end-to-end train →
objective run, and every API endpoint including `/metrics` and the request logs.

### CI/CD

- **CI** (`.github/workflows/ci.yml`), on every push to `main` and every pull request: `ruff`,
  `pytest`, a build of all three Docker images (not pushed), and an API smoke test
  (`scripts/smoke-api.sh`) that starts the API container on a tiny fixture run and checks that
  `/health`, `/push`, and `/metrics` respond. Run it locally with `scripts/smoke-api.sh`.
- **Release** (`.github/workflows/release.yml`), when a version tag is pushed: builds the three
  images and publishes them to GitHub Container Registry as
  `ghcr.io/jsheen/pushnyt-{train,objective,api}`, tagged with the version and `latest`:

  ```bash
  git tag v0.2.0 && git push origin v0.2.0
  ```

Training on the real data is not part of CI (it needs the GCS data); the quality gate guards it when
you run it.

### Outputs

Each run writes to its own folder under `outputs/`, `outputs/full` by default (`output.dir` in the
config, or `--output-dir`):

```
outputs/
  baseline-no-emb/        # original groups minus embedding retrieval
  recency-depth-no-emb/   # adds article recency + user depth, minus embedding retrieval
  full/                   # default: every feature group
  analysis/               # pushnyt-exploration and pushnyt-fatigue: exploration.*, fatigue.*
  nyt-sim/                # pushnyt-nyt-sim: scores, pushes, daily.csv, summary.json
  notifier/pushes.db      # notifier: every candidate, covariates, sends, per-reader opens / reads / ♥ / saves (SQLite)
```

| File | Contents |
|------|----------|
| `xgb_click.json` | Trained XGBoost model |
| `metrics.json` | Test-set AUC, group AUC, log loss, CTR, AUC by slot, bootstrap CIs, calibration (Platt parameters, ECE, Brier before/after), return-model AUC, best iteration |
| `calibration.png` | Reliability diagram, raw vs. calibrated |
| `feature_importance.csv` | Gain per feature |
| `feature_importance.png` | Feature-importance chart, colored by feature group |
| `xgb_return.json` | Trained return model (P(return within 24 hours)), used to score live |
| `return_feature_importance.csv`, `.png` | Gain per feature for the return model, and its chart |
| `config.yml` | Config used for the run |
| `predictions.parquet` | Test-set candidates: impression, user, slot, article, label, `p_click` (calibrated), `p_click_raw`, `novelty`, `p_return` |
| `objective/tradeoff.csv` | Top-pick CTR and novelty at each friction, for the model and the random-ranking baseline |
| `objective/tradeoff.png` | Trade-off plot: top-pick CTR vs. never-shown share, model vs. random |
| `objective/pushes.parquet` | The chosen push per impression at the configured friction, with its score and `send` decision |
| `objective/volume.csv`, `volume.png` | Pushes per user-day vs. CTR of pushes sent at each send threshold, by risk group |
| `objective/summary.json` | Tradeoff row for the configured friction, plus send-policy results |

## Status and contact

These programs are a work in progress, as we work to improve usability, error-catching, and speed of analysis. If you find errors, please contact Justin Sheen at justinksheen (at) gmail (dot) com

Last Update: November 10, 2026
