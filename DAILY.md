# Daily routine (for Claude)

The mechanical part runs in the cloud: GitHub Actions runs `python -m papertrade run` at **17:30 IST,
Monday to Friday**, and commits state, decisions, journal and dashboard data to the repo (README,
"Running in the cloud"). The human/Claude part is the review below, done any time after that run.

Four sleeves of Rs 5,00,000 each: momentum (rules decide), thesis, India F&O and US. Since 2026-10-17
the cloud reviewer (ROUTINE_PROMPT.md, 18:30 IST) is the decision-maker for the thesis, F&O and US
sleeves: it activates or opens theses and queues and closes positions itself, within RISK_LIMITS.md,
and writes every decision with its reasoning under "Decisions" in the journal's "Claude's review". The
hourly catalyst reviewer may activate one drafted second_order or catalyst thesis per run
(SCANNER_PROMPT.md). Your part is to read the decisions and push back where the reasoning is weak.

1. **Check that today's run happened.** Open the dashboard (the GitHub Pages URL) and check the
   "As of" date, or run `git pull` in the project and look at the newest `journal/<date>.md`.

   If it is not today's market date, Yahoo had not published today's bar at 17:30, it was a holiday,
   or the run failed (look at the repo's Actions tab). Do not review a stale day as if it were today.
   To catch up, start a manual run later in the evening (Actions > Daily run > Run workflow), or, if
   Actions is unavailable, use the local fallback in step 6.

2. **Read the day.** On a phone, the dashboard shows the scorecard, positions, next-session orders,
   recent fills and the full journal. On the laptop, `git pull` and read `journal/<date>.md` in full:
   scorecard, open positions, fills, proposed orders, market snapshot, automatic observations.
   Cross-check anything surprising against `state/trades.csv` and `state/equity_curve.csv`.

3. **Write the "Claude's review" section** at the bottom of that journal file. Replace the
   placeholder text with:
   - what went right and what went wrong today, with numbers;
   - whether the fills and charges look right;
   - whether any proposed order looks wrong, and why;
   - whether any rule should change (a proposal only; see step 5);
   - what to watch tomorrow.

   Then `git commit` and `git push` it. A re-run for the same market date (a holiday, a late Yahoo
   bar, a manual run) keeps a review that is already in the file it regenerates. A review that exists
   only on the laptop is invisible to the cloud run and will conflict on the next pull, so push it
   promptly.

4. **Optionally override the next session's orders** by editing `decisions/pending_orders.json`:
   delete an order, change `side`, or set a `qty` (`null` means size automatically). Then add
   `"locked": true` at the top level. Without it, any re-run on the same date overwrites your edits
   with fresh strategy proposals. Record every override and its reason in the review section.
   **Push the override before the next 17:30 IST run**: that run fills whatever is in the repo then.

5. **Never change strategy parameters silently.** Any change to `config.py`, `strategy.py`,
   `costs.py` or `universe.py` that affects trading gets an entry in `CHANGELOG.md` before or with
   the change. The entry gives the date, what changed (old value -> new value), the reasoning and
   evidence (which journal days, which numbers), and what result would make you revert it.
   Prefer not to change rules before about 60 trading days of data unless something is broken.

6. **Fallback: run locally.** Only if Actions is down or disabled. After `git pull`, from the project
   root:

   ```
   .venv/Scripts/python -m papertrade run
   ```

   Check the `Date:` line (if it is not today's date, Yahoo has not published today's bar yet; run
   again later). If it prints `NOT RUN: stale data from Yahoo` (exit code 3), Yahoo returned a history
   older than the recorded state; nothing was written, so there is nothing to commit: run again later. Then commit and push everything under `state/`, `decisions/`, `journal/` and `docs/`
   straight away so the next cloud run starts from it.

7. **Fridays: weekly thesis-sleeve review.** Each thesis's macro reason is re-checked once a week
   (THESIS_REVIEW.md). On other days the "## Thesis sleeve" section of the journal flags invalidation
   hits, which the engine has already turned into a queued SELL. Exception, any weekday: a catalyst
   thesis flagged "CATALYST RULE DUE" gets its post-event rule applied that day.

8. **F&O and US sleeves.** Read "## India F&O sleeve" and "## US sleeve" in the journal (or
   `python -m papertrade fno status` / `us status`): positions with the pricing method used, margin,
   days to expiry, the circuit-breaker state, rejected orders and why. India option prices are a model
   (Black-Scholes), not quotes. Orders go in `decisions/fno_orders.json` / `decisions/us_orders.json`,
   each with a `reason` and a `risk_check`; formats in `papertrade/fno.py` and `papertrade/us.py`.

Weekends and NSE holidays: nothing to review. If a day is missed, the next run catches up; pending
orders fill at the first open after their decision date (stops are not re-checked for skipped days).
