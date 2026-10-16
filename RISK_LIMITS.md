# Risk limits: every limit in one place

Paper money only. Four separate sleeves of Rs 5,00,000 each (Rs 20,00,000 in all); a loss in one never
draws on another. "Code" means the engine enforces it on every run and rejects or clips what breaks it;
"prompt" means it is an instruction to the Claude reviewers (ROUTINE_PROMPT.md, SCANNER_PROMPT.md) that
the code does not check. Values live in `papertrade/config.py`. Neither reviewer may change any value
here; a change is proposed in the journal and CHANGELOG.md and made by the user (or an interactive
session the user asks).

## Momentum sleeve (rules decide; Claude only reviews)

| Limit | Value | Where |
|---|---|---|
| Open positions | 5 | code (`MAX_POSITIONS`) |
| Position size | free cash / free slots (about 20% each) | code |
| Stop-loss | close below fill price - 7% (fixed, not trailing) | code (`STOP_LOSS_PCT`) |
| Other exits | close below SMA50, or rank outside the top 10 | code |
| Slippage | 0.05% adverse on every fill | code (`SLIPPAGE_PCT`) |
| Its orders | written by the strategy; never edited by the reviewers | prompt |

## Thesis sleeve (Claude decides)

| Limit | Value | Where |
|---|---|---|
| Size per thesis | at most 25% of sleeve equity (gross of all legs); a thesis file may set lower | code (`THESIS_MAX_POSITION_PCT`) |
| Paper shorts | total short notional at most 50% of the lower of capital and equity | code (`THESIS_MAX_SHORT_PCT`) |
| Short proceeds | cannot fund buys (buys limited to cash minus open short value) | code |
| Invalidation | a numeric `invalidation_price` hit on the close queues a SELL for the next open | code |
| Catalyst exit | the post-event rule is applied literally on the first trading day after the event | prompt |
| Every thesis | a written invalidation and (catalyst / second_order) a post-event rule before the first order | prompt |
| Scanner activations | at most 1 drafted thesis per hourly run, second_order or catalyst only, all checklist answers concrete | prompt |

## India F&O sleeve and US sleeve (Claude decides)

| Limit | Value | Where |
|---|---|---|
| Open positions | 5 per sleeve (each future, option or stock line counts) | code (`DERIV_MAX_POSITIONS`) |
| Risk per order | at most 10% of sleeve equity. Risk = premium (long option), margin (future, short option, short stock), notional (long stock) | code (`DERIV_MAX_ORDER_RISK_PCT`) |
| Free cash | must cover the margin / premium plus charges the order takes out | code |
| Naked short options | rejected: a short CALL needs a long future / long call of the same underlying and expiry (US: or 100 shares per contract); a short PUT needs a short future / long put of the same expiry (US: or short stock). Each cover unit covers one short unit | code |
| Closing a cover | rejected while it would leave a short option uncovered; close the short first | code |
| Daily-loss circuit breaker | a day's equity change below -3% of starting capital (Rs 15,000; US $178.57) blocks new positions and futures rolls until the next Monday; exits always allowed; state in `state/*_portfolio.json` "breaker" | code (`DERIV_DAILY_LOSS_PCT`) |
| Futures margin | 12% (index) / 20% (stock) of the fill notional, blocked until exit | code (`FUT_MARGIN_PCT`) |
| Short option margin | 0 when covered by a future or stock; the spread's max loss (strike gap x units) when covered by a long option | code |
| US short stock margin | 50% of notional (Reg T initial) | code (`US_SHORT_STOCK_MARGIN_PCT`) |
| Every OPEN order | must carry a written `reason` and `risk_check`, or it is rejected | code |
| Futures roll | on the expiry day, closed at the close and reopened next month at the next open, through all the checks above; `"roll": false` to stop it | code |
| Slippage | futures and US stock 0.05%; India model option premiums 1%; US options none (filled at the chain mid) | code |

What the 10% rule means at this size (worked out 2026-10-17, NIFTY about 23,000):

- One NIFTY future (65 units) needs about Rs 1.8 lakh margin; one BANKNIFTY future (30 units) about
  Rs 2 lakh; one RELIANCE future (500 units) about Rs 1.2 lakh. The limit is about Rs 50,000, so
  **every future in the list is rejected**. The F&O sleeve can in practice hold long options and
  covered option spreads only. That follows from the rule as specified; it is not a bug. Changing it is
  the user's call.
- US: the sleeve is about $5,950, so about $595 per order: one share of SPY (about $770) does not fit;
  one ATM AAPL call (about $1,130) does not; a $3-5 premium option or a few shares of a cheaper stock do.

## Things NOT modelled (read before trusting a number)

- India options are mark-to-model (Black-Scholes on India VIX or 1.5x realised vol), not market quotes:
  no skew, no real spread, no liquidity check. Futures are priced from the spot with a fixed 6.5% carry.
- Margin is fixed at entry (no daily SPAN recalculation, no margin calls); exchange MTM settlement is not
  modelled separately (it nets to the same equity).
- US options are cash-settled at intrinsic on expiry; early exercise and assignment are not modelled.
- FX is fixed at `USD_INR` (84.0); a rupee move does not change the US sleeve's INR value.
- Tax is not modelled. The user is in the 30% slab: F&O profits (business income) and US short-term
  gains are taxed at the slab rate, so a gross paper profit is about 70% of that after tax.
- Lot sizes, strike steps, expiry weekdays and charge rates are config values to VERIFY on nseindia.com
  and zerodha.com (see the comments in `config.py` and `costs.py`).
