# Proposed A/B test

The NYT's own push strategy appears to send many pushes a day to everyone. PushPin is more
selective: the model picks an article every two hours and a send rule pushes it only when it's
worth it (see [notifier.md](notifier.md#how-many-pushes)). Readers don't choose a number. The
send rule is the fixed send policy today, and the contextual bandit ([bandit.md](bandit.md))
once it's validated live. The test compares three arms:

| Arm | Pushes | Papier-mâché (daily pictures) |
|-----|--------|--------------------------|
| A | NYT-style: many a day, same for everyone | — |
| B | Selective: model-picked, sent only when the send rule says so | Off |
| C | Selective: model-picked, sent only when the send rule says so | On |

A vs. B tests the selective strategy; B vs. C tests whether the pictures add engagement on top
of it. Arm A is the same "breaking" policy the bandit is compared with in simulation: the newest
story to everyone at every slot.

## Assignment

A fixed rule assigns each reader to an arm, for example a hash of `reader_id`, so a reader always
stays in the same arm and the assignment can be recomputed later. The arm is stored on the
reader. In arms A and B the papier-mâché switch is hidden; in arm C it can't be turned off (or
every change is logged), so the server knows who saw the pictures. `/demo` is outside the test.

## Outcomes

- **Return visits per reader per day** (primary): readers coming back by any route, the outcome
  the bandit maximizes. It needs a visit logged per reader whether or not they were pushed
  (e.g. opening their PushPin page), and per-reader delivery (one ntfy topic per reader); see
  [uplift.md](uplift.md#live-data).

From the interactions table:

- **Clicks per reader per day**: engagement with the pushes themselves.
- **Opt-outs** (notifications turned off): the risk the send policy manages.
- **Return rate**: days with at least one read.
- **Click-through per push**: reported, but not on its own. Arm A sends far more pushes, so its
  per-push CTR looks worse even with good targeting.

Together these give the claim the project is about: the selective arm gets X% of the clicks
for Y% of the pushes, with fewer opt-outs.

## Analysis

Pushes are not independent observations. Each reader gets many pushes and some readers simply
click more. Arm A also sends everyone the same article, so readers move together, and in arms B
and C the model learns from each reader's taps, so their CTR drifts over the test. Readers are
randomized, so readers are the unit of analysis:

- One number per reader (CTR, clicks per day), compared across arms with a t-test or a bootstrap
  over readers, like the paired bootstrap in [model.md](model.md#evaluation-and-results).
- For per-push detail, a logistic model with a random effect per reader (optionally per article),
  or standard errors clustered by reader.

Clicks are also the model's training labels, so the arm is kept as a feature or filter when
retraining. That way clicks the pictures encourage don't bias the model.

## Sample size

Rough numbers to detect a 20% relative lift in CTR (8% → 9.6%), two-sided α = 0.05, 80% power:

| Step | Value |
|------|-------|
| Pushes per arm if pushes were independent | ≈ 4,900 |
| Pushes per reader over 4 weeks | ≈ 56 (about 2 a day: the fixed rule's cap; the bandit chose ~1 a day in simulation) |
| Within-reader correlation (ICC, assumed) | 0.05 |
| Design effect, 1 + (56 − 1) × 0.05 | ≈ 3.75 |
| Pushes per arm, adjusted | ≈ 18,400 |
| Readers per arm | ≈ 330 |
| Readers in total (3 arms) | ≈ 1,000 |

Fewer pushes per reader (quiet days, or the bandit's ~1 a day) raise the number of readers
needed, but less than in proportion; the ICC is a guess until
real data gives one. With 10 readers per arm, only a difference of about 9 points (CTR roughly
doubling) would be detectable. So with a class-sized group this is a pilot: it shows the
experiment runs end to end and what the readout looks like, not a conclusive result.

## Timeline

At a large company, a feature like this usually takes a quarter or more from idea to full launch:
build and review, a few days' ramp to 1–5% of users behind a flag, a 1–4 week test (at least a
full week for weekday/weekend cycles), then a gradual rollout, often keeping a small holdout. At
NYT scale ~1,000 readers enroll within minutes; here, enrollment is the bottleneck. For PushPin:
arm assignment and arm A sending (~1 day), the readout and per-reader analysis (~1 day), a
retraining job (2–3 days), and 2–4 weeks of running the test.

## Retraining during the test

The plan is to retrain each week, and that is part of what arms B and C are. Two cautions:

- **Leakage between arms.** A model trained on everyone's clicks lets picture-driven clicks from
  arm C shape arm B's pushes. Train with the arm as a feature, or pool B and C and say so.
- **Small data.** A few hundred pushes a week is too few to retrain XGBoost from scratch. Update
  the calibration weekly and retrain on MIND plus the logs (the `scores` table stores each
  candidate's features; the `labeled` view gives the labels).

## Ideas for later arms

*Not built.* Candidates for follow-up tests once arm B has a baseline, each an arm B′ = arm B
plus one change:

- **Nostalgia: bring back an interest that has gone quiet.** A bonus of roughly
  `affinity × f(gap)` for a subcategory with strong long-run history (`subcat_affinity`) that
  hasn't been pushed lately. It must tell *dormant* (strong history, not pushed lately: bring it
  back) from *abandoned* (pushed lately and skipped: fatigue, leave it). `novelty` can't see this
  today, because it counts all exposure, history included. MIND can give only a rough check (no
  multi-week gaps); the push log has real timestamps.
- **Bridges: one article that combines two interests** (Business + Soccer → "the economics of the
  World Cup"). Score = max over pairs of the reader's interests of min(similarity to each), from
  clusters of clicked-article embeddings or the LLM profile's themes; or shared entities; or
  LLM-written bridge queries. The NYT's cross-desk pieces suit this better than MIND.
- **The push as the first step into a chat.** Arm B-chat adds **Ask Claude** next to Read on the
  card, as a handoff (`claude.ai/new?q=…` with the headline and abstract; free, logs only the
  tap) or an own chat on the card (per-token cost, sees every turn). A chat after the tap can only
  change taps if readers expect it, so the push copy should offer it, and any lift should grow
  over the first weeks. Primary outcome: reads, not opens (opens alone would reward a chat that
  replaces reading). Keep `ask` out of the click label. Related: learned push timing and volume
  (Pinterest, LinkedIn, Duolingo), LLM-written push text (PushGen, WSDM '26); no test we found of
  a push as the first step into a chat.

All three bet on long-run engagement, so offline AUC is the wrong yardstick (the
[LLM ablation](analysis.md#results) shows how little a new signal moves it). They need at least
four weeks, reported by week to separate a novelty bump from a lasting effect, and are analyzed
per reader as above.
