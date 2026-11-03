# Live scoring: what it would take

Status (2026-11-03): **phases 1–3 done as an offline replay; no live endpoint or online store yet.**

- `features.py` is split into `fit_globals` (shared state fitted on the log) and `features_for`
  (features for any set of events). Batch training calls both; output is unchanged.
- Training now also saves the return model (`xgb_return.json`).
- `live.py`: `LiveScorer.score(user, time, candidates)` computes features from that user's own
  event log plus the shared state, using the same `features_for` code; `observe()` records an
  impression's outcome. `pushnyt-live-parity <run>` replays the test day request by request.
  On a 100-user sample (6,740 rows) P(click), novelty and P(return) match batch exactly; median
  latency 112 ms per request. `tests/test_live.py` checks exact parity on synthetic data.
- Article first-seen time is the one piece of state shared across users: a replay over a user
  sample must still observe every user's impressions.
- New articles can join the catalog after training (`LiveScorer.add_articles`: text projected
  with the saved TF-IDF/SVD, a training subcategory, a publish time), and `score_many` scores
  many users in one pass (4 ms per request). `pushnyt-nyt-sim` uses both to push a week of NYT
  articles to 1,000 MIND users; see the README.

The rest of this document is the original plan; phases 4–6 are still open. The wider production
design (scheduled push jobs, delivery, feedback loop, retraining) is in [production.md](production.md).

## Where things stand

`pushnyt-api` serves **precomputed** scores. `pushnyt-train` scores every test-day candidate in
batch and writes `predictions.parquet`. The API loads that file (baked into the production image
by `scripts/build-api-image.sh`) and looks rows up by `impression_id`. It never loads
`xgb_click.json` and can't answer for a user, article or time that isn't in the file.

Live scoring means answering a new request like:

```json
POST /score {"user_id": "U123", "time": "2026-11-03T08:05:00Z", "news_ids": ["N1", "N2", "N3"]}
```

with `P(click)`, novelty and `P(return)` computed at request time.

The model is the easy part: `xgb_click.json` is 3 MB and predicts in microseconds. The hard part
is **computing the ~30 features for one request the same way batch training did**, from state
that's current up to the moment of the request.

## What each feature needs at request time

Batch training computes everything from the full impression log in one pass (`build_features`).
Live, each value has to come from stored state that is updated as events arrive.

| Group | Features | State needed | Updated |
|-------|----------|--------------|---------|
| Time-slot engagement | `slot_prior_impr`, `slot_prior_clicks`, `slot_engaged_rate`, `slot_click_share` | Per user × slot: impression and click counts; per user: total clicks; global base rate | Every impression / click |
| Push fatigue | `slot_skipped_72h`, `slot_skip_rate_72h`, `impr_last_24h`, `hours_since_last_impr`, `hours_since_last_slot_impr` | Per user: timestamped impressions with shown/skipped counts for the last 72 h; last impression time overall and per slot | Every impression |
| User depth | `user_history_len`, `user_prior_impr` | Per user: history length, impression count | Every impression |
| Similarity | `sim_text`, `sim_entity`, `subcat_affinity` | Per user: history-profile vectors (sum of clicked articles' TF-IDF/SVD and entity vectors) and subcategory weights; per article: text and entity vectors | Every click; per new article |
| Complementarity | `complement_score` | The fitted subcategory matrix `s` (frozen at training); per user: subcategory weights | Retrain; every click |
| Subcategory skips | `subcat_engaged_rate`, `subcat_prior_skips` | Per user × subcategory: shown and clicked counts; per-subcategory prior rates from training | Every impression |
| Embedding retrieval | `emb_sim_mean/max/top5`, `emb_n_close`, `emb_retrieval_rank` | Per user: clicked article IDs and the mean history embedding; the article vector index for the top-k neighbors | Every click; per new article |
| Click/skip contrast | `emb_sim_clicked_max`, `emb_sim_skipped_max`, `emb_contrast` | Per user: set of clicked and set of shown-but-skipped article IDs; article vectors | Every impression |
| LLM reader profile | `llm_sim_click`, `llm_sim_skip`, `llm_contrast` | Per user: latest day's click/skip theme vectors (≤ 4 + 4 × 384 floats) | Daily batch job |
| Article recency | `article_age_hours` | Per article: first time it was shown | First impression of each article |
| Serving extras | `novelty`, `p_return` | Per user × subcategory exposure (novelty); the return model and its features | Every impression; retrain |

Every one of these must use only events **strictly before** the request, exactly as in batch,
or the live model sees different inputs than it was trained on.

## Artifacts training doesn't save yet

`pushnyt-train` saves only `xgb_click.json`, `metrics.json` (which includes Platt parameters),
`config.yml` and outputs. Live scoring also needs, frozen from the same training run:

- **TF-IDF vocabulary + SVD components** (`text_vectors`): currently refit on every run, so new
  articles can't be projected into the same space without them.
- **Complement matrix `s`** and **per-subcategory prior click rates** (`fit_complements`,
  `sub_rate` in `build_features`).
- **Global base rates** used for shrinkage in the temporal features.
- **The return model** (`fit_return_model` in `risk.py`): trained but never saved.
- **The exact feature list and order** the booster expects (excludes applied).

These should be saved into the run folder and baked into the serving image together, so a
deployed image is one consistent model version.

## Architecture

```
            events (impressions, clicks)                     daily batch jobs
                       │                                ┌──────────────────────────┐
                       ▼                                │ embed new articles       │
             ┌──────────────────┐   write state         │ LLM profiles (Batch API) │
             │ event ingestion  │──────────────┐        │ retrain + bake new image │
             │ (Pub/Sub worker) │              ▼        └────────────┬─────────────┘
             └──────────────────┘     ┌─────────────────┐            │ writes
                                      │ online store    │◀───────────┘
 request ─▶ pushnyt-api (Cloud Run) ─▶│ user state      │
            1. read user + article    │ article state   │
               state                  │ theme vectors   │
            2. compute features       └─────────────────┘
            3. XGBoost + Platt             article vector index
            4. novelty, P(return),         (51k × 384 floats ≈ 80 MB:
               send policy                  can live in the image)
```

- **Online store:** per-user state is small (counts, a 72-hour event window, clicked/skipped ID
  sets, a few vectors). Redis (Memorystore) or Firestore keyed by `user_id`. Article state
  (vectors, first-seen time) is 51k rows and can be loaded in memory by the API.
- **Article index:** at MIND's size, exact numpy search over 51k vectors takes a few
  milliseconds, so Chroma isn't needed in the request path. Revisit beyond a few million articles.
- **LLM profiles:** stay a daily batch job (the Batch API is half price and a day's latency
  matches the feature's design). Its output becomes per-user theme vectors in the online store.

## Avoiding training/serving skew

Skew is the main risk: two implementations of the same feature (vectorized batch and per-request)
that quietly disagree. Plan:

1. **One feature implementation.** Rewrite feature logic as functions of *(user state, article
   state, request time)*. Batch training builds state by replaying the log in time order and
   calls the same functions. This is slower than today's vectorized code; profile it before
   committing to it.
2. **Parity test.** Replay the test day through the live path (events in order, a score request
   before each impression) and compare against `build_features` output. The bar: identical
   features for every row, the same bar used when feature building was batched for memory
   (commit fdb86bb).
3. **Log served features** next to each prediction, so drift between training and serving inputs
   can be monitored after launch.

## Phased plan

1. **Persist the missing artifacts** from training (list above). Partly done: the return model is
   saved; the replay refits the other shared state deterministically instead of loading it.
2. **State model:** define the per-user and per-article state schemas, plus the event-replay code
   that builds them from `behaviors.tsv`.
3. **Per-request feature functions** + the parity test against `build_features`.
4. **`/score/live` endpoint** reading state from an in-memory store first (no Redis yet). Score
   test-day requests and match the batch AUC.
5. **Event ingestion** that updates state, plus the daily jobs (new-article embedding, LLM
   profiles). Then move state to Redis/Firestore.
6. **Deploy:** Cloud Run for the API, Pub/Sub + a worker for events, Cloud Scheduler for the
   daily jobs, artifacts and images versioned by training run.

## Open questions

- **Latency budget:** how many candidates per request, and how fast must the push decision be?
  This decides whether embedding features are computed live or precomputed per user daily.
- **Cold start:** new users have no state; today their features are NaN, which XGBoost handles.
  Check that's still acceptable at higher volume.
- **New articles:** `article_age_hours` and the text/entity vectors need the article embedded
  before it can be scored, so ingestion must embed new articles within minutes.
- **Retraining cadence:** daily or weekly? Each retrain produces a new image tag; roll back by
  redeploying the previous tag.
- **Is it worth it for the course?** Batch scoring + baked image may be the right scope;
  this document is the "what production would need" answer.
