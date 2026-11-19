# Contextual bandit: when to push

XGBoost picks **which article** each reader gets. The send decision is the separate question
of **whether to push it now**. Today that's a fixed rule
(`p_click ≥ 0.07 + 0.04 × (1 − p_return)`, at most 2 a day). The contextual bandit learns that
decision instead, from what our pushes actually did, and is tested here on the NYT simulation
against pushing the latest breaking story at every slot.

Code: `src/pushpin/bandit.py`, `configs/bandit.yml`, `tests/test_bandit.py`. Plots:
`docs/bandit.png`, `docs/bandit_grid.png`. **Status:** simulation only; live still uses the fixed rule.

## Contents

- [Two models, two jobs](#two-models-two-jobs)
- [The reward and its two prices](#the-reward-and-its-two-prices)
- [The bandit](#the-bandit)
- [The planted world](#the-planted-world)
- [Policies compared](#policies-compared)
- [Results across the two prices](#results-across-the-two-prices)
- [What it learned](#what-it-learned)
- [Limitations](#limitations)
- [Running it](#running-it)

## Two models, two jobs

| | XGBoost | Contextual bandit |
|---|---|---|
| **Question** | Which article would this reader open? | Is this pick worth sending to this reader *now*? |
| **Learns from** | MIND feed logs (supervised, offline) | Its own send decisions and their outcomes (online) |
| **Target** | P(click), P(return) | sessions − λ·pushes − V·opt-outs |
| **Inputs** | Reader × article: history similarity, subcategory affinity, embeddings, Claude reader profiles, article age, slot engagement | XGBoost's outputs plus context about *our pushes* (below) |

The bandit gets no article features (that's XGBoost's job). XGBoost never sees what a push
did: the no-push outcome, annoyance or opt-outs. XGBoost's few fatigue features
(`slot_skipped_72h`, `impr_last_24h`, ...) come from feed visits readers chose to make, and their
effect is small (about 2% either side of a reader's norm; [analysis.md](analysis.md)), so they
aren't the core of the model.

## The reward and its two prices

```
reward = sessions − λ × pushes − V × opt-outs
```

Both prices are in sessions and are **set by hand**, not learned:

- **λ, the cost of one push**: how many extra visits a push must cause to be worth an
  interruption. λ = 0.04 means one extra visit per 25 pushes.
- **V, the cost of a reader turning notifications off**: they still visit on their own, so
  what's lost is what their future pushes would have caused,
  ≈ 180 days × ~1 push a day × ~0.03 net per push ≈ 5.

Because both are value judgments, the result is reported across a grid,
λ ∈ {0.02, 0.04, 0.08} × V ∈ {2, 5, 20}, not at one setting.

## The bandit

At every slot, XGBoost's top unpushed article is the candidate, and the bandit decides send or
skip from a context vector z:

| Feature | Why |
|---|---|
| `p_click`, `1 − p_return` (risk), their product | XGBoost's outputs, as summaries |
| recent pushes (last 4 hours) | short-term fatigue |
| pushes ignored this week | annoyance |
| log hours since the last open | how engaged they are right now |
| the reader's open rate on our pushes | their own response to *us* |
| is this their hour: their organic session rate at this hour ÷ their own average, from slots without a push | personal timing, kept separate from how often they come back at all (which risk covers) |

There's no clock-time feature. An earlier version had hour of day as a sine/cosine pair, which
can only draw one bump per day. With the true average effect flat across hours, that bump landed
at midday on noise.

The model is a Bayesian linear regression on `[z, send × z]`, with a Gaussian prior. The target
is each decision's reward minus the reader's own organic session rate at that hour (estimated
from slots without a push), so the model only has to explain what a push adds. Without that, the
linear model's misfit of the organic baseline (which is really `p_return × preference`) leaked
into the effect weights, because sends aren't random. The bandit then learned to push *away*
from readers' best hours, where the true effect is 2× larger. The `send × z` weights θ are the effect of sending. **Thompson sampling** draws
θ from the posterior once per reader-day and sends when θ · z > 0. The posterior is refit every
simulated night from all decisions so far (a batched bandit). Because z contains `p_click`, the
rule is a threshold on `p_click` that moves with context: it rises after recent or ignored
pushes and falls at the hours a reader tends to come back.

## The planted world

There are no real no-push outcomes (see [uplift.md](uplift.md#live-data)), so outcomes are
planted on real candidates. The 8-slot NYT simulation (`outputs/nyt-sim-8slot`: 1,000 MIND
readers, slots every two hours from 7 to 21, each reader's top 20 candidates plus the slot's
newest article) is replayed for 13 weeks with fresh draws and carried-over reader state. Per
slot (`configs/bandit.yml` has the values):

```
organic return  y0  = organic × p_return × pref(hour)
push effect     tau = lift × (1 − p_return) × interest × pref(hour) × exp(−annoyance / tolerance)
                      − fatigue × recent pushes
opened          P   = p_click × pref(hour) × exp(−annoyance / tolerance)
opt-out         P   = hazard × ((1 − p_return) / mean)^1 × annoyance / tolerance   (after an ignored push)
```

- **Annoyance** rises by 1 per ignored push and halves every 3 days.
- **Tolerance** varies by reader and is higher for readers who click more.
- **Preference by hour:** each reader prefers morning, midday or evening.
- **Risk cuts both ways:** at-risk readers gain the most from a good push but also quit the
  most easily when annoyed. The old rule fixes the sign of the risk term; the bandit has to
  learn how to weigh it.

Every policy sees the same random draws (common random numbers), so differences between
policies aren't noise from different luck.

## Policies compared

| Policy | Rule |
|---|---|
| **Contextual bandit** | as above |
| **Breaking** | the newest article to every reader at every slot (8 a day, no model, no cap) |
| **Best fixed threshold** | `send if p_click ≥ c`, best of c ∈ {0.04, …, 0.30} chosen *in hindsight*: the most a rule without context could reach |
| **Never** | the floor |

## Results across the two prices

![Contextual bandit across λ and V](bandit_grid.png)

Reward relative to never pushing, per reader-day, over 13 weeks:

| λ | V | Bandit | Bandit pushes/day | Bandit opted out | Best threshold (c) | Best threshold | Breaking |
|---|---|---|---|---|---|---|---|
| 0.02 | 2 | **+0.0130** | 1.60 | 5.3% | 0.2 | +0.0003 | −0.27 |
| 0.02 | 5 | **+0.0160** | 1.47 | 4.5% | 0.2 | +0.0003 | −0.29 |
| 0.02 | 20 | **+0.0107** | 1.22 | 3.9% | 0.2 | +0.0003 | −0.39 |
| 0.04 | 2 | **+0.0018** | 0.86 | 2.9% | 0.3 | −0.0000 | −0.38 |
| 0.04 | 5 | **+0.0074** | 1.00 | 3.3% | 0.3 | −0.0000 | −0.40 |
| 0.04 | 20 | **+0.0042** | 0.83 | 3.5% | 0.3 | −0.0000 | −0.50 |
| 0.08 | 2 | −0.0060 | 0.52 | 1.3% | 0.3 | −0.0002 | −0.59 |
| 0.08 | 5 | −0.0077 | 0.55 | 1.3% | 0.3 | −0.0002 | −0.61 |
| 0.08 | 20 | −0.0039 | 0.47 | 0.8% | 0.3 | −0.0002 | −0.71 |

- **Breaking is far worse everywhere.** Over 99% of its pushes are ignored, annoyance builds, and
  59% of readers turn notifications off.
- **At λ = 0.02 and 0.04, the bandit is the only policy that gains:** +0.002 to +0.016 a
  reader-day over never pushing, while the best fixed threshold gets about zero. A rule
  without context can't find pushes worth their cost; the bandit finds them by reader and moment.
- **At λ = 0.08, pushing doesn't pay in this world**, and the right answer is "almost never
  push". A very high fixed threshold (0.3, under 0.01 pushes a day) gets there. The bandit pushes
  about 0.5 a day and ends 0.004–0.008 below never.
- **It reacts to the prices in the right direction.** It pushes less as λ rises
  (1.2–1.6 → 0.8–1.0 a day) and as V rises.
- **It learns within weeks.** At λ = 0.04, V = 5 it loses in week 1 (−0.014, exploring) and is
  ahead of never pushing from week 3 on (+0.006 to +0.016 a week; left panel below).

## What it learned

![Contextual bandit at λ = 0.04, V = 5](bandit.png)

Posterior means of the effect-of-sending weights at λ = 0.04, V = 5:

- **Risk: +0.043, and p_click × risk: +0.048.** Push at-risk readers more, and more so when the
  pick is appealing, even though at-risk readers quit more easily when annoyed. Here the larger
  effect wins: the opposite of the old rule's `+0.04 × (1 − p_return)` in the threshold.
- **Push history:** recent pushes −0.044, pushes ignored this week −0.027, the reader's own open
  rate +0.011. Back off after pushes and after ignored ones; keep pushing readers who open.
- **Is this their hour: +0.006 per unit (± 0.001).** It pushes at readers' own best times: in
  the last three weeks it sends 28% of decisions at a reader's best hours and 5% at their worst,
  where the true effect is about half as large. Because of the 4-hour fatigue window, a push at
  7 makes 9 costly, so pushes per hour alternate (middle panel).
- **`p_click` alone: −0.017 (± 0.012), uncertain.** Its effect shows up through the
  interaction with risk instead: an appealing pick is worth sending mainly to readers who
  wouldn't come back on their own.

## Limitations

- **Planted outcomes.** The world was set before the results and not tuned afterwards, but the
  answer still depends on it: on these planted rules, not on real readers.
- **Carryover isn't credited.** Each decision's reward is its own slot's session, push cost and
  opt-out. An ignored push also lowers later effects and opens (annoyance decays over days), and
  that later cost isn't charged to the push that caused it. This is the likely reason the bandit
  over-pushes at λ = 0.08. Fixes: a longer reward window per decision, or pricing annoyance
  directly (a third price), or a reinforcement-learning view of the reader's state.
- **Exploration cost.** Thompson sampling keeps trying sends while uncertain; with 1,000 readers
  that cost is visible over 13 weeks.
- **Linear in z.** Interactions beyond `p_click × risk` aren't modeled. The organic baseline is
  subtracted using the bandit's own estimate, which is noisy for new readers.
- **Live needs an outcome without a push.** The reward needs sessions in both arms. Live, that
  means per-reader delivery (one ntfy topic per reader) and an observable visit (e.g. opening
  `/me`); see [uplift.md](uplift.md#live-data).

## Running it

```bash
uv run pushpin-bandit            # one comparison at λ = 0.04, V = 5 (a few seconds)
uv run pushpin-bandit --grid     # plus every (λ, V) in the grid
```

It needs `outputs/nyt-sim-8slot` from the push job:

```bash
docker compose up -d scorer
docker compose run --rm --no-deps push-job "pushpin-push-job --scorer http://scorer:8000 \
  --slot-hours 7,9,11,13,15,17,19,21 --day-pool --keep-top 20 --out outputs/nyt-sim-8slot"
```

Outputs go to `outputs/bandit/`: `summary.json`, `policies.csv`, `weekly.csv`, `hours.csv`,
`coefficients.csv`, `bandit.png`, and with `--grid`, `grid.csv` and `bandit_grid.png`.
