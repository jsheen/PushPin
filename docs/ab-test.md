# Proposed A/B test

The NYT's own push strategy appears to send many pushes a day to everyone. PushPin is more
selective: readers opt in to a number of pushes a day (light 2, medium 4, heavy 8, spread over the
day; see [notifier.md](notifier.md#push-volume)), and the model picks which articles fill those
slots. The test compares three arms:

| Arm | Pushes | Papier-mâché (daily pictures) |
|-----|--------|--------------------------|
| A | NYT-style: many a day, same for everyone | — |
| B | Selective: the reader's chosen volume, model-picked | Off |
| C | Selective: the reader's chosen volume, model-picked | On |

A vs. B tests the selective strategy; B vs. C tests whether the pictures add engagement on top
of it.

## Assignment

A fixed rule assigns each reader to an arm, for example a hash of `reader_id`, so a reader always
stays in the same arm and the assignment can be recomputed later. The arm is stored on the
reader. In arms A and B the papier-mâché switch is hidden; in arm C it can't be turned off (or
every change is logged), so the server knows who saw the pictures. `/demo` and public `/u/<slug>`
pages are outside the test.

## Outcomes

From the interactions table:

- **Clicks per reader per day**: total engagement.
- **Opt-outs and lowered push volume**: the risk the send policy manages.
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
| Pushes per light reader over 4 weeks | ≈ 56 (2 a day) |
| Within-reader correlation (ICC, assumed) | 0.05 |
| Design effect, 1 + (56 − 1) × 0.05 | ≈ 3.75 |
| Pushes per arm, adjusted | ≈ 18,400 |
| Readers per arm | ≈ 330 |
| Readers in total (3 arms) | ≈ 1,000 |

Medium and heavy readers get more pushes, which lowers this somewhat; the ICC is a guess until
real data gives one. With 10 readers per arm, only a difference of about 9 points (CTR roughly
doubling) would be detectable. So with a class-sized group this is a pilot: it shows the
experiment runs end to end and what the readout looks like, not a conclusive result.

## Timeline

How a feature like this typically ships at a large tech or media company (not the NYT's actual
process, which isn't public here):

| Stage | Typical length | What happens |
|-------|----------------|--------------|
| Build | Weeks to months | Design, engineering, privacy/legal review, instrumentation |
| Ramp | Days | Behind a feature flag to 1–5% of users, to catch bugs and check the logging |
| Test | 1–4 weeks | The A/B test; at least a full week for weekday/weekend cycles, longer for slow effects like opt-outs or a novelty bump that fades. Many tests run at once on separate users |
| Roll out | Weeks to months | Gradual release to everyone if it wins, often with a small holdout kept on the old version to see whether the effect lasts |

Idea to full launch is usually a quarter or more. At NYT scale, ~1,000 readers enroll within
minutes; here, enrollment is the bottleneck.

For PushPin, the compressed version:

| Piece | Effort |
|-------|--------|
| Arm assignment, hiding the papier-mâché switch, arm A sending (the same top headline to everyone, every slot) | ~1 day |
| Readout query and per-reader analysis (bootstrap, clustered standard errors) | ~1 day |
| Weekly retraining job (see below) | 2–3 days |
| Running the test | 2–4 weeks |

## Retraining during the test

The plan is to retrain each week, and that is part of what arms B and C are. Two cautions:

- **Leakage between arms.** A model trained on everyone's clicks lets picture-driven clicks from
  arm C shape arm B's pushes. Train with the arm as a feature, or pool B and C and say so.
- **Small data.** A few hundred pushes a week is too few to retrain XGBoost from scratch. Update
  the calibration weekly and retrain on MIND plus the logs (the `scores` table stores each
  candidate's features; the `labeled` view gives the labels).
