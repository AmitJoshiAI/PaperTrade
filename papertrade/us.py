"""US sleeve: US stocks and ETFs (long, and PAPER short) and US equity options, paper only.

Rs 5,00,000 (config.US_CAPITAL) converted to USD at the USD/INR close of the sleeve's start date (fx.py:
Yahoo USDINR=X, cached in state/fx_rates.csv), stored as "start_fx". The book is kept in USD; its INR
value each day is USD equity x that day's rate, so a rupee move shows up as INR P&L, reported separately
as "of which FX" (USD equity x (day rate - start rate)). The circuit breaker works on the USD day P&L, so
an FX move alone never trips it. Separate files: state/us_*,
decisions/us_orders.json. Accounting, risk rules and the circuit breaker are in derivbook.py; every limit
is in RISK_LIMITS.md. Decisions by the daily reviewer (ROUTINE_PROMPT.md).

Prices:
  stock / ETF   Yahoo daily bars. Fills at the first US open AFTER the order's decision_date (US calendar,
                not NSE's) with 0.05% adverse slippage; marks at the latest US close.
  options       REAL Yahoo option chains (Ticker.option_chain): filled and marked at the MID of bid/ask.
                The daily run is at 17:30 IST, before the US open, so the quotes are those of the latest
                US session's close: an option order fills at the mid as of that session (the first session
                after its decision_date or later), not at the open. If a contract has no two-sided quote
                (or the chain cannot be fetched), it is priced with Black-Scholes on the latest close using
                the chain's last implied vol (or the vol stored at the last good quote), and labelled so.
                An offline run never fills options (no chain) and marks them with that stored vol.
  expiry        options settle at intrinsic value on the close of their expiry day, in cash (real US
                equity options are American and deliver shares; early exercise is not modelled).
Costs (costs.us_charges): zero commission; SEC fee and FINRA TAF on sells, rounded up to the cent.
Shorts: every short stock is labelled PAPER_SHORT: an Indian resident cannot short US stocks under LRS.
50% of the notional is blocked as margin. Short options must be covered (derivbook.py rule 1): a short
call by long stock (100 shares per contract) or a long call of the same expiry; a short put by short
stock or a long put of the same expiry.
Tax note (not modelled): US short-term gains are taxed in India at the slab rate (30% for this user).

Orders, decisions/us_orders.json:

  {"orders": [
    {"action": "OPEN", "underlying": "SPY", "instrument": "STK", "side": "BUY", "lots": 1,
     "decision_date": "2026-10-19", "reason": "why", "risk_check": "notional $770 < 10% of equity ..."},
    {"action": "OPEN", "underlying": "AAPL", "instrument": "CALL", "side": "BUY", "lots": 1,
     "strike": "ATM+2", "expiry": "2026-11-20", "decision_date": "2026-10-19", "reason": "...", "risk_check": "..."},
    {"action": "CLOSE", "position": "AAPL:2026-11-20:C350:L", "decision_date": "2026-10-23", "reason": "..."}
  ]}

  instrument  STK (stock or ETF), CALL, PUT
  lots        shares for STK (or "amount" in USD instead), contracts for options (x100 shares)
  strike      a listed strike, ATM, or ATM+N / ATM-N (N listed strikes from the nearest to the last close)
  expiry      options: a listed expiry YYYY-MM-DD, or "min_days": N for the first listed expiry at least
              N calendar days after the fill
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date as _date, datetime, timezone, timedelta
from typing import Optional

import pandas as pd

from . import config, data, fx
from .costs import us_charges, Charges
from .derivbook import (DerivBook, DPos, Reject, NotYet, bs_price, intrinsic, years_to, position_id,
                        run_orders, load_queue, save_queue, record_executed, OPTION_KINDS)
from .fno import journal_lines as _journal_lines, _export, position_rows

log = logging.getLogger(__name__)

PAPER_SHORT_US = "PAPER SHORT: not possible from India (LRS)"
CHAIN_LABEL = "real option chain (mid of bid/ask)"
REF_TICKER = "SPY"          # always downloaded: fixes the US market date even with nothing held


@dataclass
class UsBook(DerivBook):
    start_fx: Optional[float] = None      # USD/INR used to convert the Rs 5,00,000 at the start
    start_fx_date: Optional[str] = None
    fx_rate: Optional[float] = None       # USD/INR of the latest mark
    fx_date: Optional[str] = None
    fx_note: str = ""

    _PORTFOLIO_FILE = "US_PORTFOLIO_FILE"
    _TRADES_FILE = "US_TRADES_FILE"
    _EQUITY_FILE = "US_EQUITY_FILE"
    CURRENCY = "USD"

    @classmethod
    def initial_capital(cls) -> float:
        return config.US_CAPITAL / config.USD_INR

    def fx(self) -> float:
        return float(self.fx_rate or config.FX_TO_INR["USD"])

    def _extra_equity(self, row: dict, prev) -> dict:
        """INR view: the day's rate, INR P&L (day and total) and the part of it that is FX alone."""
        rate, start = self.fx(), float(self.start_fx or self.fx())
        prev_usd = float(prev["equity"]) if prev else self.starting_capital
        pr = prev.get("usd_inr") if prev else None
        prev_rate = start if pr is None or pd.isna(pr) else float(pr)
        eq = self.equity()                 # unrounded, so day one shows 0.00, not a rounding residue
        return dict(usd_inr=round(rate, 4), fx_date=self.fx_date or "",
                    day_pnl_inr=round(eq * rate - (float(prev["equity_inr"]) if prev else prev_usd * prev_rate), 2),
                    day_fx_pnl_inr=round(prev_usd * (rate - prev_rate), 2),
                    total_pnl_inr=round(eq * rate - self.starting_capital * start, 2),
                    fx_pnl_inr=round(eq * (rate - start), 2))

    @classmethod
    def instrument(cls, p: DPos) -> str:
        if p.kind == "STK":
            return f"{p.underlying} stock"
        return f"{p.underlying} {p.expiry} {p.strike:g} {p.kind}"


def _ticker(u) -> str:
    t = str(u or "").strip().upper()
    if not t or "." in t or t.startswith("^"):
        raise Reject(f"underlying {u!r}: give a plain US ticker (SPY, AAPL, BRK-B)")
    return t


def _instrument(o) -> str:
    i = str(o.get("instrument", "STK")).strip().upper()
    i = {"STOCK": "STK", "ETF": "STK", "SHARES": "STK", "C": "CALL", "P": "PUT", "CE": "CALL", "PE": "PUT"}.get(i, i)
    if i not in ("STK", "CALL", "PUT"):
        raise Reject(f"instrument {o.get('instrument')!r}: use STK, CALL or PUT")
    return i


def needed_tickers(book: UsBook, orders: list) -> list[str]:
    t = {REF_TICKER} | {p.underlying for p in book.positions.values()}
    for o in orders:
        try:
            t.add(_ticker(o.get("underlying") or str(o.get("position", "")).split(":")[0]))
        except Reject:
            pass
    return sorted(t)


def load_prices(tickers: list[str], do_update: bool) -> pd.DataFrame:
    px = None
    if do_update:
        px = data.download_tickers(tickers, period="1y")
        if not px.empty:
            config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
            px.to_parquet(config.US_PRICES_FILE, index=False)
    elif config.US_PRICES_FILE.exists():
        px = pd.read_parquet(config.US_PRICES_FILE)
    out = px if px is not None and not px.empty else pd.DataFrame(
        columns=["date", "open", "high", "low", "close", "volume", "symbol"])
    out = out[out["date"] <= pd.Timestamp(_date.today())]
    present = set(out["symbol"].unique())
    out.attrs["dropped"] = [t for t in tickers if t not in present]
    return out


class UsMarket:
    def __init__(self, prices: pd.DataFrame, book: UsBook, online: bool):
        self.book, self.online = book, online
        self.opens = data.pivot(prices, "open") if len(prices) else pd.DataFrame()
        self.closes = data.pivot(prices, "close") if len(prices) else pd.DataFrame()
        self.latest = self.closes.index.max().strftime("%Y-%m-%d") if len(self.closes) else None
        self._chains, self._expiries = {}, {}
        self.warnings = []
        self.fetched_at = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%Y-%m-%d %H:%M IST")

    # --- data ---
    def _col(self, frame, t):
        return frame[t].dropna() if t in frame.columns else pd.Series(dtype=float, index=pd.DatetimeIndex([]))

    def last_close(self, t, day=None) -> tuple[float, str]:
        c = self._col(self.closes, t)
        if day:
            c = c[c.index <= pd.Timestamp(day)]
        if c.empty:
            raise NotYet(f"no {t} close")
        return float(c.iloc[-1]), c.index[-1].strftime("%Y-%m-%d")

    def expiries(self, t) -> list[str]:
        if t not in self._expiries:
            import yfinance as yf
            try:
                self._expiries[t] = list(yf.Ticker(t).options)
            except Exception as e:
                self._expiries[t] = []
                self.warnings.append(f"{t}: option expiries unavailable ({e})")
        return self._expiries[t]

    def chain(self, t, expiry, kind) -> pd.DataFrame | None:
        key = (t, expiry)
        if key not in self._chains:
            import yfinance as yf
            try:
                ch = yf.Ticker(t).option_chain(expiry)
                self._chains[key] = {"CALL": ch.calls, "PUT": ch.puts}
            except Exception as e:
                self._chains[key] = None
                self.warnings.append(f"{t} {expiry}: option chain unavailable ({e})")
        c = self._chains[key]
        return None if c is None else c[kind]

    def quote(self, p: DPos, day: str) -> tuple[float, str, str, float | None]:
        """(price, pricing text, label, implied vol) for an option at the latest session."""
        ch = self.chain(p.underlying, p.expiry, p.kind) if self.online else None
        iv = p.iv
        if ch is not None:
            row = ch[(ch["strike"] - p.strike).abs() < 1e-6]
            if len(row):
                r = row.iloc[0]
                bid, ask = float(r.get("bid") or 0), float(r.get("ask") or 0)
                civ = r.get("impliedVolatility")
                civ = float(civ) if civ is not None and not pd.isna(civ) and float(civ) >= 0.01 else None
                p.contract = str(r.get("contractSymbol", p.contract))
                if bid > 0 and ask > 0 and ask >= bid:
                    return (round((bid + ask) / 2, 4), f"chain mid: bid {bid:.2f} / ask {ask:.2f} ({p.contract}, "
                            f"quotes of the {self.latest} session, fetched {self.fetched_at})", CHAIN_LABEL, civ or iv)
                if civ:
                    iv = civ
                    src = f"chain IV {civ*100:.2f}% (no two-sided quote)"
                else:
                    src = None
            else:
                src = None
                self.warnings.append(f"{p.underlying} {p.expiry} {p.strike:g} {p.kind}: strike not in today's chain")
        else:
            src = None
        if iv is None:
            raise NotYet(f"no quote and no implied vol for {p.underlying} {p.expiry} {p.strike:g} {p.kind}")
        S, sday = self.last_close(p.underlying, day)
        t = years_to(p.expiry, sday)
        px = bs_price(S, p.strike, t, config.US_RISK_FREE_RATE, iv, p.kind)
        how = src or (f"stored IV {iv*100:.2f}% ({'offline run' if not self.online else 'chain unavailable'})")
        return (round(px, 4), f"FALLBACK Black-Scholes: spot {S:,.2f} ({sday}), {how}, r {config.US_RISK_FREE_RATE*100:g}%, "
                f"T {round(t*365)}/365", "Black-Scholes fallback (labelled), not a market quote", iv)

    # --- the interface run_orders uses ---
    def _und_kind(self, o):
        if str(o.get("action", "OPEN")).upper() == "CLOSE":
            p = self.book.positions.get(o.get("position"))
            if p is not None:
                return p.underlying, p.kind
            return _ticker(str(o.get("position") or o.get("underlying", "")).split(":")[0]), "STK"
        return _ticker(o.get("underlying")), _instrument(o)

    def fill_day(self, o):
        und, kind = self._und_kind(o)
        dd = pd.Timestamp(o["decision_date"])
        if kind in OPTION_KINDS:
            if not self.online:
                raise NotYet("options need a live chain; an offline run does not fill them")
            return self.latest if self.latest and pd.Timestamp(self.latest) > dd else None
        c = self._col(self.opens, und)
        later = c.index[c.index > dd]
        return later[0].strftime("%Y-%m-%d") if len(later) else None

    def pos_id(self, p: DPos) -> str:
        return position_id(p.underlying, p.kind, p.side, p.expiry, p.strike, ("C", "P"))

    def open_contract(self, o, fill_day, side):
        und, kind = _ticker(o.get("underlying")), _instrument(o)
        buy = side == "LONG"
        if kind == "STK":
            c = self._col(self.opens, und)
            px = float(c.loc[pd.Timestamp(fill_day)])
            price = round(px * (1 + config.SLIPPAGE_PCT if buy else 1 - config.SLIPPAGE_PCT), 4)
            lots = int(o.get("lots") or 0) or int(float(o.get("amount", 0)) // price)
            if lots <= 0:
                raise Reject(f"amount {o.get('amount')} buys no share at {price:.2f}")
            margin = round(config.US_SHORT_STOCK_MARGIN_PCT * price * lots, 2) if not buy else 0.0
            pos = DPos(id="", underlying=und, kind="STK", side=side, lots=lots, lot_size=1, entry_price=price,
                       entry_date=fill_day, margin=margin, spot=px,
                       pricing=f"open {px:,.4f} {'+' if buy else '-'}{config.SLIPPAGE_PCT*100:g}% slippage")
            return pos, price, us_charges("BUY" if buy else "SELL", lots, price), "" if buy else PAPER_SHORT_US
        exps = self.expiries(und)
        if not exps:
            raise NotYet(f"no option expiries for {und}")
        if o.get("expiry"):
            expiry = str(o["expiry"])
            if expiry not in exps:
                raise Reject(f"expiry {expiry} is not listed for {und}; listed: {', '.join(exps[:8])}...")
        elif o.get("min_days"):
            floor = _date.fromisoformat(fill_day) + timedelta(days=int(o["min_days"]))
            ok = [e for e in exps if _date.fromisoformat(e) >= floor]
            if not ok:
                raise Reject(f"no listed {und} expiry {o['min_days']}+ days out")
            expiry = ok[0]
        else:
            raise Reject("an option order needs expiry (a listed YYYY-MM-DD) or min_days")
        ch = self.chain(und, expiry, kind)
        if ch is None or ch.empty:
            raise NotYet(f"no {und} {expiry} chain")
        strikes = sorted(float(k) for k in ch["strike"])
        spec = str(o.get("strike", "")).strip().upper().replace(" ", "")
        if not spec:
            raise Reject("an option order needs a strike (a listed strike, ATM or ATM+N)")
        if spec.startswith("ATM"):
            S, _ = self.last_close(und)
            i = min(range(len(strikes)), key=lambda j: abs(strikes[j] - S))
            n = int(spec[3:]) if len(spec) > 3 else 0
            if not 0 <= i + n < len(strikes):
                raise Reject(f"{spec}: no such listed strike")
            K = strikes[i + n]
        else:
            K = float(spec)
            if not any(abs(K - k) < 1e-6 for k in strikes):
                near = sorted(strikes, key=lambda k: abs(k - K))[:4]
                raise Reject(f"strike {K:g} is not listed for {und} {expiry}; nearest: {', '.join(f'{k:g}' for k in sorted(near))}")
        lots = int(o.get("lots") or 0)
        pos = DPos(id="", underlying=und, kind=kind, side=side, lots=lots, lot_size=config.US_CONTRACT_SIZE,
                   entry_price=0.0, entry_date=fill_day, strike=K, expiry=expiry)
        price, how, label, iv = self.quote(pos, fill_day)
        if price <= 0:
            raise Reject(f"price {price} for {und} {expiry} {K:g} {kind}: nothing to trade")
        pos.entry_price, pos.pricing, pos.iv = price, how, iv
        pos.spot = self.last_close(und)[0]
        return pos, price, us_charges("BUY" if buy else "SELL", lots, price, option=True), label

    def close_price(self, p: DPos, o, fill_day):
        selling = p.side == "LONG"
        if p.kind == "STK":
            px = float(self._col(self.opens, p.underlying).loc[pd.Timestamp(fill_day)])
            price = round(px * (1 - config.SLIPPAGE_PCT if selling else 1 + config.SLIPPAGE_PCT), 4)
            return price, us_charges("SELL" if selling else "BUY", p.lots, price), f"open {px:,.4f}", \
                "" if selling else PAPER_SHORT_US
        price, how, label, iv = self.quote(p, fill_day)
        return price, us_charges("SELL" if selling else "BUY", p.lots, price, option=True), how, label

    def mark(self, p: DPos, day: str) -> None:
        if p.kind == "STK":
            S, sday = self.last_close(p.underlying, day)
            p.mark, p.spot, p.mark_date, p.pricing = round(S, 4), round(S, 4), sday, f"close {S:,.2f} ({sday})"
            return
        price, how, label, iv = self.quote(p, day)
        p.mark, p.pricing, p.iv, p.mark_date = price, how, iv, day
        p.spot = self.last_close(p.underlying, day)[0]


def expire_positions(book: UsBook, market: UsMarket, market_day: str) -> tuple[list, list]:
    fills, notes = [], []
    due = [p for p in book.positions.values() if p.kind in OPTION_KINDS and p.expiry <= market_day]
    due.sort(key=lambda p: p.side == "LONG")
    for p in due:
        try:
            S, sday = market.last_close(p.underlying, p.expiry)
        except NotYet as e:
            notes.append(f"EXPIRY of {p.id} not settled: {e}")
            continue
        iv = intrinsic(S, p.strike, p.kind)
        f = book.close(sday, p.id, p.lots, iv, Charges(), "EXPIRE",
                       f"cash-settled at intrinsic {iv:,.2f} (close {S:,.2f} on {sday})", f"expiry {p.expiry}")
        fills.append(f)
        notes.append(f"EXPIRY {p.id}: settled at intrinsic {iv:,.2f} (close {S:,.2f}), realised ${f['realised_pnl']:,.2f}")
    return fills, notes


def run_day(fallback_day: str, do_update: bool) -> dict:
    """US part of the daily run. Its market date is the latest US session, not the NSE date."""
    book = UsBook.load()
    queue = load_queue(config.US_ORDERS_FILE)
    tickers = needed_tickers(book, queue["orders"])
    prices = load_prices(tickers, do_update)
    warnings = [f"No price data for {t}" for t in prices.attrs["dropped"]]
    market = UsMarket(prices, book, online=do_update)
    market_day = market.latest or fallback_day
    # its own calendar: never mark as of a date before the last US row (raises before any US write)
    from .staleness import ensure_not_older, last_date
    ensure_not_older(market_day, last_date(UsBook.equity_curve()), "state/us_equity_curve.csv")
    rate = fx.lookup(market_day, download=do_update)
    notes = []
    if book.start_fx is None:
        if book.positions or len(UsBook.trades()):
            # never happens today (no US order had filled when start_fx was added); keep the book as it is
            book.start_fx, book.start_fx_date = 84.0, "legacy fixed rate"
            warnings.append("US sleeve has positions from before the live rate; its start rate is the old fixed 84.0")
        else:
            config.USD_INR = rate.rate
            cap = config.US_CAPITAL / rate.rate
            if config.US_EQUITY_FILE.exists():
                config.US_EQUITY_FILE.unlink()      # its rows were at the old fixed 84.0
            book = UsBook(cash=cap, starting_capital=cap, start_fx=rate.rate, start_fx_date=rate.date)
            market.book = book
            notes.append(f"US sleeve (re)initialised at the live rate: Rs {config.US_CAPITAL:,.0f} / {rate.rate:.4f} "
                         f"({rate.note}) = ${cap:,.2f}. No US order had ever filled.")
    book.fx_rate, book.fx_date, book.fx_note = rate.rate, rate.date, rate.note
    if rate.source == "fallback":
        warnings.append(f"USD/INR: {rate.note}; INR values use {rate.rate}")
    fills, n0, keep, executed = run_orders(book, queue["orders"], market, market_day)
    notes += n0
    ex_fills, ex_notes = expire_positions(book, market, market_day)
    fills += ex_fills
    notes += ex_notes
    for p in book.positions.values():
        try:
            market.mark(p, market_day)
        except (NotYet, Reject) as e:
            warnings.append(f"{p.id}: not marked today ({e}); valued at its last mark {p.mark}")
    warnings += market.warnings
    if book.start_date is None:
        book.start_date = market_day
    eq = book.record_equity(market_day)
    b = book.update_breaker(market_day, eq["day_pnl"])
    if b:
        notes.append(b)
    queue["orders"] = keep
    save_queue(config.US_ORDERS_FILE, queue)
    record_executed("us", market_day, executed)
    book.last_run = dict(date=market_day, notes=notes, warnings=warnings)
    book.save()
    spots = {}
    try:
        spots[REF_TICKER] = market.last_close(REF_TICKER)[0]
    except NotYet:
        pass
    return dict(date=market_day, book=book, fills=fills, notes=notes, warnings=warnings, equity=eq,
                orders=keep, spots=spots)


def journal_lines(s: dict) -> list[str]:
    book = s["book"]
    e = s["equity"]
    intro = (f"Separate book: Rs {config.US_CAPITAL:,.0f} = ${book.starting_capital:,.2f} at USD/INR "
             f"{book.start_fx:.4f} (close of {book.start_fx_date}). INR values use each day's USD/INR "
             f"(Yahoo USDINR=X daily close, state/fx_rates.csv). US stocks/ETFs long and paper-short, US options "
             f"priced from the {CHAIN_LABEL}; a Black-Scholes fallback is labelled. US market date {s['date']} "
             f"(US calendar). Every short stock: {PAPER_SHORT_US}. Limits in RISK_LIMITS.md.")
    rows = [f"| USD/INR used today | {book.fx():.4f} ({book.fx_note}) |",
            f"| Total P&L in INR | Rs {e['total_pnl_inr']:,.2f}, of which FX Rs {e['fx_pnl_inr']:,.2f} "
            f"(start rate {book.start_fx:.4f}) |",
            f"| Day P&L in INR | Rs {e['day_pnl_inr']:,.2f}, of which FX Rs {e['day_fx_pnl_inr']:,.2f} |"]
    return _journal_lines(s, title="## US sleeve", ccy="$", label=CHAIN_LABEL, intro=intro, inr_note=True,
                          extra_rows=rows)


def export_data() -> dict:
    book = UsBook.load()
    d = _export(book, config.US_ORDERS_FILE, CHAIN_LABEL, "USD", book.last_run.get("date") or _date.today().isoformat())
    curve = book.equity_curve()
    last = curve.iloc[-1].to_dict() if len(curve) else {}
    d["usd_inr"] = book.fx()
    d["fx_date"], d["fx_note"] = book.fx_date, book.fx_note
    d["start_fx"], d["start_fx_date"] = book.start_fx, book.start_fx_date
    for k in ("total_pnl_inr", "fx_pnl_inr", "day_pnl_inr", "day_fx_pnl_inr"):
        d[k] = last.get(k)
    d["paper_short_label"] = PAPER_SHORT_US
    return d
