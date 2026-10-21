"""Daily OHLCV download and cache for the universe (Yahoo Finance via yfinance)."""
from __future__ import annotations

import logging
import pandas as pd
import yfinance as yf

from . import config
from .universe import NIFTY50, yahoo_symbol, nse_symbol

log = logging.getLogger(__name__)


def download_prices(period: str = config.HISTORY_PERIOD) -> pd.DataFrame:
    """Return a long DataFrame: columns [date, symbol, open, high, low, close, volume].
    Symbols with no data are dropped and logged."""
    return download_symbols(NIFTY50, period)


def download_symbols(symbols, period: str = config.HISTORY_PERIOD, allow_empty: bool = False) -> pd.DataFrame:
    """Same as download_prices for any list of symbols (the thesis sleeve is not limited to Nifty 50).
    A symbol is an NSE symbol (Yahoo SYMBOL.NS), "US:XXX" for a US listing (Yahoo XXX), or an explicit
    Yahoo ticker containing a '.' (used as is); see universe.yahoo_symbol. Prices stay in the listing's
    own currency. The "symbol" column holds the symbol exactly as passed in.
    If every symbol fails: raises (as download_prices always has) unless allow_empty, which returns an
    empty frame with the same columns."""
    return _download({yahoo_symbol(s): s for s in symbols}, period, allow_empty)


def download_tickers(tickers, period: str = config.HISTORY_PERIOD, allow_empty: bool = True) -> pd.DataFrame:
    """Same frame for raw Yahoo tickers used exactly as given (^NSEI, ^INDIAVIX, SPY, RELIANCE.NS); the
    "symbol" column holds the ticker. Used by the F&O and US sleeves."""
    return _download({t: t for t in tickers}, period, allow_empty)


def _download(key_of: dict, period: str, allow_empty: bool) -> pd.DataFrame:
    tickers = list(key_of)
    raw = yf.download(
        tickers, period=period, interval="1d",
        auto_adjust=False, progress=False, group_by="ticker", threads=True,
    )
    frames = []
    dropped = []
    for t in tickers:
        try:
            df = raw[t].dropna(subset=["Close"])
        except KeyError:
            dropped.append(t)
            continue
        if df.empty:
            dropped.append(t)
            continue
        df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
        df.columns = ["open", "high", "low", "close", "volume"]
        df.index.name = "date"
        df = df.reset_index()
        df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None).dt.normalize()
        df["symbol"] = key_of.get(t, nse_symbol(t))
        frames.append(df)
    if dropped:
        log.warning("Dropped %d symbols with no Yahoo data: %s", len(dropped), ", ".join(dropped))
    if not frames and allow_empty:
        out = pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "symbol"])
        out.attrs["dropped"] = dropped
        return out
    out = pd.concat(frames, ignore_index=True)
    out = out.sort_values(["symbol", "date"]).reset_index(drop=True)
    out.attrs["dropped"] = dropped
    return out


def update_cache(check=None) -> pd.DataFrame:
    """Download and cache. `check(prices)`, if given, runs BEFORE the cache is overwritten (the engine's
    stale-data guard raises there, so a stale download never replaces a newer cache)."""
    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    prices = download_prices()
    if check is not None:
        check(prices)
    prices.to_parquet(config.PRICES_FILE, index=False)
    return prices


def load_cache() -> pd.DataFrame:
    if not config.PRICES_FILE.exists():
        return update_cache()
    return pd.read_parquet(config.PRICES_FILE)


def latest_date(prices: pd.DataFrame) -> pd.Timestamp:
    return prices["date"].max()


def pivot(prices: pd.DataFrame, field: str = "close") -> pd.DataFrame:
    """Wide DataFrame: index=date, columns=symbol."""
    return prices.pivot(index="date", columns="symbol", values=field).sort_index()
