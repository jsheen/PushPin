# Proposed A/B test

The NYT's own push strategy appears to send many pushes a day to everyone. PushPin is more
selective: the model picks an article every two hours and the send policy pushes it only when
it's worth it, at most a few a day (see [notifier.md](notifier.md#how-many-pushes)). Readers
don't choose a number. The test compares three arms:

| Arm | Pushes | Papier-mâché (daily pictures) |
|-----|--------|--------------------------|
| A | NYT-style: many a day, same for everyone | — |
| B | Selective: model-picked, sent only when it clears the send policy | Off |
| C | Selective: model-picked, sent only when it clears the send policy | On |

A vs. B tests the selective strategy; B vs. C tests whether the pictures add engagement on top
of it.

## Assignment

A fixed rule assigns each reader to an arm, for example a hash of `reader_id`, so a reader always
stays in the same arm and the assignment can be recomputed later. The arm is stored on the
reader. In arms A and B the papier-mâché switch is hidden; in arm C it can't be turned off (or
every change is logged), so the server knows who saw the pictures. `/demo` is outside the test.

## Outcomes

From the interactions table:

- **Clicks per reader per day**: total engagement.
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
| Pushes per reader over 4 weeks | ≈ 56 (at the cap of 2 a day) |
| Within-reader correlation (ICC, assumed) | 0.05 |
| Design effect, 1 + (56 − 1) × 0.05 | ≈ 3.75 |
| Pushes per arm, adjusted | ≈ 18,400 |
| Readers per arm | ≈ 330 |
| Readers in total (3 arms) | ≈ 1,000 |

Quiet days send fewer than the cap, which raises this somewhat; the ICC is a guess until
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

## Later arms: retention signals beyond similarity

*Not built; candidates for follow-up tests once arm B has a baseline.* The model learns two things
about what a reader wants: similarity ("more of what you read") and complementarity
(`complement_score`: across all users, people who read subcategory *a* go on to read *b*). Two more
ideas, borrowed from social feeds, aim at keeping readers over weeks rather than winning the next
click. Each would be an arm B′: arm B plus one bonus in the push objective.

### Nostalgia: bring back an interest that has gone quiet

A reader who loved a subcategory but hasn't been pushed it in weeks may welcome it back. The
objective can't see this today. `novelty` counts all of a reader's exposure, history clicks
included, so a well-loved subcategory always looks familiar, however long ago it was last shown.
The signal is the gap between **long-run share** and **recent share**:

- long-run: `subcat_affinity` (share of history clicks), or exposure share over all time
- recent: share of the last N days of pushes, or `hours_since_last_shown_in_subcat` and
  `hours_since_last_click_in_subcat`

It has to tell two cases apart:

| Case | Pattern | What to do |
|------|---------|------------|
| **Dormant** | Strong history, not pushed lately | Bring it back: rediscovery bonus |
| **Abandoned** | Pushed lately and skipped | Leave it alone: that's fatigue (`subcat_prior_skips`, `slot_skipped_72h`) |

The bonus is roughly `affinity × f(gap)`, alongside the novelty term.

Before spending an arm on it, MIND can give a rough check. Its pre-period history has no timestamps
and the log covers about a week, so real multi-week gaps aren't observable. But the
[exploration analysis](analysis.md#do-clicks-on-unfamiliar-subcategories-stick) design carries
over: for subcategories in a user's history, compare the click rate on the first log-period
exposure of a **dormant** one (in history, not yet shown in the log) with a **steady** one (shown
again and again). The push log has real timestamps, so the test itself measures actual gaps.

### Bridges: one article that combines two interests

Someone who reads Business and Soccer may like "the economics of the World Cup" more than either
topic alone. This works per article and per reader. Complementarity is different: it's measured
across all users and links pairs of subcategories. Ways to score a candidate:

- **Embeddings.** Group the reader's clicks into interests (clusters of clicked-article vectors, or
  the LLM profile's click themes). Score = the max over pairs (i, j) of min(sim to i, sim to j):
  high only if the article is close to *both*. `llm_sim_click` uses only the best-matching theme,
  so it can't see this.
- **Entities.** The article mentions entities the reader has read about in two different
  subcategories. Cheap and easy to explain.
- **Generated queries.** The LLM writes a few bridge queries per reader from pairs of their themes,
  and the article store retrieves matches. Closest to how social feeds do it, and the most expensive.

The NYT feeds suit this better than MIND, because they carry many cross-desk pieces
(Business × Technology, Science × Politics).

### Outcomes for these arms

Both are bets on long-run engagement, so offline AUC is the wrong yardstick: the
[LLM ablation](analysis.md#results) shows how little a new signal moves AUC once similarity is
covered. On top of the outcomes above (return rate, opt-outs, clicks per reader per day), these
arms add:

- **Later reading of the topic**: after a rediscovery or bridge push, does the reader tap that
  subcategory again in the following weeks without the bonus?
- **Per-push CTR of bonus pushes vs. arm B's**: the click cost paid now, to set against any gain
  in return and opt-outs.

They need longer than arm B vs. C: at least four weeks, since the effect is slow and a single
rediscovery push per reader is rare. Analysis is per reader, as above.

## Later arm: the push as the first step into a chat

*Not built.* The idea: a push becomes more than a link. Tapping it can lead to a conversation
with an AI assistant about the article ("is this worth my time?", "what's the main argument?",
"remind me Saturday"). The test asks whether that makes readers engage with pushes more, and
whether it leads to more reading or replaces it.

| Arm | Pushes | After the tap |
|-----|--------|---------------|
| B | Selective, model-picked | Profile with the article pinned; Read on NYT |
| B-chat | Same | Same, plus **Ask Claude** next to Read |

Assignment works as for the other arms: a hash of `reader_id`, with B-chat drawn from readers
who would otherwise be in B. Keep papier-mâché off in both, so only the chat differs.

### Two ways to build the chat

| | Handoff | Own chat |
|---|---|---|
| How | Ask Claude opens `claude.ai/new?q=…` with the headline, abstract and link filled in | A chat on the card page, calling the Claude API |
| Cost | None; it runs on the reader's own Claude account | Per token (cents per conversation) |
| What we see | Only the tap (log an `ask` interaction before redirecting) | Every turn |
| Catch | Reader needs a Claude account; Claude can't read paywalled NYT text, so the prompt carries the headline and abstract | We pay, and we store conversations (privacy policy must say so) |

The handoff is enough for a first test: the question is whether the option changes behavior,
and the `ask` tap measures whether people use it. The own chat is needed only to study *what*
people ask.

### Can it change engagement with the push itself?

A chat that appears only *after* the tap can't change whether the reader taps. It can change
taps only if the reader expects it, so the arm has to make it visible before the tap:

- push copy that offers it (e.g. ending with "Tap to ask about it"), or a notification action
  where the platform allows one (an iOS text-reply action; buttons on Android and Web Push);
- and time: readers learn over days that a push leads somewhere useful, so any lift in opens
  should grow over the first weeks rather than appear on day one.

If opens don't move but reads after a tap do, the chat helps after the tap, not before it.
That is a different claim and should be reported as one.

### Outcomes

On top of the core outcomes (opens per reader per day, return rate, opt-outs):

- **Opens per push, by week**: the push-engagement question; compare week 1 with weeks 3–4 to
  separate a novelty bump from a learned habit.
- **Reads per reader per day**: the guardrail. If opens rise and reads fall, the chat is
  replacing the article.
- **Ask rate**: share of opened pushes with an `ask`.
- **Ask without read**: an ask with no read of that article within 24 hours. Report it on its
  own; it may mean the chat was enough, which is neither a win nor a failure.
- **Return rate after an ask**: do readers who chat come back on later days more than those who
  don't (within B-chat; descriptive only, since asking isn't randomized).

Decide before the test which of opens or reads is primary. Reads are the safer choice: opens
alone would reward a chat that replaces reading.

### Analysis and cautions

- Per reader, as above. The comparison is B vs. B-chat; within-arm comparisons of askers and
  non-askers are descriptive, because readers choose whether to ask.
- **Novelty.** Run at least three to four weeks and report by week. Studies on adaptive
  notifications have found effects that fade or vanish (e.g. Mishra et al. on receptivity), and
  a new AI feature invites a first-week bump.
- **Training labels.** `ask` is a new interaction kind. Keep it out of the click label (an open
  is still the label), and keep the arm as a feature or filter when retraining, as with
  papier-mâché, so chat-driven opens don't shift the model for arm B.
- **Sample size.** The table above applies: a 20% relative lift in opens needs ~330 readers per
  arm. With a class-sized group this is a pilot: does anyone use Ask Claude, and does the
  readout work.

### Related work

The pieces exist separately: learned push timing and volume (Pinterest's volume control,
LinkedIn's offline RL for notifications, Duolingo's notification bandit), LLM-written push text
(PushGen, WSDM '26), and assistants that reach out and then converse (LLM-generated just-in-time
adaptive interventions in health; ChatGPT Pulse, Google CC). We found no test of whether making
a push the first step into a chat raises engagement with the pushes, or the reading they lead to.
