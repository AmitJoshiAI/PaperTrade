---
symbol: BAJFINANCE
thesis: Rate-sensitive lenders move on RBI policy days; be positioned two weeks before the December 2026 meeting, not after, and let a pre-written rule decide the exit.
entry_date:
horizon: 3-4 weeks
target: 0
invalidation: RBI brings the meeting forward or an inter-meeting action happens before entry (then do not enter: delete the queued order and note it here)
invalidation_price: 0
size_cap_pct: 15
status: closed
type: catalyst
event: RBI Monetary Policy Committee decision, December 2026 meeting (2-4 Dec 2026, decision 4 Dec; RBI press release prid=62422, confirmed 2026-11-06)
event_date: 2026-12-04
post_event_rule: hold if close on decision day > entry price, then trail stop 5% from the highest close; otherwise sell at next open
post_event_applied: 2026-11-14 not entered: queued BUY deleted before the 19 Nov decision date (Bajaj Finance QIP launched 11 Nov, the thesis' own clause); no position, no P&L
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
- 2026-11-06 (weekly review, Claude): still proposed; BUY Rs 75,000 dated 2026-11-19 unchanged. Confirm the December MPC date from rbi.org.in before the buy.
- 2026-11-06 (weekly strategist, Claude): MPC date confirmed 2-4 Dec 2026, decision 4 Dec (RBI release prid=62422 via search; rbi.org.in fetch blocked); event_date unchanged. Macro: RBI hiked 25 bp to 5.50% on 7 Oct and moved to calibrated tightening, so the December outcome is genuinely uncertain. BUY Rs 75,000 dated 2026-11-19 unchanged; post-event rule unchanged.
- 2026-11-07 (weekly strategist, Claude): macro: India CPI 5.54% in Sept (4.83% Aug; MoSPI via teamleaseregtech); SBI Research sees Oct-Nov above 6.5%, Nomura a December hike to 5.75%: outcome tilting to a hike, still open. Rule unchanged. New flag: Q2 (19 Oct) came with a proposed QIP up to Rs 11,700 crore plus Rs 5,800 crore warrants (finology, kotakneo via search). Reviewer: before the 19 Nov run, if a QIP is launched, delete the BUY under "what would change my mind". Log Oct CPI (12 Nov) here.
- 2026-11-13 (weekly review, Claude): still proposed; BUY Rs 75,000 dated 2026-11-19 unchanged. India October CPI printed figure not found (forecasts ICRA ~6.1%, Bank of Baroda ~6% vs 5.54% Sept; kotakneo/devdiscourse via search). QIP of Rs 11,700 crore plus Rs 5,800 crore warrants reported for a November launch, preferably the second half; no launch reported as of 13 Nov (kotakneo, multibagg via search). 19 Nov reviewer: re-check, delete the BUY if the QIP has launched.
- 2026-11-14 (weekly strategist, Claude): **not entered; BUY Rs 75,000 (decision date 2026-11-19) deleted, thesis closed.** (1) Stock-specific event before entry: a special committee of the board approved opening the Rs 11,700 crore QIP on 11 Nov 2026 with a floor price of Rs 992.25 (up to 5% discount allowed by the 2 Nov special resolution) (PTI via theweek.in, 11 Nov; Reg 30 filing summary on arthneeti.com, 11 Nov; both via search excerpts, direct fetch of both hosts blocked). The close on 13 Nov was 1,117, about 11% above the floor, so new supply priced below market lands inside the planned holding window: the policy-day test is no longer clean. This is the 'what would change my mind' clause written on 17 Oct, applied as written. (2) Macro: India October CPI printed 6.09% (Sept 5.54%), food 8.94%, first breach of the RBI's 6% ceiling in the new series (MoSPI release of 12 Nov via angelone.in, downtoearth.org.in). A December hike is now the consensus, so the 'genuinely uncertain decision' premise is weaker too. MPC date re-confirmed on rbi.org.in (prid=62422, fetched 14 Nov): 2-4 Dec 2026. A new RBI position would be a new thesis file, not a re-queue of this one.
