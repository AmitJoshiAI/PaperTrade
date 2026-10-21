"""USD/INR exchange rate: Yahoo Finance USDINR=X daily closes, cached in state/fx_rates.csv.

get_usd_inr(date) returns the USDINR=X close on or before `date`. Order of sources, each logged:
  1. a fresh Yahoo download (the same yfinance path as data.py), merged into the cache;
  2. the cache, state/fx_rates.csv (date, rate, source, fetched_at), if the download failed or was skipped
     (an offline run): the cached close on or before `date`, else the last cached close;
  3. config.USD_INR_FALLBACK, only if there is no cached rate at all.
lookup(date) returns the same rate with its date and a one-line description of where it came from.

The engine calls use_rate() once per run, so every INR conversion of a USD leg (thesis US legs, the
whole US sleeve) uses that day's rate. Human-checkable reference: the RBI / FBIL reference rate
(https://www.fbil.org.in/), published once a day around 13:30 IST; Yahoo's close and the reference rate
can differ by a few paise.
"""
from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd

from . import config

log = logging.getLogger(__name__)

YAHOO_TICKER = "USDINR=X"
FX_FILE = config.STATE_DIR / "fx_rates.csv"
COLUMNS = ["date", "rate", "source", "fetched_at"]
_refreshed = False


@dataclass
class FxRate:
    rate: float
    date: str | None          # the date of the close used (None for the config fallback)
    source: str               # "Yahoo USDINR=X", "cache" or "fallback"
    note: str                 # one line for the journal / log

    @property
    def label(self) -> str:
        return f"{self.rate:.4f} ({self.note})"


def _read_cache() -> pd.DataFrame:
    if not FX_FILE.exists():
        return pd.DataFrame(columns=COLUMNS)
    return pd.read_csv(FX_FILE, dtype={"date": str, "source": str, "fetched_at": str})


def refresh(period: str = "3mo") -> bool:
    """Download USDINR=X daily closes and merge them into the cache. A row is rewritten only when its
    rate changed (today's bar firms up later), so unchanged rows keep their fetched_at. Returns True on
    success. Never raises: a failed download leaves the cache as it was."""
    global _refreshed
    try:
        from .data import download_tickers
        px = download_tickers([YAHOO_TICKER], period=period, allow_empty=True)
    except Exception as e:
        log.warning("USD/INR: download of %s failed (%s); using the cache", YAHOO_TICKER, e)
        return False
    if px.empty:
        log.warning("USD/INR: Yahoo returned no %s data; using the cache", YAHOO_TICKER)
        return False
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cache = {r["date"]: r for r in _read_cache().to_dict("records")}
    newest = px["date"].max().strftime("%Y-%m-%d")
    if cache and newest < max(cache):
        # a history ending before the cached one: keep the cache untouched (same rule as staleness.py)
        log.error("stale data from Yahoo (%s %s < cached %s); USD/INR cache not changed", YAHOO_TICKER, newest, max(cache))
        return False
    changed = False
    for d, c in zip(px["date"].dt.strftime("%Y-%m-%d"), px["close"]):
        rate = round(float(c), 4)
        old = cache.get(d)
        if old is None or abs(float(old["rate"]) - rate) > 1e-9:
            cache[d] = dict(date=d, rate=rate, source=f"Yahoo {YAHOO_TICKER} close", fetched_at=now)
            changed = True
    if changed or not FX_FILE.exists():
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(FX_FILE, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=COLUMNS)
            w.writeheader()
            for d in sorted(cache):
                w.writerow({k: cache[d][k] for k in COLUMNS})
    _refreshed = True
    return True


def lookup(date, download: bool = True) -> FxRate:
    """The USDINR=X close on or before `date` (ISO string or Timestamp), with where it came from."""
    day = pd.Timestamp(date).strftime("%Y-%m-%d")
    fresh = _refreshed or (download and refresh())
    cache = _read_cache()
    if len(cache):
        on_or_before = cache[cache["date"] <= day]
        if len(on_or_before):
            r = on_or_before.iloc[-1]
            src = "Yahoo USDINR=X" if fresh else "cache"
            note = (f"Yahoo USDINR=X close of {r['date']}" if fresh
                    else f"cached Yahoo USDINR=X close of {r['date']}, fetched {r['fetched_at']} (no fresh download)")
            out = FxRate(float(r["rate"]), r["date"], src, note)
        else:
            r = cache.iloc[-1]
            out = FxRate(float(r["rate"]), r["date"], "cache",
                         f"no close on or before {day}; last cached Yahoo USDINR=X close of {r['date']}")
    else:
        out = FxRate(float(config.USD_INR_FALLBACK), None, "fallback",
                     f"FALLBACK config.USD_INR_FALLBACK (no Yahoo rate and no cache)")
    log.info("USD/INR for %s: %s", day, out.label)
    return out


def get_usd_inr(date, download: bool = True) -> float:
    """USD/INR close on or before `date`; see the module docstring for the fallbacks."""
    return lookup(date, download).rate


def use_rate(r: FxRate) -> None:
    """Make `r` the rate for this process: config.USD_INR and config.FX_TO_INR["USD"], which the thesis
    sleeve's fx_rate() and every other USD -> INR conversion read."""
    config.USD_INR = r.rate
    config.FX_TO_INR["USD"] = r.rate
