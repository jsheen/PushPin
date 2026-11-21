# AI interface: lock-screen summaries

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

Neither effect has solid public numbers. Apple doesn't publish them, and reports from
publishers are anecdotal. Any simulation of the effect would need to plant an "absorbed"
probability and sweep it over a range, the same way the push effect is handled now.
