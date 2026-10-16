"""Thesis sleeve: macro / economic-theme positions held for weeks to years, each with a written thesis.

A separate book from the momentum sleeve. It has its own capital (config.THESIS_CAPITAL), its own state
files (state/thesis_*), its own order queue (decisions/thesis_orders.json) and its own weekly review
(THESIS_REVIEW.md). It shares the cost model (costs.charges), the slippage assumption, the daily run and
the dashboard. Nothing here reads or writes the momentum sleeve's files.

Ledger: one file per thesis, theses/<slug>.md, with front matter between two `---` lines:

    ---
    symbol: HAL                       any NSE symbol (Yahoo SYMBOL.NS), not limited to Nifty 50
    market:                           optional: US for a US listing (symbol: META -> Yahoo META, prices in USD)
    thesis: one line, the macro/economic reason
    entry_date:                       filled in by the engine on the first fill
    horizon: 12-24 months             weeks / months / years, free text
    target: 5200                      a number (price) is tracked; text is shown as-is
    invalidation: close below 3800 or defence capex cut in the budget
    invalidation_price: 3800          optional; a number here (or a pure number in `invalidation`)
                                      makes the engine queue a SELL when the close is at or below it
    size_cap_pct: 20                  max % of sleeve equity in this thesis; never above 25
    status: proposed                  proposed -> open (engine, on first fill) -> closed (engine, on full exit)
                                      (example = a worked example, never executed)
    type: theme                       theme (macro, weeks to years), catalyst (a scheduled event, days to
                                      weeks) or second_order (an event with legs on both sides, below)
    ---

A symbol may also be an explicit Yahoo ticker containing a '.' (7203.T), which then needs `currency:`
(a key of config.FX_TO_INR). Foreign prices stay in their own currency; cash, P&L and sleeve equity are
in INR at the fixed rate in config (USD_INR).

A catalyst thesis also needs (the run rejects the file without them):

    event: Q2 results                 one line: Q2 results, Union Budget, IPO listing, RBI policy ...
    event_date: 2026-10-24            ISO date of the event
    post_event_rule: sell at next open regardless      what to do the day after the event
    post_event_applied:               set by the reviewer (date + what was done) once the rule is applied
    rule_due_date:                    optional; the rule falls due after this date instead of event_date

The engine does not interpret post_event_rule (it is free text). It counts days to the event, and from
the first trading day after event_date (or rule_due_date) it flags the thesis as "CATALYST RULE DUE" in
the journal until post_event_applied is filled in, an exit order (SELL / COVER) is queued for every held
leg, or it is closed.

A second_order thesis (an event, the obvious winner, the non-obvious losers) has the catalyst fields
above, no `symbol`, and two or more legs instead:

    legs:
      - symbol: META
        market: US
        side: LONG                    LONG (the winner) or SHORT (a loser)
        weight: 0.5                   share of the thesis' size cap; weights are normalised to sum to 1
        rationale: gains rental income from GPUs it already owns
      - symbol: CRWV
        market: US
        side: SHORT
        weight: 0.25
        rationale: a hyperscaler renting out spare GPUs competes with its core business

A SHORT is a PAPER short: India's cash market allows shorting only intraday, so holding one needs a stock
future (F&O). Every short is labelled "PAPER SHORT: needs F&O to execute" in the ledger, journal and
dashboard and is charged as a Zerodha futures trade (costs.py, product FUT). The proceeds are credited
to cash at entry but cannot fund a BUY (buys are limited to cash minus the value of open shorts), and
total short notional may not exceed config.THESIS_MAX_SHORT_PCT (50%) of the sleeve's capital.

    Free text below: the long-form argument, evidence, what would change the view, review log.

Files whose name starts with "_" (e.g. _TEMPLATE.md) are ignored.

Orders: decisions/thesis_orders.json, written by Claude (or you):

    {"orders": [{"thesis": "hal-defence", "side": "BUY", "amount": 40000, "decision_date": "2026-10-16",
                 "reason": "open thesis"}]}

  thesis        slug of theses/<slug>.md
  leg           the leg's symbol (META, CRWV ...); required when the thesis has more than one leg
  side          BUY / SELL (a LONG leg) or SHORT / COVER (a SHORT leg)
  qty / amount  optional. BUY / SHORT: qty shares, or Rs amount, or (neither) the leg's share of the
                thesis' size cap. SELL / COVER: qty shares, or (neither) the whole position.
  decision_date fills at the leg's first open AFTER this date (each listing on its own calendar). If
                missing, the engine stamps today's market date, so the order fills at the next open.

A BUY or SHORT is always clipped to the size cap (gross value of all the thesis' legs <= cap% of sleeve
equity at the fill price); a BUY also to free cash, a SHORT also to the short limit. Filled or rejected
orders leave the queue and are recorded in decisions/thesis_executed_<date>.json; orders that cannot fill
yet stay queued. Fill order on a day: SELL, COVER, SHORT, BUY.
"""
from __future__ import annotations

import csv
import json
import logging
import math
import re
from dataclasses import dataclass, field, asdict
from datetime import date as _date
from pathlib import Path

import pandas as pd

from . import config, data
from .costs import charges
from .portfolio import Portfolio, Position, TRADE_COLUMNS
from .universe import yahoo_symbol

log = logging.getLogger(__name__)

FIELDS = ["symbol", "market", "currency", "thesis", "entry_date", "horizon", "target", "invalidation",
          "invalidation_price", "size_cap_pct", "status", "type", "event", "event_date", "post_event_rule",
          "post_event_applied", "rule_due_date", "legs"]
STATUSES = ("proposed", "open", "closed", "example")
TYPES = ("theme", "catalyst", "second_order")
EVENT_TYPES = ("catalyst", "second_order")
LEG_SIDES = ("LONG", "SHORT")
ORDER_SIDES = ("BUY", "SELL", "SHORT", "COVER")
EXIT_SIDES = ("SELL", "COVER")
PAPER_SHORT = "PAPER SHORT: needs F&O to execute"


def fx_rate(currency: str) -> float:
    return float(config.FX_TO_INR[currency])


def display_symbol(key: str) -> str:
    return f"{key[3:]} (US)" if key.startswith("US:") else key


@dataclass
class ThesisPosition(Position):
    """A Position that may be short (qty < 0) and priced in another currency. avg_cost is per share in the
    listing's currency: for a long, the fill price plus buy charges; for a short, the fill price minus the
    entry charges (net proceeds per share). P&L in INR = (close - avg_cost) * qty * fx for both."""
    currency: str = "INR"
    thesis: str = ""


class ThesisPortfolio(Portfolio):
    """Cash, charges, avg cost and ledgers like the momentum Portfolio, pointed at the thesis files, plus
    paper shorts and foreign-currency legs. The momentum Portfolio class itself is not changed."""
    _PORTFOLIO_FILE = "THESIS_PORTFOLIO_FILE"
    _TRADES_FILE = "THESIS_TRADES_FILE"
    _EQUITY_FILE = "THESIS_EQUITY_FILE"
    _CAPITAL = "THESIS_CAPITAL"
    TRADE_COLUMNS = TRADE_COLUMNS + ["thesis", "leg_side", "currency", "fx", "label"]

    @classmethod
    def load(cls) -> "ThesisPortfolio":
        capital = getattr(config, cls._CAPITAL)
        if not cls._path("_PORTFOLIO_FILE").exists():
            return cls(cash=capital, starting_capital=capital)
        d = json.loads(cls._path("_PORTFOLIO_FILE").read_text())
        d["positions"] = {k: ThesisPosition(**v) for k, v in d["positions"].items()}
        return cls(**d)

    # ---------- valuation (INR) ----------
    def market_value(self, closes: dict) -> float:
        """Net INR value: longs positive, shorts negative (the cost of buying them back)."""
        return sum(p.qty * closes.get(s, p.avg_cost) * fx_rate(getattr(p, "currency", "INR"))
                   for s, p in self.positions.items())

    def short_notional(self, prices: dict) -> float:
        """INR value of all open shorts at `prices` (avg cost where a price is missing)."""
        return sum(-p.qty * prices.get(s, p.avg_cost) * fx_rate(getattr(p, "currency", "INR"))
                   for s, p in self.positions.items() if p.qty < 0)

    # ---------- trading ----------
    def trade(self, day: str, symbol: str, side: str, qty: int, price: float, reason: str, *,
              thesis: str = "", currency: str = "INR", stop: float = 0.0) -> dict:
        """BUY / SELL a long, SHORT / COVER a paper short. `price` is in the listing's currency; cash moves
        in INR at the fixed config rate. Longs use the CNC charges, shorts the FUT (futures) charges."""
        side = side.upper()
        fx = fx_rate(currency)
        px_inr = price * fx
        product = "FUT" if side in ("SHORT", "COVER") else config.PRODUCT
        c = charges("SELL" if side in ("SELL", "SHORT") else "BUY", qty, px_inr, product)
        gross = qty * px_inr
        p = self.positions.get(symbol)
        pnl = ""
        if side == "BUY":
            if p is not None and p.qty < 0:
                raise ValueError(f"{symbol} is held short; COVER it first")
            total = gross + c.total
            if total > self.cash + 1e-6:
                raise ValueError(f"insufficient cash for {symbol}: need {total:.2f}, have {self.cash:.2f}")
            self.cash -= total
            net_cash = -total
            if p is not None:
                new_qty = p.qty + qty
                p.avg_cost = (p.avg_cost * p.qty + total / fx) / new_qty
                p.qty = new_qty
                p.highest_close = max(p.highest_close, price)
            else:
                self.positions[symbol] = ThesisPosition(symbol=symbol, qty=qty, avg_cost=total / qty / fx,
                                                        entry_date=day, stop=stop, highest_close=price,
                                                        currency=currency, thesis=thesis)
        elif side == "SELL":
            if p is None or p.qty <= 0 or qty > p.qty:
                raise ValueError(f"cannot sell {qty} {symbol}: not held long")
            net = gross - c.total
            self.cash += net
            net_cash = net
            pnl = net - p.avg_cost * qty * fx
            p.qty -= qty
        elif side == "SHORT":
            if p is not None and p.qty > 0:
                raise ValueError(f"{symbol} is held long; SELL it first")
            net = gross - c.total
            self.cash += net
            net_cash = net
            if p is not None:
                new_short = -p.qty + qty
                p.avg_cost = (p.avg_cost * -p.qty + net / fx) / new_short
                p.qty = -new_short
            else:
                self.positions[symbol] = ThesisPosition(symbol=symbol, qty=-qty, avg_cost=net / qty / fx,
                                                        entry_date=day, stop=stop, highest_close=price,
                                                        currency=currency, thesis=thesis)
        elif side == "COVER":
            if p is None or p.qty >= 0 or qty > -p.qty:
                raise ValueError(f"cannot cover {qty} {symbol}: not held short")
            total = gross + c.total          # a cover is never blocked for cash: the loss is real either way
            self.cash -= total
            net_cash = -total
            pnl = p.avg_cost * qty * fx - total
            p.qty += qty
        else:
            raise ValueError(f"unknown side {side}")
        self.costs_paid += c.total
        if pnl != "":
            self.realised_pnl += pnl
            pnl = round(pnl, 2)
        if symbol in self.positions and self.positions[symbol].qty == 0:
            del self.positions[symbol]
        short = side in ("SHORT", "COVER")
        row = dict(date=day, symbol=symbol, side=side, qty=qty, price=round(price, 2), gross=round(gross, 2),
                   charges=c.total, net_cash=round(net_cash, 2), product=product,
                   reason=(f"{PAPER_SHORT} | {reason}" if short else reason), realised_pnl=pnl,
                   thesis=thesis, leg_side="SHORT" if short else "LONG", currency=currency, fx=fx,
                   label=PAPER_SHORT if short else "")
        self._append_trade(row)
        return row

    @classmethod
    def _append_trade(cls, row: dict) -> None:
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        path = cls._path("_TRADES_FILE")
        cols = cls.TRADE_COLUMNS
        if path.exists():               # keep an older file's header rather than misalign its columns
            with open(path, newline="") as f:
                cols = next(csv.reader(f), None) or cols
        new = not path.exists()
        with open(path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerow(row)


# ---------- ledger ----------
@dataclass
class Leg:
    symbol: str          # internal key: HAL, US:META, 7203.T
    side: str            # LONG / SHORT
    weight: float = 1.0
    rationale: str = ""
    currency: str = "INR"

    @property
    def display(self) -> str:
        return display_symbol(self.symbol)

    @property
    def role(self) -> str:
        return "winner" if self.side == "LONG" else "loser"


def leg_key(raw, market="", currency="") -> tuple[str, str]:
    """(internal symbol key, currency) for a symbol as written in a thesis file."""
    raw = str(raw or "").strip().upper()
    market = str(market or "").strip().upper()
    currency = str(currency or "").strip().upper()
    if not raw:
        raise ValueError("missing symbol")
    if raw.startswith("US:"):
        raw, market = raw[3:], "US"
    if market == "US":
        if "." in raw:
            raise ValueError(f"{raw}: market: US takes the plain Yahoo ticker (e.g. META, BRK-B)")
        key, ccy = "US:" + raw, "USD"
    elif market not in ("", "NSE"):
        raise ValueError(f"market {market!r}: use NSE (default) or US, or an explicit Yahoo ticker with a '.' and a currency")
    elif raw.endswith(".NS"):
        key, ccy = raw[:-3], "INR"
    elif raw.endswith(".BO"):
        key, ccy = raw, "INR"
    elif "." in raw:
        if not currency:
            raise ValueError(f"{raw}: an explicit Yahoo ticker needs currency: (one of {', '.join(config.FX_TO_INR)})")
        key, ccy = raw, currency
    else:
        key, ccy = raw, "INR"
    if currency and currency != ccy:
        raise ValueError(f"{raw}: currency {currency} does not match the listing ({ccy})")
    if ccy not in config.FX_TO_INR:
        raise ValueError(f"{raw}: currency {ccy} has no rate in config.FX_TO_INR")
    return key, ccy


@dataclass
class Thesis:
    slug: str
    path: Path
    fields: dict = field(default_factory=dict)
    body: str = ""
    legs: list = field(default_factory=list)

    def get(self, key, default=""):
        return self.fields.get(key, default)

    @property
    def symbol(self) -> str:
        """The single symbol's key for theme/catalyst theses; "" for a second_order thesis (use legs)."""
        return self.legs[0].symbol if self.legs and self.type != "second_order" else ""

    @property
    def label(self) -> str:
        """Human-readable symbols: HAL, META (US), or 'META (US) vs CRWV (US), NBIS (US)'."""
        if self.type != "second_order":
            return self.legs[0].display if self.legs else ""
        longs = [l.display for l in self.legs if l.side == "LONG"]
        shorts = [l.display for l in self.legs if l.side == "SHORT"]
        return " vs ".join(x for x in (", ".join(longs), ", ".join(shorts)) if x)

    @property
    def status(self) -> str:
        return str(self.get("status", "proposed")).strip().lower() or "proposed"

    @property
    def type(self) -> str:
        return str(self.get("type", "theme")).strip().lower() or "theme"

    @property
    def event_date(self):
        v = str(self.get("event_date", "")).strip()
        return _date.fromisoformat(v) if v else None

    @property
    def rule_due_date(self):
        v = str(self.get("rule_due_date", "")).strip()
        return _date.fromisoformat(v) if v else self.event_date

    @property
    def invalidation_price(self):
        if self.type == "second_order":
            return None          # legs on both sides: one price cannot invalidate the pair
        return _num(self.get("invalidation_price")) or _num(self.get("invalidation"))

    @property
    def target_price(self):
        return None if self.type == "second_order" else _num(self.get("target"))

    @property
    def cap_pct(self) -> float:
        """Size cap as a fraction of sleeve equity: the file's size_cap_pct, never above the hard cap.
        For a second_order thesis it caps the gross value of all legs together."""
        v = _num(str(self.get("size_cap_pct", "")).replace("%", ""))
        hard = config.THESIS_MAX_POSITION_PCT
        return hard if v is None or v <= 0 else min(v / 100, hard)

    def find_leg(self, name) -> Leg | None:
        if not name:
            return self.legs[0] if len(self.legs) == 1 else None
        n = str(name).strip().upper()
        for l in self.legs:
            if n in (l.symbol, l.display.upper(), l.symbol.split(":", 1)[-1], l.symbol + ".NS"):
                return l
        return None


_NUM_RE = re.compile(r"^\s*(?:rs\.?|inr|₹)?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*$", re.I)


def _num(v):
    """A value counts as numeric only if it is a bare number (optionally with Rs / commas). Text such as
    'close below 3800 or budget cut' is a condition for the reviewer, not a price for the engine."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(v) else None
    m = _NUM_RE.match(str(v))
    return float(m.group(1).replace(",", "")) if m else None


def _split_front_matter(text: str):
    """Return (front-matter lines, body) or (None, text) if the file has no front matter."""
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        return None, text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return lines[1:i], "\n".join(lines[i + 1:])
    return None, text


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def _parse_fields(fm: list[str]) -> tuple[dict, list | None]:
    """Flat `key: value` lines, plus one `legs:` block of indented `- key: value` items."""
    fields, legs, in_legs, cur = {}, None, False, None
    for line in fm:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if in_legs and line[:1] in (" ", "\t"):
            item = line.strip()
            if item.startswith("-"):
                cur = {}
                legs.append(cur)
                item = item[1:].strip()
                if not item:
                    continue
            elif cur is None:
                raise ValueError(f"legs: each leg starts with '- ': {line!r}")
            if ":" not in item:
                raise ValueError(f"leg line is not 'key: value': {line!r}")
            k, v = item.split(":", 1)
            cur[k.strip().lower()] = _unquote(v)
            continue
        in_legs = False
        if ":" not in line:
            raise ValueError(f"front matter line is not 'key: value': {line!r}")
        k, v = line.split(":", 1)
        k = k.strip().lower()
        if k == "legs":
            if v.strip():
                raise ValueError("legs: put each leg on its own indented lines starting with '- '")
            legs, in_legs, cur = [], True, None
            continue
        fields[k] = _unquote(v)
    return fields, legs


def _parse_legs(raw: list[dict]) -> list[Leg]:
    out = []
    for i, d in enumerate(raw, 1):
        try:
            key, ccy = leg_key(d.get("symbol"), d.get("market"), d.get("currency"))
            side = str(d.get("side", "")).strip().upper()
            if side not in LEG_SIDES:
                raise ValueError(f"side {d.get('side')!r} is not LONG or SHORT")
            w = _num(d.get("weight")) if str(d.get("weight", "")).strip() else 1.0
            if w is None or w <= 0:
                raise ValueError(f"weight {d.get('weight')!r} is not a positive number")
            if not str(d.get("rationale", "")).strip():
                raise ValueError("missing rationale (why this leg gains or loses from the event)")
        except ValueError as e:
            raise ValueError(f"leg {i}: {e}")
        out.append(Leg(symbol=key, side=side, weight=w, rationale=str(d.get("rationale")).strip(), currency=ccy))
    keys = [l.symbol for l in out]
    dup = sorted({k for k in keys if keys.count(k) > 1})
    if dup:
        raise ValueError(f"legs name {', '.join(dup)} more than once")
    return out


def parse_thesis(path: Path) -> Thesis:
    fm, body = _split_front_matter(path.read_text(encoding="utf-8"))
    if fm is None:
        raise ValueError("no front matter (the file must start with a --- line)")
    fields, raw_legs = _parse_fields(fm)
    t = Thesis(slug=path.stem, path=path, fields=fields, body=body)
    if t.status not in STATUSES:
        raise ValueError(f"status {t.status!r} is not one of {', '.join(STATUSES)}")
    if t.type not in TYPES:
        raise ValueError(f"type {t.type!r} is not one of {', '.join(TYPES)}")
    if t.type == "second_order":
        if str(t.get("symbol", "")).strip():
            raise ValueError("a second_order thesis names its symbols under legs:, not symbol:")
        t.legs = _parse_legs(raw_legs or [])
        if len(t.legs) < 2:
            raise ValueError("a second_order thesis needs at least two legs (the winner and a loser)")
    else:
        if raw_legs is not None:
            raise ValueError("legs: is only for type: second_order")
        key, ccy = leg_key(t.get("symbol"), t.get("market"), t.get("currency"))
        t.legs = [Leg(symbol=key, side="LONG", weight=1.0, rationale=t.get("thesis"), currency=ccy)]
    if t.type in EVENT_TYPES:
        try:
            ed = t.event_date
        except ValueError:
            raise ValueError(f"event_date {t.get('event_date')!r} is not an ISO date (YYYY-MM-DD)")
        try:
            t.rule_due_date
        except ValueError:
            raise ValueError(f"rule_due_date {t.get('rule_due_date')!r} is not an ISO date (YYYY-MM-DD)")
        missing = [k for k, ok in (("event", t.get("event")), ("event_date", ed),
                                   ("post_event_rule", t.get("post_event_rule"))) if not ok]
        if missing:
            raise ValueError(f"{t.type} thesis is missing {', '.join(missing)}")
    return t


def load_theses() -> tuple[dict, list[str]]:
    """Return ({slug: Thesis}, warnings). Unparseable files are skipped with a warning, never fatal."""
    out, warnings = {}, []
    if not config.THESES_DIR.exists():
        return out, warnings
    for p in sorted(config.THESES_DIR.glob("*.md")):
        if p.name.startswith("_"):
            continue
        try:
            out[p.stem] = parse_thesis(p)
        except Exception as e:  # a typo in one file must not stop the run
            warnings.append(f"theses/{p.name} skipped: {e}")
            log.warning("theses/%s skipped: %s", p.name, e)
    return out, warnings


def update_front_matter(path: Path, updates: dict) -> None:
    """Set top-level keys in a thesis file's front matter in place, keeping every other line (including
    the indented legs: block) and the body."""
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    lines = text.split("\n")
    end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    for k, v in updates.items():
        for i in range(1, end):
            if lines[i][:1] not in (" ", "\t") and ":" in lines[i] and lines[i].split(":", 1)[0].strip().lower() == k:
                lines[i] = f"{k}: {v}"
                break
        else:
            lines.insert(end, f"{k}: {v}")
            end += 1
    path.write_text("\n".join(lines), encoding="utf-8")


# ---------- orders ----------
def load_orders() -> dict:
    if not config.THESIS_ORDERS_FILE.exists():
        return {"orders": []}
    d = json.loads(config.THESIS_ORDERS_FILE.read_text(encoding="utf-8"))
    d.setdefault("orders", [])
    return d


def save_orders(d: dict) -> None:
    config.DECISIONS_DIR.mkdir(parents=True, exist_ok=True)
    config.THESIS_ORDERS_FILE.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")


def _record_executed(day: str, entries: list[dict]) -> None:
    if not entries:
        return
    path = config.DECISIONS_DIR / f"thesis_executed_{day}.json"
    old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    path.write_text(json.dumps(old + entries, indent=2, default=str) + "\n", encoding="utf-8")


def _order_leg(t: Thesis, o: dict) -> Leg | None:
    return t.find_leg(o.get("leg") or o.get("symbol"))


# ---------- prices ----------
def needed_symbols(theses: dict, pf: ThesisPortfolio, orders: list[dict]) -> list[str]:
    syms = {l.symbol for t in theses.values() if t.status in ("proposed", "open") for l in t.legs}
    syms |= set(pf.positions)
    for o in orders:
        t = theses.get(o.get("thesis"))
        if t is not None:
            syms |= {l.symbol for l in t.legs}
    return sorted(syms)


def load_prices(main_prices: pd.DataFrame, symbols: list[str], do_update: bool) -> pd.DataFrame:
    """Long price frame for `symbols`, capped at the momentum sleeve's market date. Nifty 50 names are taken
    from the momentum download; others (any NSE symbol, US:XXX, explicit Yahoo tickers) are downloaded
    (do_update) into data_cache/thesis_prices.parquet, or read from that cache. Prices are in each
    listing's own currency. Symbols with no data are listed in .attrs["dropped"]."""
    market_day = main_prices["date"].max()
    in_main = set(main_prices["symbol"].unique())
    frames = [main_prices[main_prices["symbol"].isin([s for s in symbols if s in in_main])]]
    extra = [s for s in symbols if s not in in_main]
    if extra:
        ex = None
        if do_update:
            ex = data.download_symbols(extra, allow_empty=True)
            config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
            if not ex.empty:
                ex.to_parquet(config.THESIS_PRICES_FILE, index=False)
        elif config.THESIS_PRICES_FILE.exists():
            ex = pd.read_parquet(config.THESIS_PRICES_FILE)
        if ex is not None and not ex.empty:
            frames.append(ex[ex["symbol"].isin(extra)])
    out = pd.concat([f for f in frames if not f.empty], ignore_index=True) if any(not f.empty for f in frames) \
        else main_prices.iloc[0:0]
    out = out[out["date"] <= market_day]
    present = set(out["symbol"].unique())
    out.attrs["dropped"] = [s for s in symbols if s not in present]
    return out


# ---------- the daily step ----------
def _day(ts) -> str:
    return pd.Timestamp(ts).strftime("%Y-%m-%d")


def _px_dict(row: pd.Series) -> dict:
    return {k: float(v) for k, v in row.items() if v is not None and not pd.isna(v)}


def execute_orders(pf: ThesisPortfolio, theses: dict, prices: pd.DataFrame, market_day: str) -> tuple[list, list]:
    """Fill queued thesis orders at each leg's first open after the order's decision_date. Returns (fills, notes)."""
    book = load_orders()
    orders = book["orders"]
    if not orders:
        return [], []
    opens = data.pivot(prices, "open") if not prices.empty else pd.DataFrame()
    opens_ff = opens.ffill()          # sizing values every leg at its latest open on or before the fill day
    keep, fills, notes, executed = [], [], [], []

    def done(o, status, detail, fill=None):
        executed.append(dict(order=o, status=status, detail=detail, fill=fill))
        notes.append(f"{o.get('side', '?')} {o.get('thesis', '?')}: {status} - {detail}")

    # exits first so their cash is available to buys, as in the momentum sleeve
    rank = {"SELL": 0, "COVER": 1, "SHORT": 2, "BUY": 3}
    ordered = sorted(orders, key=lambda o: rank.get(str(o.get("side", "")).upper(), 4))
    for o in ordered:
        side = str(o.get("side", "")).upper()
        t = theses.get(o.get("thesis"))
        if side not in ORDER_SIDES:
            done(o, "rejected", f"side must be one of {', '.join(ORDER_SIDES)}, got {o.get('side')!r}")
            continue
        if t is None:
            done(o, "rejected", f"no thesis file theses/{o.get('thesis')}.md")
            continue
        if t.status == "example":
            done(o, "rejected", "status is example: a worked example is never executed")
            continue
        leg = _order_leg(t, o)
        if leg is None:
            done(o, "rejected", f"leg {o.get('leg') or o.get('symbol')!r} not found; thesis legs: "
                                f"{', '.join(l.display for l in t.legs)}" if (o.get("leg") or o.get("symbol"))
                 else f"thesis has {len(t.legs)} legs; give \"leg\" (one of {', '.join(l.display for l in t.legs)})")
            continue
        if not o.get("decision_date"):
            o["decision_date"] = market_day          # fills at the next open, never at one already past
            keep.append(o)
            notes.append(f"{side} {t.slug}: no decision_date; stamped {market_day}, fills at the next open")
            continue
        decided = pd.Timestamp(o["decision_date"])
        if len(opens) == 0 or not (opens.index > decided).any():
            keep.append(o)
            continue
        sym = leg.symbol
        col = opens[sym].dropna() if sym in opens.columns else pd.Series(dtype=float)
        later = col.index[col.index > decided]
        if len(later) == 0:
            keep.append(o)
            notes.append(f"{side} {t.slug}: no open price for {sym} since the {o['decision_date']} decision; order stays queued")
            continue
        fill_day = later[0]
        px = float(col.loc[fill_day])
        fx = fx_rate(leg.currency)
        ccy = "" if leg.currency == "INR" else f" {leg.currency}"
        show = leg.display
        reason = (f"thesis {t.slug}: " + str(o.get("reason", ""))).strip()
        pos = pf.positions.get(sym)

        if side in EXIT_SIDES:
            if side == "SELL" and (pos is None or pos.qty <= 0):
                done(o, "rejected", f"{sym} not held" if pos is None else f"{sym} is held short; use COVER")
                continue
            if side == "COVER" and (pos is None or pos.qty >= 0):
                done(o, "rejected", f"{sym} not held short" if pos is None or pos.qty == 0 else f"{sym} is held long; use SELL")
                continue
            held = abs(pos.qty)
            px = round(px * (1 - config.SLIPPAGE_PCT), 2) if side == "SELL" else round(px * (1 + config.SLIPPAGE_PCT), 2)
            qty = min(int(o["qty"]), held) if o.get("qty") else held
            f = pf.trade(_day(fill_day), sym, side, qty, px, reason, thesis=t.slug, currency=leg.currency)
            fills.append(f)
            done(o, "filled", f"{side} {qty} {show} @ {px}{ccy}" + (f"  {PAPER_SHORT}" if side == "COVER" else ""), f)
            if not any(l.symbol in pf.positions for l in t.legs):
                update_front_matter(t.path, {"status": "closed", "exit_date": _day(fill_day)})
                t.fields["status"] = "closed"
            continue

        # BUY / SHORT
        if t.status == "closed":
            done(o, "rejected", "thesis status is closed")
            continue
        want_side = "LONG" if side == "BUY" else "SHORT"
        if leg.side != want_side:
            done(o, "rejected", f"leg {show} is {leg.side}; use {'BUY' if leg.side == 'LONG' else 'SHORT'}")
            continue
        if side == "BUY" and pos is not None and pos.qty < 0:
            done(o, "rejected", f"{sym} is held short; COVER it first")
            continue
        if side == "SHORT" and pos is not None and pos.qty > 0:
            done(o, "rejected", f"{sym} is held long; SELL it first")
            continue
        px = round(px * (1 + config.SLIPPAGE_PCT), 2) if side == "BUY" else round(px * (1 - config.SLIPPAGE_PCT), 2)
        unit = px * fx                                    # INR per share
        marks = _px_dict(opens_ff.loc[fill_day])      # sleeve valued at the fill day's opens (as before)
        equity = pf.equity(marks)
        marks[sym] = px                                # this leg's own exposure at its fill price
        held = abs(pos.qty) if pos is not None else 0
        others = sum(abs(pf.positions[l.symbol].qty) * marks.get(l.symbol, pf.positions[l.symbol].avg_cost) * fx_rate(l.currency)
                     for l in t.legs if l.symbol != sym and l.symbol in pf.positions)
        cap = t.cap_pct * equity
        room = cap - others - held * unit
        share = leg.weight / sum(l.weight for l in t.legs)
        if o.get("qty"):
            want = int(o["qty"]) * unit
        elif o.get("amount"):
            want = float(o["amount"])
        else:
            want = share * cap - held * unit
        if side == "BUY":
            free = pf.cash - pf.short_notional(marks)     # short proceeds do not fund longs
            budget = min(want, room, free)
            qty = int(budget // unit)
            while qty > 0 and qty * unit + charges("BUY", qty, unit).total > free:
                qty -= 1
            limit_txt = f"cash Rs {free:,.0f}"
        else:
            limit = config.THESIS_MAX_SHORT_PCT * min(pf.starting_capital, equity)
            free = limit - pf.short_notional(marks)
            budget = min(want, room, free)
            qty = int(budget // unit)
            limit_txt = (f"short limit Rs {limit:,.0f} ({config.THESIS_MAX_SHORT_PCT*100:.0f}% of capital), "
                         f"Rs {pf.short_notional(marks):,.0f} already short")
        if qty <= 0:
            done(o, "rejected", f"no room: cap {t.cap_pct*100:.0f}% of sleeve (Rs {cap:,.0f}), "
                                f"held Rs {held*unit + others:,.0f}, {limit_txt}, price {px}{ccy}")
            continue
        clipped = "" if qty * unit >= want - unit else \
            f" (clipped from Rs {want:,.0f} by the {t.cap_pct*100:.0f}% cap or {'cash' if side == 'BUY' else 'short limit'})"
        stop = (t.invalidation_price or 0.0) if side == "BUY" else 0.0
        f = pf.trade(_day(fill_day), sym, side, qty, px, reason, thesis=t.slug, currency=leg.currency, stop=stop)
        # The momentum stop (fill - 7%) means nothing here; the thesis' own invalidation price is the stop.
        pf.positions[sym].stop = stop
        fills.append(f)
        done(o, "filled", f"{side} {qty} {show} @ {px}{ccy}{clipped}" + (f"  {PAPER_SHORT}" if side == "SHORT" else ""), f)
        if t.status == "proposed" or not t.get("entry_date"):
            upd = {"status": "open"}
            if not t.get("entry_date"):
                upd["entry_date"] = _day(fill_day)
            update_front_matter(t.path, upd)
            t.fields.update(upd)
    book["orders"] = keep
    save_orders(book)
    _record_executed(market_day, executed)
    return fills, notes


def check_invalidations(pf: ThesisPortfolio, theses: dict, last: pd.Series, market_day: str) -> list[str]:
    """Queue a SELL for every held single-stock thesis whose close is at or below its numeric invalidation
    price. Second-order legs have no engine stop: the post-event rule and the reviewer close them."""
    by_symbol = {t.symbol: t for t in theses.values() if t.status == "open" and t.type != "second_order"}
    book = load_orders()
    queued = {(o.get("thesis"), str(o.get("side", "")).upper()) for o in book["orders"]}
    hits = []
    for sym, pos in pf.positions.items():
        t = by_symbol.get(sym)
        if t is None or t.invalidation_price is None or pos.qty <= 0:
            continue
        c = last.get(sym)
        if c is None or pd.isna(c) or float(c) > t.invalidation_price:
            continue
        msg = f"INVALIDATION HIT: {t.slug} ({t.label}) closed {float(c):,.2f} <= invalidation {t.invalidation_price:,.2f}"
        if (t.slug, "SELL") not in queued:
            book["orders"].append(dict(thesis=t.slug, side="SELL", qty=None, decision_date=market_day,
                                       reason=f"invalidation hit: close {float(c):.2f} <= {t.invalidation_price:.2f}"))
            msg += "; SELL queued for the next open (delete it from decisions/thesis_orders.json to override)"
        else:
            msg += "; a SELL is already queued"
        hits.append(msg)
    if hits:
        save_orders(book)
    return hits


def catalyst_state(t: Thesis, market_day: str, orders: list[dict], held=()) -> dict:
    """Days to the event (calendar) and whether the post-event rule is due and not yet applied. For a
    thesis with several legs the rule counts as handled only when every held leg has an exit queued."""
    md = _date.fromisoformat(market_day)
    days = (t.event_date - md).days
    passed = (t.rule_due_date - md).days < 0
    exits = [o for o in orders if o.get("thesis") == t.slug and str(o.get("side", "")).upper() in EXIT_SIDES]
    held_legs = [l for l in t.legs if l.symbol in held]
    if len(t.legs) > 1 and held_legs:
        missing = [l.display for l in held_legs if not any(_order_leg(t, o) is l for o in exits)]
        sell_queued = not missing
    else:
        missing, sell_queued = [], bool(exits)
    applied = bool(str(t.get("post_event_applied", "")).strip())
    return dict(days_to_event=days, passed=passed, sell_queued=sell_queued, applied=applied,
                rule_due_date=str(t.rule_due_date), missing_exits=missing,
                due=passed and t.status == "open" and not applied and not sell_queued)


def catalyst_flags(theses: dict, orders: list[dict], market_day: str, held=()) -> list[str]:
    out = []
    for t in theses.values():
        if t.type not in EVENT_TYPES or t.status in ("closed", "example"):
            continue
        c = catalyst_state(t, market_day, orders, held)
        when = f"{t.get('event')} was on {t.event_date}" + (
            f" (rule due after {t.rule_due_date})" if t.rule_due_date != t.event_date else "")
        if c["due"]:
            legs = f" Legs with no exit queued: {', '.join(c['missing_exits'])}." if c["missing_exits"] else ""
            exit_word = "SELL" if t.type != "second_order" else "SELL / COVER for each leg"
            out.append(f"CATALYST RULE DUE: {t.slug} ({t.label}) - {when}, "
                       f"{-c['days_to_event']} day(s) ago. Apply the post-event rule: \"{t.get('post_event_rule')}\".{legs} "
                       f"Then fill in post_event_applied in theses/{t.slug}.md (or queue the {exit_word} it specifies).")
        elif c["passed"] and t.status == "open" and c["sell_queued"] and not c["applied"]:
            out.append(f"Catalyst {t.slug}: post-event {'exits' if len(t.legs) > 1 else 'SELL'} queued for the next open.")
        elif c["passed"] and t.status == "proposed":
            out.append(f"Catalyst {t.slug}: {t.get('event')} ({t.event_date}) passed with no position. "
                       f"Close the thesis or move the event.")
    return out


def run_day(main_prices: pd.DataFrame, do_update: bool) -> dict:
    """Thesis-sleeve part of the daily run. Called by engine.run_day AFTER the momentum sleeve is saved."""
    market_day = _day(main_prices["date"].max())
    theses, warnings = load_theses()
    pf = ThesisPortfolio.load()
    orders = load_orders()["orders"]
    symbols = needed_symbols(theses, pf, orders)
    prices = load_prices(main_prices, symbols, do_update)
    for s in prices.attrs["dropped"]:
        warnings.append(f"No price data for {s} (Yahoo {yahoo_symbol(s)}); dropped from today's thesis run")
        log.warning("Thesis sleeve: no price data for %s; dropped", s)

    fills, notes = execute_orders(pf, theses, prices, market_day)

    closes = data.pivot(prices, "close") if not prices.empty else pd.DataFrame()
    last = closes.ffill().iloc[-1] if len(closes) else pd.Series(dtype=float)
    for sym, pos in pf.positions.items():
        c = last.get(sym)
        if c is not None and not pd.isna(c) and pos.qty > 0:
            pos.highest_close = max(pos.highest_close, float(c))
    if pf.start_date is None:
        pf.start_date = market_day
    eq_row = pf.record_equity(market_day, {k: float(v) for k, v in last.items() if not pd.isna(v)})
    pf.save()

    hits = check_invalidations(pf, theses, last, market_day)
    hits += catalyst_flags(theses, load_orders()["orders"], market_day, set(pf.positions))
    open_legs = {l.symbol for t in theses.values() if t.status == "open" for l in t.legs}
    held_without = [display_symbol(s) for s in pf.positions if s not in open_legs]
    if held_without:
        warnings.append(f"Held with no open thesis file: {', '.join(held_without)}. Write or reopen the thesis.")
    return dict(date=market_day, pf=pf, theses=theses, last=last, closes=closes, fills=fills, notes=notes,
                hits=hits, warnings=warnings, equity=eq_row, orders=load_orders()["orders"])


# ---------- views shared by the journal and the dashboard ----------
def _pct(a, b):
    return None if a is None or b is None or not b else (a / b - 1) * 100


def _close(last, sym):
    c = last.get(sym) if last is not None else None
    return None if c is None or pd.isna(c) else float(c)


def _days_since(market_day, entry):
    try:
        return (_date.fromisoformat(market_day) - _date.fromisoformat(entry)).days if entry else None
    except ValueError:
        return None


def _second_order_symbols(theses: dict) -> set:
    return {l.symbol for t in theses.values() if t.type == "second_order" and t.status in ("proposed", "open")
            for l in t.legs}


def thesis_rows(pf: ThesisPortfolio, theses: dict, last: pd.Series, market_day: str, orders=()) -> list[dict]:
    """One row per proposed/open single-stock thesis (plus any held symbol without one), with live P&L and
    distances. Legs of second_order theses are in second_order_rows instead. Money in INR, prices in the
    listing's currency."""
    rows = []
    by_symbol = {}
    singles = [t for t in theses.values() if t.status in ("proposed", "open") and t.type != "second_order"]
    for t in singles:
        by_symbol.setdefault(t.symbol, t)
    legs2 = _second_order_symbols(theses)
    held = set(pf.positions)
    syms = list(dict.fromkeys([s for s in pf.positions if s not in legs2 or s in by_symbol] + [t.symbol for t in singles]))
    for sym in syms:
        t = by_symbol.get(sym)
        p = pf.positions.get(sym)
        c = _close(last, sym)
        ccy = t.legs[0].currency if t else getattr(p, "currency", "INR")
        fx = fx_rate(ccy)
        entry = (t.get("entry_date") if t else "") or (p.entry_date if p else "")
        days = _days_since(market_day, entry)
        cat = catalyst_state(t, market_day, list(orders), held) if t is not None and t.type == "catalyst" else None
        rows.append(dict(
            type=t.type if t else "theme",
            event=t.get("event") if cat else None, event_date=str(t.event_date) if cat else None,
            days_to_event=cat["days_to_event"] if cat else None,
            post_event_rule=t.get("post_event_rule") if cat else None,
            post_event_applied=t.get("post_event_applied") if cat else None,
            post_event_due=cat["due"] if cat else False,
            slug=t.slug if t else None, symbol=display_symbol(sym), currency=ccy,
            status=t.status if t else "open (no thesis file)",
            thesis=t.get("thesis") if t else "", horizon=t.get("horizon") if t else "",
            entry_date=entry or None, days_held=days if p else None,
            target=t.get("target") if t else "", target_price=t.target_price if t else None,
            invalidation=t.get("invalidation") if t else "", invalidation_price=t.invalidation_price if t else None,
            size_cap_pct=(t.cap_pct * 100) if t else None,
            qty=p.qty if p else 0, avg_cost=p.avg_cost if p else None, last_close=c,
            market_value=None if (p is None or c is None) else c * p.qty * fx,
            unrealised_pnl=None if (p is None or c is None) else (c - p.avg_cost) * p.qty * fx,
            unrealised_pct=None if (p is None or c is None) else (c / p.avg_cost - 1) * 100 * (1 if p.qty > 0 else -1),
            to_target_pct=_pct(t.target_price if t else None, c),
            to_invalidation_pct=_pct(t.invalidation_price if t else None, c),
        ))
    return rows


def _realised_by_leg(trades: pd.DataFrame) -> dict:
    """{(thesis slug, symbol): realised INR} from the thesis ledger."""
    if trades is None or trades.empty or "thesis" not in trades.columns:
        return {}
    r = pd.to_numeric(trades["realised_pnl"], errors="coerce").fillna(0.0)
    return r.groupby([trades["thesis"].fillna("").astype(str), trades["symbol"].astype(str)]).sum().to_dict()


def second_order_rows(pf: ThesisPortfolio, theses: dict, last: pd.Series, market_day: str, orders=(),
                      trades: pd.DataFrame | None = None) -> list[dict]:
    """One entry per proposed/open second_order thesis: each leg with its own P&L, and the combined P&L."""
    realised = _realised_by_leg(ThesisPortfolio.trades() if trades is None else trades)
    held = set(pf.positions)
    out = []
    for t in theses.values():
        if t.type != "second_order" or t.status not in ("proposed", "open"):
            continue
        cat = catalyst_state(t, market_day, list(orders), held)
        legs, un_total, re_total, gross = [], 0.0, 0.0, 0.0
        un_known = True
        for l in t.legs:
            p = pf.positions.get(l.symbol)
            c = _close(last, l.symbol)
            fx = fx_rate(l.currency)
            un = None if (p is None or c is None) else (c - p.avg_cost) * p.qty * fx
            if p is not None and c is None:
                un_known = False
            re = float(realised.get((t.slug, l.symbol), 0.0))
            un_total += un or 0.0
            re_total += re
            if p is not None:
                gross += abs(p.qty) * (c if c is not None else p.avg_cost) * fx
            legs.append(dict(
                symbol=l.display, side=l.side, role=l.role, weight=l.weight, rationale=l.rationale,
                currency=l.currency, fx=fx, qty=p.qty if p else 0, avg_cost=p.avg_cost if p else None,
                last_close=c, entry_date=p.entry_date if p else None,
                market_value=None if (p is None or c is None) else c * p.qty * fx,
                unrealised_pnl=un,
                unrealised_pct=None if un is None else un / (abs(p.qty) * p.avg_cost * fx) * 100,
                realised_pnl=re, pnl=(un or 0.0) + re,
                label=PAPER_SHORT if l.side == "SHORT" else None,
            ))
        entry = t.get("entry_date") or None
        out.append(dict(
            slug=t.slug, thesis=t.get("thesis"), status=t.status, horizon=t.get("horizon"), label=t.label,
            entry_date=entry, days_held=_days_since(market_day, entry) if entry else None,
            size_cap_pct=t.cap_pct * 100, event=t.get("event"), event_date=str(t.event_date),
            days_to_event=cat["days_to_event"], rule_due_date=cat["rule_due_date"],
            post_event_rule=t.get("post_event_rule"), post_event_applied=t.get("post_event_applied"),
            post_event_due=cat["due"], missing_exits=cat["missing_exits"],
            invalidation=t.get("invalidation"),
            legs=legs, unrealised_pnl=un_total if un_known else None, realised_pnl=re_total,
            combined_pnl=(un_total + re_total) if un_known else None, gross_exposure=gross,
        ))
    return out


def _f(x, dp=0):
    return "n/a" if x is None else f"{x:,.{dp}f}"


def _when(d):
    return "today" if d == 0 else (f"in {d}" if d > 0 else f"{-d} ago")


def journal_lines(s: dict, momentum_equity: float | None = None) -> list[str]:
    pf, market_day = s["pf"], s["date"]
    eq = s["equity"]["equity"]
    total = eq - pf.starting_capital
    marks = {k: float(v) for k, v in s["last"].items() if not pd.isna(v)}
    shorts = pf.short_notional(marks)
    lines = ["## Thesis sleeve", "",
             f"Separate book: Rs {pf.starting_capital:,.0f} starting capital, macro/economic theses held weeks to years. "
             f"Weekly review every Friday (THESIS_REVIEW.md). Not part of the Rs {config.TARGET_GROSS_PER_DAY:,.0f}/day target.", "",
             "| Metric | Value |", "|---|---|",
             f"| Sleeve equity | Rs {_f(eq)} |",
             f"| Total P&L since start | Rs {_f(total)} ({total / pf.starting_capital * 100:+.2f}%) |",
             f"| Cash / invested | Rs {_f(pf.cash)} / Rs {_f(eq - pf.cash)} |",
             f"| Charges paid / realised P&L | Rs {_f(pf.costs_paid)} / Rs {_f(pf.realised_pnl)} |"]
    if shorts:
        limit = config.THESIS_MAX_SHORT_PCT * min(pf.starting_capital, eq)
        lines.append(f"| Paper short notional | Rs {_f(shorts)} of Rs {_f(limit)} limit "
                     f"({config.THESIS_MAX_SHORT_PCT*100:.0f}% of capital). {PAPER_SHORT} |")
    if momentum_equity is not None:
        lines.append(f"| Combined equity (momentum + thesis) | Rs {_f(momentum_equity + eq)} of Rs "
                     f"{_f(config.STARTING_CAPITAL + pf.starting_capital)} paper capital |")
    lines.append("")

    rows = thesis_rows(pf, s["theses"], s["last"], market_day, s["orders"])
    so_rows = second_order_rows(pf, s["theses"], s["last"], market_day, s["orders"])
    held = [r for r in rows if r["qty"]]
    lines += ["### Open theses", ""]
    if held:
        lines += ["| Thesis | Symbol | Qty | Avg cost | Close | Unrealised | Days held | Horizon | To target | To invalidation |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
        for r in held:
            tgt = f"{r['to_target_pct']:+.1f}% ({_f(r['target_price'], 2)})" if r["to_target_pct"] is not None else (r["target"] or "-")
            inv = f"{r['to_invalidation_pct']:+.1f}% ({_f(r['invalidation_price'], 2)})" if r["to_invalidation_pct"] is not None else (r["invalidation"] or "-")
            un = f"Rs {_f(r['unrealised_pnl'])} ({r['unrealised_pct']:+.1f}%)" if r["unrealised_pnl"] is not None else "no price"
            ccy = "" if r["currency"] == "INR" else f" {r['currency']}"
            lines.append(f"| {r['slug'] or '-'} | {r['symbol']} | {r['qty']} | {_f(r['avg_cost'], 2)}{ccy} | {_f(r['last_close'], 2)}{ccy} | "
                         f"{un} | {r['days_held'] if r['days_held'] is not None else '-'} | {r['horizon'] or '-'} | {tgt} | {inv} |")
    elif any(l["qty"] for r in so_rows for l in r["legs"]):
        lines.append("No single-stock theses held. Second-order positions are below.")
    else:
        lines.append("None. The sleeve is all cash.")
    lines.append("")
    proposed = [r for r in rows if not r["qty"] and r["status"] == "proposed"]
    if proposed:
        lines += ["Proposed, not yet bought: " + "; ".join(f"{r['slug']} ({r['symbol']}, close {_f(r['last_close'], 2)})" for r in proposed), ""]

    if so_rows:
        lines += ["### Second-order theses (winner vs losers)", ""]
        for r in so_rows:
            due = " **CATALYST RULE DUE**" if r["post_event_due"] else ""
            rule_when = f", rule due after {r['rule_due_date']}" if r["rule_due_date"] != r["event_date"] else ""
            combined = (f"Rs {_f(r['combined_pnl'])} (unrealised Rs {_f(r['unrealised_pnl'])}, realised Rs {_f(r['realised_pnl'])})"
                        if r["combined_pnl"] is not None else "n/a (a held leg has no price)")
            lines += [f"**{r['slug']}** ({r['status']}): {r['thesis']}", "",
                      f"Event: {r['event']} on {r['event_date']} ({_when(r['days_to_event']).replace(' ago', ' days ago')}{rule_when}). "
                      f"Post-event rule: {r['post_event_rule']}. Applied: {r['post_event_applied'] or '-'}.{due}", "",
                      "| Role | Leg | Side | Weight | Qty | Avg cost | Close | Unrealised | Realised | Rationale |",
                      "|---|---|---|---|---|---|---|---|---|---|"]
            for l in r["legs"]:
                ccy = "" if l["currency"] == "INR" else f" {l['currency']}"
                side = l["side"] if l["side"] == "LONG" else f"SHORT ({PAPER_SHORT})"
                un = (f"Rs {_f(l['unrealised_pnl'])} ({l['unrealised_pct']:+.1f}%)" if l["unrealised_pnl"] is not None
                      else ("-" if not l["qty"] else "no price"))
                lines.append(f"| {l['role']} | {l['symbol']} | {side} | {l['weight']:g} | {l['qty']} | "
                             f"{_f(l['avg_cost'], 2) + ccy if l['avg_cost'] is not None else '-'} | "
                             f"{_f(l['last_close'], 2) + ccy if l['last_close'] is not None else 'no price'} | {un} | "
                             f"Rs {_f(l['realised_pnl'])} | {l['rationale']} |")
            lines += ["", f"Combined P&L of the thesis: {combined}. Gross exposure Rs {_f(r['gross_exposure'])} "
                          f"(cap {r['size_cap_pct']:.0f}% of the sleeve).", ""]

    cats = [r for r in rows if r["type"] == "catalyst"]
    if cats:
        lines += ["### Catalyst theses", "",
                  "| Thesis | Symbol | Status | Event | Event date | Days to event | Post-event rule | Applied |",
                  "|---|---|---|---|---|---|---|---|"]
        for r in cats:
            applied = r["post_event_applied"] or ("**DUE**" if r["post_event_due"] else "-")
            lines.append(f"| {r['slug']} | {r['symbol']} | {r['status']} | {r['event']} | {r['event_date']} | {_when(r['days_to_event'])} | "
                         f"{r['post_event_rule']} | {applied} |")
        lines.append("")

    lines += ["### Thesis fills today", ""]
    if s["fills"]:
        lines += ["| Side | Symbol | Qty | Price | Charges | Realised | Reason |", "|---|---|---|---|---|---|---|"]
        for f in s["fills"]:
            ccy = "" if f.get("currency", "INR") == "INR" else f" {f['currency']}"
            lines.append(f"| {f['side']} | {display_symbol(f['symbol'])} | {f['qty']} | {f['price']:,.2f}{ccy} | {f['charges']:,.2f} | "
                         f"{f['realised_pnl'] if f['realised_pnl'] != '' else '-'} | {f['reason']} |")
    else:
        lines.append("No thesis fills.")
    lines.append("")

    lines += ["### Thesis orders queued for the next open", ""]
    if s["orders"]:
        lines += ["| Side | Thesis | Qty / amount | Decided | Reason |", "|---|---|---|---|---|"]
        for o in s["orders"]:
            side = str(o.get("side")).upper()
            size = o.get("qty") or (f"Rs {float(o['amount']):,.0f}" if o.get("amount") else ("all" if side in EXIT_SIDES else "cap"))
            leg = f" / {o['leg']}" if o.get("leg") else ""
            tag = f" ({PAPER_SHORT})" if side in ("SHORT", "COVER") else ""
            lines.append(f"| {o.get('side')}{tag} | {o.get('thesis')}{leg} | {size} | {o.get('decision_date', '-')} | {o.get('reason', '')} |")
    else:
        lines.append("None.")
    lines.append("")

    obs = list(s["hits"]) + list(s["notes"]) + list(s["warnings"])
    if _date.fromisoformat(market_day).weekday() == 4:
        obs.append("Friday: weekly thesis review is due (THESIS_REVIEW.md).")
    if obs:
        lines += ["### Thesis observations", ""] + [f"- {o}" for o in obs]
    while lines and lines[-1] == "":
        lines.pop()          # review.py adds the blank line before "## Claude's review"
    return lines


def export_data(main_prices: pd.DataFrame | None, momentum_curve: pd.DataFrame) -> dict:
    """Dashboard view of the sleeve. Read-only: no download, no state change (cached prices only)."""
    theses, warnings = load_theses()
    pf = ThesisPortfolio.load()
    orders = load_orders()["orders"]
    if main_prices is not None and not main_prices.empty:
        prices = load_prices(main_prices, needed_symbols(theses, pf, orders), do_update=False)
        warnings += [f"No price data for {s} (Yahoo {yahoo_symbol(s)})" for s in prices.attrs["dropped"]]
        closes = data.pivot(prices, "close") if not prices.empty else pd.DataFrame()
        market_day = _day(main_prices["date"].max())
    else:
        closes, market_day = pd.DataFrame(), _date.today().isoformat()
    last = closes.ffill().iloc[-1] if len(closes) else pd.Series(dtype=float)
    marks = {k: float(v) for k, v in last.items() if not pd.isna(v)}
    curve = pf.equity_curve()
    eq = float(curve["equity"].iloc[-1]) if len(curve) else pf.equity(marks)

    # combined line on the momentum sleeve's dates; before the sleeve's first row it was unallocated cash
    th = curve.set_index(curve["date"].astype(str)) if len(curve) else None
    combined, th_last = [], None
    for r in momentum_curve.to_dict("records"):
        d = str(r["date"])
        if th is not None and d in th.index:
            th_last = th.loc[d]
        t_eq = float(th_last["equity"]) if th_last is not None else pf.starting_capital
        t_cash = float(th_last["cash"]) if th_last is not None else pf.starting_capital
        t_pos = int(th_last["positions"]) if th_last is not None else 0
        combined.append(dict(date=d, equity=float(r["equity"]) + t_eq, cash=float(r["cash"]) + t_cash,
                             positions=int(r["positions"]) + t_pos, momentum=float(r["equity"]), thesis=t_eq))

    trades = ThesisPortfolio.trades()
    return dict(
        capital=pf.starting_capital, start_date=pf.start_date, cash=pf.cash, equity=eq,
        total_pnl=eq - pf.starting_capital, total_pnl_pct=(eq / pf.starting_capital - 1) * 100,
        costs_paid=pf.costs_paid, realised_pnl=pf.realised_pnl, max_position_pct=config.THESIS_MAX_POSITION_PCT * 100,
        short_notional=pf.short_notional(marks),
        short_limit=config.THESIS_MAX_SHORT_PCT * min(pf.starting_capital, eq),
        paper_short_label=PAPER_SHORT, usd_inr=config.USD_INR,
        theses=thesis_rows(pf, theses, last, market_day, orders),
        second_order=second_order_rows(pf, theses, last, market_day, orders, trades),
        closed=[dict(slug=t.slug, symbol=t.label, thesis=t.get("thesis"), exit_date=t.get("exit_date"))
                for t in theses.values() if t.status == "closed"],
        orders=orders, warnings=warnings,
        equity_curve=curve.astype(object).where(curve.notna(), None).to_dict("records"),
        trades=trades.astype(object).where(trades.notna(), None).to_dict("records")[-20:],
        combined_equity_curve=combined,
        combined_capital=config.STARTING_CAPITAL + pf.starting_capital,
    )
