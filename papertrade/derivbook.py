"""Shared book for the two derivative sleeves: India F&O (fno.py) and US (us.py).

What lives here: Black-Scholes, realised volatility, monthly-expiry dates, the position model, the cash /
margin accounting, the risk rules and the daily-loss circuit breaker, the order loop, and the ledgers.
What the sleeves add: which instruments exist, how a contract is priced at a fill and at the close, how
it expires, and the journal / dashboard views. Nothing here touches the momentum or thesis books.

Accounting (all money in the book's currency: INR for F&O, USD for the US sleeve):

    futures            open:  cash -= margin + charges          value = margin + sign x (mark - entry) x units
                       close: cash += margin + sign x (exit - entry) x units - charges
    options, stock     LONG open:  cash -= price x units + charges           value = mark x units
                       SHORT open: cash += price x units - charges - margin  value = margin - mark x units
                       LONG close: cash += price x units - charges
                       SHORT close: cash += margin - price x units - charges

    equity = cash + sum(value) = cash + margin blocked + mark-to-market. units = lots x lot_size.
    Realised P&L of a close = sign x (exit - entry) x units - entry charges (pro rata) - exit charges.

Risk rules (config, listed in RISK_LIMITS.md), checked on every OPEN order before it fills:
  1. no naked short options: a short CALL needs a LONG future, a LONG call or (US) long stock in the same
     underlying, and for futures and options the same expiry; a short PUT needs a SHORT future, a LONG
     put or (US) short stock. Cover is counted in units and each unit covers one short unit only.
     A close that would leave a short option uncovered is rejected (close the short first).
  2. at most DERIV_MAX_POSITIONS open positions.
  3. per-order risk <= DERIV_MAX_ORDER_RISK_PCT of sleeve equity. Risk = premium (long option), margin
     (future, short option, short stock) or notional (long stock).
  4. free cash must cover what the order takes out of cash (margin, premium, charges).
  5. daily-loss circuit breaker: a day's equity change below -DERIV_DAILY_LOSS_PCT x starting capital
     blocks new positions (and futures rolls) until the next Monday. Exits are always allowed.
Margin of a short option: 0 when it is covered by a future or stock (that cover's margin or cash is
already blocked), else its spread's maximum loss against the long option covering it (strike gap x units).
"""
from __future__ import annotations

import calendar
import csv
import json
import logging
import math
import re
from dataclasses import dataclass, field, asdict
from datetime import date as _date, timedelta
from typing import Optional

import pandas as pd

from . import config

log = logging.getLogger(__name__)

KINDS = ("FUT", "CALL", "PUT", "STK")
OPTION_KINDS = ("CALL", "PUT")


# ---------- maths ----------
def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(spot: float, strike: float, t_years: float, rate: float, sigma: float, right: str) -> float:
    """European Black-Scholes price on a non-dividend spot. At or after expiry (t <= 0) or with no vol it
    is the intrinsic value."""
    right = right.upper()
    if t_years <= 0 or sigma <= 0:
        return max(spot - strike, 0.0) if right == "CALL" else max(strike - spot, 0.0)
    v = sigma * math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * t_years) / v
    d2 = d1 - v
    disc = strike * math.exp(-rate * t_years)
    if right == "CALL":
        return spot * norm_cdf(d1) - disc * norm_cdf(d2)
    return disc * norm_cdf(-d2) - spot * norm_cdf(-d1)


def intrinsic(spot: float, strike: float, right: str) -> float:
    return max(spot - strike, 0.0) if right.upper() == "CALL" else max(strike - spot, 0.0)


def realised_vol(closes: pd.Series, days: int = config.REALISED_VOL_DAYS) -> Optional[float]:
    """Annualised close-to-close volatility of the last `days` daily log returns (sample std x sqrt(252))."""
    c = closes.dropna()
    if len(c) < days + 1:
        return None
    r = (c.iloc[-(days + 1):].astype(float)).apply(math.log).diff().dropna()
    return float(r.std(ddof=1) * math.sqrt(252))


def years_to(expiry: str, day: str) -> float:
    """Calendar days from `day` to `expiry`, / 365. The open and the close of a day use the same value."""
    return max((_date.fromisoformat(expiry) - _date.fromisoformat(day)).days, 0) / 365.0


# ---------- calendar ----------
def last_weekday(year: int, month: int, weekday: int) -> _date:
    d = _date(year, month, calendar.monthrange(year, month)[1])
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def monthly_expiry(spec, after: str, weekday: int) -> str:
    """Resolve an order's expiry against its fill day. "current" (default) = the first monthly expiry
    strictly after the fill day; "next" = the one after that; "YYYY-MM" = that month's; "YYYY-MM-DD" =
    exactly that date. Raises ValueError if the result is not after the fill day."""
    a = _date.fromisoformat(after)
    spec = str(spec or "current").strip().lower()
    if spec in ("current", "near", "next", "far"):
        y, m = a.year, a.month
        e = last_weekday(y, m, weekday)
        if e <= a:
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
            e = last_weekday(y, m, weekday)
        if spec in ("next", "far"):
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
            e = last_weekday(y, m, weekday)
    elif re.fullmatch(r"\d{4}-\d{2}", spec):
        e = last_weekday(int(spec[:4]), int(spec[5:]), weekday)
    else:
        try:
            e = _date.fromisoformat(spec)
        except ValueError:
            raise ValueError(f"expiry {spec!r}: use current, next, YYYY-MM or YYYY-MM-DD")
    if e <= a:
        raise ValueError(f"expiry {e} is not after the fill day {after}")
    return e.isoformat()


def next_monday(day: str) -> str:
    d = _date.fromisoformat(day)
    return (d + timedelta(days=7 - d.weekday())).isoformat()


def strike_spec(spec, spot: float, step: float) -> float:
    """A number (rounded to the step), "ATM" (spot rounded to the step) or "ATM+N" / "ATM-N" (N steps)."""
    s = str(spec).strip().upper().replace(" ", "")
    m = re.fullmatch(r"ATM([+-]\d+)?", s)
    if m:
        base = round(spot / step) * step
        return round(base + (int(m.group(1)) if m.group(1) else 0) * step, 4)
    try:
        v = float(s.replace(",", ""))
    except ValueError:
        raise ValueError(f"strike {spec!r}: give a number, ATM or ATM+N / ATM-N")
    return round(round(v / step) * step, 4)


# ---------- positions ----------
@dataclass
class DPos:
    id: str
    underlying: str
    kind: str                 # FUT / CALL / PUT / STK
    side: str                 # LONG / SHORT
    lots: int
    lot_size: int
    entry_price: float        # per unit: futures price, option premium or share price
    entry_date: str
    strike: Optional[float] = None
    expiry: Optional[str] = None
    margin: float = 0.0       # blocked from cash, released at close
    entry_charges: float = 0.0
    roll: bool = False        # futures: auto-roll at expiry
    pricing: str = ""         # how the last price (fill or mark) was obtained
    iv: Optional[float] = None
    mark: Optional[float] = None
    mark_date: Optional[str] = None
    spot: Optional[float] = None
    contract: str = ""        # exchange contract symbol where known (US chains)
    reason: str = ""

    @property
    def units(self) -> int:
        return self.lots * self.lot_size

    @property
    def sign(self) -> int:
        return 1 if self.side == "LONG" else -1

    def value(self, mark: Optional[float] = None) -> float:
        m = self.entry_price if mark is None else mark
        if self.kind == "FUT":
            return self.margin + self.sign * (m - self.entry_price) * self.units
        if self.side == "LONG":
            return m * self.units
        return self.margin - m * self.units

    def pnl(self, mark: Optional[float] = None) -> float:
        m = self.entry_price if mark is None else mark
        return self.sign * (m - self.entry_price) * self.units


def position_id(underlying: str, kind: str, side: str, expiry=None, strike=None, opt_letters=("CE", "PE")) -> str:
    if kind == "STK":
        body = "STK"
    elif kind == "FUT":
        body = f"{expiry}:FUT"
    else:
        k = f"{strike:g}"
        body = f"{expiry}:{opt_letters[0] if kind == 'CALL' else opt_letters[1]}{k}"
    return f"{underlying}:{body}:{'L' if side == 'LONG' else 'S'}"


def _covers(cover: DPos, short: DPos) -> bool:
    if cover.underlying != short.underlying or cover is short:
        return False
    if short.kind == "CALL":
        if cover.kind == "STK":
            return cover.side == "LONG"
        if cover.kind in ("FUT", "CALL"):
            return cover.side == "LONG" and cover.expiry == short.expiry
    if short.kind == "PUT":
        if cover.kind == "STK":
            return cover.side == "SHORT"
        if cover.kind == "FUT":
            return cover.side == "SHORT" and cover.expiry == short.expiry
        if cover.kind == "PUT":
            return cover.side == "LONG" and cover.expiry == short.expiry
    return False


def allocate_cover(positions: list) -> dict:
    """{short option id: [(cover position, units)] or None if not fully covered}. Futures and stock are
    used before long options; each cover unit is used once."""
    left = {id(p): p.units for p in positions}
    out = {}
    shorts = [p for p in positions if p.kind in OPTION_KINDS and p.side == "SHORT"]
    for s in sorted(shorts, key=lambda p: (p.underlying, p.expiry or "", p.id)):
        need, used = s.units, []
        cands = [c for c in positions if _covers(c, s)]
        cands.sort(key=lambda c: (c.kind in OPTION_KINDS, c.id))
        for c in cands:
            if need <= 0:
                break
            take = min(need, left[id(c)])
            if take > 0:
                used.append((c, take))
                left[id(c)] -= take
                need -= take
        out[s.id] = used if need <= 0 else None
    return out


def short_option_margin(short: DPos, cover: list) -> float:
    """0 for the part covered by a future / stock; strike gap x units (the spread's max loss) for the part
    covered by a long option of the same right."""
    m = 0.0
    for c, u in cover or []:
        if c.kind in OPTION_KINDS:
            gap = (c.strike - short.strike) if short.kind == "CALL" else (short.strike - c.strike)
            m += max(gap, 0.0) * u
    return m


# ---------- the book ----------
class Reject(Exception):
    """The order is removed from the queue and recorded as rejected."""


class NotYet(Exception):
    """The order stays queued (no fill day / no quote yet)."""


TRADE_COLUMNS = ["date", "position", "underlying", "instrument", "action", "side", "lots", "units", "price",
                 "notional", "charges", "margin", "cash_change", "realised_pnl", "pricing", "label", "reason"]
EQUITY_COLUMNS = ["date", "cash", "margin", "positions_value", "equity", "equity_inr", "day_pnl", "positions",
                  "costs_paid"]


@dataclass
class DerivBook:
    cash: float
    starting_capital: float
    positions: dict = field(default_factory=dict)      # id -> DPos
    start_date: Optional[str] = None
    costs_paid: float = 0.0
    realised_pnl: float = 0.0
    breaker: dict = field(default_factory=dict)        # {} or {tripped_on, until, day_pnl, limit}
    last_run: dict = field(default_factory=dict)       # {date, notes} for the dashboard

    # set by subclasses
    _PORTFOLIO_FILE = ""
    _TRADES_FILE = ""
    _EQUITY_FILE = ""
    CURRENCY = "INR"

    def fx(self) -> float:
        """Book currency -> INR for this book's latest mark (the US book overrides it with its day rate)."""
        return float(config.FX_TO_INR[self.CURRENCY])

    def _extra_equity(self, row: dict, prev: Optional[dict]) -> dict:
        """Extra equity-curve columns (the US book adds its FX columns). None for this base book."""
        return {}

    @classmethod
    def initial_capital(cls) -> float:
        raise NotImplementedError

    @classmethod
    def _path(cls, name):
        return getattr(config, getattr(cls, name))

    @classmethod
    def load(cls):
        if not cls._path("_PORTFOLIO_FILE").exists():
            cap = cls.initial_capital()
            return cls(cash=cap, starting_capital=cap)
        d = json.loads(cls._path("_PORTFOLIO_FILE").read_text(encoding="utf-8"))
        d["positions"] = {k: DPos(**v) for k, v in d.get("positions", {}).items()}
        return cls(**d)

    def save(self) -> None:
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        d = asdict(self)
        self._path("_PORTFOLIO_FILE").write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")

    # ---------- valuation ----------
    def margin_used(self) -> float:
        return sum(p.margin for p in self.positions.values())

    def equity(self) -> float:
        return self.cash + sum(p.value(p.mark) for p in self.positions.values())

    # ---------- trading ----------
    def open(self, day: str, pos: DPos, price: float, chg, label: str = "") -> dict:
        """Add `pos` (lots, margin already set) at `price`. Merges into an existing position of the same id
        (same contract and side): weighted entry price, margins and charges add up."""
        units = pos.units
        if pos.kind == "FUT":
            delta = -(pos.margin + chg.total)
        elif pos.side == "LONG":
            delta = -(price * units + chg.total)
        else:
            delta = price * units - chg.total - pos.margin
        self.cash += delta
        self.costs_paid += chg.total
        pos.entry_price, pos.entry_charges = price, chg.total
        old = self.positions.get(pos.id)
        if old is not None:
            tot = old.units + units
            old.entry_price = (old.entry_price * old.units + price * units) / tot
            old.lots += pos.lots
            old.margin += pos.margin
            old.entry_charges += chg.total
            old.pricing, old.iv, old.reason = pos.pricing, pos.iv, pos.reason
            old.roll = old.roll or pos.roll
        else:
            pos.mark, pos.mark_date = price, day
            self.positions[pos.id] = pos
        return self._ledger(day, pos, "OPEN", "BUY" if pos.side == "LONG" else "SELL", pos.lots, price, chg.total,
                            pos.margin, delta, "", pos.pricing, label, pos.reason)

    def close(self, day: str, pid: str, lots: int, price: float, chg, action: str, pricing: str, reason: str,
              label: str = "") -> dict:
        p = self.positions[pid]
        lots = min(lots, p.lots)
        frac = lots / p.lots
        units = lots * p.lot_size
        margin = p.margin * frac
        if p.kind == "FUT":
            delta = margin + p.sign * (price - p.entry_price) * units - chg.total
        elif p.side == "LONG":
            delta = price * units - chg.total
        else:
            delta = margin - price * units - chg.total
        pnl = p.sign * (price - p.entry_price) * units - p.entry_charges * frac - chg.total
        self.cash += delta
        self.costs_paid += chg.total
        self.realised_pnl += pnl
        row = self._ledger(day, p, action, "SELL" if p.side == "LONG" else "BUY", lots, price, chg.total, margin,
                           delta, round(pnl, 2), pricing, label, reason)
        p.margin -= margin
        p.entry_charges -= p.entry_charges * frac
        p.lots -= lots
        if p.lots == 0:
            del self.positions[pid]
        return row

    def _ledger(self, day, p, action, side, lots, price, charges, margin, delta, pnl, pricing, label, reason) -> dict:
        units = lots * p.lot_size
        row = dict(date=day, position=p.id, underlying=p.underlying, instrument=self.instrument(p), action=action,
                   side=side, lots=lots, units=units, price=round(price, 4), notional=round(price * units, 2),
                   charges=round(charges, 2), margin=round(margin, 2), cash_change=round(delta, 2),
                   realised_pnl=pnl, pricing=pricing, label=label, reason=reason)
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        path = self._path("_TRADES_FILE")
        new = not path.exists()
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=TRADE_COLUMNS, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerow(row)
        return row

    @classmethod
    def instrument(cls, p: DPos) -> str:
        if p.kind == "STK":
            return f"{p.underlying} stock"
        if p.kind == "FUT":
            return f"{p.underlying} FUT {p.expiry}"
        return f"{p.underlying} {p.expiry} {p.strike:g} {p.kind}"

    @classmethod
    def trades(cls) -> pd.DataFrame:
        if not cls._path("_TRADES_FILE").exists():
            return pd.DataFrame(columns=TRADE_COLUMNS)
        return pd.read_csv(cls._path("_TRADES_FILE"))

    # ---------- equity curve and circuit breaker ----------
    @classmethod
    def equity_curve(cls) -> pd.DataFrame:
        if not cls._path("_EQUITY_FILE").exists():
            return pd.DataFrame(columns=EQUITY_COLUMNS)
        return pd.read_csv(cls._path("_EQUITY_FILE"))

    def record_equity(self, day: str) -> dict:
        curve = self.equity_curve()
        curve = curve[curve["date"].astype(str) != str(day)]
        prev = curve[curve["date"].astype(str) < str(day)]
        prev_eq = float(prev["equity"].iloc[-1]) if len(prev) else self.starting_capital
        eq = self.equity()
        row = dict(date=day, cash=round(self.cash, 2), margin=round(self.margin_used(), 2),
                   positions_value=round(eq - self.cash, 2), equity=round(eq, 2),
                   equity_inr=round(eq * self.fx(), 2), day_pnl=round(eq - prev_eq, 2),
                   positions=len(self.positions), costs_paid=round(self.costs_paid, 2))
        row.update(self._extra_equity(row, prev.iloc[-1].to_dict() if len(prev) else None))
        curve = pd.concat([curve, pd.DataFrame([row])], ignore_index=True) if len(curve) else pd.DataFrame([row])
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        curve.to_csv(self._path("_EQUITY_FILE"), index=False)
        return row

    def update_breaker(self, day: str, day_pnl: float) -> Optional[str]:
        """Trip the breaker on a day loss beyond the limit; clear it once its Monday has come. Returns a
        journal line when something changed."""
        limit = config.DERIV_DAILY_LOSS_PCT * self.starting_capital
        if day_pnl < -limit:
            if self.breaker.get("tripped_on") == day:
                return None
            self.breaker = dict(tripped_on=day, until=next_monday(day), day_pnl=round(day_pnl, 2), limit=round(limit, 2))
            return (f"CIRCUIT BREAKER TRIPPED: day P&L {day_pnl:,.2f} is a loss of more than "
                    f"{config.DERIV_DAILY_LOSS_PCT*100:.0f}% of starting capital ({limit:,.2f}). "
                    f"No new positions until {self.breaker['until']} (exits still allowed).")
        if self.breaker and self.breaker.get("tripped_on") == day:     # same-day re-run with a smaller loss
            self.breaker = {}
            return "Circuit breaker cleared: the re-run's day P&L is within the limit."
        if self.breaker and day >= self.breaker.get("until", ""):
            self.breaker = {}
            return "Circuit breaker expired: new positions allowed again."
        return None

    def blocked(self, fill_day: str) -> bool:
        return bool(self.breaker) and fill_day < self.breaker.get("until", "")

    def breaker_text(self) -> str:
        if not self.breaker:
            return f"off (trips on a day loss over {config.DERIV_DAILY_LOSS_PCT*100:.0f}% of starting capital)"
        b = self.breaker
        return f"ON since {b['tripped_on']} (day P&L {b['day_pnl']:,.2f}); no new positions until {b['until']}"

    # ---------- risk ----------
    def check_open(self, pos: DPos, price: float, chg_total: float, fill_day: str) -> None:
        """Rules 1-5 for an OPEN. Sets pos.margin for a short option. Raises Reject."""
        if self.blocked(fill_day):
            raise Reject(f"circuit breaker: no new positions until {self.breaker['until']}")
        merging = pos.id in self.positions
        if not merging and len(self.positions) >= config.DERIV_MAX_POSITIONS:
            raise Reject(f"max {config.DERIV_MAX_POSITIONS} open positions already held")
        if pos.kind in OPTION_KINDS and pos.side == "SHORT":
            trial = list(self.positions.values()) + ([] if merging else [pos])
            if merging:   # check the merged size
                trial = [p for p in trial if p.id != pos.id] + [DPos(**{**asdict(self.positions[pos.id]),
                                                                       "lots": self.positions[pos.id].lots + pos.lots})]
            alloc = allocate_cover(trial)
            mine = alloc.get(pos.id)
            if mine is None:
                raise Reject("naked short option: needs a future / long option of the same underlying and expiry"
                             + (" or the stock" if self.CURRENCY == "USD" else "")
                             + " covering every unit (a short CALL needs a LONG cover, a short PUT a SHORT future"
                             + ("/stock" if self.CURRENCY == "USD" else "") + " or a LONG put)")
            total = short_option_margin(next(p for p in trial if p.id == pos.id), mine)
            pos.margin = max(total - (self.positions[pos.id].margin if merging else 0.0), 0.0)
        units = pos.units
        if pos.kind == "FUT" or pos.side == "SHORT":
            risk, what = pos.margin, "margin"
        elif pos.kind == "STK":
            risk, what = price * units, "notional"
        else:
            risk, what = price * units, "premium"
        eq = self.equity()
        cap = config.DERIV_MAX_ORDER_RISK_PCT * eq
        if risk > cap + 1e-9:
            per_lot = risk / pos.lots
            raise Reject(f"per-order risk: {what} {risk:,.2f} is above {config.DERIV_MAX_ORDER_RISK_PCT*100:.0f}% "
                         f"of sleeve equity ({cap:,.2f}); max {int(cap // per_lot) if per_lot else 0} lot(s) at this price")
        if pos.kind == "FUT":
            need = pos.margin + chg_total
        elif pos.side == "LONG":
            need = price * units + chg_total
        else:
            need = pos.margin + chg_total - price * units
        if need > self.cash + 1e-9:
            raise Reject(f"free cash {self.cash:,.2f} is less than the {need:,.2f} this order blocks (margin/premium + charges)")

    def check_close(self, pid: str, lots: int) -> None:
        """Reject a close that would leave a short option uncovered."""
        p = self.positions[pid]
        if p.kind in OPTION_KINDS and p.side == "SHORT":
            return
        rest = [q for q in self.positions.values() if q.id != pid]
        if lots < p.lots:
            rest.append(DPos(**{**asdict(p), "lots": p.lots - lots}))
        bad = [k for k, v in allocate_cover(rest).items() if v is None]
        if bad:
            raise Reject(f"closing {pid} would leave {', '.join(bad)} naked; close the short option first")


def order_side(o: dict) -> str:
    s = str(o.get("side", "BUY")).strip().upper()
    if s not in ("BUY", "SELL"):
        raise Reject(f"side {o.get('side')!r}: use BUY or SELL")
    return s


def order_action(o: dict) -> str:
    a = str(o.get("action", "OPEN")).strip().upper()
    if a not in ("OPEN", "CLOSE"):
        raise Reject(f"action {o.get('action')!r}: use OPEN or CLOSE")
    return a


def find_position(book: DerivBook, o: dict) -> str:
    """A CLOSE names its position by id ("position"), or by underlying (+ instrument / strike / expiry)
    when that matches exactly one open position."""
    pid = o.get("position")
    if pid:
        if pid not in book.positions:
            raise Reject(f"no open position {pid}")
        return pid
    und = str(o.get("underlying", "")).strip().upper()
    inst = str(o.get("instrument", "")).strip().upper()
    inst = {"CE": "CALL", "PE": "PUT", "C": "CALL", "P": "PUT", "STOCK": "STK"}.get(inst, inst)
    hits = [p for p in book.positions.values() if p.underlying == und and (not inst or p.kind == inst)
            and (o.get("strike") in (None, "") or abs(float(o["strike"]) - (p.strike or 0)) < 1e-6)
            and (not o.get("expiry") or p.expiry == o.get("expiry"))]
    if len(hits) != 1:
        raise Reject(f"CLOSE needs \"position\" (an id from the journal): {len(hits)} open position(s) match")
    return hits[0].id


def load_queue(path) -> dict:
    if not path.exists():
        return {"orders": []}
    d = json.loads(path.read_text(encoding="utf-8"))
    d.setdefault("orders", [])
    return d


def save_queue(path, d: dict) -> None:
    config.DECISIONS_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")


def record_executed(prefix: str, day: str, entries: list) -> None:
    if not entries:
        return
    path = config.DECISIONS_DIR / f"{prefix}_executed_{day}.json"
    old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    path.write_text(json.dumps(old + entries, indent=2, default=str) + "\n", encoding="utf-8")


def run_orders(book: DerivBook, orders: list, market, market_day: str) -> tuple[list, list, list, list]:
    """The order loop shared by both sleeves. `market` supplies fill_day(o), open_contract(o, fill_day, side) ->
    (DPos with side set, price, Charges, label; a short option's margin is set by the risk check), pos_id(pos) and close_price(pos, o, fill_day) -> (price,
    Charges, pricing, label). Order: CLOSEs of short options, other CLOSEs, then OPENs of covers
    (futures, stock, long options) before short options. Returns (fills, notes, kept, executed)."""
    fills, notes, keep, executed = [], [], [], []

    def done(o, status, detail, fill=None):
        executed.append(dict(order=o, status=status, detail=detail, fill=fill))
        notes.append(f"{o.get('action', 'OPEN')} {o.get('side', '')} {o.get('position') or o.get('underlying', '?')}"
                     f" {o.get('instrument', '')}: {status} - {detail}".replace("  ", " "))

    def rank(o):
        a = str(o.get("action", "OPEN")).upper()
        inst = str(o.get("instrument", "")).upper()
        short_opt = str(o.get("side", "")).upper() == "SELL" and inst in ("CE", "PE", "CALL", "PUT")
        p = book.positions.get(str(o.get("position", "")))
        if a == "CLOSE":
            return 0 if p is not None and p.kind in OPTION_KINDS and p.side == "SHORT" else 1
        return 3 if short_opt else 2

    for o in sorted(orders, key=rank):
        try:
            action = order_action(o)
            if not o.get("decision_date"):
                o["decision_date"] = market_day
                notes.append(f"order for {o.get('position') or o.get('underlying')}: no decision_date; stamped {market_day}")
                keep.append(o)
                continue
            fill_day = market.fill_day(o)
            if fill_day is None:
                keep.append(o)
                continue
            if action == "CLOSE":
                pid = find_position(book, o)
                lots = int(o.get("lots") or book.positions[pid].lots)
                if lots <= 0:
                    raise Reject("lots must be positive")
                book.check_close(pid, lots)
                price, chg, pricing, label = market.close_price(book.positions[pid], o, fill_day)
                f = book.close(fill_day, pid, lots, price, chg, "CLOSE", pricing, str(o.get("reason", "")), label)
                fills.append(f)
                done(o, "filled", f"closed {lots} lot(s) of {pid} @ {price:,.2f} ({pricing}); realised {f['realised_pnl']:,.2f}", f)
                continue
            if not str(o.get("reason", "")).strip():
                raise Reject("an OPEN order needs a written \"reason\"")
            if not str(o.get("risk_check", "")).strip():
                raise Reject("an OPEN order needs \"risk_check\": the risk rule(s) the decision checked")
            side = order_side(o)
            lots = int(o.get("lots") or 0)
            if lots <= 0 and not o.get("amount"):
                raise Reject("give lots (contracts / shares) as a positive whole number")
            pos, price, chg, label = market.open_contract(o, fill_day, "LONG" if side == "BUY" else "SHORT")
            if pos.lots <= 0:
                raise Reject("give lots (contracts / shares) as a positive whole number")
            pos.id = market.pos_id(pos)
            pos.reason = str(o.get("reason", ""))
            if pos.id not in book.positions:
                opp = pos.id[:-1] + ("S" if pos.side == "LONG" else "L")
                if opp in book.positions:
                    raise Reject(f"{opp} is open on the other side; CLOSE it instead")
            book.check_open(pos, price, chg.total, fill_day)
            f = book.open(fill_day, pos, price, chg, label)
            fills.append(f)
            done(o, "filled", f"opened {pos.id} x{pos.lots} @ {price:,.2f} ({pos.pricing}); margin {pos.margin:,.2f}", f)
        except NotYet as e:
            keep.append(o)
            notes.append(f"order for {o.get('position') or o.get('underlying')}: stays queued - {e}")
        except Reject as e:
            done(o, "rejected", str(e))
        except Exception as e:   # a malformed order must not stop the run
            log.exception("order failed")
            done(o, "rejected", f"error: {e}")
    return fills, notes, keep, executed
