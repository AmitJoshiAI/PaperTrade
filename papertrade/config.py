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

# Thesis legs priced in another currency (e.g. a US ticker, `market: US`) keep their prices in that
# currency; cash, P&L and sleeve equity are in INR at this FIXED rate. 84.0 is a placeholder, not a live
# rate: change it here when it drifts. Changing it revalues every open foreign leg at once (no FX history).
USD_INR = 84.0
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

US_CAPITAL = 500_000.0                # INR; held in USD at USD_INR (fixed, see above): Rs 5,00,000 / 84 = $5,952.38
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
# derivatives (index AND stock) from Thursday to Tuesday in September 2025, so both are Tuesday here. The
# brief that commissioned this sleeve said Thursday for stock futures; Tuesday follows the 2026 sources.
# VERIFY on nseindia.com. A holiday expiry is handled by settling on the last trading day on or before it.
INDEX_EXPIRY_WEEKDAY = 1
STOCK_EXPIRY_WEEKDAY = 1
# Underlyings. lot = contract lot size, step = option strike interval. VERIFY ON nseindia.com: NSE revises
# lot sizes (NIFTY 75 -> 65 and BANKNIFTY 35 -> 30 from the January 2026 series, per 2026 broker notes) and
# the F&O list itself. Only the symbols listed here can be traded in the sleeve; add a line to add one.
FNO_INDEX = {
    "NIFTY": {"yahoo": "^NSEI", "lot": 65, "step": 50},
    "BANKNIFTY": {"yahoo": "^NSEBANK", "lot": 30, "step": 100},
}
FNO_STOCKS = {                         # NSE symbol -> lot size and strike step. Examples only: VERIFY each.
    "RELIANCE": {"lot": 500, "step": 10},
    "HDFCBANK": {"lot": 550, "step": 10},
    "ICICIBANK": {"lot": 700, "step": 10},
    "INFY": {"lot": 400, "step": 20},
    "SBIN": {"lot": 750, "step": 10},
    "TCS": {"lot": 175, "step": 20},
    "ONGC": {"lot": 2250, "step": 2.5},
    "HINDPETRO": {"lot": 2025, "step": 5},
}

# US sleeve. Options use REAL Yahoo option chains (mid of bid/ask); 100 shares per contract.
US_CONTRACT_SIZE = 100
US_SHORT_STOCK_MARGIN_PCT = 0.50      # paper short stock: 50% of notional blocked (Reg T initial margin)
US_RISK_FREE_RATE = 0.04              # approximate US T-bill yield; used ONLY by the labelled Black-Scholes fallback
                                      # when a chain has no two-sided quote. VERIFY / update by hand.
