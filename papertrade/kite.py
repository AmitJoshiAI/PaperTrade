"""Zerodha Kite Connect, READ ONLY (KITE_SETUP.md phase 1: measure, do not replace).

This module reads market data from the user's own Kite Connect app. It exposes exactly five functions and
nothing else: available(), instruments(exchange), ohlc_day(symbols, day), chain_snapshot(sym, kind,
expiries, spot) and basket_margin(legs). There is no order placement, modification or cancellation here or
anywhere in the repository; tests/test_no_orders.py fails the suite if an order method name appears in
code under papertrade/ or tools/, and the client object below is wrapped so that only the read methods
listed in _ALLOWED can be reached at all.

Credentials: KITE_API_KEY and KITE_ACCESS_TOKEN from the environment (GitHub Actions secrets), else, when
run on the user's PC, from Windows Credential Manager via keyring (service "papertrade-kite", written by
tools/kite_login.py). A token lives one trading day (until about 06:00 IST the next morning); a locally
stored token is used only if it was made today (IST). Nothing here prints or logs a key or a token.

available() is the gate for every caller: it is False (and `reason` says why, e.g. "no token") unless a key
and a token are present AND profile() answered once in this process. Callers fall back to Yahoo / the NSE
scrape and put "Kite: <reason>" in the journal.

Rate limits (Kite Connect docs): historical data 3 requests/s, quote 1 request/s with at most 500
instruments per call, other endpoints 10 requests/s. _pace() keeps below them.

Instruments dumps are cached per IST day in data_cache/kite_instruments_<EXCHANGE>_<YYYY-MM-DD>.parquet.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import date as _date, datetime, timedelta, timezone

from . import config

log = logging.getLogger(__name__)

SERVICE = "papertrade-kite"          # keyring service name, shared with tools/kite_login.py
IST = timezone(timedelta(hours=5, minutes=30))
QUOTE_BATCH = 500
_MIN_GAP = {"historical": 1 / 3 + 0.02, "quote": 1.02, "other": 0.11}
# Underlying quote keys for the indices (Kite's tradingsymbols on NSE); stocks are "NSE:<SYMBOL>".
INDEX_QUOTE = {"NIFTY": "NSE:NIFTY 50", "BANKNIFTY": "NSE:NIFTY BANK"}
# The only client methods this module may reach (read-only market data, profile check, margin calculator).
_ALLOWED = frozenset({"profile", "instruments", "historical_data", "quote", "basket_order_margins"})

reason = "not checked yet"           # why available() is False; read as kite.reason
_state: dict = {"checked": False, "ok": False, "client": None}
_last_call: dict = {}
_inst_mem: dict = {}


class _ReadOnly:
    """Wraps a KiteConnect client and refuses every attribute outside _ALLOWED."""

    def __init__(self, client):
        object.__setattr__(self, "_c", client)

    def __getattr__(self, name):
        if name not in _ALLOWED:
            raise AttributeError(f"papertrade.kite is read-only: {name} is not available")
        return getattr(object.__getattribute__(self, "_c"), name)


def today_ist() -> str:
    return datetime.now(IST).date().isoformat()


def _keyring_get(name: str) -> str | None:
    """A value from Windows Credential Manager, or None (no keyring package, no backend, not stored)."""
    if os.environ.get("GITHUB_ACTIONS"):
        return None
    try:
        import keyring
        return keyring.get_password(SERVICE, name) or None
    except Exception:
        return None


def _credentials() -> tuple[str | None, str | None, str]:
    """(api_key, access_token, where). Environment first, then keyring (a local token only if made today)."""
    key = os.environ.get("KITE_API_KEY") or None
    token = os.environ.get("KITE_ACCESS_TOKEN") or None
    where = "environment"
    if not key:
        key = _keyring_get("api_key")
        where = "keyring" if key else where
    if not token:
        t = _keyring_get("access_token")
        if t and _keyring_get("access_token_date") == today_ist():
            token, where = t, "keyring"
    return key, token, where


def _pace(kind: str) -> None:
    gap = _MIN_GAP[kind]
    last = _last_call.get(kind)
    if last is not None:
        wait = gap - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
    _last_call[kind] = time.monotonic()


def _err(e: Exception) -> str:
    msg = str(e).strip().splitlines()[0] if str(e).strip() else ""
    return f"{type(e).__name__}: {msg}"[:200] if msg else type(e).__name__


def available() -> bool:
    """True if a key and a token are present and profile() works. Checked once per process (cached);
    on False, `reason` holds why ("no token", "no API key", "kiteconnect not installed", "<error>")."""
    global reason
    if _state["checked"]:
        return _state["ok"]
    _state["checked"] = True
    key, token, _where = _credentials()
    if not token:
        reason = "no token"
        return False
    if not key:
        reason = "no API key"
        return False
    try:
        from kiteconnect import KiteConnect           # imported here: it pulls in Twisted for the ticker
    except Exception:
        reason = "kiteconnect not installed"
        return False
    try:
        client = KiteConnect(api_key=key, access_token=token, timeout=20)
        _pace("other")
        client.profile()
    except Exception as e:
        reason = _err(e)
        log.warning("Kite not available: %s", reason)
        return False
    _state.update(ok=True, client=_ReadOnly(client))
    reason = ""
    return True


def _client():
    if not available():
        raise RuntimeError(f"Kite not available ({reason})")
    return _state["client"]


# ---------- instruments ----------
def instruments(exchange: str):
    """The instruments dump for one exchange ("NSE", "NFO") as a pandas DataFrame, cached per IST day in
    data_cache/. Columns as Kite sends them (instrument_token, tradingsymbol, name, expiry as ISO text or "",
    strike, lot_size, instrument_type, segment, exchange, ...)."""
    import pandas as pd
    exchange = exchange.upper()
    day = today_ist()
    if (exchange, day) in _inst_mem:
        return _inst_mem[(exchange, day)]
    path = config.CACHE_DIR / f"kite_instruments_{exchange}_{day}.parquet"
    if path.exists():
        try:
            df = pd.read_parquet(path)
            _inst_mem[(exchange, day)] = df
            return df
        except Exception as e:
            log.warning("Kite instruments cache %s unreadable (%s); downloading again", path.name, e)
    c = _client()
    _pace("other")
    rows = c.instruments(exchange)
    df = pd.DataFrame(rows)
    if "expiry" in df.columns:
        df["expiry"] = df["expiry"].map(lambda v: v.isoformat() if hasattr(v, "isoformat") else (str(v) if v else ""))
    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        df.to_parquet(path, index=False)
        for old in config.CACHE_DIR.glob(f"kite_instruments_{exchange}_*.parquet"):
            if old != path:
                old.unlink(missing_ok=True)
    except Exception as e:
        log.warning("Kite instruments cache not written (%s)", e)
    _inst_mem[(exchange, day)] = df
    return df


def _options(sym: str):
    """NFO option rows of one underlying (name == sym)."""
    df = instruments("NFO")
    return df[(df["name"] == sym) & (df["instrument_type"].isin(["CE", "PE"]))]


# ---------- cash market: official open / close ----------
def ohlc_day(symbols, day: str) -> dict:
    """{SYMBOL: {"open", "high", "low", "close"}} for NSE cash symbols on market day `day` (YYYY-MM-DD), from
    Kite's historical day candle. A symbol not found in the NSE dump, or with no candle that day, is left out."""
    df = instruments("NSE")
    eq = df[df["segment"] == "NSE"] if "segment" in df.columns else df
    tok = dict(zip(eq["tradingsymbol"], eq["instrument_token"]))
    c = _client()
    out = {}
    for s in symbols:
        t = tok.get(s)
        if t is None:
            log.warning("Kite: %s not in the NSE instruments dump", s)
            continue
        _pace("historical")
        try:
            candles = c.historical_data(int(t), f"{day} 00:00:00", f"{day} 23:59:59", "day")
        except Exception as e:
            log.warning("Kite historical %s %s: %s", s, day, _err(e))
            continue
        for k in candles or []:
            d = k.get("date")
            if d is not None and (d.date().isoformat() if hasattr(d, "date") else str(d)[:10]) == day:
                out[s] = {f: float(k[f]) for f in ("open", "high", "low", "close")}
    return out


# ---------- option chain snapshot ----------
def _quotes(keys: list[str]) -> dict:
    c = _client()
    out = {}
    for i in range(0, len(keys), QUOTE_BATCH):
        _pace("quote")
        out.update(c.quote(keys[i:i + QUOTE_BATCH]) or {})
    return out


def _num(v) -> float:
    try:
        x = float(v)
        return x if x == x else 0.0
    except (TypeError, ValueError):
        return 0.0


def _best(depth: dict | None, side: str) -> float:
    lvl = ((depth or {}).get(side) or [])
    return _num(lvl[0].get("price")) if lvl else 0.0


def chain_snapshot(sym: str, kind: str, expiries, spot: float) -> dict:
    """A snapshot of `sym`'s options for the given ISO expiries, in the exact state/chains/<SYM>.json format
    of chains.py (see its docstring), with source "zerodha". Strikes within config.CHAIN_STRIKE_BAND of
    `spot` (the Yahoo close: it picks the strikes; the snapshot's underlying_value is Kite's own last price
    when Kite has it). Per contract: bid / ask = best level of the depth, ltp, oi, and iv = Black-Scholes IV
    (percent, as NSE reports it) solved from the mid when both sides are > 0, else 0. Raises on failure."""
    from . import chains
    from .candidates import _implied_vol
    from .derivbook import years_to
    opts = _options(sym)
    if opts.empty:
        raise RuntimeError(f"{sym}: no options in the Kite NFO dump")
    listed = sorted({e for e in opts["expiry"] if e})
    want = [e for e in expiries if e in listed]
    if not want:
        raise RuntimeError(f"{sym}: none of {', '.join(expiries)} is listed on Kite")
    band = config.CHAIN_STRIKE_BAND
    sel = opts[opts["expiry"].isin(want)]
    if spot and spot > 0:
        sel = sel[(sel["strike"] - spot).abs() <= band * spot]
    if sel.empty:
        raise RuntimeError(f"{sym}: no strikes within {band:.0%} of {spot:,.2f}")
    under_key = INDEX_QUOTE.get(sym, f"NSE:{sym}")
    keys = [f"NFO:{t}" for t in sel["tradingsymbol"]]
    q = _quotes([under_key] + keys)
    S = _num((q.get(under_key) or {}).get("last_price")) or float(spot or 0)
    if S <= 0:
        raise RuntimeError(f"{sym}: no underlying price")
    chains_out: dict = {}
    stamps = []
    for r in sel.itertuples(index=False):
        qq = q.get(f"NFO:{r.tradingsymbol}")
        if not qq:
            continue
        ts = qq.get("timestamp") or qq.get("last_trade_time")
        if ts is not None and hasattr(ts, "strftime"):
            stamps.append(ts)
        bid, ask = _best(qq.get("depth"), "buy"), _best(qq.get("depth"), "sell")
        iv = 0.0
        if bid > 0 and ask > 0 and ask >= bid:
            t = years_to(r.expiry, (ts.date().isoformat() if hasattr(ts, "date") else today_ist()))
            v = _implied_vol((bid + ask) / 2, S, float(r.strike), t, config.CARRY_RATE,
                             "CALL" if r.instrument_type == "CE" else "PUT")
            iv = round(v * 100, 2) if v else 0.0
        side = {"bid": bid, "ask": ask, "ltp": _num(qq.get("last_price")), "iv": iv, "oi": _num(qq.get("oi"))}
        row = chains_out.setdefault(r.expiry, {}).setdefault(chains._strike_key(r.strike), {"CE": None, "PE": None})
        row[r.instrument_type] = side
    if not chains_out:
        raise RuntimeError(f"{sym}: Kite returned no option quotes")
    if not stamps:
        raise RuntimeError(f"{sym}: Kite quotes carry no timestamp")
    ts = max(stamps)
    return {"symbol": sym, "kind": kind, "timestamp": ts.strftime("%d-%b-%Y %H:%M:%S"), "date": ts.date().isoformat(),
            "fetched_at": datetime.now(IST).strftime("%Y-%m-%d %H:%M IST"), "source": "zerodha",
            "underlying_value": S, "expiries": listed, "chains": chains_out}


# ---------- margin calculator ----------
def basket_margin(legs) -> float | None:
    """The broker's required margin (Rs) for a basket of option legs, from Kite's basket margin calculator
    (a calculation only: nothing is sent to the exchange), not counting the user's real positions. legs:
    [{"underlying", "expiry" (ISO), "strike", "right" ("CE"/"PE"), "side" ("BUY"/"SELL"), "units"}].
    None if a leg's contract is not in the NFO dump or the call fails."""
    try:
        params = []
        for l in legs:
            o = _options(str(l["underlying"]).upper())
            m = o[(o["expiry"] == l["expiry"]) & (o["instrument_type"] == l["right"]) & ((o["strike"] - float(l["strike"])).abs() < 1e-6)]
            if m.empty:
                return None
            params.append({"exchange": "NFO", "tradingsymbol": m["tradingsymbol"].iloc[0],
                           "transaction_type": "BUY" if str(l["side"]).upper() == "BUY" else "SELL",
                           "variety": "regular", "product": "NRML", "order_type": "MARKET",
                           "quantity": int(l["units"]), "price": 0, "trigger_price": 0})
        c = _client()
        _pace("other")
        r = c.basket_order_margins(params, consider_positions=False, mode="compact") or {}
        for part in ("final", "initial"):
            tot = (r.get(part) or {}).get("total")
            if tot is not None:
                return round(float(tot), 2)
        return None
    except Exception as e:
        log.warning("Kite basket margin: %s", _err(e))
        return None
