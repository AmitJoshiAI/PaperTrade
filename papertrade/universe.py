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
    """NSE symbol -> SYMBOL.NS. Two other forms are used only by the thesis sleeve (Nifty 50 symbols have
    neither a '.' nor a ':'): "US:META" (a US listing, `market: US`) -> META, and a symbol that already
    contains a '.' (an explicit Yahoo ticker such as 7203.T) -> unchanged."""
    if nse_symbol.startswith("US:"):
        return nse_symbol[3:]
    if "." in nse_symbol:
        return nse_symbol
    return f"{nse_symbol}.NS"


def nse_symbol(yahoo: str) -> str:
    return yahoo[:-3] if yahoo.endswith(".NS") else yahoo
