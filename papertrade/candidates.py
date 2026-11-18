"""Daily option candidates for the reviewer (ROUTINE_PROMPT.md step 5c): "## F&O candidates" and
"## US candidates" in the journal, the same data in docs/data.json (dashboard card "Option candidates").

Written by the daily run, which has network; the cloud reviewer has none, so this is what it decides with.
State: state/fno_candidates.json and state/us_candidates.json (latest run only; export reads them).

India (fno.run_day): NIFTY and BANKNIFTY always (config.FNO_CANDIDATE_INDICES), plus every config.FNO_STOCKS
symbol that an open thesis holds or a queued thesis order names. For the current monthly expiry (and the
nearest listed expiry for config.FNO_WEEKLY_INDICES, and an "event:" expiry, the first monthly expiry on or
after a thesis or calendar event that the current one ends before; see fno_events and
strategy_reviews/events.json): spot, days to expiry, ATM strike
(the engine's: spot rounded to the config strike step) and ATM-2..ATM+2 calls and puts per unit and per lot,
then six 1-lot structures. US (us.run_day): config.US_CANDIDATE_TICKERS, the nearest monthly expiry (third Friday)
14-42 days after the US market date, ATM = the listed strike nearest the last close, ATM-2..ATM+2 listed
strikes (standard $0.50-grid strikes only: adjusted contracts are skipped), the same structures per contract (x100).

Every price says where it came from:
  "real NSE chain (mid)" / "real yfinance chain (mid)"   mid of a two-sided bid/ask at the close
  "real Zerodha chain (mid)"                             the same from Kite Connect quotes (kite.py), when a
                                                         token is present (KITE_SETUP.md)
  "model (BS on NSE chain IV)" / "model (BS on chain IV)" Black-Scholes with the chain's implied vol (the
                                                         strike's own, else the ATM IV): one-sided quote
  "model (BS on VIX / realised vol)"                     no chain today: India VIX (index) or 1.5x 30-day
                                                         realised vol (stock), as the sleeve's model

Structures (1 lot / 1 contract). Legs are listed covers (BUY) first: the order to write them in.
  debit   bull call spread  BUY CE ATM, SELL CE ATM+2   max loss = debit, max gain = gap - debit, BE = ATM + debit
          bear put spread   BUY PE ATM, SELL PE ATM-2   max loss = debit, max gain = gap - debit, BE = ATM - debit
          long straddle     BUY CE ATM + BUY PE ATM     max loss = both premiums, gain unlimited, BE = ATM +/- both
  credit  bull put spread   BUY PE wing, SELL PE short  BE = short - credit
          bear call spread  BUY CE wing, SELL CE short  BE = short + credit
          iron condor       both of the above (4 legs)  BEs = put short - total credit, call short + total credit
          max gain = credit x units - entry charges; max loss = (gap - credit) x units + entry charges (condor:
          the wider side's gap minus the total credit); "n/a: inconsistent prices" unless 0 < credit < gap on
          each side and the condor's total credit < its wider gap.
  Credit strikes: expected move = the expiry's ATM straddle (CE + PE at ATM). Short = the listed strike nearest
  ATM -/+ that move and at least one step from ATM; wing = config.CREDIT_WING_STEPS[sleeve] steps further out (India 2, US 1), else 1
  step. India: a step is the config strike step and the strike must be on NSE's listed list (the cached chain
  keeps +/-15% of the spot) and on the config step grid (the engine rounds a strike to it); US: a step is one
  listed strike. A leg is usable when it has a price >= config.CANDIDATE_MIN_LEG_PRICE (India's fill is the
  model -1% and the engine rejects one below 0.05). If none is usable: short ATM-/+1, wing ATM-/+2 (the tabled
  rows), said in "strike_note"; else one n/a line.
PASS/FAIL (simulate): each leg runs, as one OPEN order of a single queue, through the engine's own code on a
copy of the book: the run_orders order (BUY covers before SELL options), its opposite-side check,
DerivBook.check_open (breaker, slots, cover via allocate_cover, short-option margin = strike gap x units,
per-order risk <= 10% of equity = premium for a long leg / margin for a short leg, free cash), then the cash
effect of DerivBook.open. A rejected leg is skipped and the rest tried, as the engine does, so a short whose
cover failed is rejected as naked. Max lots = the largest n for which every leg passes. An expiry on or before
the next weekday (the earliest fill day) is n/a: the engine rejects it. Each PASS is for that structure alone
on today's book. Margin: the engine blocks gap x units per short leg (a condor blocks both sides).
Also per structure: max loss as % of sleeve equity, "event-trade size" fits / too big (max loss <=
config.EVENT_TRADE_MAX_LOSS_PCT of equity) and liquidity labels (DEFERRED 13, _quote_flags): "wide quotes" =
a real-chain leg whose ask - bid > CANDIDATE_WIDE_SPREAD_PCT of the mid and > CANDIDATE_WIDE_SPREAD_MIN, or a
one-sided quote; "no quote" = a strike with no bid/ask while the ATM has one; US before the open (no bid/ask
anywhere): "no trade in the last session" from Yahoo's lastTradeDate. Labels never block.
India "fill est." (_fno_fill_est): each leg priced the way fno.FnoMarket.open_contract will fill it (the same
chain IV per strike, T from the fill day, +/-1%) but at today's close as the spot; a credit structure whose
estimated fill credit is <= 0 or under half the mid credit is labelled. US (_us_fill_check): each leg of a PASS
row is priced by the engine's own us.UsMarket.quote on this run's chain; a leg it would price at 0 or not at
all makes the row FAIL (before the US open that is every out-of-the-money leg: DEFERRED 4).
Max loss and the free-cash need include the entry charges (costs.py); exit charges are not included.
These are close prices. Orders fill at the NEXT open: India at the model (Black-Scholes on the latest chain
IV, +1% buy / -1% sell), US at the chain mid of the next session; the filled credit or debit differs and is
not re-checked against the gap. "ATM" in an order is re-resolved at the fill, so the section prints numeric
strikes and the exact expiry to put in the order.
"""
from __future__ import annotations

import copy
import json
import logging
import math

import pandas as pd
from datetime import date as _date, timedelta

from . import config, chains
from .costs import charges, us_charges
from .derivbook import bs_price, years_to, NotYet, DPos, Reject, position_id

log = logging.getLogger(__name__)

US_CHAIN_LABEL = "real yfinance chain (mid)"
US_IV_LABEL = "model (BS on chain IV)"
US_LT_LABEL = "model (BS on IV implied by last trades)"


def _implied_vol(price, S, K, t, r, kind) -> float | None:
    """Black-Scholes implied vol by bisection, or None if the price is outside the no-arbitrage range."""
    lo, hi = 0.01, 5.0
    if t <= 0 or price <= bs_price(S, K, t, r, lo, kind) or price >= bs_price(S, K, t, r, hi, kind):
        return None
    for _ in range(80):
        mid = (lo + hi) / 2
        if bs_price(S, K, t, r, mid, kind) < price:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def _traded_since(q, md) -> bool:
    """True if the contract's last trade was in the latest US session (Yahoo shows bid = ask = 0 and IV 0.00001
    outside US hours, which is when the 17:33 IST run happens; the last trade is then the only real price)."""
    try:
        lp, ltd = float(q.get("lastPrice") or 0), q.get("lastTradeDate")
        return lp > 0 and ltd is not None and pd.Timestamp(ltd).date() >= md
    except Exception:
        return False
OFFSETS = (-2, -1, 0, 1, 2)


def us_strikes(calls, puts) -> list:
    """Strikes listed for both calls and puts, on the standard $0.50 grid only: Yahoo also lists adjusted
    contracts after a special distribution (QQQ 604.78 / 609.78, F 11.67 on 17 Nov 2026) under ordinary-looking
    symbols; their deliverable is not 100 shares, so they are not priced. Shared with us.UsMarket."""
    ks = sorted({float(k) for k in calls["strike"]} & {float(k) for k in puts["strike"]})
    return [k for k in ks if abs(k * 2 - round(k * 2)) < 1e-6] or ks


def last_trade_iv(calls, puts, S: float, md, tt: float) -> tuple[float | None, int]:
    """(IV, number of trades used): outside US hours (the 17:33 IST run is US pre-market) Yahoo shows bid = ask
    = 0 and a meaningless IV (0.00001 or a few %). One IV per underlying and expiry is then backed out of the
    latest session's (md) last trades at the three listed strikes nearest the spot, calls and puts (median),
    and every strike is priced from it. Used by the candidates table and by the engine's own fill pricer
    (us.UsMarket.quote), so the two agree."""
    strikes = us_strikes(calls, puts)
    if not strikes or tt <= 0:
        return None, 0
    i = min(range(len(strikes)), key=lambda j: abs(strikes[j] - S))
    ivs = []
    for off in (-1, 0, 1):
        if not 0 <= i + off < len(strikes):
            continue
        K = strikes[i + off]
        for right, frame in (("CALL", calls), ("PUT", puts)):
            row = frame[(frame["strike"] - K).abs() < 1e-6]
            q = row.iloc[0] if len(row) else None
            if q is not None and _traded_since(q, md):
                v = _implied_vol(float(q.get("lastPrice")), S, K, tt, config.US_RISK_FREE_RATE, right)
                if v:
                    ivs.append(v)
    return (sorted(ivs)[len(ivs) // 2] if ivs else None), len(ivs)


# ---------- which underlyings ----------
def fno_candidate_symbols() -> tuple[list[str], list[str], dict]:
    """(symbols, notes, {symbol: why}). Indices first, then F&O-list stocks named by an open thesis or a queued
    thesis order. A thesis symbol not on config.FNO_STOCKS is reported in notes, not shown."""
    why = {u: ["index"] for u in config.FNO_CANDIDATE_INDICES}
    notes = []
    try:
        from . import thesis
        theses, _w = thesis.load_theses()
        held = []
        for slug, t in theses.items():
            if t.status == "open":
                held += [(l.symbol, f"open thesis {slug}") for l in t.legs]
        for o in thesis.load_orders().get("orders", []):
            t = theses.get(o.get("thesis"))
            if t is None:
                continue
            leg = t.find_leg(o.get("leg") or o.get("symbol"))
            for l in ([leg] if leg else t.legs):
                held.append((l.symbol, f"queued thesis order {t.slug} ({o.get('side', '')} {o.get('decision_date', '')})"))
        for sym, reason in held:
            if sym in config.FNO_STOCKS:
                why.setdefault(sym, [])
                if reason not in why[sym]:
                    why[sym].append(reason)
            elif not sym.startswith("US:") and "." not in sym:
                n = f"{sym} ({reason}) is not on the sleeve's F&O list (config.FNO_STOCKS): no candidates"
                if n not in notes:
                    notes.append(n)
    except Exception as e:      # a thesis typo must not stop the candidates
        notes.append(f"thesis files not read for candidates ({e})")
    return list(why), notes, why


def fno_event_dates(market_day: str) -> list[dict]:
    """[{slug, date, symbols}] for every proposed or open catalyst / second_order thesis whose event_date (or
    rule_due_date, if later) is after the market date. Why: the current monthly expiry can end before the
    event (23 Nov 2026 vs the RBI decision of 4 Dec 2026), and a structure that expires first never sees it.
    A thesis whose post_event_applied is filled in is skipped: its rule has fired and it is being exited, so
    there is nothing left to hold through (crude-ongc-vs-hpcl on 6 Nov 2026: rule applied, its rule_due_date
    of 15 Dec is only the time box). Never raises: a thesis typo must not stop the candidates."""
    out = []
    try:
        from . import thesis
        theses, _w = thesis.load_theses()
        for slug, t in theses.items():
            if t.status not in ("proposed", "open") or t.type not in thesis.EVENT_TYPES:
                continue
            if str(t.get("post_event_applied", "")).strip():
                continue
            ds = [d for d in (t.event_date, t.rule_due_date) if d]
            if not ds or max(ds).isoformat() <= market_day:
                continue
            out.append(dict(slug=slug, date=max(ds).isoformat(), symbols=sorted({l.symbol for l in t.legs}),
                            origin="thesis"))
    except Exception as e:
        log.warning("thesis event dates for the F&O candidates not read: %s", e)
    return sorted(out, key=lambda ev: (ev["date"], ev["slug"]))


# ---------- the event calendar (strategy_reviews/events.json) ----------
EVENTS_FILE = config.ROOT / "strategy_reviews" / "events.json"
EVENT_SLEEVES = ("fno", "us")
EVENTS_NAME = "strategy_reviews/events.json"


def load_event_calendar(path=None) -> tuple[list[dict], list[str]]:
    """([{slug, date, symbols, sleeves, event, source, origin="calendar"}], warnings) from the calendar file the
    weekly strategist maintains: scheduled events (RBI, FOMC, CPI) that no thesis names but that the option
    candidates must still hold through. Every entry, past ones too (callers filter by date). Never raises:
    a missing file is logged and gives nothing; a malformed file or a bad entry gives a warning (the entry is
    skipped). The warnings reach the journal's F&O Observations (fno.run_day via calendar_warnings)."""
    from pathlib import Path
    p = EVENTS_FILE if path is None else Path(path)
    warns: list[str] = []

    def warn(msg):
        log.warning("%s", msg)
        warns.append(msg)
    try:
        if not p.exists():
            log.warning("event calendar %s not found: no calendar events", p)
            return [], []
        data = json.loads(p.read_text(encoding="utf-8-sig"))
        evs = data.get("events") if isinstance(data, dict) else None
        if not isinstance(evs, list):
            warn(f"{EVENTS_NAME} ignored: no \"events\" list")
            return [], warns
    except Exception as e:
        warn(f"{EVENTS_NAME} ignored: not readable ({e})")
        return [], warns
    out, seen = [], set()
    for i, e in enumerate(evs, 1):
        try:
            if not isinstance(e, dict):
                raise ValueError("not an object")
            slug = e.get("slug")
            if not isinstance(slug, str) or not slug.strip():
                raise ValueError("no slug")
            slug = slug.strip()
            d = e.get("date")
            if not isinstance(d, str):
                raise ValueError("no date")
            d = _date.fromisoformat(d.strip()).isoformat()
            sl = e.get("sleeves")
            if not isinstance(sl, list) or not sl or any(s not in EVENT_SLEEVES for s in sl):
                raise ValueError(f"sleeves must be a non-empty list of {' / '.join(EVENT_SLEEVES)} (got {sl!r})")
            sy = e.get("symbols", [])
            if sy is None:
                sy = []
            if not isinstance(sy, list) or any(not isinstance(s, str) or not s.strip() for s in sy):
                raise ValueError(f"symbols must be a list of symbol strings (got {sy!r})")
        except Exception as ex:
            warn(f"{EVENTS_NAME} entry {i} skipped: {ex}")
            continue
        if (slug, d) in seen:
            warn(f"{EVENTS_NAME} entry {i} skipped: duplicate of {slug} {d}")
            continue
        seen.add((slug, d))
        out.append(dict(slug=slug, date=d, symbols=sorted({s.strip() for s in sy}), sleeves=list(dict.fromkeys(sl)),
                        event=str(e.get("event") or ""), source=str(e.get("source") or ""), origin="calendar"))
    return sorted(out, key=lambda ev: (ev["date"], ev["slug"])), warns


def calendar_events(sleeve: str, market_day: str, path=None) -> list[dict]:
    """The calendar's events for one sleeve ("fno" / "us") that are after the market date."""
    evs, _w = load_event_calendar(path)
    return [ev for ev in evs if sleeve in ev["sleeves"] and ev["date"] > market_day]


def calendar_warnings(path=None) -> list[str]:
    """The calendar's warnings (malformed file, skipped entries) for the journal's F&O Observations."""
    return load_event_calendar(path)[1]


def fno_events(market_day: str, path=None) -> list[dict]:
    """The events the F&O candidates must hold through: the thesis events (fno_event_dates) plus the calendar's
    future "fno" events, de-duplicated by (slug, date) (the thesis entry wins), sorted by date. Each has
    origin "thesis" or "calendar". Never raises."""
    out = fno_event_dates(market_day)
    seen = {(ev["slug"], ev["date"]) for ev in out}
    for ev in calendar_events("fno", market_day, path):
        if (ev["slug"], ev["date"]) not in seen:
            seen.add((ev["slug"], ev["date"]))
            out.append(ev)
    return sorted(out, key=lambda ev: (ev["date"], ev["slug"]))


def _event_name(ev: dict) -> str:
    """"rbi-mpc-december 2026-12-04", plus " (calendar)" for a calendar event: the expiry block's label."""
    return f"{ev['slug']} {ev['date']}" + (" (calendar)" if ev.get("origin") == "calendar" else "")


def _fno_calendar(out: dict, cal: list[dict]) -> list[dict]:
    """For each calendar F&O event, which tabled expiry block of each underlying it applies to holds through it
    (the first tabled expiry on or after the event day), read from the built blocks."""
    res = []
    for ev in cal:
        holds = []
        for x in out["underlyings"]:
            u = x["underlying"]
            if not fno_events_for(u, [ev]):
                continue
            if x.get("error"):
                holds.append(dict(underlying=u, label=None, expiry=None, note="no table today"))
                continue
            ok = sorted((ex for ex in x.get("expiries", []) if ex["expiry"] >= ev["date"]), key=lambda ex: ex["expiry"])
            if ok:
                holds.append(dict(underlying=u, label=ok[0]["label"], expiry=ok[0]["expiry"]))
            else:
                unl = any(w.startswith(f"event {ev['slug']} {ev['date']}:") for w in x.get("warnings", []))
                holds.append(dict(underlying=u, label=None, expiry=None,
                                  note="its expiry is not listed on NSE yet" if unl else "no tabled expiry holds through it"))
        res.append(dict(slug=ev["slug"], date=ev["date"], event=ev.get("event", ""), holds=holds))
    return res


def fno_events_for(sym: str, events: list[dict]) -> list[dict]:
    """The events an underlying's candidates must hold through: every one for an index (a policy day moves the
    index too), only its own theses' events for a stock."""
    return [ev for ev in events if sym in config.FNO_CANDIDATE_INDICES or sym in ev["symbols"]]


# ---------- shared maths ----------
REAL_LABELS = (chains.CHAIN_LABEL, chains.ZERODHA_CHAIN_LABEL, US_CHAIN_LABEL)


def next_weekday(day: str) -> str:
    """The first weekday after `day`: the earliest an order decided on `day` can fill (NSE holidays not known)."""
    d = _date.fromisoformat(day) + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.isoformat()


class Ladder:
    """The strikes a credit leg may use, and what "n steps" means. India (step given): a step is the config
    strike step, and a strike n steps away must itself be in `strikes` (NSE's listed strikes when there is a
    chain, else the config grid). US (step None): a step is one listed strike."""

    def __init__(self, strikes, atm: float, step: float | None = None):
        self.k = sorted({round(float(x), 4) for x in strikes})
        self.atm, self.step = round(float(atm), 4), step

    def _idx(self, K):
        return min(range(len(self.k)), key=lambda j: abs(self.k[j] - K)) if self.k else None

    def beyond(self, K: float, n: int, d: int):
        """The strike n steps from K in direction d (-1 down, +1 up), or None if it is not listed."""
        if self.step:
            K2 = round(K + d * n * self.step, 4)
            return K2 if any(abs(K2 - x) < 1e-6 for x in self.k) else None
        i = self._idx(K)
        if i is None or abs(self.k[i] - K) > 1e-6 or not 0 <= i + d * n < len(self.k):
            return None
        return self.k[i + d * n]

    def shorts(self, d: int) -> list:
        """Strikes on side d at least one step from ATM."""
        if self.step:
            lim = self.atm + d * self.step
        else:
            i = self._idx(self.atm)
            if i is None or not 0 <= i + d < len(self.k):
                return []
            lim = self.k[i + d]
        return [K for K in self.k if d * (K - lim) >= -1e-6]


def _leg(side, right, strike, q):
    q = q or {}
    d = dict(side=side, right=right, strike=strike, price=q.get("price"), src=q.get("src"),
             bid=q.get("bid"), ask=q.get("ask"), **({"last": q.get("last")} if "last" in q else {}))
    fe = q.get("fill_est_buy" if side == "BUY" else "fill_est_sell")
    if fe is not None:
        d["fill_est"] = fe            # India: the engine's fill price at today's spot (_fno_fill_est)
    return d


def _leg_name(l, us=False) -> str:
    r = ("CALL" if l["right"] == "CE" else "PUT") if us else l["right"]
    return f"{l['side']} {r} {_k(l['strike'])}"


def _pick_credit(ladder: Ladder, rows: dict, em, right: str, d: int, quote_at, usable, W: int = 2):
    """(short quote, wing quote, short strike, wing strike, note) for one credit side, or (None, why).
    Short = the listed strike nearest ATM + d x expected move, at least one step from ATM; wing = the
    configured number of steps further out (1 if that is not listed / priced). Else the fallback toward ATM:
    short ATM+d, wing ATM+2d (the tabled rows). `usable(q, K)` says whether a quote can be used."""
    W = max(1, int(W))
    why = []
    if em:
        cands = ladder.shorts(d)
        if cands:
            target = ladder.atm + d * em
            Ks = min(cands, key=lambda K: (abs(K - target), d * K))
            qs = quote_at(Ks, right)
            if usable(qs, Ks):
                tried = []
                for n in dict.fromkeys((W, 1)):
                    Kw = ladder.beyond(Ks, n, d)
                    if Kw is None:
                        tried.append(f"{_k(Ks + d * n * ladder.step) if ladder.step else f'{n} listed strikes out'} not listed")
                        continue
                    qw = quote_at(Kw, right)
                    if usable(qw, Kw):
                        note = "" if n == W else f"wing {n} step beyond the short ({'; '.join(tried)})"
                        return qs, qw, Ks, Kw, note
                    tried.append(f"{_k(Kw)} has no usable price")
                why.append(f"short {_k(Ks)}: no usable wing ({'; '.join(tried)})")
            else:
                why.append(f"wanted short {_k(Ks)} has no usable price")
        else:
            why.append("no listed strike one step or more from ATM")
    else:
        why.append("no ATM straddle price (expected move unknown)")
    r1, r2 = rows.get(d), rows.get(2 * d)
    if r1 and r2:
        q1, q2 = r1.get(right), r2.get(right)
        if usable(q1, r1["strike"]) and usable(q2, r2["strike"]):
            return q1, q2, r1["strike"], r2["strike"], f"fallback to ATM{d:+d} / ATM{2 * d:+d}: " + "; ".join(why)
    return None, "; ".join(why + [f"fallback ATM{d:+d} / ATM{2 * d:+d} has no usable price either"])


def simulate(book, legs: list, lots: int, ctx: dict) -> dict:
    """Run `legs` as OPEN orders of one queue through the engine's own checks on a copy of the book, the way
    derivbook.run_orders does: BUY (cover) orders before SELL options, each in the given order; per leg the
    opposite-side check of run_orders, then DerivBook.check_open (breaker, slots, cover and short-option
    margin, 10% per-order risk, free cash), then the cash and position effect of DerivBook.open (no ledger).
    A rejected leg is not opened and the next one is still tried, as in the engine. Prices are the table's
    close prices (the real fill price differs). Returns {ok, rejects, legs, margin, cash_change, cash_need,
    slots, eq0, cap0}."""
    sim = copy.copy(book)
    sim.positions = {k: copy.copy(p) for k, p in book.positions.items()}
    eq0 = sim.equity()
    order = [l for l in legs if l["side"] == "BUY"] + [l for l in legs if l["side"] != "BUY"]
    rejects, out, spent, need_peak = [], [], 0.0, 0.0
    for l in order:
        kind = "CALL" if l["right"] == "CE" else "PUT"
        side = "LONG" if l["side"] == "BUY" else "SHORT"
        price = float(l["price"])
        pos = DPos(id="", underlying=ctx["underlying"], kind=kind, side=side, lots=lots, lot_size=ctx["lot"],
                   entry_price=price, entry_date=ctx.get("fill_day") or "", strike=l["strike"], expiry=ctx["expiry"],
                   spot=ctx.get("spot"))
        pos.id = position_id(ctx["underlying"], kind, side, ctx["expiry"], l["strike"], ctx["letters"])
        chg = ctx["charge_fn"](l["side"], lots, price)
        name = _leg_name(l, ctx.get("us", False))
        cap = config.DERIV_MAX_ORDER_RISK_PCT * sim.equity()
        try:
            opp = pos.id[:-1] + ("S" if side == "LONG" else "L")
            if pos.id not in sim.positions and opp in sim.positions:
                raise Reject(f"{opp} is open on the other side; CLOSE it instead")
            risk, what = sim.check_open(pos, price, chg, ctx.get("fill_day") or "9999-12-31")
        except Reject as e:
            msg = str(e)
            if side == "SHORT" and msg.startswith("naked short option") and any(
                    r.startswith(f"BUY {name.split()[1]} ") for r in rejects):
                msg = "naked short option (its cover above was rejected)"
            rejects.append(f"{name}: {msg}")
            continue
        units = pos.units
        delta = -(price * units + chg) if side == "LONG" else price * units - chg - pos.margin
        need = -delta if side == "LONG" else pos.margin + chg - price * units
        need_peak = max(need_peak, spent + need)
        spent -= delta
        sim.cash += delta
        old = sim.positions.get(pos.id)
        if old is not None:
            old.lots += pos.lots
            old.margin += pos.margin
        else:
            pos.mark = price
            sim.positions[pos.id] = pos
        out.append(dict(leg=name, risk=round(risk, 2), what=what, margin=round(pos.margin, 2),
                        cash_change=round(delta, 2), cap=round(cap, 2), new_slot=old is None))
    return dict(ok=not rejects, rejects=rejects, legs=out, margin=round(sum(x["margin"] for x in out), 2),
                cash_change=round(sum(x["cash_change"] for x in out), 2), cash_need=round(need_peak, 2),
                slots=sum(1 for x in out if x["new_slot"]), eq0=eq0, cap0=config.DERIV_MAX_ORDER_RISK_PCT * eq0)


def max_lots(book, legs, ctx, limit: int = 100_000) -> int:
    """The largest whole number of lots for which every leg passes simulate() (0 if 1 lot does not)."""
    if not simulate(book, legs, 1, ctx)["ok"]:
        return 0
    lo, hi = 1, 2
    while hi <= limit and simulate(book, legs, hi, ctx)["ok"]:
        lo, hi = hi, hi * 2
    if hi > limit:
        return lo
    while hi - lo > 1:
        mid = (lo + hi) // 2
        lo, hi = (mid, hi) if simulate(book, legs, mid, ctx)["ok"] else (lo, mid)
    return lo


def _quote_flags(legs: list, ccy: str, atm_real: bool, us: bool) -> list[str]:
    """Liquidity labels (DEFERRED 13). wide: a real-chain leg whose ask - bid is more than
    CANDIDATE_WIDE_SPREAD_PCT of its mid and more than CANDIDATE_WIDE_SPREAD_MIN; one-sided: only a bid or
    only an ask; no quote: neither, while the expiry's ATM call and put do have real quotes (so it is this
    strike, not the hour, that has no market; the price is then a model price). US before the open (the 17:33 IST
    run) Yahoo shows no bid/ask at all; what it does show is each contract's last trade, so a model-priced US leg
    that did not trade in the latest session is labelled "no trade in the last session"."""
    out = []
    for l in legs:
        b, a = float(l.get("bid") or 0), float(l.get("ask") or 0)
        nm = _leg_name(l, us)
        if l.get("src") in REAL_LABELS and b > 0 and a > 0:
            m = (a + b) / 2
            if a - b > max(config.CANDIDATE_WIDE_SPREAD_PCT * m, config.CANDIDATE_WIDE_SPREAD_MIN[ccy]):
                out.append(f"wide quotes: {nm} bid/ask {b:,.2f}/{a:,.2f} ({(a - b) / m * 100:.0f}% of mid)")
        elif (b > 0) != (a > 0):
            out.append(f"wide quotes: {nm} one-sided (bid {b:,.2f} / ask {a:,.2f}), model price")
        elif atm_real and l.get("src") not in REAL_LABELS:
            out.append(f"no quote: {nm}, model price")
        elif us and l.get("src") not in REAL_LABELS and "last" in l and l["last"] is None:
            out.append(f"no trade in the last session: {nm}")
    return out


def _quotes_cell(st: dict) -> str:
    """The journal's Quotes cell: the labels, else whether every leg is a real two-sided mid."""
    if st.get("quotes"):
        return "; ".join(st["quotes"])
    return "ok" if all(l.get("src") in REAL_LABELS for l in st["legs"]) else "model prices (no live bid/ask)"


def _finish(s: dict, ctx: dict) -> dict:
    """Engine check, max lots, % of equity, event-trade size and quote labels for a structure whose numbers
    (max_loss etc.) are set."""
    book, legs = ctx["book"], s["legs"]
    exp, fd = ctx["expiry"], ctx.get("fill_day")
    eq = ctx["eq"]
    s["max_loss_pct"] = round(s["max_loss"] / eq * 100, 2) if eq else None
    s["event_size"] = "fits" if eq and s["max_loss"] <= config.EVENT_TRADE_MAX_LOSS_PCT * eq + 1e-9 else "too big"
    s["quotes"] = _quote_flags(legs, ctx["ccy"], ctx.get("atm_real", False), ctx.get("us", False))
    if legs and all(l.get("fill_est") is not None for l in legs):
        net = sum(l["fill_est"] if l["side"] == "SELL" else -l["fill_est"] for l in legs)   # + = credit
        if s.get("kind") == "credit":
            s["fill_est_credit"], s["fill_est_lot"] = round(net, 4), round(net * s["units"], 2)
            tab = s.get("net_credit") or 0
            if net <= 0 or net < 0.5 * tab:
                s["quotes"].append(f"engine fill est. credit {net:,.2f} / unit vs {tab:,.2f} at the mids (the fill "
                                   f"model prices each leg on NSE's own strike IV, not the mid)")
        else:
            s["fill_est_debit"], s["fill_est_lot"] = round(-net, 4), round(-net * s["units"], 2)
    longs = [l["price"] * s["units"] for l in legs if l["side"] == "BUY"]
    s["long_leg_max"] = round(max(longs), 2) if longs else 0.0
    s["long_leg_pct"] = round(s["long_leg_max"] / eq * 100, 2) if eq else None
    if fd and exp <= fd:
        s.update(verdict="n/a", why=f"expires {exp}, on or before the earliest fill day {fd}: the engine rejects an "
                                    f"expiry that is not after the fill day")
        return s
    r = simulate(book, legs, 1, ctx)
    top = max(r["legs"], key=lambda x: x["risk"]) if r["legs"] else None
    s.update(margin=r["margin"], cash_change=r["cash_change"], cash_need=r["cash_need"], slots_needed=len(legs),
             order_risk=top["risk"] if top else None,
             order_risk_what=f"{top['what']} ({top['leg']})" if top else None,
             order_risk_pct=round(top["risk"] / r["eq0"] * 100, 2) if top and r["eq0"] else None,
             rule_10pct="FAIL" if any("per-order risk" in x for x in r["rejects"]) else "PASS",
             free_cash="FAIL" if any("free cash" in x for x in r["rejects"]) else "PASS",
             slots="FAIL" if any("open positions" in x for x in r["rejects"]) else "PASS",
             max_lots=max_lots(book, legs, ctx) if r["ok"] else 0,
             verdict="PASS" if r["ok"] else "FAIL", why="; ".join(r["rejects"]))
    return s


def _structures(rows: dict, ctx: dict, quote_at=None, ladder: Ladder | None = None) -> list[dict]:
    """rows: {offset: {"strike", "CE": {"price", "src", "bid", "ask", ...}, "PE": {...}}} for ATM-2..ATM+2.
    quote_at(K, right) prices any other strike of the expiry (credit legs); ladder: the strikes they may
    use. ctx: book, underlying, lot, expiry, letters, charge_fn(side, lots, price) -> total, fill_day (None:
    no expiry check), eq, ccy, us, atm_real, min_price. Returns six structures: three debit, three credit."""
    units, cf = ctx["lot"], ctx["charge_fn"]

    def row_leg(side, right, off):
        r = rows.get(off) or {}
        return _leg(side, right, r.get("strike"), r.get(right))

    out = []
    for name, legs in [("Bull call spread", [row_leg("BUY", "CE", 0), row_leg("SELL", "CE", 2)]),
                       ("Bear put spread", [row_leg("BUY", "PE", 0), row_leg("SELL", "PE", -2)]),
                       ("Long straddle", [row_leg("BUY", "CE", 0), row_leg("BUY", "PE", 0)])]:
        s = dict(name=name, kind="debit", expiry=ctx["expiry"], legs=legs, units=units)
        if any(l["price"] is None or l["strike"] is None for l in legs):
            out.append({**s, "verdict": "n/a", "why": "a leg has no price"})
            continue
        debit = sum((l["price"] if l["side"] == "BUY" else -l["price"]) for l in legs)
        k0 = legs[0]["strike"]
        ch = sum(cf(l["side"], 1, l["price"]) for l in legs)
        if name == "Long straddle":
            mg, be = None, [round(k0 - debit, 2), round(k0 + debit, 2)]
        else:
            gap = abs(legs[1]["strike"] - legs[0]["strike"])
            if not 0 < debit < gap:
                out.append({**s, "verdict": "n/a",
                            "why": f"inconsistent prices: net debit {debit:.2f} is outside 0..{gap:g} (strike gap)"})
                continue
            mg = round((gap - debit) * units - ch, 2)
            be = [round(k0 + debit if name.startswith("Bull") else k0 - debit, 2)]
        s.update(net_debit=round(debit, 4), net_debit_lot=round(debit * units, 2), entry_charges=round(ch, 2),
                 max_loss=round(debit * units + ch, 2), max_gain=mg, breakevens=be)
        out.append(_finish(s, ctx))

    # credit: expected move = the ATM straddle of this expiry
    r0 = rows.get(0) or {}
    ce0, pe0 = (r0.get("CE") or {}).get("price"), (r0.get("PE") or {}).get("price")
    em = (ce0 + pe0) if ce0 is not None and pe0 is not None else None
    minp = ctx.get("min_price", 0.0)

    def usable(q, K):
        if not q or q.get("price") is None or q["price"] < minp or q.get("listed") is False:
            return False
        st = ctx.get("strike_step")      # India: the engine rounds a strike to the config step
        return not st or abs(round(K / st) * st - K) < 1e-6

    if quote_at is None or ladder is None:
        quote_at = lambda K, right: next(((r.get(right)) for r in rows.values() if abs(r["strike"] - K) < 1e-6), None)
        ladder = Ladder([r["strike"] for r in rows.values()], r0.get("strike") or 0, ctx.get("strike_step"))
    sides = {}
    for key, right, d in (("put", "PE", -1), ("call", "CE", 1)):
        sides[key] = _pick_credit(ladder, rows, em, right, d, quote_at, usable,
                                    config.CREDIT_WING_STEPS[ctx["ccy"]])
    specs = [("Bull put spread", ["put"]), ("Bear call spread", ["call"]), ("Iron condor", ["put", "call"])]
    for name, keys in specs:
        s = dict(name=name, kind="credit", expiry=ctx["expiry"], units=units, expected_move=round(em, 4) if em else None)
        bad = [f"{k} side: {sides[k][1]}" for k in keys if sides[k][0] is None]
        if bad:
            s.update(legs=[], verdict="n/a", why="; ".join(bad))
            out.append(s)
            continue
        pairs = [(k, sides[k]) for k in keys]
        right = {"put": "PE", "call": "CE"}
        legs = [_leg("BUY", right[k], v[3], v[1]) for k, v in pairs] + [_leg("SELL", right[k], v[2], v[0]) for k, v in pairs]
        s["legs"] = legs
        notes = [f"{k}: {v[4]}" for k, v in pairs if v[4]]
        if notes:
            s["strike_note"] = "; ".join(notes)
        credits = {k: v[0]["price"] - v[1]["price"] for k, v in pairs}
        gaps = {k: abs(v[2] - v[3]) for k, v in pairs}
        total = sum(credits.values())
        badp = [f"{k} credit {credits[k]:.2f} is outside 0..{gaps[k]:g} (strike gap)" for k in keys
                if not 0 < credits[k] < gaps[k]]
        if not badp and not total < max(gaps.values()):
            badp.append(f"total credit {total:.2f} is not below the wider gap {max(gaps.values()):g}")
        if badp:
            s.update(verdict="n/a", why="inconsistent prices: " + "; ".join(badp))
            out.append(s)
            continue
        ch = sum(cf(l["side"], 1, l["price"]) for l in legs)
        be = []
        if "put" in keys:
            be.append(round(sides["put"][2] - total, 2))
        if "call" in keys:
            be.append(round(sides["call"][2] + total, 2))
        s.update(net_credit=round(total, 4), net_credit_lot=round(total * units, 2), entry_charges=round(ch, 2),
                 strike_gap=max(gaps.values()), max_loss=round((max(gaps.values()) - total) * units + ch, 2),
                 max_gain=round(total * units - ch, 2), breakevens=be)
        out.append(_finish(s, ctx))
    return out


def _src_text(r: dict) -> str:
    a, b = (r.get("CE") or {}).get("src"), (r.get("PE") or {}).get("src")
    return a if a == b else f"CE: {a}; PE: {b}"


# ---------- India ----------
def build_fno(market, book, market_day: str, chain_notes=(), use_kite: bool = False) -> dict:
    """Candidates from the F&O sleeve's market (fno.FnoMarket, after the day's fetch) and book. use_kite (an
    online run): when kite.available(), each PASS structure gets "zerodha_margin", Kite's basket margin for
    its legs (read only), shown as a "Zerodha margin" column; otherwise a "Kite: <why>" note."""
    from .fno import spec
    eq, cash = book.equity(), book.cash
    cap = config.DERIV_MAX_ORDER_RISK_PCT * eq
    free = config.DERIV_MAX_POSITIONS - len(book.positions)
    nxt = next_weekday(market_day)          # the earliest fill day, as simulate() uses it
    breaker = book.blocked(nxt)
    syms, notes, why = fno_candidate_symbols()
    events = fno_events(market_day)
    out = dict(date=market_day, currency="INR", equity=round(eq, 2), cash=round(cash, 2), cap=round(cap, 2),
               open_positions=len(book.positions), max_positions=config.DERIV_MAX_POSITIONS,
               breaker=book.breaker_text() if breaker else None, notes=list(notes), underlyings=[])
    vix = None
    try:
        vix = market.spot_close(config.FNO_VIX_SYMBOL, market_day)[0]
    except Exception:
        pass
    out["india_vix"] = vix
    for u in syms:
        try:
            out["underlyings"].append(_fno_one(market, spec(u), market_day, eq, why.get(u, []),
                                               fno_events_for(u, events), book))
        except Exception as e:
            log.exception("F&O candidates for %s failed", u)
            out["underlyings"].append(dict(underlying=u, error=str(e), why=why.get(u, [])))
    srcs = {r.get("source") for x in out["underlyings"] for ex in x.get("expiries", []) for r in ex.get("rows", [])}
    out["sources"] = sorted(s for s in srcs if s)
    # every future calendar event (also one a thesis already names): which built block holds through it
    out["calendar"] = _fno_calendar(out, calendar_events("fno", market_day))
    _zerodha_margins(out, use_kite)
    return out


def _zerodha_margins(out: dict, use_kite: bool) -> None:
    from . import kite
    if not use_kite:
        out["notes"].append("Kite: not used (offline run); no Zerodha margin column")
        return
    if not kite.available():
        out["notes"].append(f"Kite: {kite.reason}; no Zerodha margin column")
        return
    out["zerodha_margin"] = True
    missing = 0
    for x in out["underlyings"]:
        for ex in x.get("expiries", []):
            for st in ex.get("structures", []):
                if st.get("verdict") != "PASS":
                    continue
                legs = [dict(underlying=x["underlying"], expiry=ex["expiry"], strike=l["strike"], right=l["right"],
                             side=l["side"], units=st["units"]) for l in st["legs"]]
                st["zerodha_margin"] = kite.basket_margin(legs)
                missing += st["zerodha_margin"] is None
    if missing:
        out["notes"].append(f"Kite: basket margin not returned for {missing} structure(s) (blank in the Zerodha margin column)")


def _fno_one(market, sp, market_day, eq, why, events=(), book=None) -> dict:
    u = sp["name"]
    S, sday = market.spot_close(sp["yahoo"], market_day)
    snap = chains.usable(market.today.get(u), market_day)
    listed = market.listed(u)
    exps = []
    monthly = chains.monthly_after(market_day, sp["weekday"], listed)
    if u in config.FNO_WEEKLY_INDICES:
        w = chains.nearest_after(market_day, listed) if listed else None
        if not listed:          # no NSE list: the next <weekday> after the market date
            d = _date.fromisoformat(market_day) + timedelta(days=1)
            while d.weekday() != sp["weekday"]:
                d += timedelta(days=1)
            w = d.isoformat()
        if w and w != monthly:
            exps.append(("weekly", w))
    exps.append(("monthly", monthly))
    # an event after the current monthly expiry: also the first monthly expiry that holds through it, so the
    # reviewer can price a structure that is still open on the event day. Events sharing an expiry share a row.
    by_exp, unlisted = {}, []
    for ev in events:
        e = chains.event_expiry(ev["date"], sp["weekday"], listed)
        if e in {x for _l, x in exps}:
            continue                    # the event is before an expiry already shown
        if listed and e not in listed:  # stocks list ~3 monthlies: an order for it would be rejected today
            unlisted.append(f"event {ev['slug']} {ev['date']}: its expiry {e} is not listed on NSE yet")
            continue
        by_exp.setdefault(e, []).append(_event_name(ev))
    exps += [("event: " + "; ".join(evs), e) for e, evs in sorted(by_exp.items())]
    res = dict(underlying=u, kind=sp["kind"], lot=sp["lot"], step=sp["step"], why=why, spot=round(S, 2), spot_date=sday,
               chain=dict(timestamp=snap["timestamp"], underlying=snap.get("underlying_value"), source=snap.get("source"))
               if snap else None, warnings=unlisted, expiries=[])
    if snap and snap.get("underlying_value") and abs(snap["underlying_value"] / S - 1) > 0.005:
        res["warnings"].append(f"{chains.source_name(snap)} chain underlying {snap['underlying_value']:,.2f} differs from the Yahoo close "
                               f"{S:,.2f} by more than 0.5%")
    fill_day = next_weekday(market_day)
    for label, e in exps:
        t = years_to(e, market_day)
        atm = round(round(S / sp["step"]) * sp["step"], 4)
        has_chain = bool(snap and e in snap.get("chains", {}))
        if has_chain:
            steps = chains.listed_steps(snap, e, S)
            if steps and sp["step"] not in steps:
                res["warnings"].append(f"{e}: NSE strike gaps near the spot are {steps}, config step is {sp['step']:g}")
        cache = {}

        def quote_of(K, right, e=e, t=t, has_chain=has_chain, cache=cache):
            """One contract at the close: the chain mid, else BS on the chain IV, else the model (labelled)."""
            key = (round(K, 4), right)
            if key in cache:
                return cache[key]
            kind = "CALL" if right == "CE" else "PUT"
            q = chains.quote(snap, e, K, right) if has_chain else None
            m = chains.mid(q)
            if m is not None:
                price, src, iv = m, chains.chain_label(snap), (q["iv"] / 100 if q.get("iv", 0) > 0 else None)
            else:
                iv = (chains.strike_iv(snap, e, K, right) or chains.atm_iv(snap, e, S)) if has_chain else None
                if iv:
                    src = chains.CHAIN_IV_LABEL
                else:
                    iv, _how = market.model_sigma(sp, market_day, at_open=False)
                    src = chains.MODEL_LABEL
                price = round(bs_price(S, K, t, config.CARRY_RATE, iv, kind), 2)
            lot = price * sp["lot"]
            cache[key] = dict(price=price, src=src, iv_pct=round(iv * 100, 2) if iv else None,
                              bid=q.get("bid") if q else None, ask=q.get("ask") if q else None,
                              listed=(q is not None) if has_chain else None,
                              per_lot=round(lot, 2), pct=round(lot / eq * 100, 2) if eq else None,
                              **_fno_fill_est(market, sp, u, e, K, kind, S, fill_day))
            return cache[key]

        rows, table = {}, []
        for off in OFFSETS:
            K = round(atm + off * sp["step"], 4)
            r = dict(offset=off, strike=K, CE=quote_of(K, "CE"), PE=quote_of(K, "PE"))
            r["source"] = _src_text(r)
            rows[off] = r
            table.append(r)
            if has_chain and r["CE"]["listed"] is False and r["PE"]["listed"] is False:
                res["warnings"].append(f"{e} strike {K:g} is not listed on NSE")
        # the strikes a credit leg may use: NSE's listed strikes (the cache keeps +/-CHAIN_STRIKE_BAND of the
        # spot), else the config step grid over the same band
        if has_chain:
            grid = [float(k) for k in snap["chains"][e]]
        else:
            n = int(config.CHAIN_STRIKE_BAND * S / sp["step"])
            grid = [round(atm + i * sp["step"], 4) for i in range(-n, n + 1) if atm + i * sp["step"] > 0]
        atm_real = all((rows[0].get(x) or {}).get("src") in REAL_LABELS for x in ("CE", "PE"))
        ctx = dict(book=book, underlying=u, lot=sp["lot"], expiry=e, letters=("CE", "PE"), fill_day=fill_day, spot=S,
                   charge_fn=lambda side, lots, px, L=sp["lot"]: charges(side, lots * L, px, "OPT").total,
                   eq=eq, ccy="INR", us=False, atm_real=atm_real, strike_step=sp["step"],
                   min_price=config.CANDIDATE_MIN_LEG_PRICE["INR"])
        st = _structures(rows, ctx, quote_of, Ladder(grid, atm, sp["step"]))
        res["expiries"].append(dict(label=label, expiry=e, days=(_date.fromisoformat(e) - _date.fromisoformat(market_day)).days,
                                    atm=atm, atm_iv_pct=round(chains.atm_iv(snap, e, S) * 100, 2) if has_chain and chains.atm_iv(snap, e, S) else None,
                                    expected_move=_em(rows), fill_day=fill_day, rows=table, structures=st))
    return res


def _fno_fill_est(market, sp, u, e, K, kind, S, fill_day) -> dict:
    """What the engine would fill this contract at on the next open, with today's close standing in for that
    open: the engine's own fno.FnoMarket.option_price (its fill_sigma on the snapshot the fill will read, which
    is today's) and fno.fill_premium (+/- slippage), so the estimate and the fill agree by construction except
    for the overnight spot move. {} if there is no vol (the fill would not happen either)."""
    from .fno import fill_premium
    try:
        snap = market.today.get(u) or market.prior.get(u)
        m, _how, _sig, _S = market.option_price(sp, kind, K, e, fill_day, at_open=True, spot=S, prior=snap)
    except Exception:
        return {}
    return dict(fill_est_buy=fill_premium(m, True), fill_est_sell=fill_premium(m, False))


def _em(rows) -> float | None:
    r0 = rows.get(0) or {}
    c, p = (r0.get("CE") or {}).get("price"), (r0.get("PE") or {}).get("price")
    return round(c + p, 4) if c is not None and p is not None else None


# ---------- US ----------
def _is_monthly(e: str) -> bool:
    d = _date.fromisoformat(e)
    return (d.weekday() == 4 and 15 <= d.day <= 21) or (d.weekday() == 3 and 14 <= d.day <= 20)


def build_us(market, book, market_day: str) -> dict:
    """Candidates from the US sleeve's market (us.UsMarket: its cached chains, so nothing is fetched twice)."""
    eq, cash = book.equity(), book.cash
    cap = config.DERIV_MAX_ORDER_RISK_PCT * eq
    free = config.DERIV_MAX_POSITIONS - len(book.positions)
    nxt = next_weekday(market_day)          # the earliest fill day, as simulate() uses it
    breaker = book.blocked(nxt)
    out = dict(date=market_day, currency="USD", equity=round(eq, 2), cash=round(cash, 2), cap=round(cap, 2),
               open_positions=len(book.positions), max_positions=config.DERIV_MAX_POSITIONS,
               breaker=book.breaker_text() if breaker else None, fetched_at=market.fetched_at, usd_inr=book.fx(),
               notes=[], underlyings=[])
    over = []
    for t in config.US_CANDIDATE_TICKERS:
        if t not in config.US_CANDIDATE_REFERENCE:
            try:
                px, _d = market.last_close(t)
                if px > config.US_CANDIDATE_MAX_PRICE:
                    over.append(f"{t} ${px:,.2f}")
                    continue
            except Exception:
                pass                       # no close: _us_one reports it
        try:
            out["underlyings"].append(_us_one(market, t, market_day, eq, book))
        except Exception as e:
            log.warning("US candidates for %s: %s", t, e)
            out["underlyings"].append(dict(underlying=t, error=str(e)))
    if over:
        out["notes"].append(f"Above the ${config.US_CANDIDATE_MAX_PRICE:g} price bar, not shown: " + ", ".join(over))
    nfill = sum(x.get("fill_check_fails", 0) for x in out["underlyings"])
    if nfill:
        out["notes"].append(
            f"US fill note: {nfill} structure(s) that pass the risk rules are FAIL because the engine could not fill "
            f"every leg on this run's quotes (us.UsMarket.open_option_price: no bid/ask and no last-trade IV for the "
            f"contract, or a model price under ${config.US_MIN_OPTION_PRICE:.2f}); such an order stays queued and a "
            f"short whose cover did not fill is rejected as naked.")
    out["reference"] = list(config.US_CANDIDATE_REFERENCE)
    srcs = {r.get("source") for x in out["underlyings"] for ex in x.get("expiries", []) for r in ex.get("rows", [])}
    out["sources"] = sorted(s for s in srcs if s)
    out["calendar"] = _us_calendar(out, calendar_events("us", market_day))
    return out


def _us_calendar(out: dict, cal: list[dict]) -> list[dict]:
    """For each calendar US event: the tickers whose tabled expiry (the nearest monthly 14-42 days out) is
    still open on the event day, and the ones whose expiry ends before it, read from the built tables."""
    res = []
    for ev in cal:
        held, short = {}, {}
        for x in out["underlyings"]:
            if x.get("error") or not x.get("expiries"):
                continue
            e = x["expiries"][0]["expiry"]
            (held if e >= ev["date"] else short).setdefault(e, []).append(x["underlying"])
        res.append(dict(slug=ev["slug"], date=ev["date"], event=ev.get("event", ""), held=held, short=short))
    return res


def _us_one(market, t, market_day, eq, book=None) -> dict:
    S, sday = market.last_close(t)
    exps = market.expiries(t)
    if not exps:
        raise NotYet("no option expiries from yfinance")
    md = _date.fromisoformat(sday)
    inrange = [e for e in exps if config.US_CANDIDATE_MIN_DAYS <= (_date.fromisoformat(e) - md).days <= config.US_CANDIDATE_MAX_DAYS]
    monthly = [e for e in inrange if _is_monthly(e)]
    if not inrange:
        raise NotYet(f"no listed expiry {config.US_CANDIDATE_MIN_DAYS}-{config.US_CANDIDATE_MAX_DAYS} days out")
    e = (monthly or inrange)[0]
    calls, puts = market.chain(t, e, "CALL"), market.chain(t, e, "PUT")
    if calls is None or puts is None or calls.empty:
        raise NotYet(f"no {e} chain")
    strikes = us_strikes(calls, puts)
    i = min(range(len(strikes)), key=lambda j: abs(strikes[j] - S))
    tt = years_to(e, sday)

    def raw_q(K, right):
        frame = calls if right == "CE" else puts
        row = frame[(frame["strike"] - K).abs() < 1e-6]
        return row.iloc[0] if len(row) else None

    lt_iv, _n = last_trade_iv(calls, puts, S, md, tt)
    cache = {}

    def quote_of(K, right):
        """One listed contract: the chain mid, else BS on Yahoo's IV, else BS on the last-trade IV (labelled)."""
        key = (round(K, 4), right)
        if key in cache:
            return cache[key]
        kind = "CALL" if right == "CE" else "PUT"
        q = raw_q(K, right)
        bid = float(q.get("bid") or 0) if q is not None else 0.0
        ask = float(q.get("ask") or 0) if q is not None else 0.0
        civ = q.get("impliedVolatility") if q is not None else None
        civ = float(civ) if (bid > 0 or ask > 0) and civ is not None and civ == civ and float(civ) >= 0.01 else None
        used_iv = civ
        if bid > 0 and ask > 0 and ask >= bid:
            price, src = round((bid + ask) / 2, 4), US_CHAIN_LABEL
        elif civ:
            price, src = round(bs_price(S, K, tt, config.US_RISK_FREE_RATE, civ, kind), 4), US_IV_LABEL
        elif lt_iv:
            used_iv = lt_iv
            price, src = round(bs_price(S, K, tt, config.US_RISK_FREE_RATE, lt_iv, kind), 4), US_LT_LABEL
        else:
            price, src = None, "no quote"
        per = None if price is None else round(price * config.US_CONTRACT_SIZE, 2)
        last = float(q.get("lastPrice") or 0) if q is not None and _traded_since(q, md) else None
        cache[key] = dict(price=price, src=src, iv_pct=round(used_iv * 100, 2) if used_iv else None, bid=bid or None,
                          ask=ask or None, last=last, contract=str(q.get("contractSymbol", "")) if q is not None else "",
                          per_lot=per, pct=round(per / eq * 100, 2) if per is not None and eq else None)
        return cache[key]

    rows, table = {}, []
    for off in OFFSETS:
        if not 0 <= i + off < len(strikes):
            continue
        K = strikes[i + off]
        r = dict(offset=off, strike=K, CE=quote_of(K, "CE"), PE=quote_of(K, "PE"))
        r["source"] = _src_text(r)
        rows[off] = r
        table.append(r)
    atm_real = all((rows[0].get(x) or {}).get("src") in REAL_LABELS for x in ("CE", "PE"))
    # fill_day: the next weekday after the US market date (US holidays not known); it drives the circuit-breaker
    # check in check_open and the expiry check (never binding here: expiries are 14+ days out)
    ctx = dict(book=book, underlying=t, lot=config.US_CONTRACT_SIZE, expiry=e, letters=("C", "P"), fill_day=next_weekday(sday),
               charge_fn=lambda side, lots, px: us_charges(side, lots, px, option=True).total,
               eq=eq, ccy="USD", us=True, atm_real=atm_real, strike_step=None,
               min_price=config.CANDIDATE_MIN_LEG_PRICE["USD"])
    st = _structures(rows, ctx, quote_of, Ladder(strikes, strikes[i]))
    nfill = _us_fill_check(market, t, e, sday, st, ctx)
    return dict(underlying=t, fill_check_fails=nfill, spot=round(S, 2), spot_date=sday, lot=config.US_CONTRACT_SIZE, why=[],
                monthly=bool(monthly), warnings=[] if monthly else [f"no third-Friday expiry in range; using {e}"],
                expiries=[dict(label="monthly" if monthly else "nearest in range", expiry=e,
                               days=(_date.fromisoformat(e) - md).days, atm=strikes[i], rows=table, structures=st,
                               expected_move=_em(rows), atm_iv_pct=round(lt_iv * 100, 2) if lt_iv else None)])


def _us_fill_check(market, t, e, sday, structures, ctx=None) -> int:
    """The mirror of a US option fill: each leg of a PASS structure is priced by the engine's own
    us.UsMarket.open_option_price on this run's chain (what an OPEN would fill at on these quotes). A leg it
    cannot price (NotYet: no quote and no last-trade IV, or under US_MIN_OPTION_PRICE) would stay queued while
    its short is rejected as naked, so the structure is FAIL here. If the engine's prices differ from the
    table's, the risk check is re-run on the engine's prices. A leg filled at a model price (before the US open:
    the last-trade IV, not a quote) is labelled. The real fill is at the next run's quotes, which can differ.
    Returns how many structures it turned to FAIL."""
    if not hasattr(market, "open_option_price"):
        return 0
    n = 0
    for s in structures:
        if s.get("verdict") != "PASS":
            continue
        bad, model, fill_legs = [], [], []
        for l in s["legs"]:
            p = DPos(id="", underlying=t, kind="CALL" if l["right"] == "CE" else "PUT",
                     side="LONG" if l["side"] == "BUY" else "SHORT", lots=1, lot_size=config.US_CONTRACT_SIZE,
                     entry_price=0.0, entry_date=sday, strike=l["strike"], expiry=e)
            nm = _leg_name(l, True)
            try:
                px, how, label, _iv = market.open_option_price(p, sday)
            except Exception as ex:          # NotYet: the order would stay queued, so its short is rejected
                bad.append(f"{nm} not filled ({str(ex)[:90]})")
                continue
            l["engine_fill_now"] = round(px, 4)
            fill_legs.append({**l, "price": px})
            if not str(label).startswith("real"):
                model.append(f"{nm} {px:.2f}")
        if bad:
            n += 1
            s.update(verdict="FAIL", max_lots=0, why="no fill on this run's quotes (US fill note): " + "; ".join(bad))
            continue
        if ctx and any(abs(fl["price"] - l["price"]) > 1e-6 for fl, l in zip(fill_legs, s["legs"])):
            r = simulate(ctx["book"], fill_legs, 1, ctx)
            if not r["ok"]:
                n += 1
                s.update(verdict="FAIL", max_lots=0, why="at the engine's fill prices: " + "; ".join(r["rejects"]))
                continue
        if model:
            s.setdefault("quotes", []).append("engine fill now = MODEL price (no bid/ask; last-trade IV), not a "
                                              "quote: " + ", ".join(model))
    return n


# ---------- state ----------
def save(path, d: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(d, indent=1, default=str) + "\n", encoding="utf-8")


def load(path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except Exception:
        return None


# ---------- journal ----------
def _m(v, ccy, dp=0):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "n/a"
    return (f"Rs {v:,.{dp}f}" if ccy == "INR" else f"${v:,.{dp if dp else 2}f}")


def _k(v):
    return f"{v:,.2f}".rstrip("0").rstrip(".") if v is not None else "-"


def _legs_text(s, us: bool = False) -> str:
    """The legs in the order to write them (covers first), each with its close price per unit."""
    return " + ".join(f"{_leg_name(l, us)} @ {l['price']:,.2f}" for l in s["legs"]) + f" ({s['expiry']})"


def structures_text(ccy: str) -> str:
    """The journal paragraph that explains the structure table (both sleeves)."""
    lot, step = ("lot", "strike step") if ccy == "INR" else ("contract (x100)", "listed strike")
    c, p = ("CE", "PE") if ccy == "INR" else ("CALL", "PUT")
    pct = config.EVENT_TRADE_MAX_LOSS_PCT * 100
    return (f"Structures, 1 {lot} each. Debit: bull call spread (BUY {c} ATM, SELL {c} ATM+2), bear put spread (BUY {p} "
            f"ATM, SELL {p} ATM-2), long straddle (BUY {c} + {p} ATM): max loss = net debit. Credit: bull put spread (SELL "
            f"{p} below the spot, BUY {p} further below), bear call spread (SELL {c} above, BUY {c} further above), iron "
            f"condor (both). Credit short strikes = the listed strikes nearest ATM -/+ the expected move (the ATM straddle of "
            f"that expiry), at least 1 {step} from ATM; each wing {config.CREDIT_WING_STEPS[ccy]} {step}(s) beyond its short "
            f"(1 if not listed or priced); a row that fell back toward ATM says so. A credit structure receives the net "
            f"credit; max loss = (strike gap - credit) x units + entry charges (condor: the wider side's gap minus the "
            f"total credit); max gain = credit x units - entry charges. **Margin**: the engine blocks strike gap x units "
            f"for every short leg (a condor blocks both sides, more than its max loss) and releases it when the short is "
            f"closed. **PASS/FAIL** is the engine's own check (derivbook.check_open) run on a copy of the book for every "
            f"leg, in the order the engine fills them (BUY covers first, then SELLs): per-order risk <= 10% of equity "
            f"(premium for a long leg, margin for a short leg), free cash, free position slots (a condor takes 4), "
            f"circuit breaker, expiry after the fill day. Each PASS is for that structure alone on today's book: two "
            f"structures queued together share the slots and the cash, and the engine fills leg by leg, so the second "
            f"can be left half-built. **Event-trade size: fits** = max loss <= {pct:g}% of sleeve "
            f"equity. **Quotes**: \"wide quotes\" = a real-chain leg whose ask - bid is over "
            f"{config.CANDIDATE_WIDE_SPREAD_PCT * 100:g}% of its mid, or a one-sided quote; \"no quote\" = the strike has "
            f"no bid/ask while the ATM does (model price). A labelled row still passes; its credit may exist only on "
            f"paper. Max loss includes entry charges, not exit charges. **To trade a row**: write ALL its legs in one "
            f"run, one OPEN per leg in the printed order (covers first), the same decision_date and lots, the numeric "
            f"strike, the exact expiry and instrument {c} / {p} exactly (other spellings are not recognised as "
            f"short options, so such a SELL can be checked before its cover and rejected as naked). To exit, CLOSE every leg by its position id "
            f"(the engine then closes the short first; without ids a long written first is rejected and dropped).")


def _heading(label: str) -> str:
    """First letter up only (as the dashboard does): "Monthly", "Event: bajaj-finance-rbi-december 2026-12-04"
    (str.title() would mangle the thesis slug)."""
    return label[:1].upper() + label[1:]


def _fits(q: dict, cap: float) -> bool:
    return bool(q) and q.get("per_lot") is not None and q["per_lot"] <= cap + 1e-9


def _fail_line(s: dict) -> str:
    return f"{s['name']} FAIL ({s['why']})" if s.get("verdict") == "FAIL" else f"{s['name']} n/a ({s.get('why', '')})"


def _fails_text(structures: list) -> str:
    """The failing structures of one expiry, one clause each; once when they all fail for the same reason."""
    if len(structures) > 1 and len({(s.get("verdict"), s.get("why")) for s in structures}) == 1:
        s = structures[0]
        return f"all {len(structures)} structures {'FAIL' if s.get('verdict') == 'FAIL' else 'n/a'} ({s.get('why', '')})"
    return "; ".join(_fail_line(s) for s in structures)


def _lines_common(c: dict, ccy: str, unit_word: str) -> list[str]:
    """Only what PASSES is tabled: strike rows where at least one side's premium per lot / contract fits the
    10% limit, and structures with verdict PASS. Each FAIL structure is one line; an underlying where nothing
    fits (or no data) is one line in "Did not fit" at the end."""
    cap = c.get("cap") or 0.0
    lines, nofit = [], []
    ref = set(c.get("reference") or [])
    for x in c.get("underlyings", []):
        name = x["underlying"]
        if x.get("error"):
            nofit.append(f"**{name}**: not available today ({x['error']})")
            continue
        exps = [ex for ex in x["expiries"] if any(st.get("verdict") == "PASS" for st in ex["structures"])]
        if not exps:
            ex = x["expiries"][0] if x["expiries"] else None
            cheapest = min((q["per_lot"] for r in (ex or {}).get("rows", []) for q in (r.get("CE"), r.get("PE"))
                            if q and q.get("per_lot") is not None), default=None)
            nofit.append(f"**{name}** {x['spot']:,.2f}" + (" (reference)" if name in ref else "") +
                         (f", {ex['expiry']}: cheapest ATM+/-2 premium {_m(cheapest, ccy)} per {unit_word} vs the "
                          f"{_m(cap, ccy)} limit; " + _fails_text(ex["structures"]) if ex else ""))
            continue
        head = f"### {name} ({x.get('kind', 'US')}, {unit_word} {x['lot']})"
        why = [w for w in x.get("why") or [] if w != "index"]
        if why:
            head += " - " + "; ".join(why)
        lines += [head, ""]
        ch = x.get("chain")
        sp = f"Spot {x['spot']:,.2f} (Yahoo close {x['spot_date']})"
        if ch:
            sp += (f"; {'Zerodha' if ch['source'] == 'zerodha' else 'NSE'} chain underlying {ch['underlying']:,.2f} "
                   f"at {ch['timestamp']} ({ch['source']})")
        elif ccy == "INR":
            sp += "; no NSE chain of this date (model prices)"
        lines += [sp + ".", ""]
        for w in x.get("warnings", []):
            lines.append(f"- WARNING: {w}")
        if x.get("warnings"):
            lines.append("")
        for ex in x["expiries"]:
            passing = [st for st in ex["structures"] if st.get("verdict") == "PASS"]
            if not passing:
                lines += [f"**{_heading(ex['label'])} expiry {ex['expiry']}**: no structure fits: "
                          + _fails_text(ex["structures"]) + ".", ""]
                continue
            iv = (f", {'IV from last trades' if ccy == 'USD' else 'chain ATM IV'} {ex['atm_iv_pct']:.2f}%"
                  if ex.get("atm_iv_pct") else "")
            lines += [f"**{_heading(ex['label'])} expiry {ex['expiry']}** ({ex['days']} days), ATM {_k(ex['atm'])}{iv}", ""]
            c_, p_ = ("Call", "Put") if ccy == "USD" else ("CE", "PE")
            lines += [f"| Strike | {c_} / unit | {c_} per {unit_word} | {c_} % eq | {p_} / unit | {p_} per {unit_word} | {p_} % eq | Source |",
                      "|---|---|---|---|---|---|---|---|"]
            hidden = 0
            for r in ex["rows"]:
                ce, pe = r.get("CE") or {}, r.get("PE") or {}
                if not (_fits(ce, cap) or _fits(pe, cap)):
                    hidden += 1
                    continue
                tag = "ATM" if r["offset"] == 0 else f"ATM{r['offset']:+d}"

                def cells(q):
                    if q.get("price") is None:
                        return "n/a | n/a | n/a"
                    over = "" if _fits(q, cap) else " (over limit)"
                    return f"{q['price']:,.2f} | {_m(q.get('per_lot'), ccy)}{over} | {q['pct']:,.2f}%"
                lines.append(f"| {_k(r['strike'])} ({tag}) | {cells(ce)} | {cells(pe)} | {r['source']} |")
            if hidden:
                lines.append(f"\n{hidden} strike(s) of ATM-2..ATM+2 omitted: both premiums above the {_m(cap, ccy)} limit.")
            if ex.get("expected_move"):
                lines += ["", f"Expected move (ATM straddle): {ex['expected_move']:,.2f} = "
                              f"{ex['expected_move'] / x['spot'] * 100:.1f}% of the spot."]
            zm = bool(c.get("zerodha_margin"))       # Kite token present: Kite's basket margin per structure
            dp = 2 if ccy == "USD" else 0
            lines += ["", f"| Structure (1 {unit_word}) | Legs: write in this order (price / unit) | Net debit / credit | "
                          "Max loss | % of equity | Event-trade size | Max gain | Breakeven | Largest order risk (10% rule) | "
                          "Margin blocked | Cash needed | Verdict | Quotes |" + (" Zerodha margin |" if zm else ""),
                      "|---|---|---|---|---|---|---|---|---|---|---|---|---|" + ("---|" if zm else "")]
            for st in passing:
                mg = "unlimited" if st["max_gain"] is None else _m(st["max_gain"], ccy, dp)
                be = " / ".join(_k(v) for v in st["breakevens"])
                v = f"PASS (up to {st['max_lots']} {unit_word}{'' if st['max_lots'] == 1 else 's'})"
                zc = (f" {_m(st['zerodha_margin'], ccy) if st.get('zerodha_margin') is not None else ''} |" if zm else "")
                net = (f"credit {_m(st['net_credit_lot'], ccy, dp)}" if st.get("kind") == "credit"
                       else f"debit {_m(st['net_debit_lot'], ccy, dp)}")
                if st.get("fill_est_lot") is not None:
                    net += f" (fill est. {_m(st['fill_est_lot'], ccy, dp)})"
                name = st["name"] + (f" ({st['strike_note']})" if st.get("strike_note") else "")
                lines.append(f"| {name} | {_legs_text(st, ccy == 'USD')} | {net} | {_m(st['max_loss'], ccy, dp)} | "
                             f"{st['max_loss_pct']:.2f}% | {st['event_size']} | {mg} | {be} | "
                             f"{st['order_risk_what']} {_m(st['order_risk'], ccy, dp)} = {st['order_risk_pct']:.1f}% | "
                             f"{_m(st['margin'], ccy, dp)} | {_m(st['cash_need'], ccy, dp)} | {v} | "
                             f"{_quotes_cell(st)} |" + zc)
            fails = [st for st in ex["structures"] if st.get("verdict") != "PASS"]
            if fails:
                lines += [""] + [f"- {_fail_line(st)}" for st in fails]
            lines.append("")
    if nofit:
        lines += ["### No structure passes (one line each)", ""] + [f"- {n}" for n in nofit] + [""]
    return lines


def _ev_text(ev: dict) -> str:
    return f"{ev['slug']} {ev['date']}" + (f" ({ev['event']})" if ev.get("event") else "")


def _fno_calendar_line(cal) -> str | None:
    """One line: each upcoming calendar F&O event and the expiry block that holds through it."""
    if cal is None:
        return None
    if not cal:
        return f"**Calendar events** ({EVENTS_NAME}): none upcoming for F&O."
    parts = []
    for ev in cal:
        groups: dict = {}
        for h in ev["holds"]:
            key = (f"\"{_heading(h['label'])} expiry {h['expiry']}\"" if h.get("expiry")
                   else f"no block ({h.get('note', '')})")
            groups.setdefault(key, []).append(h["underlying"])
        held = "; ".join(f"{', '.join(us)} -> {k}" for k, us in groups.items()) or "no candidate underlying"
        parts.append(f"{_ev_text(ev)}: {held}")
    return f"**Calendar events** ({EVENTS_NAME}): " + "; ".join(parts) + "."


def _us_calendar_line(cal) -> str | None:
    """One line: each upcoming calendar US event and the tabled expiry that covers it (or does not)."""
    if cal is None:
        return None
    if not cal:
        return f"**Calendar events** ({EVENTS_NAME}): none upcoming for the US sleeve."
    parts = []
    for ev in cal:
        bits = [f"held through by the tabled expiry {e} ({len(ts)} ticker{'s' if len(ts) != 1 else ''})"
                for e, ts in sorted(ev["held"].items())]
        bits += [f"NOT held: expiry {e} ends before it ({', '.join(ts)})" for e, ts in sorted(ev["short"].items())]
        parts.append(f"{_ev_text(ev)}: " + ("; ".join(bits) or "no tabled ticker"))
    return f"**Calendar events** ({EVENTS_NAME}): " + "; ".join(parts) + "."


def fno_lines(c: dict | None) -> list[str]:
    lines = ["## F&O candidates", ""]
    if not c:
        return lines + ["Not available this run (see the run log)."]
    cl = _fno_calendar_line(c.get("calendar"))
    if cl:
        lines += [cl, ""]
    lines += [f"For the reviewer (ROUTINE_PROMPT.md step 5c). Close of {c['date']}. Sleeve equity "
              f"{_m(c['equity'], 'INR')}; the 10% per-order limit is {_m(c['cap'], 'INR')}; free cash "
              f"{_m(c['cash'], 'INR')}; open positions {c['open_positions']} of {c['max_positions']}; circuit breaker "
              f"{c['breaker'] or 'off'}" + (f"; India VIX {c['india_vix']:.2f}" if c.get("india_vix") else "") + ".", "",
              "Pricing source on every row: **real NSE chain (mid)** = mid of NSE's best bid/ask at the chain's timestamp; "
              "**model (BS on NSE chain IV)** = one-sided quote, Black-Scholes on the chain's implied vol; "
              "**model (BS on VIX / realised vol)** = no chain today. These are close prices, not fills: an order fills "
              "at the next NSE open at the model price (Black-Scholes on the latest chain IV, +1% buy / -1% sell). "
              "Write the numeric strikes and the exact expiry date shown here in the order (\"ATM\" is re-resolved at "
              "the fill-day open). **fill est.** = the engine's fill price for the structure if the next open were at "
              "today's close (the same chain IV per strike the fill will use, +/-1%): the filled credit or debit will "
              "be near it, not near the mids, and it is never re-checked against the strike gap. Only what passes is "
              "tabled; each failing structure is one line."
              + (" **real Zerodha chain (mid)** = the same mid from Zerodha Kite Connect quotes (IV solved from the mid); "
                 "**Zerodha margin** = Kite's basket margin for the structure's legs (the broker's own number, read only; "
                 "the sleeve's own risk check above is unchanged)."
                 if (c.get("zerodha_margin") or chains.ZERODHA_CHAIN_LABEL in " ".join(c.get("sources") or [])) else ""), "",
              structures_text("INR"), ""]
    lines += [f"- {n}" for n in c.get("notes", [])]
    if c.get("notes"):
        lines.append("")
    lines += _lines_common(c, "INR", "lot")
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def us_lines(c: dict | None, offline: bool = False) -> list[str]:
    lines = ["## US candidates", ""]
    if offline:
        return lines + ["Not refreshed: offline run (no yfinance option chains). The last online run's candidates "
                        "are in state/us_candidates.json."]
    if not c:
        return lines + ["Not available this run (see the run log)."]
    if c.get("not_refreshed"):      # us.run_day: a US session was in progress at run time
        cal = calendar_events("us", _date.today().isoformat())
        return lines + ([f"**Calendar events** ({EVENTS_NAME}): " + "; ".join(_ev_text(ev) for ev in cal)
                         + " (expiry coverage not checked: no table this run).", ""] if cal else []) + [c["not_refreshed"]]
    cl = _us_calendar_line(c.get("calendar"))
    if cl:
        lines += [cl, ""]
    lines += [f"For the reviewer (ROUTINE_PROMPT.md step 5c). US market date {c['date']}, chains fetched "
              f"{c['fetched_at']}. Sleeve equity {_m(c['equity'], 'USD')}; the 10% per-order limit is "
              f"{_m(c['cap'], 'USD')}; free cash {_m(c['cash'], 'USD')}; open positions {c['open_positions']} of "
              f"{c['max_positions']}; circuit breaker {c['breaker'] or 'off'}. One contract = 100 shares.", "",
              "Pricing source on every row: **real yfinance chain (mid)** = mid of Yahoo's bid/ask (quotes of the latest "
              "US session); **model (BS on chain IV)** = no two-sided quote, Black-Scholes on Yahoo's implied vol; "
              "**no quote** = neither (Yahoo's IV is used only when the row has a bid or an ask). An option order fills "
              "at the next run after its decision_date: at the chain mid when the quote is two-sided, else at the "
              "model price below. "
              "Expiry: the nearest monthly (third Friday) 14-42 days out; ATM = the listed strike nearest the close. "
              "Only what passes is tabled; each failing structure is one line. SPY and QQQ are reference only (one "
              "contract is above the limit at this size; a narrow credit spread on them can still pass). "
              "**model (BS on IV implied by last trades)** = outside US hours (the 17:33 IST run is US pre-market) Yahoo "
              "shows no bid/ask, so one IV is backed out of the latest session's near-the-money last trades and every "
              "strike is priced from it. A pre-market US option FILL is this same MODEL price (us.py uses the same "
              "function), not a quote: no bid/ask, no spread, no check that anyone would trade there. A leg under "
              f"${config.US_MIN_OPTION_PRICE:.2f} is never filled (the order waits).", "",
              structures_text("USD"), ""]
    lines += [f"- {n}" for n in c.get("notes", [])]
    lines += _lines_common(c, "USD", "contract")
    while lines and lines[-1] == "":
        lines.pop()
    return lines
