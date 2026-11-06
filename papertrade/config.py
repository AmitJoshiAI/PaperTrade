"""Project-wide settings. Edit here, not in the modules."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "state"
JOURNAL_DIR = ROOT / "journal"
DECISIONS_DIR = ROOT / "decisions"
CACHE_DIR = ROOT / "data_cache"

PORTFOLIO_FILE = STATE_DIR / "portfolio.json"
TRADES_FILE = STATE_DIR / "trades.csv"
EQUITY_FILE = STATE_DIR / "equity_curve.csv"
PENDING_ORDERS_FILE = DECISIONS_DIR / "pending_orders.json"
PRICES_FILE = CACHE_DIR / "prices_daily.parquet"

# Thesis sleeve (separate book: own capital, own ledger, own weekly review; see THESIS_REVIEW.md)
THESES_DIR = ROOT / "theses"
THESIS_PORTFOLIO_FILE = STATE_DIR / "thesis_portfolio.json"
THESIS_TRADES_FILE = STATE_DIR / "thesis_trades.csv"
THESIS_EQUITY_FILE = STATE_DIR / "thesis_equity_curve.csv"
THESIS_ORDERS_FILE = DECISIONS_DIR / "thesis_orders.json"
THESIS_PRICES_FILE = CACHE_DIR / "thesis_prices.parquet"

# Capital and targets (INR)
STARTING_CAPITAL = 500_000.0
TARGET_NET_PER_DAY = 1_000.0          # after tax, the user's goal
ASSUMED_TAX_RATE = 0.20               # STCG on delivery equity held < 1 year (FY25-26 rate)
TARGET_GROSS_PER_DAY = TARGET_NET_PER_DAY / (1 - ASSUMED_TAX_RATE)   # 1250 pre-tax

# Strategy parameters (momentum rotation baseline)
MAX_POSITIONS = 5
LOOKBACK_MOMENTUM = 63      # ~3 months of trading days
SMA_FAST = 20
SMA_SLOW = 50
RANK_ENTRY = 5              # enter if in top N by momentum
RANK_EXIT = 10              # exit if falls below top N
STOP_LOSS_PCT = 0.07        # 7% from entry
MIN_HISTORY_DAYS = 80

# Execution assumptions
SLIPPAGE_PCT = 0.0005       # 0.05% adverse slippage on fills at open
HISTORY_PERIOD = "2y"       # how much daily history to pull from Yahoo
MAX_STALE_BARS = 3          # carry a missing close forward at most this many bars (data gaps, not a strategy rule)

# Thesis sleeve capital. ADDED to the momentum sleeve's Rs 5,00,000, not carved out of it, so the
# momentum book and its history are untouched. Total paper capital is now Rs 10,00,000.
THESIS_CAPITAL = 500_000.0
THESIS_MAX_POSITION_PCT = 0.25        # hard cap per thesis: 25% of sleeve equity; a thesis file may set a lower cap
THESIS_MAX_SHORT_PCT = 0.50           # paper shorts: total short notional <= 50% of the sleeve's capital
                                      # (the lower of starting capital and current equity), checked at each SHORT fill

# USD legs (thesis US legs and the whole US sleeve) keep their prices in USD; INR values use the day's
# USD/INR rate: Yahoo USDINR=X daily close, cached in state/fx_rates.csv (papertrade/fx.py). Each run sets
# USD_INR and FX_TO_INR["USD"] to that day's rate before the thesis and US sleeves run. The value below is
# used only when there is neither a Yahoo rate nor a cached one (fx.py logs it as FALLBACK).
USD_INR_FALLBACK = 95.0
USD_INR = USD_INR_FALLBACK            # overwritten each run by fx.use_rate()
FX_TO_INR = {"INR": 1.0, "USD": USD_INR}

PRODUCT = "CNC"             # CNC = delivery. "MIS" = intraday (not used by baseline strategy)

# ---------------------------------------------------------------------------------------------------
# India F&O sleeve (papertrade/fno.py) and US sleeve (papertrade/us.py). Two more separate books, each
# ADDED on top of the existing capital (total paper capital Rs 20,00,000). Every limit below is listed
# again, with its reason, in RISK_LIMITS.md. The reviewer routines may NOT change them.
FNO_CAPITAL = 500_000.0
FNO_PORTFOLIO_FILE = STATE_DIR / "fno_portfolio.json"
FNO_TRADES_FILE = STATE_DIR / "fno_trades.csv"
FNO_EQUITY_FILE = STATE_DIR / "fno_equity_curve.csv"
FNO_ORDERS_FILE = DECISIONS_DIR / "fno_orders.json"
FNO_PRICES_FILE = CACHE_DIR / "fno_prices.parquet"

US_CAPITAL = 500_000.0                # INR; converted to USD at the USD/INR close of the sleeve's start date
                                      # (state/us_portfolio.json "start_fx"); INR equity = USD equity x day's rate
US_PORTFOLIO_FILE = STATE_DIR / "us_portfolio.json"
US_TRADES_FILE = STATE_DIR / "us_trades.csv"
US_EQUITY_FILE = STATE_DIR / "us_equity_curve.csv"
US_ORDERS_FILE = DECISIONS_DIR / "us_orders.json"
US_PRICES_FILE = CACHE_DIR / "us_prices.parquet"

# Risk rules shared by both sleeves (enforced in code: papertrade/derivbook.py)
DERIV_MAX_POSITIONS = 5               # open positions (every future / option / stock line counts as one)
DERIV_MAX_ORDER_RISK_PCT = 0.10       # per order: premium (long option), margin (future, short option,
                                      # short stock) or notional (long stock) <= 10% of sleeve equity
DERIV_DAILY_LOSS_PCT = 0.03           # circuit breaker: a day's loss > 3% of starting capital blocks new
                                      # positions until the next Monday (exits are always allowed)

# India F&O pricing. Futures: spot x (1 + CARRY_RATE x days_to_expiry / 365). Options: Black-Scholes on the
# spot (European), r = CARRY_RATE, vol = India VIX / 100 for index options and 1.5 x the stock's 30-day
# realised vol for stock options. MARK-TO-MODEL, NOT MARKET QUOTES: Yahoo has no NSE option chains.
CARRY_RATE = 0.065
STOCK_OPT_VOL_MULT = 1.5
REALISED_VOL_DAYS = 30
FNO_VIX_SYMBOL = "^INDIAVIX"
FUT_MARGIN_PCT = {"index": 0.12, "stock": 0.20}    # of notional at entry, blocked from cash until exit
FNO_OPT_SLIPPAGE_PCT = 0.01           # 1% adverse on model option premiums (stands in for the bid/ask spread
                                      # a model price does not have); futures use SLIPPAGE_PCT like the other sleeves
# Monthly expiry = the last <weekday> of the month (0 = Monday ... 3 = Thursday). NSE moved ALL its equity
# derivatives (index AND stock) from Thursday to Tuesday in September 2025: NIFTY weekly and monthly (last
# Tuesday), BANKNIFTY and stock options monthly (last Tuesday). Verified: broker notes, 4 Nov 2026, and NSE's
# own expiry list (option-chain-contract-info), which shows a holiday expiry moved to the trading day before
# (24 Nov 2026 -> Mon 23 Nov 2026). When a chain cache exists (state/chains/<SYM>.json, written by the daily
# run), fno.py snaps the computed Tuesday to NSE's listed date; without one, a holiday expiry settles on the
# last trading day on or before it.
INDEX_EXPIRY_WEEKDAY = 1
STOCK_EXPIRY_WEEKDAY = 1
# Underlyings. lot = contract lot size, step = option strike interval near the money. Lot sizes verified
# against NSE's fo_mktlots.csv (NOV-26 and DEC-26 series) and broker notes, 4 Nov 2026: NIFTY 65, BANKNIFTY 30.
# NSE revises lots twice a year; the JAN-27 series changes HDFCBANK to 750, INFY to 500 and TCS to 250 (the
# others are unchanged): update these lines when the January 2027 series becomes the current expiry. The
# daily run compares this table with fo_mktlots.csv and warns in the journal on any mismatch (chains.py).
# Strike steps checked against NSE's listed strikes near the spot on 4 Nov 2026. Only the symbols listed here
# can be traded in the sleeve; add a line to add one (it is not the full NSE F&O list).
FNO_INDEX = {
    "NIFTY": {"yahoo": "^NSEI", "lot": 65, "step": 50},
    "BANKNIFTY": {"yahoo": "^NSEBANK", "lot": 30, "step": 100},
}
FNO_STOCKS = {                         # NSE symbol -> lot size and strike step
    "RELIANCE": {"lot": 500, "step": 10},
    "HDFCBANK": {"lot": 650, "step": 10},      # was 550 (wrong); 750 from the JAN-27 series
    "ICICIBANK": {"lot": 700, "step": 10},
    "INFY": {"lot": 400, "step": 20},          # 500 from the JAN-27 series
    "SBIN": {"lot": 750, "step": 10},
    "TCS": {"lot": 225, "step": 20},           # was 175 (wrong); 250 from the JAN-27 series
    "ONGC": {"lot": 2250, "step": 2.5},
    "HINDPETRO": {"lot": 2025, "step": 5},
    "BAJFINANCE": {"lot": 750, "step": 10},    # added 4 Nov 2026: the bajaj-finance-rbi-december thesis
}
# Daily F&O candidates (candidates.py, journal "## F&O candidates"): these indices always, plus every
# FNO_STOCKS symbol held by an open thesis or named by a queued thesis order. NIFTY also shows its nearest
# weekly expiry. Real NSE option chains are fetched by the daily run (chains.py) and cached in state/chains/.
FNO_CANDIDATE_INDICES = ["NIFTY", "BANKNIFTY"]
FNO_WEEKLY_INDICES = ["NIFTY"]
CHAINS_DIR = STATE_DIR / "chains"
FNO_CANDIDATES_FILE = STATE_DIR / "fno_candidates.json"
US_CANDIDATES_FILE = STATE_DIR / "us_candidates.json"
CHAIN_STRIKE_BAND = 0.15              # keep strikes within +/-15% of the spot in the cache (file size)

# US sleeve. Options use REAL Yahoo option chains (mid of bid/ask); 100 shares per contract.
US_CONTRACT_SIZE = 100
US_SHORT_STOCK_MARGIN_PCT = 0.50      # paper short stock: 50% of notional blocked (Reg T initial margin)
US_RISK_FREE_RATE = 0.04              # approximate US T-bill yield; used ONLY by the labelled Black-Scholes fallback
                                      # when a chain has no two-sided quote. VERIFY / update by hand.
# US candidates (journal "## US candidates"). SPY and QQQ are listed for reference only (one ATM contract,
# $800-1,600, is above the 10% limit of about $529 at this size). The sector / country / commodity / bond ETFs
# let the reviewer express a macro view inside the limit (financials XLF, regional banks KRE, energy XLE,
# utilities XLU, staples XLP, health XLV, gold miners GDX, silver SLV, Brazil EWZ, emerging markets EEM,
# long Treasuries TLT); the five stocks are cheap, liquid single names. Every ticker except the reference ones
# is shown only while its last close is at or below US_CANDIDATE_MAX_PRICE (checked each run, so the list
# follows the prices); the ones above it are named in one line. Edit freely. Added ETFs 6 Nov 2026.
US_CANDIDATE_REFERENCE = ["SPY", "QQQ"]
US_CANDIDATE_TICKERS = ["SPY", "QQQ", "XLF", "KRE", "XLE", "XLU", "XLP", "XLV", "GDX", "SLV", "EWZ", "EEM", "TLT",
                        "F", "PFE", "INTC", "SOFI", "BAC"]
US_CANDIDATE_MAX_PRICE = 65.0         # "under about $60": a little headroom so XLE / EEM do not flicker in and out
US_CANDIDATE_MIN_DAYS = 14
US_CANDIDATE_MAX_DAYS = 42
