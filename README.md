# PaperTrade

A paper-trading engine for Nifty 50 cash stocks. It downloads daily prices from Yahoo Finance, runs a
simple momentum-rotation strategy, simulates fills at the next day's open with Zerodha's real charges,
and writes a daily journal for review. No real orders are ever placed.

**Total paper capital is Rs 10,00,000, in two separate sleeves:**

| Sleeve | Capital | What | Files | Review |
|---|---|---|---|---|
| Momentum | Rs 5,00,000 | the daily rotation rules below; the Rs 1,000/day target applies here | `state/portfolio.json`, `trades.csv`, `equity_curve.csv`, `decisions/pending_orders.json` | daily (DAILY.md) |
| Thesis | Rs 5,00,000 | macro / economic-theme buys held weeks to years, one written thesis per position | `theses/<slug>.md`, `state/thesis_*`, `decisions/thesis_orders.json` | weekly, Fridays (THESIS_REVIEW.md) |

The thesis sleeve was added on top of the original Rs 5,00,000 (CHANGELOG 2026-10-17), so the momentum
sleeve's capital, rules and history are unchanged. Both share the cost model, slippage, daily run and
dashboard. How to open a thesis: copy `theses/_TEMPLATE.md`.

Phase 1 is about measuring, not being clever: the strategy is deliberately simple so the daily review
has something concrete to criticise.

## Install

Windows, Python 3.11+:

```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

`requirements.txt` pins yfinance 1.7.0, pandas 3.0.6, numpy 2.5.4 and pyarrow 26.0.0 (pyarrow is needed
for the parquet price cache). The GitHub Actions run installs the same file.

## Commands

Run from the project root:

| Command | What it does |
|---|---|
| `.venv/Scripts/python -m papertrade run` | Full daily cycle: download prices, fill yesterday's pending orders at today's open, mark to market, propose orders for the next open, write the journal. |
| `... run --offline` | Same, using the cached prices (no download). |
| `... status` | Print cash, positions, and the last equity rows. |
| `... pending` | Print `decisions/pending_orders.json`. |
| `... thesis` | Print the thesis sleeve: cash, holdings, theses and queued thesis orders. |
| `... costs QTY PRICE [--product MIS]` | Show Zerodha charges for a buy and a sell of QTY shares at PRICE. |
| `... export` | Rewrite `docs/data.json` for the dashboard from the current state. Read-only: no download, no state change. `run` already does this at the end. |
| `... reset --yes` | Wipe `state/`, `decisions/`, `journal/` and start both sleeves again (Rs 5,00,000 each). `theses/` is kept. |

Running `run` twice on the same market date is safe. Orders do not fill until a later trading day
exists, and that date's equity row is replaced, not duplicated.

## Folder layout

```
papertrade/        the package
  config.py        capital, targets, strategy parameters, paths  (edit parameters here only)
  universe.py      Nifty 50 symbol list
  data.py          Yahoo download -> data_cache/prices_daily.parquet
  costs.py         Zerodha CNC/MIS charge model
  portfolio.py     cash, positions, trade ledger, equity curve
  strategy.py      momentum rotation rules
  thesis.py        thesis sleeve: ledger parsing, thesis orders, invalidation check, journal section
  engine.py        the daily cycle
  review.py        journal writer
  export.py        dashboard data -> docs/data.json
  cli.py           command line
state/             portfolio.json, trades.csv, equity_curve.csv; thesis_portfolio.json, thesis_trades.csv,
                   thesis_equity_curve.csv (thesis sleeve)
decisions/         pending_orders.json (next session's orders), executed_YYYY-MM-DD.json (what filled);
                   thesis_orders.json, thesis_executed_YYYY-MM-DD.json (thesis sleeve)
theses/            <slug>.md, one per thesis (copy _TEMPLATE.md); reviews/ for the Friday write-ups
journal/           YYYY-MM-DD.md daily reviews
data_cache/        prices_daily.parquet, thesis_prices.parquet (re-downloadable; git-ignored)
docs/              index.html (mobile dashboard), data.json (written by run/export), .nojekyll
.github/workflows/ daily.yml (scheduled cloud run)
requirements.txt   pinned dependencies
DAILY.md           the daily routine
THESIS_REVIEW.md   the weekly thesis-sleeve review
CHANGELOG.md       every rule change, with reasoning
```

## Cost model (Zerodha, NSE equity)

Rates in `papertrade/costs.py`, checked 2026-10-17. Re-check after any budget or SEBI/exchange circular.

| Charge | Delivery (CNC) | Intraday (MIS) |
|---|---|---|
| Brokerage | 0 | min(0.03%, Rs 20) per order |
| STT | 0.1% buy and sell | 0.025% sell only |
| NSE transaction | 0.00297% | 0.00297% |
| SEBI | 0.0001% | 0.0001% |
| Stamp duty | 0.015% buy | 0.003% buy |
| GST | 18% of (brokerage + transaction + SEBI) | same |
| DP charge | Rs 15.93 per scrip per sell day | none |

Example: 100 shares at Rs 1,200 delivery costs Rs 142.34 to buy and Rs 140.27 to sell. A round
trip is Rs 282.61, or about 0.24% of turnover. That is the break-even move.

Each buy's charges are added to the position's average cost. A sell's realised P&L is net proceeds
minus average cost times quantity, so realised P&L is after all charges.

## Strategy (baseline momentum rotation)

Evaluated on each day's close. Orders fill at the next trading day's open.

- **Universe**: Nifty 50 names with at least 80 days of history.
- **Trend filter**: close > SMA50 and SMA20 > SMA50.
- **Rank**: 63-day return, highest first, among trend-filtered names. Positive momentum is *not*
  required: if only a few names pass the trend filter, names that fell over 3 months can be bought.
- **Entry**: if fewer than 5 positions, buy the best-ranked names not held (top 5 only). Each buy is
  sized at available cash / free slots, so with 5 slots each position is about 20%.
- **Exit**: stop-loss (close below fill price minus 7%; the stop is fixed, not trailing), or close
  below SMA50, or rank outside the top 10.
- **Slippage**: 0.05% adverse on every fill.

All parameters are in `config.py`. They are changed only with a CHANGELOG.md entry (see DAILY.md).

## Assumptions and limits

- **Daily bars from Yahoo Finance.** Free, unofficial, sometimes late or gappy. A missing bar for one
  symbol is carried forward for up to 3 days (`MAX_STALE_BARS`) so a single gap does not trigger a
  false exit. A held stock with no recent price gets no automatic order; the journal flags it.
- **Today's bar can be late.** At 01:15 IST on Saturday 2026-10-17, Yahoo had a row for Friday
  2026-10-16 but no prices in it. The run's `Date:` line shows which day was actually processed.
- **Fills at the next open, at the open price plus slippage.** No intraday logic, no limit orders,
  no partial fills, no liquidity check. Nifty 50 names are liquid enough that this is roughly fair
  for Rs 5 lakh. Opening auctions can still gap well past the modelled 0.05%.
- **Corporate actions are not handled.** Yahoo's `Close` is split-adjusted retroactively, but a
  held position's quantity, average cost and stop are not. A split or bonus while holding a stock
  will look like a crash and can trigger a stop-loss at a meaningless price. The price history also
  contains unadjusted demerger drops (e.g. TMPV, 2025-10-14). Dividends are ignored.
- **Only one fill date is processed per run.** If you skip days, pending orders fill at the first
  open after the decision date, but stops are not re-checked for the skipped days.
- **The Nifty 50 list may be stale.** It is hard-coded in `universe.py`, and NSE reviews it in
  March and September. Symbols Yahoo cannot serve are dropped and listed in the journal.
- **Tax is not modelled per trade.** The target of Rs 1,000/day net assumes 20% STCG, i.e. Rs 1,250/day
  pre-tax. On Rs 5 lakh that is about 62% a year, far above any index's long-run return. The journal
  repeats this every day on purpose.

## Running in the cloud

The daily run can happen in GitHub Actions instead of on this laptop, with a phone-friendly dashboard on
GitHub Pages. Nothing here places real orders; Actions only runs the same `python -m papertrade run`.

### 1. Put the project on GitHub

**GitHub Pages on a free account needs a public repository.** That makes the whole repo public: state,
trades, journal and dashboard. There are no secrets in it (it is paper trading and needs no API keys),
but decide that consciously. Pages from a private repo needs a paid plan (Pro or Team).

From the project root (`.gitignore` already excludes `.venv/`, `data_cache/` and `__pycache__/`;
`docs/data.json` is deliberately committed because the dashboard reads it):

```
git init -b main
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/<you>/PaperTrade.git
git push -u origin main
```

### 2. The scheduled run

`.github/workflows/daily.yml` runs at **12:00 UTC (17:30 IST), Monday to Friday**, after the NSE close.
It checks out the repo, installs `requirements.txt` on Python 3.12, runs `python -m papertrade run`,
then commits any changes under `state/`, `decisions/`, `journal/` and `docs/` as `github-actions[bot]`
with the message `daily run <market date>`. If nothing changed, it does not commit.

Things to know:

- **Permissions.** The workflow asks for `contents: write`. If the push step fails with a 403, go to
  Settings > Actions > General > Workflow permissions and choose "Read and write permissions".
- **Schedules are best effort.** GitHub often starts cron jobs late at busy times (minutes, sometimes
  more) and occasionally drops one. Schedules only run on the default branch (`main`).
- **Holidays and late data.** On an NSE holiday, or if Yahoo has not published today's bar by 17:30,
  the run re-processes the previous market date. That is safe: orders do not fill until a later trading
  day exists, the equity row is replaced rather than duplicated, and a review already written in that
  day's journal is kept. Pending orders for that date are regenerated unless `"locked": true`.
- **Inactive repos.** GitHub disables scheduled workflows in public repos after 60 days without
  repository activity. The daily bot commits should count as activity, but if GitHub emails you that
  the schedule was disabled, re-enable it from the Actions tab.
- **A failed run commits nothing.** The next run starts again from the last committed state, and pending
  orders fill at the first open after their decision date.
- **No price cache in the repo.** `data_cache/` is git-ignored, so every cloud run downloads two years of
  daily bars from Yahoo (a few seconds).

### 3. Manual run

GitHub > the repo > **Actions** > **Daily run** > **Run workflow** > branch `main` > Run workflow.
This also works from a phone browser.

Avoid manual runs during market hours (09:15-15:30 IST). Yahoo's bar for today is then still forming:
fills at today's open are correct, but positions are marked and next-session orders are proposed on an
intraday price. The 17:30 run redoes that date on the real close (and regenerates unlocked orders).

### 4. Enable GitHub Pages (once)

Settings > Pages > Build and deployment:

- Source: **Deploy from a branch**
- Branch: **main**, folder: **/docs**, then Save.

After a minute or two the dashboard is at `https://<you>.github.io/PaperTrade/`. GitHub rebuilds Pages
on pushes to `main`; the workflow also requests a rebuild after its own push (that step is allowed to
fail, for example before Pages is enabled). `docs/.nojekyll` tells Pages to serve the files as they are.

### 5. On your phone

Open `https://<you>.github.io/PaperTrade/` and add it to the home screen. Top to bottom it shows: equity
with day and total P&L; progress against the Rs 1,250 pre-tax / Rs 1,000 net daily target; the equity
curve (tap or drag for values, "Show as table" for the numbers); open positions; the decisions for the
next open; the last 20 fills; and the latest journal in full. The "As of" date is the market date of
the last run. If it is more than 4 days old the page says so, which usually means a run failed (check
the Actions tab). Pages can serve a copy up to about 10 minutes old right after a run.

To preview locally: `.venv/Scripts/python -m http.server 8765 --directory docs`, then open
http://localhost:8765. Opening `docs/index.html` straight from disk does not work, because the browser
blocks reading `data.json` from a `file://` page.

### 6. Running locally as well

Once the cloud run is on, the repo on GitHub is the source of truth. `git pull` before doing anything
locally. If you run `python -m papertrade run` locally, commit and push the result straight away, or the
next cloud run starts from older state and the two conflict. Simplest rule: let Actions do the `run`;
locally, only edit journals and `decisions/pending_orders.json` (see DAILY.md).
