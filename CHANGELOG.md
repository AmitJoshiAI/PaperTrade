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
