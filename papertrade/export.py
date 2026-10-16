"""Dashboard export: write docs/data.json for the static mobile dashboard (docs/index.html).

Read-only with respect to trading state: it reads state/, decisions/, journal/ and the price cache,
and writes only docs/data.json. It never downloads prices; if the cache is missing, last closes are
reported as null and positions are valued at average cost (the same fallback Portfolio uses).

All floats are rounded to 2 dp. Two fractions from review.performance() would round to nothing at
2 dp, so they are exported as percentages instead: max_drawdown -> max_drawdown_pct,
win_rate -> win_rate_pct.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from . import config, data
from .portfolio import Portfolio
from .universe import NIFTY50

log = logging.getLogger(__name__)

DOCS_DIR = config.ROOT / "docs"
DASHBOARD_DATA_FILE = DOCS_DIR / "data.json"


def _clean(x):
    """Recursively convert to JSON-safe Python types: numpy -> builtin, NaN/inf -> None, floats -> 2 dp."""
    if isinstance(x, dict):
        return {str(k): _clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_clean(v) for v in x]
    if isinstance(x, (bool, np.bool_)):
        return bool(x)
    if isinstance(x, (int, np.integer)):
        return int(x)
    if isinstance(x, (float, np.floating)):
        x = float(x)
        return None if math.isnan(x) or math.isinf(x) else round(x, 2)
    if isinstance(x, pd.Timestamp):
        return x.strftime("%Y-%m-%d")
    if x is None or x is pd.NA or x is pd.NaT:
        return None
    return x


def _records(df: pd.DataFrame) -> list[dict]:
    if df is None or df.empty:
        return []
    return [_clean(r) for r in df.astype(object).where(df.notna(), None).to_dict("records")]


def _load_prices() -> pd.DataFrame | None:
    if not config.PRICES_FILE.exists():
        log.warning("No price cache at %s; exporting without last closes.", config.PRICES_FILE)
        return None
    return pd.read_parquet(config.PRICES_FILE)


def _dropped(prices: pd.DataFrame | None, dropped) -> list[str] | None:
    """Symbols Yahoo could not serve. Prefer the list from the download; else derive it from the cache."""
    if dropped is not None:
        return sorted(dropped)
    if prices is None:
        return None
    if "dropped" in prices.attrs:
        return sorted(prices.attrs["dropped"])
    present = set(prices["symbol"].unique())
    return sorted(s + ".NS" for s in NIFTY50 if s not in present)


def _latest_journal() -> dict | None:
    files = sorted(config.JOURNAL_DIR.glob("*.md")) if config.JOURNAL_DIR.exists() else []
    if not files:
        return None
    f = files[-1]
    return dict(date=f.stem, file=f"journal/{f.name}", markdown=f.read_text(encoding="utf-8"))


def _pending() -> dict | None:
    if not config.PENDING_ORDERS_FILE.exists():
        return None
    p = json.loads(config.PENDING_ORDERS_FILE.read_text())
    return dict(
        decision_date=p.get("decision_date"),
        generated_at=p.get("generated_at"),
        note=p.get("note", ""),
        locked=bool(p.get("locked", False)),
        orders=p.get("orders", []),
    )


def build_dashboard_data(pf: Portfolio | None = None, prices: pd.DataFrame | None = None, dropped=None) -> dict:
    from . import review  # local import, same pattern as engine.run_day

    pf = pf or Portfolio.load()
    if prices is None:
        prices = _load_prices()

    if prices is not None and not prices.empty:
        closes = data.pivot(prices, "close")
        last = closes.ffill().iloc[-1]          # same valuation rule as engine.mark_to_market
        price_date = closes.index[-1].strftime("%Y-%m-%d")
    else:
        closes = pd.DataFrame([{}])             # one empty row: performance() then values at avg cost
        last = pd.Series(dtype=float)
        price_date = None

    perf = review.performance(pf, closes)
    perf["max_drawdown_pct"] = perf.pop("max_drawdown") * 100
    wr = perf.pop("win_rate")
    perf["win_rate_pct"] = None if wr is None else wr * 100

    positions = []
    for sym, p in pf.positions.items():
        c = last.get(sym)
        c = None if c is None or pd.isna(c) else float(c)
        positions.append(dict(
            symbol=sym, qty=p.qty, avg_cost=p.avg_cost, stop=p.stop, entry_date=p.entry_date,
            highest_close=p.highest_close, last_close=c,
            market_value=None if c is None else c * p.qty,
            unrealised_pnl=None if c is None else (c - p.avg_cost) * p.qty,
            unrealised_pct=None if c is None else (c / p.avg_cost - 1) * 100,
        ))

    curve = pf.equity_curve()
    as_of = str(curve["date"].iloc[-1]) if len(curve) else price_date

    trades = Portfolio.trades()
    if len(trades):
        trades["realised_pnl"] = pd.to_numeric(trades["realised_pnl"], errors="coerce")

    out = dict(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        as_of=as_of,
        price_date=price_date,
        config=dict(
            starting_capital=config.STARTING_CAPITAL,
            target_net_per_day=config.TARGET_NET_PER_DAY,
            target_gross_per_day=config.TARGET_GROSS_PER_DAY,
            assumed_tax_rate_pct=config.ASSUMED_TAX_RATE * 100,
            max_positions=config.MAX_POSITIONS,
        ),
        performance=perf,
        equity_curve=_records(curve),
        portfolio=dict(
            cash=pf.cash, start_date=pf.start_date, starting_capital=pf.starting_capital,
            costs_paid=pf.costs_paid, realised_pnl=pf.realised_pnl, positions=positions,
        ),
        trades=_records(trades),
        pending=_pending(),
        journal=_latest_journal(),
        dropped_symbols=_dropped(prices, dropped),
    )
    # Thesis sleeve (separate book). A failure here leaves the momentum dashboard intact.
    try:
        from .thesis import export_data
        out["thesis"] = export_data(prices, curve)
    except Exception:
        log.exception("Thesis sleeve export failed; dashboard written without it.")
        out["thesis"] = None
    return _clean(out)


def write_dashboard_data(pf: Portfolio | None = None, prices: pd.DataFrame | None = None, dropped=None) -> Path:
    payload = build_dashboard_data(pf, prices, dropped)
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    DASHBOARD_DATA_FILE.write_text(json.dumps(payload, indent=2, allow_nan=False, ensure_ascii=False) + "\n",
                                   encoding="utf-8")
    return DASHBOARD_DATA_FILE
