"""Fill check, Yahoo vs Zerodha (KITE_SETUP.md phase 1, item 2). Measurement only: Yahoo stays the fill
and mark source of the cash sleeves; nothing here changes a fill.

After the daily run's fills, for every NSE cash symbol the momentum and thesis sleeves hold, bought or sold
on the market day, Kite's official day candle (kite.ohlc_day) is set beside Yahoo's open and close:

  journal  "### Fill check (Yahoo vs Zerodha)" table under the momentum sections
  file     state/fill_check.csv, one row per (date, sleeve, symbol); a same-date re-run replaces that
           date's rows. Columns: CSV_COLUMNS.

diff = Yahoo - Kite (Rs), bps = diff / Kite x 10,000. US legs (US:XXX) and explicit Yahoo tickers (.BO and
the like) are skipped: Kite has no US data and the momentum/thesis NSE names are what the fills use.
With no token (or any Kite failure) the journal gets one "Kite: <why>" line instead and the CSV is untouched.
"""
from __future__ import annotations

import logging

import pandas as pd

from . import config

log = logging.getLogger(__name__)

FILE = config.STATE_DIR / "fill_check.csv"
HEADING = "### Fill check (Yahoo vs Zerodha)"
CSV_COLUMNS = ["date", "sleeve", "symbol", "yahoo_open", "kite_open", "open_diff_rs", "open_diff_bps",
               "yahoo_close", "kite_close", "close_diff_rs", "close_diff_bps"]


def _nse(sym: str) -> bool:
    return bool(sym) and not sym.startswith("US:") and "." not in sym


def symbols(market_day: str, pf, fills, thesis_out) -> dict:
    """{symbol: [sleeves]} held, bought or sold on the market day by the momentum and thesis sleeves."""
    out: dict = {}

    def add(sym, sleeve):
        if _nse(str(sym)):
            out.setdefault(str(sym), [])
            if sleeve not in out[str(sym)]:
                out[str(sym)].append(sleeve)

    for s in pf.positions:
        add(s, "momentum")
    for f in fills or []:
        add(f.get("symbol"), "momentum")
    if thesis_out:
        for s in thesis_out["pf"].positions:
            add(s, "thesis")
        try:
            from .thesis import ThesisPortfolio
            t = ThesisPortfolio.trades()
            for s in t.loc[t["date"].astype(str) == market_day, "symbol"]:
                add(s, "thesis")
        except Exception:
            pass
        for f in thesis_out.get("fills") or []:
            add(f.get("symbol"), "thesis")
    return out


def _yahoo(frames, sym: str, day: str) -> tuple[float | None, float | None]:
    ts = pd.Timestamp(day)
    for px in frames:
        if px is None or not len(px):
            continue
        r = px[(px["symbol"] == sym) & (px["date"] == ts)]
        if len(r):
            o, c = r["open"].iloc[0], r["close"].iloc[0]
            return (None if pd.isna(o) else float(o)), (None if pd.isna(c) else float(c))
    return None, None


def _bps(a, b):
    return None if a is None or b is None or not b else round((a - b) / b * 10_000, 1)


def _f(v, dp=2):
    return "n/a" if v is None else f"{v:,.{dp}f}"


def run(market_day: str, pf, fills, thesis_out, prices: pd.DataFrame, do_update: bool) -> list[str]:
    """Journal lines (the table, or one "Kite: ..." line). Never raises."""
    try:
        from . import kite
        if not do_update:
            return ["Kite: not used (offline run); no fill check (Yahoo vs Zerodha)"]
        if not kite.available():
            return [f"Kite: {kite.reason}; no fill check (Yahoo vs Zerodha)"]
        syms = symbols(market_day, pf, fills, thesis_out)
        if not syms:
            return [HEADING, "", "No momentum or thesis NSE symbol held or traded today."]
        k = kite.ohlc_day(sorted(syms), market_day)
        frames = [prices, (thesis_out or {}).get("prices")]
        rows = []
        for s in sorted(syms):
            yo, yc = _yahoo(frames, s, market_day)
            yo, yc = (None if yo is None else round(yo, 2)), (None if yc is None else round(yc, 2))
            ko, kc = (k.get(s) or {}).get("open"), (k.get(s) or {}).get("close")
            rows.append(dict(date=market_day, sleeve="+".join(syms[s]), symbol=s, yahoo_open=yo, kite_open=ko,
                             open_diff_rs=None if yo is None or ko is None else round(yo - ko, 2),
                             open_diff_bps=_bps(yo, ko), yahoo_close=yc, kite_close=kc,
                             close_diff_rs=None if yc is None or kc is None else round(yc - kc, 2),
                             close_diff_bps=_bps(yc, kc)))
        _save(market_day, rows)
        lines = [HEADING, "",
                 f"Yahoo's day bar (the paper fill and mark source) against Zerodha's official day candle for {market_day}. "
                 "diff = Yahoo - Zerodha; bps of the Zerodha price. Measurement only: fills are unchanged. "
                 "Every row is also in state/fill_check.csv.", "",
                 "| Symbol | Sleeve | Yahoo open | Kite open | Diff Rs | Diff bps | Yahoo close | Kite close | Diff bps |",
                 "|---|---|---|---|---|---|---|---|---|"]
        for r in rows:
            lines.append(f"| {r['symbol']} | {r['sleeve']} | {_f(r['yahoo_open'])} | {_f(r['kite_open'])} | "
                         f"{_f(r['open_diff_rs'])} | {_f(r['open_diff_bps'], 1)} | {_f(r['yahoo_close'])} | "
                         f"{_f(r['kite_close'])} | {_f(r['close_diff_bps'], 1)} |")
        miss = [r["symbol"] for r in rows if r["kite_open"] is None]
        if miss:
            lines += ["", f"Kite: no day candle for {', '.join(miss)} (not in Kite's NSE list or no data)."]
        return lines
    except Exception as e:
        log.exception("Fill check failed")
        return [f"Kite: fill check failed ({type(e).__name__}: {e})"]


def _save(market_day: str, rows: list[dict]) -> None:
    old = pd.read_csv(FILE, dtype={"date": str}) if FILE.exists() else pd.DataFrame(columns=CSV_COLUMNS)
    old = old[old["date"].astype(str) != market_day]
    new = pd.DataFrame(rows, columns=CSV_COLUMNS)
    out = pd.concat([d for d in (old, new) if len(d)], ignore_index=True) if len(old) else new
    FILE.parent.mkdir(parents=True, exist_ok=True)
    out[CSV_COLUMNS].to_csv(FILE, index=False)
