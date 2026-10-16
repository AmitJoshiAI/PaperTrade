"""Nifty 50 universe. Constituents change twice a year (March/September review).
Verify against https://www.nseindia.com/products-services/indices-nifty50-index before trusting.
Yahoo Finance symbol = NSE symbol + ".NS". Symbols that Yahoo cannot serve are dropped at load time.
"""

NIFTY50 = [
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK",
    "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV", "BEL", "BHARTIARTL",
    "CIPLA", "COALINDIA", "DRREDDY", "EICHERMOT", "ETERNAL",
    "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE", "HINDALCO",
    "HINDUNILVR", "ICICIBANK", "INDIGO", "INFY", "ITC",
    "JIOFIN", "JSWSTEEL", "KOTAKBANK", "LT", "M&M",
    "MARUTI", "MAXHEALTH", "NESTLEIND", "NTPC", "ONGC",
    "POWERGRID", "RELIANCE", "SBILIFE", "SBIN", "SHRIRAMFIN",
    "SUNPHARMA", "TCS", "TATACONSUM", "TATASTEEL", "TECHM",
    "TITAN", "TRENT", "ULTRACEMCO", "WIPRO", "TMPV",
]
# Note: TATAMOTORS demerged in Oct 2025 into TMPV (passenger vehicles) and TMCV (commercial).
# Yahoo no longer serves TATAMOTORS.NS. If TMPV.NS also fails it is simply dropped.


def yahoo_symbol(nse_symbol: str) -> str:
    return f"{nse_symbol}.NS"


def nse_symbol(yahoo: str) -> str:
    return yahoo[:-3] if yahoo.endswith(".NS") else yahoo
