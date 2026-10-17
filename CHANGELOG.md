# CHANGELOG - strategy and rule changes (date, old -> new, why, evidence, revert condition)

## 2026-10-17 - Thesis sleeve added (momentum sleeve unchanged)

- **What**: a second, separate paper book for macro / economic-theme positions held weeks to years, each
  with a written thesis in `theses/<slug>.md`. Code in `papertrade/thesis.py`; review process in
  THESIS_REVIEW.md (weekly, Fridays).
- **Capital: ADDED, not carved out.** `THESIS_CAPITAL = 500000` is new paper money on top of the momentum
  sleeve's Rs 5,00,000, kept in a new `state/thesis_portfolio.json` (created by the first run). Total paper
  capital is now Rs 10,00,000. (First drafted as Rs 2,00,000; raised to Rs 5,00,000 at the
  user's request before any run used it, so no thesis state ever existed at 2,00,000.) Carving it out of the Rs 5,00,000 was rejected because it would mean
  rewriting `state/portfolio.json`, the equity curve and past journals. The momentum sleeve's capital,
  rules, files and Rs 1,250/day target are unchanged.
- **Rules of the sleeve**: max 25% of sleeve equity per thesis (`THESIS_MAX_POSITION_PCT`; a thesis file
  may set lower); orders queued in `decisions/thesis_orders.json` fill at the next open with the same CNC
  charges and 0.05% slippage; a numeric `invalidation_price` hit on the close queues a SELL for the next
  open automatically. Any NSE symbol is allowed, not only Nifty 50.
- **Evidence the momentum sleeve did not change**: old and new code were run on the same inputs (the
  committed state for 2026-10-15, and an 8-day replay 2026-10-06..15 from an empty book with fills and a
  same-day re-run). `state/*`, `decisions/pending_orders.json`, `decisions/executed_*.json` (except the
  wall-clock `generated_at`), the momentum part of every journal and of `docs/data.json` were
  byte-identical. Journals gain a "## Thesis sleeve" section above "## Claude's review"; the dashboard's
  hero is relabelled "Momentum sleeve equity".
- **Revert if**: the thesis step ever changes a momentum file or fails a daily run. It is wrapped so a
  failure is logged and the momentum run is still saved.

## 2026-10-17 - Thesis sleeve: catalyst theses (momentum sleeve unchanged)

- **What**: thesis files gain `type: theme | catalyst` (missing = theme, so existing files are unchanged).
  A catalyst thesis is positioned BEFORE a scheduled event and must give `event`, `event_date` (ISO) and
  `post_event_rule`; the run rejects the file (with a journal warning) if any is missing. New optional
  field `post_event_applied`, filled in by the reviewer.
- **Engine**: shows a "### Catalyst theses" table with days to the event; from the first trading day after
  `event_date` it writes "CATALYST RULE DUE" until `post_event_applied` is set, a SELL for the thesis is
  queued, or it is closed. It does not apply the rule itself: the rules are free text.
- **Review**: catalysts are checked on the first trading day after the event, any weekday
  (THESIS_REVIEW.md "Catalyst check", ROUTINE_PROMPT.md step 9). The reviewer may queue only the SELL the
  post-event rule specifies.
- **Why**: the user's examples (an IPO listing, a +20% day after results plus a dividend) were made by
  holding before a known date; the theme type had no notion of a date.
- **Evidence the momentum sleeve did not change**: same old-vs-new comparison as the entry above
  (committed 2026-10-15 state, and the 8-day replay with a catalyst thesis added): byte-identical.

## 2026-10-17 - Thesis sleeve: second-order theses and paper shorts (momentum sleeve unchanged)

- **What**: thesis files gain `type: second_order`: an event, the obvious winner held LONG and the
  non-obvious losers held SHORT, as a `legs:` block of `{symbol, side, weight, rationale}` (plus optional
  `market` / `currency`). It needs the catalyst fields; optional `rule_due_date` moves the post-event rule's
  due date (a reaction trade is often opened after the event, so the rule cannot fall due the day after
  the event). Single-symbol theses are unchanged: they are treated as one LONG leg.
- **Paper shorts**: `decisions/thesis_orders.json` accepts `side: SHORT | COVER` and a `leg`. A short is
  negative qty, proceeds credited at entry, charged as a Zerodha futures trade (new `FUT` product in
  `costs.py`: brokerage min(0.03%, Rs 20), STT 0.02% sell, NSE 0.00173%, SEBI 0.0001%, stamp 0.002% buy,
  GST 18%; marked VERIFY against zerodha.com). Total short notional <= 50% of the lower of sleeve capital
  and equity (`THESIS_MAX_SHORT_PCT`); a SHORT is clipped to it. Short proceeds cannot fund a BUY (buys
  are limited to cash minus open short value). Every short is labelled "PAPER SHORT: needs F&O to execute"
  in the ledger (`label` column and reason), journal, dashboard and CLI.
- **US tickers**: `market: US` (or an explicit Yahoo ticker containing a '.', with `currency`). Prices
  stay in USD; cash, P&L and equity are in INR at the fixed `USD_INR = 84.0` (placeholder) in config.
  `data.download_symbols` maps such symbols to the right Yahoo ticker; the Nifty 50 download is unchanged.
- **Fills**: each leg fills at its own first open after the decision date (a US leg on the US calendar).
  Previously the fill day was the first date in the combined price index; for symbols trading on the
  same calendar this is the same day.
- **Ledger**: `state/thesis_trades.csv` gains columns `thesis, leg_side, currency, fx, label` (the file
  did not exist yet; an older file keeps its header). `price` is in the listing's currency, money columns
  in INR.
- **Journal / dashboard**: "### Second-order theses (winner vs losers)" with each leg's unrealised and
  realised P&L and the thesis' combined P&L; dashboard cards show winner vs losers side by side. The
  catalyst due-flag covers every leg: it stays up until each held leg has an exit queued.
- **Evidence**: (1) a scratch test drove a long INR leg and a short USD leg through entry, a mark up, a
  mark down and exit, plus the 50% short limit and bad orders: every number matched a hand calculation to
  the paisa. (2) Old and new `thesis.py` on the same single-symbol scenario (cap-sized BUY, BUY by amount,
  invalidation SELL, catalyst flag): portfolio, equity curve, trades, journal lines and thesis-file edits
  identical. (3) `run --offline` on the committed state, old vs new code: `state/*` and the journal
  byte-identical; `pending_orders.json` and the momentum part of `docs/data.json` identical apart from
  the wall-clock `generated_at` stamps.
- **Not done**: no per-leg stop (`invalidation_price` is ignored on second-order theses); lot sizes,
  futures margin blocking, F&O-list membership and FX moves are not modelled.
- **Revert if**: a short or a US leg ever changes a momentum file, or the FUT rates turn out wrong (fix
  the rates first).

## 2026-10-17 - Catalyst scanner added (no trading rule changed)

- **What**: `papertrade/scanner.py` pulls free news feeds (NSE announcements and results RSS, BSE
  corporate announcements via BSE's public JSON, ET Markets and ET Stocks RSS, Moneycontrol RSS, CNBC top
  news RSS) and Yahoo quotes for `watchlist.yaml` (Nifty 50 + open/proposed thesis symbols + WAAREEENER,
  META, CRWV, NBIS). Flags a move of more than 4% vs the previous close or volume above 3x the 20-day
  average; scores headlines on watchlist names, catalyst keywords, moving symbols and thesis matches.
  Writes `alerts/<date>.json`, `alerts/latest.md`, `docs/alerts.json`. New dashboard section "Alerts"
  under the header. `.github/workflows/scanner.yml` runs it every 30 minutes, 09:00-15:30 IST, weekdays.
  `SCANNER_PROMPT.md` is the brief for an hourly cloud routine that writes second-order notes to
  `alerts/notes/` and may draft theses under `theses/proposed/` (not read by the engine). New dependency:
  PyYAML 6.0.3.
- **Why**: the Waaree IPO (Oct 2024) and Meta GPU-rental (1 July 2026) examples were found after the
  move. The scanner exists to see such events on the day.
- **Evidence it does not touch trading**: it reads only `theses/*.md` and `watchlist.yaml`, writes only
  `alerts/` and `docs/alerts.json`, and imports nothing from engine, strategy, portfolio or thesis.
  First real run, 2026-10-17 03:45 IST (a Saturday, so Friday's data): 6 of 7 feeds OK (Moneycontrol 403),
  54 symbols priced, 7 flagged (TECHM +8.5% on 8.1x volume, WIPRO +6.0%, TCS +4.9%, INFY +4.6%,
  BHARTIARTL volume 3.7x, CRWV -11.9%, NBIS -12.2%), 58 alerts kept, 8 HIGH.
- **Found while building**: Yahoo's daily bar for 2026-10-16 on NSE symbols had volume but no Close.
  Dropping it silently judged Thursday's move instead (it flagged NESTLEIND -7.8%, a Thursday move, and
  missed TECHM's +8.5% Friday). The scanner now uses the last 5-minute price for such a bar. The momentum
  sleeve's own download (`data.py`) drops rows without a Close and was NOT changed; worth checking
  whether its 17:30 run ever sees the same gap.
- **Revert if**: the scanner commits ever conflict with the daily run, or the alerts are judged noise
  after two weeks of use (then tune `HIGH_SCORE`, the keyword weights or `watchlist.yaml` first).

## 2026-10-17 - India F&O and US sleeves added; Claude made the decision-maker (momentum and thesis sleeves unchanged)

- **What**: two more separate paper books, Rs 5,00,000 each, ADDED on top (total paper capital Rs 20,00,000):
  - **India F&O** (`papertrade/fno.py`): NIFTY, BANKNIFTY and the stocks in `config.FNO_STOCKS`; futures priced
    spot x (1 + 6.5% x days/365); options by Black-Scholes on the spot with India VIX (index) or 1.5x 30-day
    realised vol (stock): mark-to-model, not market quotes. Monthly expiry, auto-roll of futures, options settle
    at intrinsic. Margin 12% (index) / 20% (stock) of notional, blocked from cash.
  - **US** (`papertrade/us.py`): Rs 5,00,000 at the fixed USD_INR 84 ($5,952.38); stocks/ETFs long and paper
    short; options at the mid of real Yahoo option chains, with a labelled Black-Scholes fallback on the chain's
    implied vol. Zero commission, SEC fee and FINRA TAF.
  - Shared accounting and risk rules in `papertrade/derivbook.py`, enforced in code: max 5 positions, risk per
    order at most 10% of sleeve equity, no naked short options, free cash must cover margin, daily-loss circuit
    breaker at 3% of capital (no new positions until the next Monday). All limits listed in the new
    `RISK_LIMITS.md`.
  - Costs: new `OPT` product and `us_charges()` in `costs.py`; `data.download_tickers()` for raw Yahoo tickers
    (`download_symbols` behaves exactly as before). Journal gains "## India F&O sleeve", "## US sleeve" and
    "## All sleeves"; dashboard gains a four-sleeve strip in the header, two new sections and a combined chart;
    CLI gains `fno status` and `us status`.
- **Values that differ from the brief that commissioned this** (the brief said to verify them; 2026 sources
  say otherwise, so the newer values are used; all marked VERIFY in config/costs):
  NIFTY lot 65 and BANKNIFTY 30 (not 75 / 35: revised from the January 2026 series); stock futures expire on
  the last Tuesday (not Thursday: NSE moved all equity derivatives to Tuesday in September 2025); options STT
  0.15% of sell premium and of exercised intrinsic (not 0.1%: raised from 1 April 2026 by Budget 2026-27).
  **Not changed, flagged**: futures STT in `costs.py` is still 0.02%; the same sources say 0.05% since
  1 April 2026. Changing it would change the thesis sleeve's paper-short costs, so it is left for the user.
- **Autonomy (the user's decision)**: the daily reviewer (ROUTINE_PROMPT.md) now takes all paper decisions for
  the thesis, F&O and US sleeves without waiting for approval: it may activate drafted theses from
  `theses/proposed/`, open new theses of any type it can justify with the checklist, queue F&O and US orders
  (each with a written `reason` and `risk_check`; the engine rejects an OPEN without them) and close positions.
  Every decision gets a dated entry in the journal's "Claude's review"; rule-level changes go here. The hourly
  catalyst reviewer (SCANNER_PROMPT.md) may activate at most one drafted second_order or catalyst thesis per
  run when every checklist answer is concrete. Neither may edit code, config, limits or the momentum sleeve's
  orders. THESIS_REVIEW.md and DAILY.md updated to match (the old "a new position needs the user's agreement"
  rule is gone).
- **Consequence to know**: at this capital the 10% per-order rule rejects every future in the list (one NIFTY
  lot needs about Rs 1.8 lakh margin against a Rs 50,000 limit). The F&O sleeve can in practice hold long
  options and covered spreads only. Left as specified; changing it is the user's call.
- **Evidence**: hand-checked against raw Yahoo bars (future entry, carry, margin, MTM, roll; long call vs a
  numerical integration of the lognormal payoff; covered short call; US stock long; US long and short call from
  the real AAPL chain): every figure matched to the paisa / cent (one 1-paisa rounding difference in futures
  sell charges). Old (HEAD) and new code on copies of the committed state, `run --offline` and `run`: momentum
  `state/*`, `decisions/pending_orders.json` (apart from the wall-clock `generated_at`), the momentum and thesis
  parts of the journal and of `docs/data.json` byte-identical; thesis state and orders identical. That state has
  no fills (Yahoo's 16 Oct NSE bar has no Close), so a 9-day replay from an empty book (5-15 Oct, 15 momentum
  fills) was also run old vs new: every momentum and thesis file and journal part identical.
- **Not modelled**: SPAN margin changes after entry, early exercise, option skew/liquidity, FX moves, tax.
- **Revert if**: either new sleeve ever changes a momentum or thesis file or fails a daily run (each step is
  wrapped so a failure is logged and the other sleeves are still saved).

## 2026-10-17 - USD/INR: daily Yahoo rate replaces the fixed 84.0; US sleeve re-initialised

- **Why**: the fixed `USD_INR = 84.0` was wrong. Yahoo `USDINR=X` closed at 95.78, 95.29, 95.15, 94.52 and
  94.96 over the last five bars (13-17 Oct 2026). At 84 the US sleeve's Rs 5,00,000 was $5,952; at the
  real rate it is about $5,290.
- **What**: new `papertrade/fx.py`. `get_usd_inr(date)` / `lookup(date)` download USDINR=X daily closes
  (same yfinance path as `data.py`), cache them in `state/fx_rates.csv` (date, rate, source, fetched_at)
  and return the close on or before the date; if the download fails, the cache; only with no cache,
  `config.USD_INR_FALLBACK = 95.0`. Each source is logged and named in the journal. `engine.run_day` sets
  the NSE-date rate into `config.USD_INR` / `FX_TO_INR["USD"]` before the thesis sleeve runs (thesis US legs
  use it); the US sleeve uses the close on or before its US market date.
- **US sleeve**: starting capital is Rs 5,00,000 / USD/INR of its start date, stored as `start_fx` in
  `state/us_portfolio.json`. INR equity each day = USD equity x that day's rate, so a rupee move is P&L;
  `state/us_equity_curve.csv` gains `usd_inr, fx_date, day_pnl_inr, day_fx_pnl_inr, total_pnl_inr,
  fx_pnl_inr`, and the journal and dashboard show "of which FX". Limits and the circuit breaker stay in
  USD (an FX move alone never trips the breaker).
- **Re-initialised**: `state/us_portfolio.json` and `state/us_equity_curve.csv` had been created on
  2026-10-16 at 84.0 ($5,952.38). No US order had ever filled, so the first run with this code
  re-created them at 94.5248 (Yahoo close of 2026-10-16): $5,289.62. The code does this only when the
  book has no `start_fx` AND no position or trade; otherwise it keeps the book and labels 84.0 as its
  legacy start rate.
- **Evidence**: HEAD vs new code on copies of the committed state (`run` and `run --offline`; the online
  run included real momentum fills at the 16 Oct open): momentum and thesis state, `pending_orders.json`
  (apart from `generated_at`), F&O state and orders, and the journal's momentum, thesis and F&O sections
  byte-identical. The thesis export differs only in `usd_inr` (84.0 -> the day's rate); no thesis has a US
  leg today. FX split checked by hand: rate 94.5248 -> 95.00 with a $10 loss gives INR P&L 1,563.63, of
  which FX 2,513.63 on the day (previous USD equity x rate change).
- **Revert if**: Yahoo's USDINR=X proves unreliable (it had a 93.55 print on 2026-08-26 between 95.70 and
  95.43, likely a bad tick); then switch the source, not back to a fixed rate.
