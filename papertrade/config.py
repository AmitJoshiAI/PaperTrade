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
