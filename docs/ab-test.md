# Proposed A/B test

The NYT's own push strategy appears to send many pushes a day to everyone. PushPin is more
selective: readers opt in to a number of pushes a day (light 2, medium 4, heavy 8, spread over the
day; see [notifier.md](notifier.md#push-volume)), and the model picks which articles fill those
slots. The test compares three arms:

| Arm | Pushes | Papier-mâché (dinosaurs) |
|-----|--------|--------------------------|
| A | NYT-style: many a day, same for everyone | — |
| B | Selective: the reader's chosen volume, model-picked | Off |
| C | Selective: the reader's chosen volume, model-picked | On |

A vs. B tests the selective strategy; B vs. C tests whether the dinosaurs add engagement on top
of it.

## Assignment

A fixed rule assigns each reader to an arm, for example a hash of `reader_id`, so a reader always
stays in the same arm and the assignment can be recomputed later. The arm is stored on the
reader. In arms A and B the papier-mâché switch is hidden; in arm C it can't be turned off (or
every change is logged), so the server knows who saw dinosaurs. `/demo` and public `/u/<slug>`
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
retraining. That way clicks the dinosaurs encourage don't bias the model.

With a handful of readers this won't reach significance. The point is that the experiment is
wired up and the readout is defined.
