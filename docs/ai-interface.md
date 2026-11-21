# AI interface: lock-screen agents

Phones now put an AI layer between a push and the reader. Apple Intelligence and Android's
Gemini features summarize stacks of notifications, and sometimes a single long one. The
original pushes can still be opened: tap the stack to expand it, then tap a push.

## What's known so far (November 2026)

**Summaries had a public setback.** Apple paused summaries for news apps in January 2025 after
they produced false headlines, including one attributed to the BBC
([TechCrunch](https://techcrunch.com/2025/01/16/apple-pauses-ai-notification-summaries-for-news-after-generating-false-alerts)).
They returned in iOS 26, italicized and labeled "Summarized by Apple Intelligence," with users
choosing which categories get them
([MacRumors](https://www.macrumors.com/2025/07/22/ios-26-beta-4-notification-summaries/)).
Senders can't opt out ([Courier](https://www.courier.com/blog/ios-27-notifications-marketers)).

**Apple may be shifting toward reranking.** Getting the order wrong is an inconvenience, but
a wrong summary is a false headline. Since the pause, Apple has added Prioritize
Notifications, which reorders the stack, and Reduce Interruptions, which lets only urgent
pushes through. This is our inference, not Apple's statement. The ranking signals aren't
public, and the only documented sender control is the interruption level ("time-sensitive"
breaks through Focus).

**iOS 27** (September 2026):

- **Related alerts merge** into one live summary
  ([MacRumors](https://www.macrumors.com/roundup/ios-27/)). This mostly affects senders who push
  a developing story many times, such as arm A.
- **Prioritization can be turned off per app** from a single notification
  ([iThinkDiff](https://www.ithinkdiff.com/how-to-turn-off-apple-intelligence-features-ios-27/)).
- **China gets both features** in iOS 27.3, expected in December
  ([MacRumors](https://www.macrumors.com/2026/10/30/ios-27-3-apple-intelligence-china/)).
- **The new Siri's model was trained with Google's Gemini team**
  ([MacRumors](https://www.macrumors.com/2026/06/02/ios-27-siri-features/)). Whether
  notifications use it is unconfirmed.

**There's no measured data.** Nobody has published how these features affect push engagement
(Courier). The closest evidence is from Gmail, where some senders' open rates fell 30% or more
quarter over quarter. AI summaries are a "likely but unproven" cause, and clicks fell less than
opens. The rest of this doc is reasoning. We haven't seen what the features do to PushPin's
pushes: ntfy can't report it, and we haven't tested on a phone.

## How summaries affect opens

1. **Extra friction.** A push inside a stack is one tap further away. This mostly hurts
   high-volume senders like arm A of the [A/B test](ab-test.md). PushPin averages 0.41 pushes
   per active reader-day, so its pushes usually arrive alone.
2. **Informed without opening.** If the summary says what happened, some readers won't tap.
   The log then calls the push **ignored**, even though it informed the reader. For PushPin
   this is the bigger issue:
   - The bandit's ignored and open-rate features (`src/pushpin/bandit.py`) would read a
     satisfied reader as a fatigued one and send them too few pushes.
   - The reward counts return visits, not opens, which helps. But a push whose summary already
     delivered the news brings fewer returns, and the simulation's planted push effect
     ([uplift.md](uplift.md), [bandit.md](bandit.md)) ignores that.

## A second gate: the agent's own selectivity

A push now passes two gates: PushPin's send policy, then the phone's agent, which surfaces it,
demotes it, or folds it into a summary. Both are personalized, and each sees what the other
can't:

| | PushPin | Lock-screen agent |
|---|---|---|
| **Personalizes on** | Reading history, article embeddings, Claude reader profiles | Taps and dismissals across all apps, Focus, time of day |
| **Can't see** | Other apps, how crowded the lock screen is | Why this article fits this reader |
| **Decides** | Whether to send, which article, when | Whether and how it's surfaced |

What this means for PushPin:

- **The gates mostly agree.** Both favor pushes that are few, personal, and timely. If the
  agent learns to demote senders that push everyone the same thing many times a day, the gap
  between arm A and the selective arms grows beyond what phones without an agent would show.
- **Ignored pushes count twice.** They make the bandit more cautious and may get the next push
  demoted, so a bad push costs more than the hand-set λ and V.
- **Novelty picks and editorial overrides are exposed.** An agent that ranks by past engagement
  will likely rate unfamiliar topics low.
- **The second gate can't be observed.** ntfy only reports delivery, so a demoted push looks
  the same as an ignored one.

## Future opportunity: an API for personal agents

**Status:** an idea, worth building only if lock-screen agents become common.

Today an agent summarizes a push from its text alone. Instead, a reader's agent could ask
PushPin for:

- a summary PushPin wrote
- why the story fits this reader
- how much it matters (`p_click`, the send-policy decision, the editorial flag), so the agent
  can show it now or hold it for a digest
- the `/p/{push_id}` card link, so taps are still logged

The store and `src/pushpin/api.py` already have most of this. A `GET /readers/{id}/brief`
endpoint with an MCP wrapper would cover it, and agent fetches would be logged, unlike pushes
after ntfy delivers them.

Open questions:
- whether Apple accepts outside services
- rights to summaries of full NYT articles
- opt-in per-reader tokens, since the brief exposes the reader profile
- keeping agent fetches out of the bandit's features

## Future opportunity: developing stories as threads

**Status:** an idea. iOS 27 already merges related alerts, so PushPin could treat a developing
story as one thread:

1. **Group articles into threads**: very close embeddings, same section, within a few hours.
   Neither NYT RSS nor MIND labels threads.
2. **Push the first article** through the normal send policy.
3. **Follow-ups go only to readers who opened it.** The bandit could learn this from an "opened
   earlier in this thread" feature, with each follow-up still costing λ.
4. **Link every push to one card that stays current.** The running summary then comes from
   PushPin, not from the lock screen, where the BBC error happened.

Threads would need an exemption from friction, since follow-ups are the opposite of novelty,
and they'd give the A/B test a sharp contrast with arm A. ntfy can't send Live Activities.
MIND has no threads, so the follow-up effect needs live logs or a planted simulation.
