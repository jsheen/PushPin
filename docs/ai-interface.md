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

The second gate is personalized as well. The agent ranks notifications by what it learns about
this particular reader: which apps and senders they open, what they dismiss, and when they
check their phone. So a push now goes through two personalization layers, each of which sees
things the other doesn't:

| | PushPin | Lock-screen agent |
|---|---|---|
| **Personalizes on** | Reading history, article content and embeddings, Claude reader profiles, the reader's responses to PushPin's own pushes | The reader's taps and dismissals across every app, plus on-device context (Focus, time of day, what else is waiting) |
| **Can't see** | Everything outside PushPin: other apps, other news sources, how busy the reader's lock screen is | Why this article fits this reader; it only sees the text of the push |
| **Decides** | Whether to send, which article, and when | Whether the push gets surfaced, and how |

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

## Future opportunity: an API for personal agents

**Status:** an idea, not built. Lock-screen agents are new, and many readers don't have them yet
because they need a recent phone and have to be turned on. This is worth building only if they
become common.

Right now an agent summarizes a push from its text alone. Instead, PushPin could offer an API
that a reader's personal agent calls to get the summary and PushPin's judgment directly. The
agent then has things it can't produce on its own:

- **A summary PushPin wrote and stands behind**, not the agent's rewrite of a headline.
- **Why the story fits this reader:** subcategory similarity and the Claude reader profile.
- **How much it matters and when:** `p_click`, whether it passes the send policy, the editorial
  override flag, and whether it's a novelty pick. The agent could use these to decide between
  showing it now and holding it for a digest.
- **A link to the `/p/{push_id}` card**, so taps are still logged.

Most of this is already computed. The notifier's store holds each reader's picks, push IDs and
taps, and `src/pushpin/api.py` already serves `/score` and `/push`. A `GET /readers/{id}/brief`
endpoint would repackage the current pick or picks, and an MCP wrapper would let tool-using
agents call it.

It would also partly solve the measurement problem above. Each agent fetch is logged on
PushPin's side, unlike what happens to a push after ntfy delivers it.

Open questions:

1. **Which agents could use it.** Agents that call tools (for example over MCP) could. Whether
   Apple Intelligence lets outside services feed its lock-screen summaries is unverified.
2. **Rights.** Serving NYT RSS abstracts again is different from writing and distributing
   summaries of full articles.
3. **Auth and privacy.** Readers are identified by cookie now. An agent would need a per-reader
   token, and the brief exposes the reader profile, so it should be opt-in.
4. **Keeping the bandit's data clean.** Agent fetches should be logged as their own event type,
   not mixed into the open-rate and ignored features.
