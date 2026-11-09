---
thesis: Rising crude and a weak rupee lift the rupee realisation of an oil producer (ONGC) and squeeze the marketing margin of a refiner-marketer that cannot pass pump prices through quickly (HPCL). Long the winner, paper-short the loser.
type: second_order
status: closed
event: Crude price rise plus rupee weakness through September-October 2026 (analyst commentary, 3-15 Oct 2026); not a single-day event, so the rule is time-boxed
event_date: 2026-10-16
rule_due_date: 2026-12-15
post_event_rule: close both legs at the next open after 2026-12-15; close earlier if Brent falls back to its 15 Oct 2026 level for five consecutive sessions, or if HPCL wins back half of its underperformance versus ONGC since entry (the market has rejected the link)
post_event_applied: 2026-11-06 rule triggered (Brent 15 Oct close 105.92; 2-6 Nov closes 104.77, 103.60, 94.62, 98.29, 98.69, all below); SELL ONGC + COVER HINDPETRO queued for the next open
horizon: 2 months
size_cap_pct: 25
invalidation: Government cuts excise duty or allows pump price hikes that restore HPCL's marketing margin; or a windfall tax on upstream producers removes ONGC's benefit from higher crude
entry_date: 2026-10-19
legs:
  - symbol: ONGC
    side: LONG
    weight: 0.5
    rationale: sells crude at prices linked to Brent; a higher dollar price and a weaker rupee both raise its rupee revenue per barrel
  - symbol: HINDPETRO
    side: SHORT
    weight: 0.5
    rationale: buys crude in dollars and sells petrol and diesel at pump prices that move slowly; higher crude plus a weaker rupee compresses its marketing margin until prices are allowed to catch up. HINDPETRO is on the NSE F&O list, so this short could be executed with a stock future.
exit_date: 2026-11-09
---

## The second-order checklist, filled in

1. **What is the event?** Crude and the dollar have risen and the rupee has weakened through
   September and October 2026 (market commentary 3-15 Oct 2026). Not one headline; a running macro
   condition, so the thesis is time-boxed to 15 Dec 2026.
2. **Who gains, and why?** ONGC. Its revenue is Brent in dollars converted to rupees; both legs of
   the move raise it. Costs are mostly fixed rupee costs.
3. **Who loses, and why?** HPCL. It imports crude in dollars and sells fuel in rupees at prices that
   are managed, so its marketing margin absorbs the squeeze. Of the three oil marketing companies it
   has the smallest upstream and refining cushion.
4. **What breaks the link?** A windfall tax on producers (removes ONGC's gain), an excise cut or
   pump price hike (restores HPCL's margin), or crude falling back. Price sign: HPCL recovering half
   of its underperformance versus ONGC while crude stays high.
5. **When do both legs close?** Together, at the next open after 15 Dec 2026, or earlier on the
   conditions above.
6. **Can it be traded for real?** Yes in principle: ONGC long in cash, HPCL short via a stock future.
   Paper only here; the short is labelled "PAPER SHORT: needs F&O to execute".
7. **Is the entry late?** Partly. Crude has already risen. The bet is that the margin squeeze shows
   up in HPCL's Q2/Q3 numbers and the pass-through lags, so the relative move continues.

## Sizing

25% cap on the Rs 5,00,000 sleeve = Rs 1,25,000 gross: about Rs 62,500 long ONGC and Rs 62,500
paper-short HPCL. Closes on 15 Oct 2026: ONGC Rs 224.64, HPCL Rs 348.50.

## Review log

- 2026-10-17 (opened by Claude): opened as the Indian version of the Meta / CoreWeave shape. Weekly:
  record Brent, USD/INR, and the ONGC-minus-HPCL return since entry.
- 2026-10-23 (weekly review, Claude): Combined +Rs 2,116 (ONGC +1.8%, HINDPETRO short +1.6%). No policy break known; Brent not checked. Unchanged.
- 2026-10-30 (weekly review, Claude): combined +Rs 1,120. Brent not reliably verifiable from this sandbox; no excise cut or windfall tax found. No change; rule due after 2026-12-15.
- 2026-11-06 (Claude, orchestrator): Brent (BZ=F) fell from 117.64 on 30 Oct to 94.62 on 4 Nov and has closed below the 15 Oct reference (105.92) for five straight sessions, which is the first early-exit condition of the post-event rule. Since entry ONGC is +3.0% and HINDPETRO +11.1% (the loser outperformed the winner by 8.1 points): the market read falling crude as good for the refiner, the opposite of the thesis premise. Combined P&L about Rs -4,700. Both legs queued to close at the Monday 9 Nov open. Lesson for the next pair: the second exit condition ("HPCL wins back half of its underperformance") assumed the trade would first go our way and never armed; future pairs need an absolute relative-performance stop (e.g. close if the loser outperforms the winner by 5 points since entry).
- 2026-11-09 (daily reviewer): both legs filled at the open: SELL ONGC 279 @ 235.88 (realised +Rs 3,404.78 after charges), COVER HINDPETRO 181 @ 386.19 (realised -Rs 7,729.58). Realised pair P&L -Rs 4,324.80, better than the Rs -4,700 to -5,500 the strategist expected. Thesis closed; the loser (the short) outweighed the winner because the market read falling crude as good for the refiner.
