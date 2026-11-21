# AI interface: lock-screen agents

Phones now put an AI layer between a push and the reader. Apple Intelligence and Android's
Gemini features summarize stacks of notifications from the same app, and sometimes a single
long one. A summary changes what the lock screen **shows**, but the original notifications are
still there and can still be opened. Tapping a summarized stack expands it, and tapping an
individual push inside opens it as usual.

Summaries don't block opens. They make an open less likely in two ways.

## 1. Extra friction

A push inside a summarized stack is one tap further away: first expand the stack, then open
the push. That cost lands mostly on high-volume senders whose pushes pile up. PushPin's send
policy averages 0.41 pushes per active reader-day, so most of its pushes arrive alone and
skip this cost. Arm A of the [A/B test](ab-test.md) (NYT-style, many a day) is the arm most
likely to get stacked.

## 2. Informed without opening

If the summary tells the reader what happened, some of them won't tap. They got value from
the push, but the log records it as **ignored**. For PushPin this matters more than the
friction does:

- The bandit's `ignored this week` and open-rate features (`src/pushpin/bandit.py`) would read
  a satisfied reader as a fatigued one, so the bandit would send them fewer pushes than it
  should.
- The reward counts return visits, not opens, so it already handles part of this case. But if
  the summary delivers the news, one push causes fewer return visits, and the planted push
  effect in the NYT simulation ([uplift.md](uplift.md), [bandit.md](bandit.md)) doesn't
  account for that.

## A second gate: the agent's own selectivity

Lock-screen agents do more than summarize. They also decide what deserves attention.
Apple's prioritized notifications move what its model thinks matters to the top of the stack
and leave the rest below. Focus modes and scheduled digests hold back what they judge can wait.
The agent is selective in the same way PushPin is, so a push now has to get through two gates:

1. **PushPin's send policy** decides whether the pick is worth sending to this reader now
   (the fixed rule today, the bandit in simulation).
2. **The agent on the phone** decides whether the push it received gets surfaced, demoted, or
   folded into a summary or digest.

What this means for PushPin:

- **The gates mostly agree.** Both reward pushes that are few, personal, and timely. A
  selective sender that pushes things the reader acts on is probably the kind the agent learns
  to surface. A sender that pushes everyone the same many times a day (arm A) is probably the
  kind it learns to demote. If so, the second gate widens the gap between selective and NYT-style
  pushing, beyond what the [A/B test](ab-test.md) would show on phones without the agent.
- **Ignored pushes count twice.** The agent likely learns from the same taps and dismissals the
  bandit does. A push that's ignored makes the bandit more cautious, and it may also lead the
  agent to demote the sender's next push. That second effect raises the real cost of a bad
  push above the λ and V set by hand.
- **Some pushes are more exposed than others.** Novelty picks (friction 0.2) are topics the
  reader hasn't shown interest in, so an agent ranking by past engagement is likely to rate
  them low. The editorial override's "important for everyone" stories depend on the agent
  agreeing that they're important.
- **The second gate can't be observed.** ntfy reports delivery, not whether the push was
  surfaced, demoted, or summarized. A demoted push and an ignored push look the same in the
  logs, which adds to the "ignored" ambiguity in section 2.

None of these effects has solid public numbers. Apple doesn't publish them, and reports from
publishers are anecdotal. Any simulation of the effect would need to plant an "absorbed"
probability and sweep it over a range, the same way the push effect is handled now.
