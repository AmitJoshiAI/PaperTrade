---
symbol: SYMBOL
thesis: One line. The macro or economic reason this stock should do well over the horizon.
entry_date:
horizon: 12-24 months
target: 0
invalidation: The fact that would prove the thesis wrong, e.g. close below 0 or the policy is reversed
invalidation_price: 0
size_cap_pct: 20
status: proposed
type: theme
event:
event_date:
post_event_rule:
post_event_applied:
---

Copy this file to `theses/<slug>.md` (lower-case, hyphens, e.g. `hal-defence-indigenisation.md`) and
fill it in. Files starting with `_` are ignored by the engine. Field rules are in `papertrade/thesis.py`
and THESIS_REVIEW.md; in short:

- `symbol`: NSE symbol, any listed stock (Yahoo `SYMBOL.NS` must exist).
- `target` / `invalidation_price`: a bare number is tracked by the engine. `invalidation_price` is a
  hard exit: a close at or below it queues a SELL for the next open. Leave it blank if the
  invalidation is only a fact to check (a policy, a data release), not a price.
- `size_cap_pct`: max share of the sleeve's equity in this one thesis. The engine never allows more
  than 25%.
- `entry_date` and `status: open` / `closed` are written by the engine when orders fill.
- `type`: `theme` (a macro reason, held weeks to years; leave the event fields blank) or `catalyst`
  (positioned BEFORE a known, scheduled event: results, Union Budget, IPO listing, RBI policy; held
  days to a few weeks). A catalyst needs `event` (one line), `event_date` (YYYY-MM-DD) and
  `post_event_rule` (what to do the day after: "sell at next open regardless", "hold if close > entry",
  "trail stop 5%"). The journal counts down to the event and, from the first trading day after it,
  flags the thesis until the rule is applied. When you apply it, write the date and what you did in
  `post_event_applied` (e.g. `2026-10-27 sold at open per rule`).
- `market: US` (optional) for a US listing: `symbol: META` then means Yahoo `META`, priced in USD and
  converted to INR at the fixed `USD_INR` in config.
- `type: second_order`: an event with a winner held long and losers held as PAPER shorts. It has no
  `symbol`; it has a `legs:` block instead. Start from `theses/_EXAMPLE_second_order.md`, not this file,
  and answer the second-order checklist in THESIS_REVIEW.md first.

To buy, add an order to `decisions/thesis_orders.json`:

```
{"orders": [{"thesis": "<slug>", "side": "BUY", "amount": 40000,
             "decision_date": "YYYY-MM-DD", "reason": "opening the thesis"}]}
```

## Why (the long form)

What is happening in the economy or policy, the numbers behind it, why this company is a direct
beneficiary, and what the market may be missing.

## What would change my mind

## Review log

- YYYY-MM-DD (weekly review): macro reason still true? invalidation hit? horizon unchanged?
