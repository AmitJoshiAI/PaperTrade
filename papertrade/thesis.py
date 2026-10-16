"""Thesis sleeve: macro / economic-theme positions held for weeks to years, each with a written thesis.

A separate book from the momentum sleeve. It has its own capital (config.THESIS_CAPITAL), its own state
files (state/thesis_*), its own order queue (decisions/thesis_orders.json) and its own weekly review
(THESIS_REVIEW.md). It shares the cost model (costs.charges via Portfolio), the slippage assumption,
the daily run and the dashboard. Nothing here reads or writes the momentum sleeve's files.

Ledger: one file per thesis, theses/<slug>.md, with front matter between two `---` lines:

    ---
    symbol: HAL                       any NSE symbol (Yahoo SYMBOL.NS), not limited to Nifty 50
    thesis: one line, the macro/economic reason
    entry_date:                       filled in by the engine on the first fill
    horizon: 12-24 months             weeks / months / years, free text
    target: 5200                      a number (price) is tracked; text is shown as-is
    invalidation: close below 3800 or defence capex cut in the budget
    invalidation_price: 3800          optional; a number here (or a pure number in `invalidation`)
                                      makes the engine queue a SELL when the close is at or below it
    size_cap_pct: 20                  max % of sleeve equity in this thesis; never above 25
    status: proposed                  proposed -> open (engine, on first fill) -> closed (engine, on full exit)
    type: theme                       theme (macro, weeks to years) or catalyst (a scheduled event, days to weeks)
    ---

A catalyst thesis also needs (the run rejects the file without them):

    event: Q2 results                 one line: Q2 results, Union Budget, IPO listing, RBI policy ...
    event_date: 2026-10-24            ISO date of the event
    post_event_rule: sell at next open regardless      what to do the day after the event
    post_event_applied:               set by the reviewer (date + what was done) once the rule is applied

The engine does not interpret post_event_rule (it is free text). It counts days to the event, and from
the first trading day after event_date it flags the thesis as "CATALYST RULE DUE" in the journal until
post_event_applied is filled in, a SELL for it is queued, or it is closed.
    Free text below: the long-form argument, evidence, what would change the view, review log.

Files whose name starts with "_" (e.g. _TEMPLATE.md) are ignored.

Orders: decisions/thesis_orders.json, written by Claude (or you):

    {"orders": [{"thesis": "hal-defence", "side": "BUY", "amount": 40000, "decision_date": "2026-10-16",
                 "reason": "open thesis"}]}

  thesis        slug of theses/<slug>.md; the symbol comes from that file
  side          BUY or SELL
  qty / amount  optional. BUY: qty shares, or Rs amount, or (neither) the thesis' full size cap.
                SELL: qty shares, or (neither) the whole position.
  decision_date fills at the first trading day's open AFTER this date. If missing, the engine stamps
                today's market date, so the order fills at the next open (never at an open already past).

A BUY is always clipped to the size cap (position value <= cap% of sleeve equity at the fill price) and to
available cash. Filled or rejected orders leave the queue and are recorded in
decisions/thesis_executed_<date>.json; orders that cannot fill yet stay queued.
"""
from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import date as _date
from pathlib import Path

import pandas as pd

from . import config, data
from .costs import charges
from .portfolio import Portfolio

log = logging.getLogger(__name__)

FIELDS = ["symbol", "thesis", "entry_date", "horizon", "target", "invalidation", "invalidation_price",
          "size_cap_pct", "status", "type", "event", "event_date", "post_event_rule", "post_event_applied"]
STATUSES = ("proposed", "open", "closed")
TYPES = ("theme", "catalyst")


class ThesisPortfolio(Portfolio):
    """The momentum Portfolio logic (cash, charges, avg cost, ledgers) pointed at the thesis files."""
    _PORTFOLIO_FILE = "THESIS_PORTFOLIO_FILE"
    _TRADES_FILE = "THESIS_TRADES_FILE"
    _EQUITY_FILE = "THESIS_EQUITY_FILE"
    _CAPITAL = "THESIS_CAPITAL"


# ---------- ledger ----------
@dataclass
class Thesis:
    slug: str
    path: Path
    fields: dict = field(default_factory=dict)
    body: str = ""

    def get(self, key, default=""):
        return self.fields.get(key, default)

    @property
    def symbol(self) -> str:
        return str(self.get("symbol")).strip().upper()

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
    def invalidation_price(self):
        return _num(self.get("invalidation_price")) or _num(self.get("invalidation"))

    @property
    def target_price(self):
        return _num(self.get("target"))

    @property
    def cap_pct(self) -> float:
        """Size cap as a fraction of sleeve equity: the file's size_cap_pct, never above the hard cap."""
        v = _num(str(self.get("size_cap_pct", "")).replace("%", ""))
        hard = config.THESIS_MAX_POSITION_PCT
        return hard if v is None or v <= 0 else min(v / 100, hard)


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


def parse_thesis(path: Path) -> Thesis:
    fm, body = _split_front_matter(path.read_text(encoding="utf-8"))
    if fm is None:
        raise ValueError("no front matter (the file must start with a --- line)")
    fields = {}
    for line in fm:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise ValueError(f"front matter line is not 'key: value': {line!r}")
        k, v = line.split(":", 1)
        fields[k.strip().lower()] = _unquote(v)
    t = Thesis(slug=path.stem, path=path, fields=fields, body=body)
    if not t.symbol:
        raise ValueError("missing symbol")
    if t.status not in STATUSES:
        raise ValueError(f"status {t.status!r} is not one of {', '.join(STATUSES)}")
    if t.type not in TYPES:
        raise ValueError(f"type {t.type!r} is not one of {', '.join(TYPES)}")
    if t.type == "catalyst":
        try:
            ed = t.event_date
        except ValueError:
            raise ValueError(f"event_date {t.get('event_date')!r} is not an ISO date (YYYY-MM-DD)")
        missing = [k for k, ok in (("event", t.get("event")), ("event_date", ed),
                                   ("post_event_rule", t.get("post_event_rule"))) if not ok]
        if missing:
            raise ValueError(f"catalyst thesis is missing {', '.join(missing)}")
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
    """Set keys in a thesis file's front matter in place, keeping every other line and the body."""
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    lines = text.split("\n")
    end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    for k, v in updates.items():
        for i in range(1, end):
            if ":" in lines[i] and lines[i].split(":", 1)[0].strip().lower() == k:
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


# ---------- prices ----------
def needed_symbols(theses: dict, pf: ThesisPortfolio, orders: list[dict]) -> list[str]:
    syms = {t.symbol for t in theses.values() if t.status in ("proposed", "open")}
    syms |= set(pf.positions)
    for o in orders:
        t = theses.get(o.get("thesis"))
        if t is not None:
            syms.add(t.symbol)
    return sorted(syms)


def load_prices(main_prices: pd.DataFrame, symbols: list[str], do_update: bool) -> pd.DataFrame:
    """Long price frame for `symbols`, capped at the momentum sleeve's market date. Nifty 50 names are taken
    from the momentum download; others are downloaded (do_update) into data_cache/thesis_prices.parquet,
    or read from that cache. Symbols with no data are listed in .attrs["dropped"]."""
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


def _sleeve_equity_at(pf: ThesisPortfolio, px_row: pd.Series) -> float:
    v = pf.cash
    for s, p in pf.positions.items():
        px = px_row.get(s)
        v += p.qty * (float(px) if px is not None and not pd.isna(px) else p.avg_cost)
    return v


def execute_orders(pf: ThesisPortfolio, theses: dict, prices: pd.DataFrame, market_day: str) -> tuple[list, list]:
    """Fill queued thesis orders at the first open after each order's decision_date. Returns (fills, notes)."""
    book = load_orders()
    orders = book["orders"]
    if not orders:
        return [], []
    opens = data.pivot(prices, "open") if not prices.empty else pd.DataFrame()
    keep, fills, notes, executed = [], [], [], []

    def done(o, status, detail, fill=None):
        executed.append(dict(order=o, status=status, detail=detail, fill=fill))
        notes.append(f"{o.get('side', '?')} {o.get('thesis', '?')}: {status} - {detail}")

    # sells first so their cash is available to buys, as in the momentum sleeve
    ordered = sorted(orders, key=lambda o: 0 if str(o.get("side", "")).upper() == "SELL" else 1)
    for o in ordered:
        side = str(o.get("side", "")).upper()
        t = theses.get(o.get("thesis"))
        if side not in ("BUY", "SELL"):
            done(o, "rejected", f"side must be BUY or SELL, got {o.get('side')!r}")
            continue
        if t is None:
            done(o, "rejected", f"no thesis file theses/{o.get('thesis')}.md")
            continue
        if not o.get("decision_date"):
            o["decision_date"] = market_day          # fills at the next open, never at one already past
            keep.append(o)
            notes.append(f"{side} {t.slug}: no decision_date; stamped {market_day}, fills at the next open")
            continue
        later = opens.index[opens.index > pd.Timestamp(o["decision_date"])] if len(opens) else []
        if len(later) == 0:
            keep.append(o)
            continue
        fill_day = later[0]
        sym = t.symbol
        px = opens.loc[fill_day, sym] if sym in opens.columns else None
        if px is None or pd.isna(px):
            keep.append(o)
            notes.append(f"{side} {t.slug}: no open price for {sym} since the {o['decision_date']} decision; order stays queued")
            continue
        reason = (f"thesis {t.slug}: " + str(o.get("reason", ""))).strip()
        if side == "SELL":
            pos = pf.positions.get(sym)
            if pos is None:
                done(o, "rejected", f"{sym} not held")
                continue
            px = round(float(px) * (1 - config.SLIPPAGE_PCT), 2)
            qty = min(int(o["qty"]), pos.qty) if o.get("qty") else pos.qty
            f = pf.sell(_day(fill_day), sym, qty, px, reason)
            fills.append(f)
            done(o, "filled", f"SELL {qty} {sym} @ {px}", f)
            if sym not in pf.positions:
                update_front_matter(t.path, {"status": "closed", "exit_date": _day(fill_day)})
                t.fields["status"] = "closed"
        else:
            if t.status == "closed":
                done(o, "rejected", "thesis status is closed")
                continue
            px = round(float(px) * (1 + config.SLIPPAGE_PCT), 2)
            equity = _sleeve_equity_at(pf, opens.loc[fill_day])
            held = pf.positions[sym].qty if sym in pf.positions else 0
            room = t.cap_pct * equity - held * px
            if o.get("qty"):
                want = int(o["qty"]) * px
            elif o.get("amount"):
                want = float(o["amount"])
            else:
                want = room
            budget = min(want, room, pf.cash)
            qty = int(budget // px)
            while qty > 0 and qty * px + charges("BUY", qty, px).total > pf.cash:
                qty -= 1
            if qty <= 0:
                done(o, "rejected", f"no room: cap {t.cap_pct*100:.0f}% of sleeve (Rs {t.cap_pct*equity:,.0f}), "
                                    f"held Rs {held*px:,.0f}, cash Rs {pf.cash:,.0f}, price {px}")
                continue
            clipped = "" if qty * px >= want - px else f" (clipped from Rs {want:,.0f} by the {t.cap_pct*100:.0f}% cap or cash)"
            f = pf.buy(_day(fill_day), sym, qty, px, reason)
            # The momentum stop (fill - 7%) means nothing here; the thesis' own invalidation price is the stop.
            pf.positions[sym].stop = t.invalidation_price or 0.0
            fills.append(f)
            done(o, "filled", f"BUY {qty} {sym} @ {px}{clipped}", f)
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
    """Queue a SELL for every held thesis whose close is at or below its numeric invalidation price."""
    by_symbol = {t.symbol: t for t in theses.values() if t.status == "open"}
    book = load_orders()
    queued = {(o.get("thesis"), str(o.get("side", "")).upper()) for o in book["orders"]}
    hits = []
    for sym, pos in pf.positions.items():
        t = by_symbol.get(sym)
        if t is None or t.invalidation_price is None:
            continue
        c = last.get(sym)
        if c is None or pd.isna(c) or float(c) > t.invalidation_price:
            continue
        msg = f"INVALIDATION HIT: {t.slug} ({sym}) closed {float(c):,.2f} <= invalidation {t.invalidation_price:,.2f}"
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


def catalyst_state(t: Thesis, market_day: str, orders: list[dict]) -> dict:
    """Days to the event (calendar) and whether the post-event rule is due and not yet applied."""
    days = (t.event_date - _date.fromisoformat(market_day)).days
    sell_queued = any(o.get("thesis") == t.slug and str(o.get("side", "")).upper() == "SELL" for o in orders)
    applied = bool(str(t.get("post_event_applied", "")).strip())
    passed = days < 0
    return dict(days_to_event=days, passed=passed, sell_queued=sell_queued, applied=applied,
                due=passed and t.status == "open" and not applied and not sell_queued)


def catalyst_flags(theses: dict, orders: list[dict], market_day: str) -> list[str]:
    out = []
    for t in theses.values():
        if t.type != "catalyst" or t.status == "closed":
            continue
        c = catalyst_state(t, market_day, orders)
        if c["due"]:
            out.append(f"CATALYST RULE DUE: {t.slug} ({t.symbol}) - {t.get('event')} was on {t.event_date}, "
                       f"{-c['days_to_event']} day(s) ago. Apply the post-event rule: \"{t.get('post_event_rule')}\". "
                       f"Then fill in post_event_applied in theses/{t.slug}.md (or queue the SELL it specifies).")
        elif c["passed"] and t.status == "open" and c["sell_queued"] and not c["applied"]:
            out.append(f"Catalyst {t.slug}: post-event SELL queued for the next open.")
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
        warnings.append(f"No price data for {s} (Yahoo {s}.NS); dropped from today's thesis run")
        log.warning("Thesis sleeve: no price data for %s; dropped", s)

    fills, notes = execute_orders(pf, theses, prices, market_day)

    closes = data.pivot(prices, "close") if not prices.empty else pd.DataFrame()
    last = closes.ffill().iloc[-1] if len(closes) else pd.Series(dtype=float)
    for sym, pos in pf.positions.items():
        c = last.get(sym)
        if c is not None and not pd.isna(c):
            pos.highest_close = max(pos.highest_close, float(c))
    if pf.start_date is None:
        pf.start_date = market_day
    eq_row = pf.record_equity(market_day, {k: float(v) for k, v in last.items() if not pd.isna(v)})
    pf.save()

    hits = check_invalidations(pf, theses, last, market_day)
    hits += catalyst_flags(theses, load_orders()["orders"], market_day)
    held_without = [s for s in pf.positions if s not in {t.symbol for t in theses.values() if t.status == "open"}]
    if held_without:
        warnings.append(f"Held with no open thesis file: {', '.join(held_without)}. Write or reopen the thesis.")
    return dict(date=market_day, pf=pf, theses=theses, last=last, closes=closes, fills=fills, notes=notes,
                hits=hits, warnings=warnings, equity=eq_row, orders=load_orders()["orders"])


# ---------- views shared by the journal and the dashboard ----------
def _pct(a, b):
    return None if a is None or b is None or not b else (a / b - 1) * 100


def thesis_rows(pf: ThesisPortfolio, theses: dict, last: pd.Series, market_day: str, orders=()) -> list[dict]:
    """One row per proposed/open thesis (plus any held symbol without one), with live P&L and distances."""
    rows = []
    by_symbol = {}
    for t in theses.values():
        if t.status in ("proposed", "open"):
            by_symbol.setdefault(t.symbol, t)
    syms = list(dict.fromkeys(list(pf.positions) + [t.symbol for t in theses.values() if t.status in ("proposed", "open")]))
    for sym in syms:
        t = by_symbol.get(sym)
        p = pf.positions.get(sym)
        c = last.get(sym) if last is not None else None
        c = None if c is None or pd.isna(c) else float(c)
        entry = (t.get("entry_date") if t else "") or (p.entry_date if p else "")
        try:
            days = (_date.fromisoformat(market_day) - _date.fromisoformat(entry)).days if entry else None
        except ValueError:
            days = None
        cat = catalyst_state(t, market_day, list(orders)) if t is not None and t.type == "catalyst" else None
        rows.append(dict(
            type=t.type if t else "theme",
            event=t.get("event") if cat else None, event_date=str(t.event_date) if cat else None,
            days_to_event=cat["days_to_event"] if cat else None,
            post_event_rule=t.get("post_event_rule") if cat else None,
            post_event_applied=t.get("post_event_applied") if cat else None,
            post_event_due=cat["due"] if cat else False,
            slug=t.slug if t else None, symbol=sym, status=t.status if t else "open (no thesis file)",
            thesis=t.get("thesis") if t else "", horizon=t.get("horizon") if t else "",
            entry_date=entry or None, days_held=days if p else None,
            target=t.get("target") if t else "", target_price=t.target_price if t else None,
            invalidation=t.get("invalidation") if t else "", invalidation_price=t.invalidation_price if t else None,
            size_cap_pct=(t.cap_pct * 100) if t else None,
            qty=p.qty if p else 0, avg_cost=p.avg_cost if p else None, last_close=c,
            market_value=None if (p is None or c is None) else c * p.qty,
            unrealised_pnl=None if (p is None or c is None) else (c - p.avg_cost) * p.qty,
            unrealised_pct=None if (p is None or c is None) else (c / p.avg_cost - 1) * 100,
            to_target_pct=_pct(t.target_price if t else None, c),
            to_invalidation_pct=_pct(t.invalidation_price if t else None, c),
        ))
    return rows


def _f(x, dp=0):
    return "n/a" if x is None else f"{x:,.{dp}f}"


def journal_lines(s: dict, momentum_equity: float | None = None) -> list[str]:
    pf, market_day = s["pf"], s["date"]
    eq = s["equity"]["equity"]
    total = eq - pf.starting_capital
    lines = ["## Thesis sleeve", "",
             f"Separate book: Rs {pf.starting_capital:,.0f} starting capital, macro/economic theses held weeks to years. "
             f"Weekly review every Friday (THESIS_REVIEW.md). Not part of the Rs {config.TARGET_GROSS_PER_DAY:,.0f}/day target.", "",
             "| Metric | Value |", "|---|---|",
             f"| Sleeve equity | Rs {_f(eq)} |",
             f"| Total P&L since start | Rs {_f(total)} ({total / pf.starting_capital * 100:+.2f}%) |",
             f"| Cash / invested | Rs {_f(pf.cash)} / Rs {_f(eq - pf.cash)} |",
             f"| Charges paid / realised P&L | Rs {_f(pf.costs_paid)} / Rs {_f(pf.realised_pnl)} |"]
    if momentum_equity is not None:
        lines.append(f"| Combined equity (momentum + thesis) | Rs {_f(momentum_equity + eq)} of Rs "
                     f"{_f(config.STARTING_CAPITAL + pf.starting_capital)} paper capital |")
    lines.append("")

    rows = thesis_rows(pf, s["theses"], s["last"], market_day, s["orders"])
    held = [r for r in rows if r["qty"]]
    lines += ["### Open theses", ""]
    if held:
        lines += ["| Thesis | Symbol | Qty | Avg cost | Close | Unrealised | Days held | Horizon | To target | To invalidation |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
        for r in held:
            tgt = f"{r['to_target_pct']:+.1f}% ({_f(r['target_price'], 2)})" if r["to_target_pct"] is not None else (r["target"] or "-")
            inv = f"{r['to_invalidation_pct']:+.1f}% ({_f(r['invalidation_price'], 2)})" if r["to_invalidation_pct"] is not None else (r["invalidation"] or "-")
            un = f"Rs {_f(r['unrealised_pnl'])} ({r['unrealised_pct']:+.1f}%)" if r["unrealised_pnl"] is not None else "no price"
            lines.append(f"| {r['slug'] or '-'} | {r['symbol']} | {r['qty']} | {_f(r['avg_cost'], 2)} | {_f(r['last_close'], 2)} | "
                         f"{un} | {r['days_held'] if r['days_held'] is not None else '-'} | {r['horizon'] or '-'} | {tgt} | {inv} |")
    else:
        lines.append("None. The sleeve is all cash.")
    lines.append("")
    proposed = [r for r in rows if not r["qty"] and r["status"] == "proposed"]
    if proposed:
        lines += ["Proposed, not yet bought: " + "; ".join(f"{r['slug']} ({r['symbol']}, close {_f(r['last_close'], 2)})" for r in proposed), ""]

    cats = [r for r in rows if r["type"] == "catalyst"]
    if cats:
        lines += ["### Catalyst theses", "",
                  "| Thesis | Symbol | Status | Event | Event date | Days to event | Post-event rule | Applied |",
                  "|---|---|---|---|---|---|---|---|"]
        for r in cats:
            d = r["days_to_event"]
            when = "today" if d == 0 else (f"in {d}" if d > 0 else f"{-d} ago")
            applied = r["post_event_applied"] or ("**DUE**" if r["post_event_due"] else "-")
            lines.append(f"| {r['slug']} | {r['symbol']} | {r['status']} | {r['event']} | {r['event_date']} | {when} | "
                         f"{r['post_event_rule']} | {applied} |")
        lines.append("")

    lines += ["### Thesis fills today", ""]
    if s["fills"]:
        lines += ["| Side | Symbol | Qty | Price | Charges | Realised | Reason |", "|---|---|---|---|---|---|---|"]
        for f in s["fills"]:
            lines.append(f"| {f['side']} | {f['symbol']} | {f['qty']} | {f['price']:,.2f} | {f['charges']:,.2f} | "
                         f"{f['realised_pnl'] if f['realised_pnl'] != '' else '-'} | {f['reason']} |")
    else:
        lines.append("No thesis fills.")
    lines.append("")

    lines += ["### Thesis orders queued for the next open", ""]
    if s["orders"]:
        lines += ["| Side | Thesis | Qty / amount | Decided | Reason |", "|---|---|---|---|---|"]
        for o in s["orders"]:
            size = o.get("qty") or (f"Rs {float(o['amount']):,.0f}" if o.get("amount") else ("all" if str(o.get("side")).upper() == "SELL" else "cap"))
            lines.append(f"| {o.get('side')} | {o.get('thesis')} | {size} | {o.get('decision_date', '-')} | {o.get('reason', '')} |")
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
        warnings += [f"No price data for {s} (Yahoo {s}.NS)" for s in prices.attrs["dropped"]]
        closes = data.pivot(prices, "close") if not prices.empty else pd.DataFrame()
        market_day = _day(main_prices["date"].max())
    else:
        closes, market_day = pd.DataFrame(), _date.today().isoformat()
    last = closes.ffill().iloc[-1] if len(closes) else pd.Series(dtype=float)
    curve = pf.equity_curve()
    eq = float(curve["equity"].iloc[-1]) if len(curve) else pf.equity({k: float(v) for k, v in last.items() if not pd.isna(v)})

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
        theses=thesis_rows(pf, theses, last, market_day, orders),
        closed=[dict(slug=t.slug, symbol=t.symbol, thesis=t.get("thesis"), exit_date=t.get("exit_date"))
                for t in theses.values() if t.status == "closed"],
        orders=orders, warnings=warnings,
        equity_curve=curve.astype(object).where(curve.notna(), None).to_dict("records"),
        trades=trades.astype(object).where(trades.notna(), None).to_dict("records")[-20:],
        combined_equity_curve=combined,
        combined_capital=config.STARTING_CAPITAL + pf.starting_capital,
    )
