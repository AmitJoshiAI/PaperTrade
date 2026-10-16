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
    tickers = [yahoo_symbol(s) for s in NIFTY50]
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
        df["symbol"] = nse_symbol(t)
        frames.append(df)
    if dropped:
        log.warning("Dropped %d symbols with no Yahoo data: %s", len(dropped), ", ".join(dropped))
    out = pd.concat(frames, ignore_index=True)
    out = out.sort_values(["symbol", "date"]).reset_index(drop=True)
    out.attrs["dropped"] = dropped
    return out


def update_cache() -> pd.DataFrame:
    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    prices = download_prices()
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
