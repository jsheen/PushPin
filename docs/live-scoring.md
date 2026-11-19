# Live scoring

Status (2026-11-19): **live scoring runs.** The scorer service (`scorer_api.py`, `POST /score`)
scores the notifier's requests at every slot, locally in Docker or as a private Cloud Run service
([deploy-gcp.md](deploy-gcp.md)). There's no separate online store: each request carries the
reader's earlier pushes and taps as events, and the notifier's database is the record, so the
scorer stays stateless ([notifier.md](notifier.md)). `pushpin-api` still serves the precomputed
test-day scores in `predictions.parquet`. The wider production design is in
[production.md](production.md).

## How it works

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

## What each feature needs at request time

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

## Avoiding training/serving skew

Skew, two implementations of a feature that quietly disagree, is the main risk. It's handled by
the single `features_for` implementation and the parity test above. The remaining step is to
**log served features** next to each prediction (the notifier's `scores` table already does), so
drift between training and serving inputs can be monitored.

## At scale

- **Online store.** Rebuilding state from events per request is fine for a few readers. At
  publisher scale, per-user state (counts, a 72-hour window, clicked/skipped IDs, a few vectors)
  would live in Redis (Memorystore) or Firestore, updated by an event-ingestion worker.
- **Article index.** Exact numpy search over ~50k vectors takes a few milliseconds, so no vector
  database is needed in the request path below a few million articles.
- **LLM profiles** stay a daily batch job (the Batch API is half price, and a day's latency fits
  the feature). Their output becomes per-user theme vectors in the store.
- **New articles** must be embedded within minutes of publishing to be scorable.
