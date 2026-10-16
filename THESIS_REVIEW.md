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

- for `type: catalyst` theses, counts days to `event_date` and, from the first trading day after it,
  writes "CATALYST RULE DUE" in the journal until `post_event_applied` is filled in, a SELL for that
  thesis is queued, or the thesis is closed. It does not interpret `post_event_rule` (free text).

What it cannot do is judge whether the macro reason is still true. That is this review.

## Two thesis types

- **theme**: a macro or economic reason (defence indigenisation, railway capex, rate cuts), held weeks
  to years. Reviewed weekly, below.
- **catalyst**: a scheduled event (results, Union Budget, IPO listing, RBI policy). The money is made
  by being positioned BEFORE the event, so the BUY must be queued early enough to fill before
  `event_date`; the horizon is days to a few weeks. The exit is decided in advance in
  `post_event_rule`. Catalysts are reviewed on the first trading day after the event (next section),
  not only on Fridays.

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
4. Nothing else. The reviewer may queue only the SELL the rule specifies: no other sells, no buys,
   no changes to the rule after the event.

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

4. Act only within these limits:
   - **SELL**: queue one in `decisions/thesis_orders.json` if the invalidation hit or the reason is no
     longer true. Give `decision_date` as today's market date and a `reason`.
   - **BUY / new theses**: propose them in the weekly review, do not queue them. A new position needs the
     user's agreement in an interactive session (they are a beginner and the point is to learn from each
     thesis). After agreement, write `theses/<slug>.md` with `status: proposed` and queue the BUY.
   - Never change the momentum sleeve's files, `config.py` or any code.

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
