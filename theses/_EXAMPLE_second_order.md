---
thesis: Meta renting out spare GPUs adds AI-compute supply that competes with the GPU-cloud specialists; Meta gains revenue from capex it already spent
type: second_order
status: example
event: Bloomberg report that Meta will rent out spare GPU capacity
event_date: 2026-07-01
rule_due_date: 2026-07-15
post_event_rule: close both sides at the next open after 2026-07-15; close earlier if CRWV and NBIS win back half of their event-day fall while META holds its gain (the market has rejected the link)
post_event_applied:
horizon: 2 weeks
size_cap_pct: 20
invalidation: Meta denies or scales back the plan, or CoreWeave / Nebius announce demand (a large new contract) showing the extra supply does not matter
entry_date:
legs:
  - symbol: META
    market: US
    side: LONG
    weight: 0.5
    rationale: earns rental income on GPUs it has already paid for, so the same capex now has two uses
  - symbol: CRWV
    market: US
    side: SHORT
    weight: 0.25
    rationale: CoreWeave sells rented GPU capacity; a hyperscaler with spare GPUs is new supply and pricing pressure
  - symbol: NBIS
    market: US
    side: SHORT
    weight: 0.25
    rationale: Nebius is a smaller GPU-cloud seller with the same exposure and less pricing power than CoreWeave
---

# Worked example: second-order catalyst (never executed)

`status: example` and the `_` at the start of the file name mean the engine never reads or trades this
file. It shows how to write a second-order thesis. Copy it to `theses/<slug>.md`, change the fields and
set `status: proposed` to use the format for a real idea.

## The event and the market's reaction

On 1 July 2026 Bloomberg reported that Meta would rent out spare GPU capacity. That day Meta rose about
9%, CoreWeave fell about 14% and Nebius about 17%. (These figures are as the user recorded them; they
were not re-checked against price data when this example was written.)

## The second-order checklist, filled in

1. **What is the event?** A report that Meta will sell spare GPU time to outside customers.
2. **Who gains, and why?** Meta (the obvious winner). GPUs it bought for its own AI work can now also
   earn rent, so the market gives more value to the same capex.
3. **Who loses, and why?** The less obvious losers are the companies whose whole business is renting
   GPUs: CoreWeave and Nebius. A hyperscaler with spare GPUs adds supply and can undercut on price.
   Nebius is smaller, so it is hit harder (it fell more).
4. **What breaks the link?** Meta denies or narrows the plan; the spare capacity turns out to be small;
   CoreWeave or Nebius show demand still outruns supply (a large new contract); or the losers win back
   half of their fall while Meta holds its gain, which means the market has decided the link is weak.
5. **When do both sides close?** At a fixed date (`rule_due_date`, two weeks after the event), or
   earlier if the link breaks. Both sides close together. Holding only one side turns a pair trade
   into a plain bet on one stock.

## What the orders would look like

Size cap 20% of the Rs 5,00,000 sleeve = Rs 1,00,000 gross across all three legs. Weights 0.5 / 0.25 /
0.25 give META about Rs 50,000 long, CRWV and NBIS about Rs 25,000 short each. Prices are in USD and
converted at the fixed `USD_INR` in config (84.0, a placeholder).

```
{"orders": [
  {"thesis": "meta-gpu-rental", "leg": "META", "side": "BUY",   "decision_date": "2026-07-01", "reason": "winner"},
  {"thesis": "meta-gpu-rental", "leg": "CRWV", "side": "SHORT", "decision_date": "2026-07-01", "reason": "loser"},
  {"thesis": "meta-gpu-rental", "leg": "NBIS", "side": "SHORT", "decision_date": "2026-07-01", "reason": "loser"}
]}
```

To close: SELL for META, COVER for CRWV and NBIS. The reviewer may queue exactly these after the rule
falls due (ROUTINE_PROMPT.md step 9).

## What this example cannot do

- **Catch the first day.** Orders fill at the next open after the decision, so the 9% / 14% / 17% moves
  have already happened by then. A trade placed after the news is a bet that the move CONTINUES, not
  a way to capture it. Prices often gap on the open and then reverse.
- **Be executed from India as written.** These are US listings: they need an overseas broker under
  the LRS limit, and shorting them is not possible through NSE at all. Every short leg is labelled
  "PAPER SHORT: needs F&O to execute". For an Indian stock, holding a short past the day needs a stock
  future (F&O), and only stocks on the NSE F&O list have futures.
- **Size like a real futures trade.** Lot sizes, margin blocking and the FX rate moving are not
  modelled. Costs on US legs use the Zerodha rates (CNC for the long, FUT for the shorts) on the
  rupee value as a stand-in; real US-broker and FX costs differ.
