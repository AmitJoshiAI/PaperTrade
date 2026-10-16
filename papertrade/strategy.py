"""Baseline strategy: momentum rotation on Nifty 50 cash stocks.

Rules (evaluated on the latest close, orders fill at the NEXT open):
  Universe filter : at least MIN_HISTORY_DAYS of data.
  Trend filter    : close > SMA_SLOW and SMA_FAST > SMA_SLOW.
  Rank            : LOOKBACK_MOMENTUM-day return, highest first, among trend-filtered names.
  Entry           : if positions < MAX_POSITIONS, buy the best-ranked names not yet held, equal weight
                    (cash / free slots).
  Exit            : stop-loss (close < entry * (1 - STOP_LOSS_PCT)), or close < SMA_SLOW,
                    or rank falls below RANK_EXIT.

This is deliberately simple and transparent. It exists so the daily review has something concrete
to criticise; the point of Phase 1 is to measure, not to be clever.
"""
from __future__ import annotations

import logging

import pandas as pd

from . import config
from .portfolio import Portfolio

log = logging.getLogger(__name__)


def indicators(closes: pd.DataFrame) -> pd.DataFrame:
    """closes: wide (date x symbol). Returns a per-symbol table for the last row.
    Isolated missing bars are carried forward (MAX_STALE_BARS) so one Yahoo gap does not
    blank a 50-day SMA and trigger a spurious exit."""
    closes = closes.ffill(limit=config.MAX_STALE_BARS)
    last = closes.iloc[-1]
    sma_f = closes.rolling(config.SMA_FAST).mean().iloc[-1]
    sma_s = closes.rolling(config.SMA_SLOW).mean().iloc[-1]
    mom = closes.iloc[-1] / closes.shift(config.LOOKBACK_MOMENTUM).iloc[-1] - 1
    hist = closes.notna().sum()
    tbl = pd.DataFrame({
        "close": last, "sma_fast": sma_f, "sma_slow": sma_s,
        "momentum": mom, "history": hist,
    })
    tbl["trend_ok"] = (tbl["close"] > tbl["sma_slow"]) & (tbl["sma_fast"] > tbl["sma_slow"])
    tbl["eligible"] = tbl["trend_ok"] & (tbl["history"] >= config.MIN_HISTORY_DAYS) & tbl["momentum"].notna()
    ranked = tbl[tbl["eligible"]].sort_values("momentum", ascending=False)
    tbl["rank"] = pd.Series(range(1, len(ranked) + 1), index=ranked.index)
    return tbl.sort_values("rank")


def propose(closes: pd.DataFrame, pf: Portfolio) -> list[dict]:
    """Return a list of order proposals: {symbol, side, qty|None, reason}.
    qty for buys is None here and sized at execution from the next open."""
    tbl = indicators(closes)
    orders: list[dict] = []

    # Exits first
    for sym, pos in pf.positions.items():
        if sym not in tbl.index or pd.isna(tbl.at[sym, "close"]):
            # No usable price: an auto-sell could not fill anyway. Flag it for the human review instead.
            log.warning("Held %s has no recent close; no automatic order. Review manually.", sym)
            continue
        row = tbl.loc[sym]
        if row["close"] < pos.stop:
            orders.append(dict(symbol=sym, side="SELL", qty=pos.qty,
                               reason=f"stop-loss: close {row['close']:.2f} < stop {pos.stop:.2f}"))
        elif row["close"] < row["sma_slow"]:
            orders.append(dict(symbol=sym, side="SELL", qty=pos.qty,
                               reason=f"trend break: close {row['close']:.2f} < SMA{config.SMA_SLOW} {row['sma_slow']:.2f}"))
        elif pd.isna(row["rank"]) or row["rank"] > config.RANK_EXIT:
            r = "n/a" if pd.isna(row["rank"]) else int(row["rank"])
            orders.append(dict(symbol=sym, side="SELL", qty=pos.qty,
                               reason=f"momentum rank {r} fell below top {config.RANK_EXIT}"))

    exiting = {o["symbol"] for o in orders}
    held_after = [s for s in pf.positions if s not in exiting]
    free_slots = config.MAX_POSITIONS - len(held_after)

    # Entries
    if free_slots > 0:
        candidates = tbl[tbl["eligible"] & (tbl["rank"] <= config.RANK_ENTRY)]
        for sym, row in candidates.iterrows():
            if free_slots == 0:
                break
            if sym in pf.positions or sym in exiting:
                continue
            orders.append(dict(symbol=sym, side="BUY", qty=None,
                               reason=f"momentum rank {int(row['rank'])}, {config.LOOKBACK_MOMENTUM}d return {row['momentum']*100:.1f}%"))
            free_slots -= 1
    return orders
