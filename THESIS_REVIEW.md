# Thesis sleeve: weekly review (for Claude, every Friday)

The thesis sleeve is a separate Rs 5,00,000 paper book for macro or economic-theme positions held for
weeks to years (for example defence indigenisation, railway capex, rate cuts). It does not change the
momentum sleeve and is not part of the Rs 1,000/day target. Each position has a written thesis in
`theses/<slug>.md`; orders go through `decisions/thesis_orders.json` and fill at the next open with the
same Zerodha delivery charges and 0.05% slippage as the momentum sleeve. Field rules: `papertrade/thesis.py`
(module docstring) and `theses/_TEMPLATE.md`.

What the daily run already does by itself:

- fills queued thesis orders at the first open after their `decision_date`;
- clips every BUY to the thesis' size cap (never more than 25% of sleeve equity) and to available cash;
- marks the sleeve to market and writes the "## Thesis sleeve" section of the daily journal;
- if a thesis has a numeric `invalidation_price` and the close is at or below it, queues a SELL for the
  next open (delete that order from `decisions/thesis_orders.json` before the next 17:30 IST run to
  override, and say why in the review);
- sets `status: open` and `entry_date` on the first fill, `status: closed` and `exit_date` on a full exit.

- for `type: catalyst` and `type: second_order` theses, counts days to `event_date` and, from the first
  trading day after it (or after `rule_due_date` if set), writes "CATALYST RULE DUE" in the journal
  until `post_event_applied` is filled in, an exit (SELL / COVER) is queued for every held leg, or the
  thesis is closed. It does not interpret `post_event_rule` (free text).
- for second-order theses, shows each leg with its own P&L and the combined P&L of the thesis, and
  labels every short "PAPER SHORT: needs F&O to execute".

What it cannot do is judge whether the macro reason is still true. That is this review.

## Two thesis types

- **theme**: a macro or economic reason (defence indigenisation, railway capex, rate cuts), held weeks
  to years. Reviewed weekly, below.
- **catalyst**: a scheduled event (results, Union Budget, IPO listing, RBI policy). The money is made
  by being positioned BEFORE the event, so the BUY must be queued early enough to fill before
  `event_date`; the horizon is days to a few weeks. The exit is decided in advance in
  `post_event_rule`. Catalysts are reviewed on the first trading day after the event (next section),
  not only on Fridays.
- **second_order**: an event, the obvious winner and the non-obvious losers, with positions on both
  sides (`legs:`, each LONG or SHORT with a weight and a rationale). Example: on 1 July 2026 Bloomberg
  reported Meta would rent out spare GPUs; Meta rose, the GPU-cloud sellers CoreWeave and Nebius fell
  (`theses/_EXAMPLE_second_order.md`). It has the catalyst fields, and its post-event rule must say when
  to close BOTH sides. Every SHORT is a PAPER short ("PAPER SHORT: needs F&O to execute").

## Second-order checklist (before proposing one, and again at each review)

Write the answers in the thesis file. If any answer is vague, it is not a thesis yet.

1. **What is the event?** One line, with the date and the source (report, filing, policy decision).
2. **Who gains, and why?** The obvious winner and the mechanism (more revenue, lower cost, a rival
   weakened). Not "it went up".
3. **Who loses, and why?** The non-obvious losers and the mechanism: new supply, lost customers,
   pricing pressure, a substitute. Each SHORT leg needs its own rationale.
4. **What breaks the link?** The fact that would show the loss does not follow from the event (a
   denial, a scaled-back plan, a loser announcing strong demand), and the price sign that the market
   disagrees (the losers win back half their fall while the winner holds).
5. **When do both legs close?** A date (`rule_due_date`) or a condition, written in `post_event_rule`
   before the first order. Close the legs together: one side alone is a plain directional bet.
6. **Can it actually be traded?** An Indian short needs the stock on the NSE F&O list (a future); a US
   leg needs an overseas account and cannot be shorted from India. Paper trading it is still useful,
   but say so in the file.
7. **Is the entry late?** Orders fill at the next open after the decision. If the event moved prices
   on the day, the trade is a bet that the move continues. Say why it should.

Mechanics the engine enforces: each leg's order names it (`"leg": "CRWV"`); BUY / SELL on LONG legs,
SHORT / COVER on SHORT legs; the size cap applies to the gross value of all legs together; total short
notional across the sleeve stays under 50% of its capital; short proceeds cannot fund buys. US legs are
converted at the fixed `USD_INR` in config. A second-order thesis has no engine stop
(`invalidation_price` is ignored): the post-event rule and this review close it.

## Catalyst check (any weekday: the first trading day after event_date)

The daily journal flags it as "CATALYST RULE DUE". On that day, for that thesis only:

1. Read `post_event_rule` and the day's close (and the event outcome: results, policy decision).
2. Apply the rule literally. If it calls for a sale ("sell at next open regardless", "hold if close
   > entry" with the close below entry, "trail stop 5%" with the stop hit), queue that SELL in
   `decisions/thesis_orders.json` with today's market date as `decision_date` and the rule as `reason`.
   If it calls for holding, queue nothing.
3. Either way, fill in `post_event_applied` in the thesis file with the date and what was done
   (`2026-10-27 held: close 412 > entry 398`), and add a line to its "## Review log". A "trail stop"
   rule stays live after that: check it again at each later review until it triggers or the thesis is
   closed.
   Timing: the check runs after the close of the first trading day after the event, so a SELL queued
   then fills one session later than "next open" suggests (event Friday, flagged Monday evening, sold
   Tuesday open). For a rule that sells regardless of the outcome, the SELL may be queued ahead, with
   `decision_date` = `event_date`, so it fills at the first open after the event.
   For a second-order thesis apply the rule to every leg: a SELL for each LONG leg and a COVER for each
   SHORT leg it calls for, each with `"leg"` set. The flag stays up until every held leg has its exit.
4. Never change the rule after the event. Any other decision on the same thesis (adding, a new
   thesis on the outcome) is a separate, written decision under ROUTINE_PROMPT.md step 5.

## Every Friday (after the 17:30 IST run): theme theses and anything open

1. `git pull`. Read the "## Thesis sleeve" section of today's journal, every open thesis file in
   `theses/`, `decisions/thesis_orders.json` and the newest `decisions/thesis_executed_*.json`.

2. For each open thesis, answer in writing, with numbers and dated sources:
   - **Is the macro reason still true?** What changed this week in the data or policy the thesis rests on
     (budget allocation, order inflows, RBI decision, monthly data release, company results)?
     Price movement alone is not evidence for or against the thesis.
   - **Has the invalidation hit?** For a numeric `invalidation_price`, the engine already checks the close.
     For a text invalidation (a policy reversal, a missed quarter), check it yourself.
   - **Should the horizon change?** Only with a reason. Extending a horizon because the stock is down is
     the classic way a thesis turns into a hope; say so if that is what is happening.
   - **Is the position still within its cap?** A winner can grow past it. The engine only caps buys,
     it never trims; propose a trim if a position is above its cap.

3. Append a dated entry to the thesis file's "## Review log" (keep the front matter as it is unless a
   field must change; if you change `target`, `horizon` or `invalidation_price`, write old -> new and why
   in the log entry).

4. Act. Since 2026-10-17 the user has authorised Claude to take the paper decisions itself, without
   waiting for approval (CHANGELOG 2026-10-17, "Autonomous decisions"):
   - **SELL / COVER**: queue one in `decisions/thesis_orders.json` if the invalidation hit or the reason
     is no longer true. Give `decision_date` as today's market date and a `reason`.
   - **BUY / SHORT / new theses**: allowed, with the checklist above answered in the thesis file and the
     limits in RISK_LIMITS.md checked first. Write `theses/<slug>.md` with `status: proposed` and queue
     the orders. Drafts in `theses/proposed/` are activated by moving them into `theses/`.
   - Every decision gets a dated entry with the reasoning in the journal's "Claude's review" (and in the
     thesis file's review log); a change to how decisions are made goes in CHANGELOG.md.
   - Never change the momentum sleeve's files, `config.py`, `RISK_LIMITS.md` values or any code.

5. Write the weekly summary to `theses/reviews/<YYYY-MM-DD>.md` (not in `journal/`: the dashboard shows
   the newest file there as the daily journal): one paragraph per open thesis, the sleeve's equity and
   P&L since start against the Nifty 50 over the same period if you can get it, and any proposals.
   Commit and push.

## Sizing and discipline reminders

- 25% of the sleeve per thesis is the ceiling, not the default. A first position of 10-20% leaves room
  to be wrong.
- Every thesis needs an invalidation written down BEFORE buying. Prefer one that is a price
  (`invalidation_price`) plus one that is a fact.
- Holding periods over a year change the tax (LTCG instead of STCG). Tax is not modelled per trade.
- Corporate actions (splits, bonuses) are not handled, as in the momentum sleeve; a split looks like a
  crash and can trip a numeric invalidation. Check the news before acting on a sudden large drop.
