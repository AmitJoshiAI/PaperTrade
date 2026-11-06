"""India F&O sleeve: index and stock futures and options, paper only, Rs 5,00,000 (config.FNO_CAPITAL).

A separate book (state/fno_*, decisions/fno_orders.json) run by engine.run_day after the thesis sleeve.
Accounting, risk rules and the circuit breaker are in derivbook.py; every limit is in RISK_LIMITS.md.

Instruments: NIFTY (^NSEI), BANKNIFTY (^NSEBANK) and the stocks in config.FNO_STOCKS (an editable list,
not the full NSE F&O list; lot sizes checked against NSE's fo_mktlots.csv on 4 Nov 2026).
Expiry: the last Tuesday of the month (config INDEX_EXPIRY_WEEKDAY / STOCK_EXPIRY_WEEKDAY), snapped to NSE's
listed expiry date when a chain cache exists (a holiday moves it to the trading day before: 24 Nov 2026 ->
23 Nov). An exact YYYY-MM-DD (for example a NIFTY weekly) is taken as given, and must be listed when the
list is known. With no cache, a holiday expiry settles on the last trading day on or before it.

Pricing. Real NSE option chains are fetched by the daily run (chains.py, cached in state/chains/):
  marks    option at the chain's bid/ask MID at the day's close when the strike has a two-sided quote
           ("real NSE chain (mid)"); otherwise Black-Scholes on the chain's IV for that strike or the ATM IV;
           otherwise (no chain today) the model below.
  fills    always the model, at the fill day's open spot, +/-1% slippage (FNO_OPT_SLIPPAGE_PCT): the chain
           is a close snapshot, not the open. Its vol is the IV of the latest chain BEFORE the fill day (no
           look-ahead), else the model vol.
  model    future = spot x (1 + CARRY_RATE x calendar days to expiry / 365)
           option = Black-Scholes (European) on the spot, r = CARRY_RATE, vol = India VIX / 100 for index
           options, 1.5 x the stock's 30-day realised vol for stock options ("model (BS on VIX / realised
           vol)"): no skew, no spread. Futures are always the carry model, with 0.05% slippage (SLIPPAGE_PCT).
Fills at the underlying's first NSE open after the order's decision_date (spot = that open; VIX = that
day's VIX open). Marks at the day's close. Futures margin = FUT_MARGIN_PCT (index 12%, stock 20%) of the
fill notional, blocked until exit.

Expiry, at the close of the expiry day: futures settle at the spot close; options at intrinsic value
(a long ITM option pays STT on the intrinsic value). A future with roll (the default) is reopened in the
next month at the next open, through the same risk checks (journalled; a roll is skipped while the
circuit breaker is on). An order with "roll": false is not rolled.

Orders, decisions/fno_orders.json, written by the reviewer (ROUTINE_PROMPT.md):

  {"orders": [
    {"action": "OPEN", "underlying": "NIFTY", "instrument": "CE", "side": "BUY", "lots": 1,
     "strike": "ATM+2", "expiry": "current", "decision_date": "2026-10-19",
     "reason": "why", "risk_check": "premium Rs 9,000 < 10% of equity; 1 of 5 positions; breaker off"},
    {"action": "CLOSE", "position": "NIFTY:2026-10-27:CE23100:L", "decision_date": "2026-10-21",
     "reason": "why"}
  ]}

  action      OPEN (default) or CLOSE
  instrument  FUT, CE / CALL, PE / PUT
  side        BUY (long) or SELL (short). A short option must be covered (derivbook.py rule 1).
  lots        whole lots; units = lots x lot size
  strike      options: a number (rounded to the step), ATM, or ATM+N / ATM-N (N strike steps)
  expiry      current (default: first expiry after the fill day), next, YYYY-MM or YYYY-MM-DD
  roll        futures only, default true
  position    CLOSE: the position id shown in the journal (or underlying + instrument if unique)
  lots (CLOSE) optional: part of the position; default all
"""
from __future__ import annotations

import logging
import re
from datetime import date as _date

import pandas as pd

from . import config, data, chains
from .costs import charges, exercise_charges, Charges
from .derivbook import (DerivBook, DPos, Reject, NotYet, bs_price, intrinsic, realised_vol, years_to,
                        monthly_expiry, strike_spec, position_id, run_orders, load_queue, save_queue,
                        record_executed, OPTION_KINDS)

log = logging.getLogger(__name__)

MODEL_LABEL = "model (Black-Scholes / carry) unless a mark says real NSE chain (mid)"


class FnoBook(DerivBook):
    _PORTFOLIO_FILE = "FNO_PORTFOLIO_FILE"
    _TRADES_FILE = "FNO_TRADES_FILE"
    _EQUITY_FILE = "FNO_EQUITY_FILE"
    CURRENCY = "INR"

    @classmethod
    def initial_capital(cls) -> float:
        return float(config.FNO_CAPITAL)

    @classmethod
    def instrument(cls, p: DPos) -> str:
        if p.kind == "FUT":
            return f"{p.underlying} FUT {p.expiry}"
        return f"{p.underlying} {p.expiry} {p.strike:g} {'CE' if p.kind == 'CALL' else 'PE'}"


def spec(underlying: str) -> dict:
    u = str(underlying or "").strip().upper()
    if u in config.FNO_INDEX:
        s = config.FNO_INDEX[u]
        return dict(name=u, kind="index", yahoo=s["yahoo"], lot=int(s["lot"]), step=float(s["step"]),
                    weekday=config.INDEX_EXPIRY_WEEKDAY, margin_pct=config.FUT_MARGIN_PCT["index"])
    if u in config.FNO_STOCKS:
        s = config.FNO_STOCKS[u]
        return dict(name=u, kind="stock", yahoo=f"{u}.NS", lot=int(s["lot"]), step=float(s["step"]),
                    weekday=config.STOCK_EXPIRY_WEEKDAY, margin_pct=config.FUT_MARGIN_PCT["stock"])
    raise Reject(f"{u or '(blank)'} is not on the sleeve's F&O list (config.FNO_INDEX / FNO_STOCKS)")


def _instrument(o) -> str:
    i = str(o.get("instrument", "")).strip().upper()
    i = {"CE": "CALL", "PE": "PUT", "C": "CALL", "P": "PUT", "FUTURE": "FUT", "FUTURES": "FUT"}.get(i, i)
    if i not in ("FUT", "CALL", "PUT"):
        raise Reject(f"instrument {o.get('instrument')!r}: use FUT, CE or PE")
    return i


# ---------- prices ----------
def needed_tickers(book: FnoBook, orders: list) -> list[str]:
    t = {config.FNO_VIX_SYMBOL} | {s["yahoo"] for s in config.FNO_INDEX.values()}
    for p in book.positions.values():
        t.add(spec(p.underlying)["yahoo"])
    for o in orders:
        try:
            t.add(spec(o.get("underlying") or str(o.get("position", "")).split(":")[0])["yahoo"])
        except Reject:
            pass
    from .candidates import fno_candidate_symbols
    for u in fno_candidate_symbols()[0]:
        t.add(spec(u)["yahoo"])
    return sorted(t)


def load_prices(main_prices: pd.DataFrame, tickers: list[str], do_update: bool) -> pd.DataFrame:
    """Daily bars (1 year, for the realised vol) capped at the momentum sleeve's market date, so all
    sleeves process the same NSE day. Rows without a Close are dropped, as in data.py."""
    market_day = main_prices["date"].max()
    px = None
    if do_update:
        px = data.download_tickers(tickers, period="1y")
        if not px.empty:
            config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
            px.to_parquet(config.FNO_PRICES_FILE, index=False)
    elif config.FNO_PRICES_FILE.exists():
        px = pd.read_parquet(config.FNO_PRICES_FILE)
    if px is None or px.empty:
        out = pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "symbol"])
    else:
        out = px[px["date"] <= market_day]
    present = set(out["symbol"].unique())
    out.attrs["dropped"] = [t for t in tickers if t not in present]
    return out


class FnoMarket:
    """Prices and contract maths for one run. Spot and VIX come from the daily bars."""

    def __init__(self, prices: pd.DataFrame, market_day: str, book: FnoBook, prior: dict | None = None,
                 today: dict | None = None, listed: dict | None = None):
        self.market_day, self.book = market_day, book
        self.opens = data.pivot(prices, "open") if len(prices) else pd.DataFrame()
        self.closes = data.pivot(prices, "close") if len(prices) else pd.DataFrame()
        self.prior = prior or {}       # {SYM: chain snapshot as cached BEFORE today's fetch}: fill IVs
        self.today = today or {}       # {SYM: chain snapshot of the market date}: marks and candidates
        self._listed = listed or {}    # {SYM: NSE's listed expiry dates}, from the newest snapshot of any date

    def listed(self, sym) -> list:
        return self._listed.get(sym) or (self.today.get(sym) or self.prior.get(sym) or {}).get("expiries") or []

    def resolve_expiry(self, o_expiry, fill_day, sp) -> str:
        listed = self.listed(sp["name"])
        s = str(o_expiry or "current").strip().lower()
        if s in ("current", "near"):
            return chains.monthly_after(fill_day, sp["weekday"], listed)
        if s in ("next", "far"):
            return chains.monthly_after(fill_day, sp["weekday"], listed, skip=1)
        if re.fullmatch(r"\d{4}-\d{2}", s):
            e = chains.snap_to_listed(monthly_expiry(s, "1970-01-01", sp["weekday"]), listed)
            if e <= fill_day:
                raise Reject(f"expiry {e} is not after the fill day {fill_day}")
            return e
        e = monthly_expiry(s, fill_day, sp["weekday"])       # exact date (validates it is after the fill)
        if listed and e not in listed:
            raise Reject(f"expiry {e} is not listed on NSE for {sp['name']}; listed: {', '.join(listed[:6])}")
        return e

    # --- raw series ---
    def _col(self, frame, ticker) -> pd.Series:
        return frame[ticker].dropna() if ticker in frame.columns else pd.Series(dtype=float, index=pd.DatetimeIndex([]))

    def spot_open(self, ticker, day) -> float:
        c = self._col(self.opens, ticker)
        if pd.Timestamp(day) not in c.index:
            raise NotYet(f"no {ticker} open on {day}")
        return float(c.loc[pd.Timestamp(day)])

    def spot_close(self, ticker, day) -> tuple[float, str]:
        """Close on the last bar on or before `day` (the expiry-day rule and the stale-bar fallback)."""
        c = self._col(self.closes, ticker)
        c = c[c.index <= pd.Timestamp(day)]
        if c.empty:
            raise NotYet(f"no {ticker} close on or before {day}")
        return float(c.iloc[-1]), c.index[-1].strftime("%Y-%m-%d")

    def sigma(self, sp: dict, day: str, at_open: bool, expiry: str | None = None, strike: float | None = None,
              kind: str | None = None) -> tuple[float, str]:
        if expiry and strike is not None and kind:
            if at_open:
                got = chains.fill_iv(sp["name"], self.prior.get(sp["name"]), day, expiry, strike, kind)
                if got:
                    return got
            else:
                snap = chains.usable(self.today.get(sp["name"]), day)
                iv = chains.strike_iv(snap, expiry, strike, kind) if snap else None
                if iv:
                    return iv, f"{chains.source_name(snap)} chain IV {iv*100:.2f}% at this strike (close of {day})"
                iv = chains.atm_iv(snap, expiry if snap and expiry in snap.get("chains", {}) else None) if snap else None
                if iv:
                    return iv, f"{chains.source_name(snap)} chain ATM IV {iv*100:.2f}% (close of {day})"
        return self.model_sigma(sp, day, at_open)

    def model_sigma(self, sp: dict, day: str, at_open: bool) -> tuple[float, str]:
        if sp["kind"] == "index":
            vix = config.FNO_VIX_SYMBOL
            if at_open:
                o = self._col(self.opens, vix)
                if pd.Timestamp(day) in o.index:
                    v = float(o.loc[pd.Timestamp(day)])
                    return v / 100, f"India VIX open {v:.2f}"
                c = self._col(self.closes, vix)
                c = c[c.index < pd.Timestamp(day)]
            else:
                c = self._col(self.closes, vix)
                c = c[c.index <= pd.Timestamp(day)]
            if c.empty:
                raise NotYet("no India VIX value")
            v = float(c.iloc[-1])
            return v / 100, f"India VIX close {v:.2f} ({c.index[-1]:%Y-%m-%d})"
        c = self._col(self.closes, sp["yahoo"])
        c = c[c.index < pd.Timestamp(day)] if at_open else c[c.index <= pd.Timestamp(day)]
        rv = realised_vol(c)
        if rv is None:
            raise NotYet(f"fewer than {config.REALISED_VOL_DAYS + 1} closes for {sp['yahoo']}")
        return config.STOCK_OPT_VOL_MULT * rv, f"{config.STOCK_OPT_VOL_MULT:g} x {config.REALISED_VOL_DAYS}d realised vol {rv*100:.2f}%"

    # --- the interface run_orders uses ---
    def fill_day(self, o) -> str | None:
        und = o.get("underlying")
        if str(o.get("action", "OPEN")).upper() == "CLOSE" and o.get("position") in self.book.positions:
            und = self.book.positions[o["position"]].underlying
        elif not und and o.get("position"):
            und = str(o["position"]).split(":")[0]
        c = self._col(self.opens, spec(und)["yahoo"])
        later = c.index[c.index > pd.Timestamp(o["decision_date"])]
        return later[0].strftime("%Y-%m-%d") if len(later) else None

    def pos_id(self, p: DPos) -> str:
        return position_id(p.underlying, p.kind, p.side, p.expiry, p.strike, ("CE", "PE"))

    def future_price(self, spot: float, expiry: str, day: str) -> tuple[float, str]:
        days = (_date.fromisoformat(expiry) - _date.fromisoformat(day)).days
        f = spot * (1 + config.CARRY_RATE * max(days, 0) / 365)
        return f, f"carry: spot {spot:,.2f} x (1 + {config.CARRY_RATE*100:g}% x {max(days, 0)}/365)"

    def option_price(self, sp, kind, strike, expiry, day, at_open) -> tuple[float, str, float, float]:
        S = self.spot_open(sp["yahoo"], day) if at_open else self.spot_close(sp["yahoo"], day)[0]
        sig, src = self.sigma(sp, day, at_open, expiry, strike, kind)
        t = years_to(expiry, day)
        px = bs_price(S, strike, t, config.CARRY_RATE, sig, kind)
        days = round(t * 365)
        return px, f"model: Black-Scholes, spot {S:,.2f}, IV {sig*100:.2f}% ({src}), T {days}/365", sig, S

    def open_contract(self, o, fill_day, side):
        sp = spec(o.get("underlying"))
        kind = _instrument(o)
        lots = int(o.get("lots") or 0)
        try:
            expiry = self.resolve_expiry(o.get("expiry"), fill_day, sp)
        except ValueError as e:
            raise Reject(str(e))
        S = self.spot_open(sp["yahoo"], fill_day)
        buy = side == "LONG"
        if kind == "FUT":
            f, how = self.future_price(S, expiry, fill_day)
            price = round(f * (1 + config.SLIPPAGE_PCT if buy else 1 - config.SLIPPAGE_PCT), 2)
            units = lots * sp["lot"]
            pos = DPos(id="", underlying=sp["name"], kind="FUT", side=side, lots=lots, lot_size=sp["lot"],
                       entry_price=price, entry_date=fill_day, expiry=expiry,
                       margin=round(sp["margin_pct"] * price * units, 2),
                       roll=bool(o.get("roll", True)) and not o.get("no_roll"),
                       pricing=how + f", {config.SLIPPAGE_PCT*100:g}% slippage", spot=S)
            chg = charges("BUY" if buy else "SELL", units, price, "FUT")
            return pos, price, chg, ""
        if o.get("strike") in (None, ""):
            raise Reject("an option order needs a strike (a number, ATM or ATM+N)")
        try:
            K = strike_spec(o["strike"], S, sp["step"])
        except ValueError as e:
            raise Reject(str(e))
        model, how, sig, _ = self.option_price(sp, kind, K, expiry, fill_day, at_open=True)
        price = round(model * (1 + config.FNO_OPT_SLIPPAGE_PCT if buy else 1 - config.FNO_OPT_SLIPPAGE_PCT), 2)
        if price < 0.05:
            raise Reject(f"model premium {model:.4f} is below one tick (0.05)")
        pos = DPos(id="", underlying=sp["name"], kind=kind, side=side, lots=lots, lot_size=sp["lot"],
                   entry_price=price, entry_date=fill_day, strike=K, expiry=expiry, iv=sig, spot=S,
                   pricing=how + f", model {model:,.2f} {'+' if buy else '-'}{config.FNO_OPT_SLIPPAGE_PCT*100:g}% slippage")
        chg = charges("BUY" if buy else "SELL", lots * sp["lot"], price, "OPT")
        return pos, price, chg, MODEL_LABEL

    def close_price(self, p: DPos, o, fill_day):
        sp = spec(p.underlying)
        selling = p.side == "LONG"
        if p.kind == "FUT":
            S = self.spot_open(sp["yahoo"], fill_day)
            f, how = self.future_price(S, p.expiry, fill_day)
            price = round(f * (1 - config.SLIPPAGE_PCT if selling else 1 + config.SLIPPAGE_PCT), 2)
            return price, charges("SELL" if selling else "BUY", p.units, price, "FUT"), how, ""
        model, how, _, _ = self.option_price(sp, p.kind, p.strike, p.expiry, fill_day, at_open=True)
        price = round(model * (1 - config.FNO_OPT_SLIPPAGE_PCT if selling else 1 + config.FNO_OPT_SLIPPAGE_PCT), 2)
        price = max(price, 0.05)
        return price, charges("SELL" if selling else "BUY", p.units, price, "OPT"), how, MODEL_LABEL

    def mark(self, p: DPos, day: str) -> None:
        sp = spec(p.underlying)
        S, sday = self.spot_close(sp["yahoo"], day)
        if p.kind == "FUT":
            f, how = self.future_price(S, p.expiry, day)
            p.mark, p.pricing = round(f, 2), how + ("" if sday == day else f" (spot close of {sday})")
        else:
            snap = chains.usable(self.today.get(p.underlying), day)
            q = chains.quote(snap, p.expiry, p.strike, p.kind) if snap else None
            m = chains.mid(q)
            if m is not None:
                p.mark, p.pricing = m, (f"{chains.chain_label(snap)}: bid {q['bid']:,.2f} / ask {q['ask']:,.2f} "
                                        f"({chains.source_name(snap)} {snap['timestamp']})")
                p.iv = round(q["iv"] / 100, 4) if q.get("iv", 0) > 0 else p.iv
            else:
                px, how, sig, _ = self.option_price(sp, p.kind, p.strike, p.expiry, day, at_open=False)
                p.mark, p.iv, p.pricing = round(px, 2), sig, how + ("" if sday == day else f" (spot close of {sday})")
        p.spot, p.mark_date = round(S, 4), day


def expire_positions(book: FnoBook, market: FnoMarket, market_day: str) -> tuple[list, list, list]:
    """Settle every position whose expiry is on or before the market day, at the underlying's close on the
    last trading day on or before the expiry. Returns (fills, notes, roll orders)."""
    fills, notes, rolls = [], [], []
    due = [p for p in book.positions.values() if p.expiry and p.expiry <= market_day]
    due.sort(key=lambda p: (p.kind == "FUT", p.side == "LONG"))     # short options settle first
    for p in due:
        sp = spec(p.underlying)
        try:
            S, sday = market.spot_close(sp["yahoo"], p.expiry)
        except NotYet as e:
            notes.append(f"EXPIRY of {p.id} not settled: {e}")
            continue
        S = round(S, 4)
        lots = p.lots                      # read before close(), which zeroes it
        if p.kind == "FUT":
            chg = charges("SELL" if p.side == "LONG" else "BUY", p.units, S, "FUT")
            action = "ROLL-CLOSE" if p.roll else "EXPIRE"
            f = book.close(sday, p.id, p.lots, S, chg, action, f"settled at the spot close {S:,.2f} on {sday}",
                           f"expiry {p.expiry}" + ("; auto-roll" if p.roll else ""))
            fills.append(f)
            notes.append(f"EXPIRY {p.id}: futures settled at {S:,.2f} on {sday}, realised {f['realised_pnl']:,.2f}"
                         + ("; ROLL queued into the next month at the next open" if p.roll else "; not rolled (roll: false)"))
            if p.roll:
                rolls.append(dict(action="OPEN", underlying=p.underlying, instrument="FUT",
                                  side="BUY" if p.side == "LONG" else "SELL", lots=lots, expiry="current",
                                  roll=True, decision_date=sday, auto=True,
                                  reason=f"auto-roll of {p.id} (expired {p.expiry})",
                                  risk_check="engine roll: the same margin, 10% risk, position and breaker checks as any order"))
        else:
            iv = intrinsic(S, p.strike, p.kind)
            chg = exercise_charges(iv * p.units) if (p.side == "LONG" and iv > 0) else Charges()
            f = book.close(sday, p.id, p.lots, iv, chg, "EXPIRE",
                           f"settled at intrinsic {iv:,.2f} (spot close {S:,.2f} on {sday})", f"expiry {p.expiry}")
            fills.append(f)
            notes.append(f"EXPIRY {p.id}: settled at intrinsic {iv:,.2f} (spot {S:,.2f}), realised {f['realised_pnl']:,.2f}")
    return fills, notes, rolls


def run_day(main_prices: pd.DataFrame, do_update: bool) -> dict:
    """F&O part of the daily run. Called by engine.run_day after the thesis sleeve."""
    market_day = main_prices["date"].max().strftime("%Y-%m-%d")
    from .staleness import ensure_not_older, last_date
    ensure_not_older(market_day, last_date(FnoBook.equity_curve()), "state/fno_equity_curve.csv")
    book = FnoBook.load()
    queue = load_queue(config.FNO_ORDERS_FILE)
    tickers = needed_tickers(book, queue["orders"])
    prices = load_prices(main_prices, tickers, do_update)
    warnings = [f"No price data for {t}" for t in prices.attrs["dropped"]]
    try:
        prior, today, listed, chain_notes = load_chains(book, queue["orders"], market_day, do_update, prices)
    except Exception as e:                 # fail soft: the sleeve runs on the model
        log.exception("NSE option chains step failed")
        prior, today, listed, chain_notes = {}, {}, {}, [f"NSE option chains step failed ({e}); model prices only"]
    market = FnoMarket(prices, market_day, book, prior, today, listed)

    fills, notes, keep, executed = run_orders(book, queue["orders"], market, market_day)
    ex_fills, ex_notes, rolls = expire_positions(book, market, market_day)
    fills += ex_fills
    notes += ex_notes
    if rolls:
        f2, n2, k2, e2 = run_orders(book, rolls, market, market_day)
        fills += f2
        notes += [("ROLL " + n) for n in n2]
        keep += k2
        executed += e2
        for e in e2:
            if e["status"] == "rejected":
                notes.append(f"ROLL REJECTED for {e['order']['reason']}: {e['detail']}")
    for p in book.positions.values():
        try:
            market.mark(p, market_day)
        except (NotYet, Reject) as e:
            warnings.append(f"{p.id}: not marked today ({e}); valued at its last mark {p.mark}")
    if book.start_date is None:
        book.start_date = market_day
    eq = book.record_equity(market_day)
    b = book.update_breaker(market_day, eq["day_pnl"])
    if b:
        notes.append(b)
    queue["orders"] = keep
    save_queue(config.FNO_ORDERS_FILE, queue)
    record_executed("fno", market_day, executed)
    warnings += chain_notes
    cands = None
    try:
        from .candidates import build_fno, save as save_cands
        cands = build_fno(market, book, market_day, chain_notes, use_kite=do_update)
        save_cands(config.FNO_CANDIDATES_FILE, cands)
    except Exception as e:
        log.exception("F&O candidates failed")
        warnings.append(f"F&O candidates could not be built ({e})")
    book.last_run = dict(date=market_day, notes=notes, warnings=warnings)
    book.save()
    spots = {}
    for name, s in config.FNO_INDEX.items():
        try:
            spots[name] = market.spot_close(s["yahoo"], market_day)[0]
        except NotYet:
            pass
    try:
        spots["INDIAVIX"] = market.spot_close(config.FNO_VIX_SYMBOL, market_day)[0]
    except NotYet:
        pass
    return dict(date=market_day, book=book, fills=fills, notes=notes, warnings=warnings, equity=eq,
                orders=keep, spots=spots, candidates=cands)


def load_chains(book: FnoBook, orders: list, market_day: str, do_update: bool,
                prices: pd.DataFrame | None = None) -> tuple[dict, dict, dict, list[str]]:
    """(prior, today, listed, notes). listed = NSE's listed expiry dates per underlying from the newest
    snapshot of any date (an expiry list does not go stale with the quotes). prior = each underlying's
    cached chain BEFORE today's fetch (fill IVs, no look-ahead); today = the snapshots whose timestamp is the market date (marks, candidates). An online run
    fetches the candidates' and the open positions' underlyings from NSE (chains.refresh); offline reads the
    cache only.

    Zerodha (papertrade/kite.py, KITE_SETUP.md phase 1): when kite.available(), each underlying's snapshot is
    taken from Kite first, for the same expiries the NSE path picks (chains.expiry_picker on Kite's listed
    expiries), and saved to state/chains/ (source "zerodha", marks labelled chains.ZERODHA_CHAIN_LABEL). The
    NSE scrape still runs; it owns the file only where Kite failed or is not of the market date, and on days
    both exist the journal compares their ATM mids per expiry. No token or any Kite error: the NSE path
    exactly as before, plus a "Kite: <why>, NSE scrape used" line."""
    from .candidates import fno_candidate_symbols, fno_event_dates, fno_events_for
    syms = list(fno_candidate_symbols()[0])
    for p in book.positions.values():
        syms.append(p.underlying)
    for o in orders:
        u = o.get("underlying") or str(o.get("position", "")).split(":")[0]
        try:
            syms.append(spec(u)["name"])
        except Reject:
            pass
    syms = list(dict.fromkeys(syms))
    specs = {u: dict(kind=spec(u)["kind"], weekday=spec(u)["weekday"], lot=spec(u)["lot"]) for u in syms}
    prior = {u: s for u in syms if (s := chains.load(u))}
    if do_update:
        extra = {}
        for p in book.positions.values():
            if p.kind in OPTION_KINDS:
                extra.setdefault(p.underlying, set()).add(p.expiry)
        table = {u: dict(lot=spec(u)["lot"]) for u in list(config.FNO_INDEX) + list(config.FNO_STOCKS)}
        events = fno_event_dates(market_day)      # the candidates' event expiries, so they get real quotes
        ev_dates = {u: [ev["date"] for ev in fno_events_for(u, events)] for u in syms}
        weekly = set(config.FNO_WEEKLY_INDICES)
        kite_snaps, kite_notes = _kite_chains(specs, market_day, extra, weekly, ev_dates, prices)
        fetched, notes = chains.refresh(specs, market_day, extra, weekly=weekly, lot_table=table,
                                        event_dates=ev_dates, no_save=set(kite_snaps))
        kite_notes += _compare_chains(kite_snaps, fetched)
        for u, ks in kite_snaps.items():
            chains.save(ks, prior.get(u))           # the Zerodha snapshot owns state/chains/<SYM>.json today
        fetched = {**fetched, **kite_snaps}
        snaps = {**prior, **fetched}
    else:
        snaps, notes = prior, ["offline run: no NSE chain fetch; cached chains are used only if they are of the market date"]
    today = {u: s for u, s in snaps.items() if s.get("date") == market_day}
    listed = {u: s.get("expiries") or [] for u, s in snaps.items()}
    if do_update:
        got = sorted(today)
        notes.insert(0, f"NSE option chains for {market_day}: " + (", ".join(got) if got else "none")
                     + f" of {len(syms)} ({', '.join(syms)})")
        notes[1:1] = kite_notes
    return prior, today, listed, notes


def _spot(prices: pd.DataFrame | None, ticker: str, market_day: str) -> float | None:
    if prices is None or not len(prices):
        return None
    c = prices[(prices["symbol"] == ticker) & (prices["date"] <= pd.Timestamp(market_day))].dropna(subset=["close"])
    return float(c.sort_values("date")["close"].iloc[-1]) if len(c) else None


def _kite_chains(specs: dict, market_day: str, extra: dict, weekly, ev_dates: dict, prices) -> tuple[dict, list[str]]:
    """{SYM: Zerodha snapshot} for the underlyings Kite could price today, and journal notes. Never raises.
    A snapshot is kept only if it is of the market date and has at least one two-sided quote (after the
    close Kite may show an empty book; then the NSE scrape is the better source that day)."""
    from . import kite
    if not kite.available():
        return {}, [f"Kite: {kite.reason}, NSE scrape used"]
    out, notes = {}, []
    try:
        nfo = kite.instruments("NFO")
    except Exception as e:
        return {}, [f"Kite: instruments dump failed ({type(e).__name__}: {e}), NSE scrape used"]
    for u, sp in specs.items():
        try:
            opt = nfo[(nfo["name"] == u) & nfo["instrument_type"].isin(["CE", "PE"])]
            listed = sorted({e for e in opt["expiry"] if e})
            if not listed:
                raise RuntimeError("no options listed on Kite")
            S = _spot(prices, spec(u)["yahoo"], market_day)
            if not S:
                raise RuntimeError("no Yahoo close to centre the strikes on")
            pick = chains.expiry_picker(u, sp, market_day, weekly, extra, ev_dates)
            snap = kite.chain_snapshot(u, sp["kind"], pick(listed), S)
            if snap["date"] != market_day:
                raise RuntimeError(f"quotes are of {snap['date']} ({snap['timestamp']}), not the market date {market_day}")
            two = sum(1 for rows in snap["chains"].values() for r in rows.values() for q in r.values()
                      if chains.mid(q) is not None)
            if not two:
                raise RuntimeError("no two-sided quote in the book")
            out[u] = snap
        except Exception as e:
            notes.append(f"Kite: {u} chain not used ({e}); NSE scrape for it")
            log.warning("Kite chain %s: %s", u, e)
    if out:
        notes.insert(0, f"Chains: real Zerodha ({len(out)} underlyings: {', '.join(out)})")
    return out, notes


def _atm_mids(snap: dict, expiry: str, S: float):
    rows = (snap.get("chains") or {}).get(expiry) or {}
    if not rows:
        return None
    k = min(rows, key=lambda x: abs(float(x) - S))
    r = rows[k]
    return k, chains.mid(r.get("CE")), chains.mid(r.get("PE"))


def _compare_chains(kite_snaps: dict, nse_snaps: dict) -> list[str]:
    """One journal line per underlying with both a Zerodha and an NSE snapshot of the same date: the ATM
    call and put mid per common expiry from each source (ATM = the strike nearest Zerodha's underlying)."""
    out = []
    fmt = lambda v: "n/a" if v is None else f"{v:,.2f}"
    for u, ks in kite_snaps.items():
        ns = nse_snaps.get(u)
        if not ns or ns.get("date") != ks.get("date"):
            continue
        S = ks.get("underlying_value") or ns.get("underlying_value") or 0
        parts = []
        for e in sorted(set(ks.get("chains", {})) & set(ns.get("chains", {}))):
            a, b = _atm_mids(ks, e, S), _atm_mids(ns, e, S)
            if not a or not b or a[0] != b[0]:
                continue
            cells = []
            for side, x, y in (("CE", a[1], b[1]), ("PE", a[2], b[2])):
                d = f" (diff {x - y:+,.2f})" if x is not None and y is not None else ""
                cells.append(f"{side} {fmt(x)} vs {fmt(y)}{d}")
            parts.append(f"{e} ATM {a[0]}: " + ", ".join(cells))
        if parts:
            out.append(f"Chain check {u}, mid Zerodha vs NSE (Zerodha {ks['timestamp']}, NSE {ns['timestamp']}): "
                       + "; ".join(parts))
    return out


# ---------- views ----------
def position_rows(book: DerivBook, market_day: str) -> list[dict]:
    rows = []
    for p in book.positions.values():
        dte = (_date.fromisoformat(p.expiry) - _date.fromisoformat(market_day)).days if p.expiry else None
        rows.append(dict(id=p.id, instrument=book.instrument(p), kind=p.kind, side=p.side, lots=p.lots,
                         units=p.units, lot_size=p.lot_size, entry=p.entry_price, entry_date=p.entry_date,
                         mark=p.mark, mark_date=p.mark_date, pnl=p.pnl(p.mark), margin=p.margin,
                         days_to_expiry=dte, pricing=p.pricing, roll=p.roll if p.kind == "FUT" else None,
                         strike=p.strike, expiry=p.expiry, spot=p.spot, iv=p.iv, reason=p.reason))
    return rows


def _f(x, dp=0):
    return "n/a" if x is None else f"{x:,.{dp}f}"


def journal_lines(s: dict, title: str = "## India F&O sleeve", ccy: str = "Rs", label: str = MODEL_LABEL,
                  intro: str | None = None, inr_note: bool = False, extra_rows=None) -> list[str]:
    """Shared by both derivative sleeves (us.py passes its own title, currency and label)."""
    book, day = s["book"], s["date"]
    eq = s["equity"]["equity"]
    total = eq - book.starting_capital
    fx = book.fx()
    inr = (lambda v: f" (Rs {v * fx:,.0f})") if inr_note else (lambda v: "")
    lines = [title, ""]
    lines += [intro or (f"Separate book: Rs {book.starting_capital:,.0f}, NIFTY / BANKNIFTY and listed stock futures and "
                        f"options. Options and futures are {label}. Decisions by the daily reviewer (ROUTINE_PROMPT.md); "
                        f"limits in RISK_LIMITS.md."), ""]
    lines += ["| Metric | Value |", "|---|---|",
              f"| Sleeve equity | {ccy} {_f(eq, 2)}{inr(eq)} |",
              f"| Day P&L | {ccy} {_f(s['equity']['day_pnl'], 2)}{inr(s['equity']['day_pnl'])} |",
              f"| Total P&L since start | {ccy} {_f(total, 2)} ({total / book.starting_capital * 100:+.2f}%) |",
              *(extra_rows or []),
              f"| Cash / margin used | {ccy} {_f(book.cash, 2)} / {ccy} {_f(book.margin_used(), 2)} |",
              f"| Charges paid / realised P&L | {ccy} {_f(book.costs_paid, 2)} / {ccy} {_f(book.realised_pnl, 2)} |",
              f"| Open positions | {len(book.positions)} of {config.DERIV_MAX_POSITIONS} |",
              f"| Circuit breaker | {book.breaker_text()} |"]
    if s.get("spots"):
        lines.append("| Market | " + ", ".join(f"{k} {v:,.2f}" for k, v in s["spots"].items()) + " |")
    lines.append("")
    rows = position_rows(book, day)
    lines += ["### Positions", ""]
    if rows:
        lines += ["| Position | Instrument | Side | Lots (units) | Entry | Mark | P&L | Margin | Days to expiry | Pricing |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
        for r in rows:
            lines.append(f"| {r['id']} | {r['instrument']} | {r['side']} | {r['lots']} ({r['units']}) | {_f(r['entry'], 2)} | "
                         f"{_f(r['mark'], 2)} | {ccy} {_f(r['pnl'], 2)} | {ccy} {_f(r['margin'], 2)} | "
                         f"{r['days_to_expiry'] if r['days_to_expiry'] is not None else '-'} | {r['pricing']} |")
    else:
        lines.append("None. The sleeve is all cash.")
    lines.append("")
    lines += ["### Fills today", ""]
    if s["fills"]:
        lines += ["| Date | Action | Side | Instrument | Lots | Price | Charges | Margin | Realised | Pricing |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
        for f in s["fills"]:
            lines.append(f"| {f['date']} | {f['action']} | {f['side']} | {f['instrument']} | {f['lots']} | {_f(f['price'], 2)} | "
                         f"{_f(f['charges'], 2)} | {_f(f['margin'], 2)} | {f['realised_pnl'] if f['realised_pnl'] != '' else '-'} | {f['pricing']} |")
    else:
        lines.append("No fills.")
    lines.append("")
    lines += ["### Orders queued", ""]
    if s["orders"]:
        lines += ["| Action | Side | Underlying / position | Instrument | Lots | Strike | Expiry | Decided | Reason | Risk check |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
        for o in s["orders"]:
            lines.append(f"| {o.get('action', 'OPEN')} | {o.get('side', '-')} | {o.get('position') or o.get('underlying', '-')} | "
                         f"{o.get('instrument', '-')} | {o.get('lots') or o.get('amount') or 'all'} | {o.get('strike', '-')} | "
                         f"{o.get('expiry', '-')} | {o.get('decision_date', '-')} | {o.get('reason', '')} | {o.get('risk_check', '')} |")
    else:
        lines.append("None.")
    lines.append("")
    obs = list(s["notes"]) + list(s["warnings"])
    if obs:
        lines += ["### Observations", ""] + [f"- {o}" for o in obs]
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def export_data(main_prices: pd.DataFrame | None = None) -> dict:
    """Dashboard view. Read-only: the state file already holds the last marks."""
    book = FnoBook.load()
    return _export(book, config.FNO_ORDERS_FILE, MODEL_LABEL, "INR",
                   main_prices["date"].max().strftime("%Y-%m-%d") if main_prices is not None and len(main_prices)
                   else (book.last_run.get("date") or _date.today().isoformat()))


def _export(book: DerivBook, orders_file, label: str, ccy: str, market_day: str) -> dict:
    curve = book.equity_curve()
    eq = float(curve["equity"].iloc[-1]) if len(curve) else book.equity()
    trades = book.trades()
    return dict(
        currency=ccy, fx_to_inr=book.fx(), capital=book.starting_capital, capital_inr=book.starting_capital * book.fx(),
        start_date=book.start_date, cash=book.cash, margin_used=book.margin_used(), equity=eq,
        equity_inr=float(curve["equity_inr"].iloc[-1]) if len(curve) else eq * book.fx(),
        total_pnl=eq - book.starting_capital, total_pnl_pct=(eq / book.starting_capital - 1) * 100,
        day_pnl=float(curve["day_pnl"].iloc[-1]) if len(curve) else 0.0,
        costs_paid=book.costs_paid, realised_pnl=book.realised_pnl, breaker=book.breaker or None,
        breaker_text=book.breaker_text(), max_positions=config.DERIV_MAX_POSITIONS,
        max_order_risk_pct=config.DERIV_MAX_ORDER_RISK_PCT * 100, daily_loss_pct=config.DERIV_DAILY_LOSS_PCT * 100,
        pricing_label=label, positions=position_rows(book, market_day),
        orders=load_queue(orders_file)["orders"], last_run=book.last_run,
        equity_curve=curve.astype(object).where(curve.notna(), None).to_dict("records"),
        trades=trades.astype(object).where(trades.notna(), None).to_dict("records")[-20:],
    )
