---
symbol: BAJFINANCE
thesis: Rate-sensitive lenders move on RBI policy days; be positioned two weeks before the December 2026 meeting, not after, and let a pre-written rule decide the exit.
entry_date:
horizon: 3-4 weeks
target: 0
invalidation: RBI brings the meeting forward or an inter-meeting action happens before entry (then do not enter: delete the queued order and note it here)
invalidation_price: 0
size_cap_pct: 15
status: proposed
type: catalyst
event: RBI Monetary Policy Committee decision, December 2026 meeting (exact date to be confirmed from the RBI calendar; estimated 4 Dec 2026)
event_date: 2026-12-04
post_event_rule: hold if close on decision day > entry price, then trail stop 5% from the highest close; otherwise sell at next open
post_event_applied:
---

## Why (the long form)

Analysts in October 2026 describe the RBI as caught between inflation and a weak rupee, with the next
meeting in early December. That makes the decision genuinely uncertain, which is when a policy day
moves rate-sensitive stocks most. Bajaj Finance is the most liquid pure consumer lender in the Nifty
50 and reacts to rate expectations more than the banks do.

The lesson applied: the money on a scheduled catalyst is made by being positioned before it. The
order is queued now with decision_date 2026-11-19, so it fills at the first open after that date,
about two weeks before the meeting. Size Rs 75,000 (15% cap). Close on 15 Oct 2026: Rs 965.25.

This is not a forecast of the decision. The post-event rule handles both outcomes: a favourable
reaction is held with a trailing stop; an unfavourable one is sold the next morning.

## What would change my mind

- The RBI moves before the meeting, or the meeting date changes: see invalidation.
- A Bajaj Finance-specific event before entry (results on or about 20-25 Oct 2026, a regulatory
  action): then the trade is no longer a clean policy-day test; the reviewer may delete the queued
  order and say why.

## Review log

- 2026-10-17 (opened by Claude as proposed): entry deliberately delayed to 2026-11-19. Reviewer:
  confirm the December MPC date from rbi.org.in and set event_date to it.
