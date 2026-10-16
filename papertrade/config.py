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

PRODUCT = "CNC"             # CNC = delivery. "MIS" = intraday (not used by baseline strategy)
