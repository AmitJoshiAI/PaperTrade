"""Guard against Yahoo returning a history that ends BEFORE a date the books have already processed.

On 2026-10-19 two late runs got a download ending 2026-10-16 (the 19th missing) and re-marked every sleeve
as of the 16th, rewriting that day's rows with positions opened on the 19th (CHANGELOG 2026-10-21). Rule:

  download date <  latest recorded date  -> StaleDataError: nothing is written; `run` exits with code 3
  download date == latest recorded date  -> the normal idempotent same-date re-run
  download date >  latest recorded date  -> a new day
"""
from __future__ import annotations

import logging

import pandas as pd

log = logging.getLogger(__name__)

STALE_EXIT_CODE = 3


class StaleDataError(RuntimeError):
    """The downloaded prices end before a date already recorded in state."""


def last_date(curve: pd.DataFrame) -> str | None:
    """Latest date in an equity curve (max, not the last row: rows can be out of order)."""
    if curve is None or curve.empty or "date" not in curve.columns:
        return None
    return str(curve["date"].astype(str).max())


def ensure_not_older(day: str, recorded: str | None, what: str) -> None:
    if recorded and str(day) < str(recorded):
        msg = f"stale data from Yahoo ({day} < {recorded}, {what})"
        log.error(msg)
        raise StaleDataError(msg)
