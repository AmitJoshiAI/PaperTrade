"""Daily option candidates for the reviewer (ROUTINE_PROMPT.md step 5c): "## F&O candidates" and
"## US candidates" in the journal, the same data in docs/data.json (dashboard card "Option candidates").

Written by the daily run, which has network; the cloud reviewer has none, so this is what it decides with.
State: state/fno_candidates.json and state/us_candidates.json (latest run only; export reads them).

India (fno.run_day): NIFTY and BANKNIFTY always (config.FNO_CANDIDATE_INDICES), plus every config.FNO_STOCKS
symbol that an open thesis holds or a queued thesis order names. For the current monthly expiry (and the
nearest listed expiry for config.FNO_WEEKLY_INDICES): spot, days to expiry, ATM strike (the engine's: spot
rounded to the config strike step) and ATM-2..ATM+2 calls and puts per unit and per lot, then three
1-lot structures. US (us.run_day): config.US_CANDIDATE_TICKERS, the nearest monthly expiry (third Friday)
14-42 days after the US market date, ATM = the listed strike nearest the last close, ATM-2..ATM+2 listed
strikes, the same structures per contract (x100).

Every price says where it came from:
  "real NSE chain (mid)" / "real yfinance chain (mid)"   mid of a two-sided bid/ask at the close
  "model (BS on NSE chain IV)" / "model (BS on chain IV)" Black-Scholes with the chain's implied vol (the
                                                         strike's own, else the ATM IV): one-sided quote
  "model (BS on VIX / realised vol)"                     no chain today: India VIX (index) or 1.5x 30-day
                                                         realised vol (stock), as the sleeve's model

Structures (1 lot / 1 contract), each checked the way derivbook.check_open will check its orders:
  bull call spread  BUY CE ATM, SELL CE ATM+2   max loss = net debit, max gain = strike gap - debit,
                                                breakeven = ATM + debit
  bear put spread   BUY PE ATM, SELL PE ATM-2   max loss = net debit, max gain = gap - debit, BE = ATM - debit
  long straddle     BUY CE ATM + BUY PE ATM     max loss = both premiums, gain unlimited, BE = ATM +/- both
  10% rule: every LONG leg's premium x units <= 10% of sleeve equity (the short leg of a debit spread is
  covered by the long leg, so its margin is 0: derivbook.short_option_margin). Free cash: the long legs'
  premiums plus entry charges <= cash (covers fill first). Slots: legs <= free positions. Breaker: off.
Max loss and the free-cash need include the entry charges (costs.py); exit charges are not included.
These are close prices. Orders fill at the NEXT open: India at the model (Black-Scholes on the latest chain
IV, +1% buy / -1% sell), US at the chain mid of the next session. "ATM" in an order is re-resolved at the
fill, so the section prints numeric strikes and the exact expiry to put in the order.
"""
from __future__ import annotations

import json
import logging
import math

import pandas as pd
from datetime import date as _date, timedelta

from . import config, chains
from .costs import charges, us_charges
from .derivbook import bs_price, years_to, NotYet

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


# ---------- shared maths ----------
def _structures(rows: dict, units: int, cap: float, cash: float, free_slots: int, breaker: bool,
                charge_fn, expiry: str) -> list[dict]:
    """rows: {offset: {"strike", "CE": {"price", "src"}, "PE": {...}}}. Returns the three structures."""
    def leg(side, right, off):
        r = rows.get(off) or {}
        q = r.get(right) or {}
        return dict(side=side, right=right, strike=r.get("strike"), price=q.get("price"), src=q.get("src"))

    specs = [("Bull call spread", [leg("BUY", "CE", 0), leg("SELL", "CE", 2)]),
             ("Bear put spread", [leg("BUY", "PE", 0), leg("SELL", "PE", -2)]),
             ("Long straddle", [leg("BUY", "CE", 0), leg("BUY", "PE", 0)])]
    out = []
    for name, legs in specs:
        s = dict(name=name, expiry=expiry, legs=legs, units=units)
        if any(l["price"] is None or l["strike"] is None for l in legs):
            s.update(verdict="n/a", why="a leg has no price")
            out.append(s)
            continue
        debit = sum((l["price"] if l["side"] == "BUY" else -l["price"]) for l in legs)
        if name != "Long straddle":
            gap = abs(legs[1]["strike"] - legs[0]["strike"])
            if not 0 < debit < gap:
                s.update(verdict="n/a", why=f"inconsistent prices: net debit {debit:.2f} is outside 0..{gap:g} (strike gap)")
                out.append(s)
                continue
        ch = sum(charge_fn(l["side"], units, l["price"]) for l in legs)
        long_prem = [l["price"] * units for l in legs if l["side"] == "BUY"]
        need = sum(long_prem) + sum(charge_fn("BUY", units, l["price"]) for l in legs if l["side"] == "BUY")
        k0 = legs[0]["strike"]
        if name == "Long straddle":
            mg, be = None, [round(k0 - debit, 2), round(k0 + debit, 2)]
        else:
            gap = abs(legs[1]["strike"] - legs[0]["strike"])
            mg = round((gap - debit) * units - ch, 2)
            be = [round(k0 + debit if name.startswith("Bull") else k0 - debit, 2)]
        worst = max(long_prem)
        rule = worst <= cap + 1e-9
        cash_ok = need <= cash + 1e-9
        slots_ok = len(legs) <= free_slots
        reasons = []
        if not rule:
            reasons.append(f"long leg premium {worst:,.0f} is above the 10% limit {cap:,.0f}")
        if not cash_ok:
            reasons.append(f"needs {need:,.0f} of free cash, has {cash:,.0f}")
        if not slots_ok:
            reasons.append(f"needs {len(legs)} position slots, {free_slots} free")
        if breaker:
            reasons.append("circuit breaker ON")
        per_lot = worst if worst > 0 else 1
        s.update(net_debit=round(debit, 4), net_debit_lot=round(debit * units, 2), entry_charges=round(ch, 2),
                 max_loss=round(debit * units + ch, 2), max_gain=mg, breakevens=be,
                 long_leg_max=round(worst, 2), long_leg_pct=round(worst / (cap / config.DERIV_MAX_ORDER_RISK_PCT) * 100, 2) if cap else None,
                 rule_10pct="PASS" if rule else "FAIL", cash_need=round(need, 2), free_cash="PASS" if cash_ok else "FAIL",
                 slots="PASS" if slots_ok else "FAIL",
                 max_lots=max(0, min(int(cap // per_lot), int(cash // (need or 1)))),
                 verdict="PASS" if not reasons else "FAIL", why="; ".join(reasons))
        out.append(s)
    return out


def _src_text(r: dict) -> str:
    a, b = (r.get("CE") or {}).get("src"), (r.get("PE") or {}).get("src")
    return a if a == b else f"CE: {a}; PE: {b}"


# ---------- India ----------
def build_fno(market, book, market_day: str, chain_notes=()) -> dict:
    """Candidates from the F&O sleeve's market (fno.FnoMarket, after the day's fetch) and book."""
    from .fno import spec
    eq, cash = book.equity(), book.cash
    cap = config.DERIV_MAX_ORDER_RISK_PCT * eq
    free = config.DERIV_MAX_POSITIONS - len(book.positions)
    nxt = (_date.fromisoformat(market_day) + timedelta(days=1)).isoformat()
    breaker = book.blocked(nxt)
    syms, notes, why = fno_candidate_symbols()
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
            out["underlyings"].append(_fno_one(market, spec(u), market_day, eq, cash, cap, free, breaker, why.get(u, [])))
        except Exception as e:
            log.exception("F&O candidates for %s failed", u)
            out["underlyings"].append(dict(underlying=u, error=str(e), why=why.get(u, [])))
    srcs = {r.get("source") for x in out["underlyings"] for ex in x.get("expiries", []) for r in ex.get("rows", [])}
    out["sources"] = sorted(s for s in srcs if s)
    return out


def _fno_one(market, sp, market_day, eq, cash, cap, free, breaker, why) -> dict:
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
    res = dict(underlying=u, kind=sp["kind"], lot=sp["lot"], step=sp["step"], why=why, spot=round(S, 2), spot_date=sday,
               chain=dict(timestamp=snap["timestamp"], underlying=snap.get("underlying_value"), source=snap.get("source"))
               if snap else None, warnings=[], expiries=[])
    if snap and snap.get("underlying_value") and abs(snap["underlying_value"] / S - 1) > 0.005:
        res["warnings"].append(f"NSE chain underlying {snap['underlying_value']:,.2f} differs from the Yahoo close "
                               f"{S:,.2f} by more than 0.5%")
    for label, e in exps:
        t = years_to(e, market_day)
        atm = round(round(S / sp["step"]) * sp["step"], 4)
        has_chain = bool(snap and e in snap.get("chains", {}))
        if has_chain:
            steps = chains.listed_steps(snap, e, S)
            if steps and sp["step"] not in steps:
                res["warnings"].append(f"{e}: NSE strike gaps near the spot are {steps}, config step is {sp['step']:g}")
        rows, table = {}, []
        for off in OFFSETS:
            K = round(atm + off * sp["step"], 4)
            r = dict(offset=off, strike=K)
            for right, kind in (("CE", "CALL"), ("PE", "PUT")):
                q = chains.quote(snap, e, K, right) if has_chain else None
                m = chains.mid(q)
                if m is not None:
                    price, src, iv = m, chains.CHAIN_LABEL, (q["iv"] / 100 if q.get("iv", 0) > 0 else None)
                else:
                    iv = (chains.strike_iv(snap, e, K, right) or chains.atm_iv(snap, e, S)) if has_chain else None
                    if iv:
                        src = chains.CHAIN_IV_LABEL
                    else:
                        iv, _how = market.model_sigma(sp, market_day, at_open=False)
                        src = chains.MODEL_LABEL
                    price = round(bs_price(S, K, t, config.CARRY_RATE, iv, kind), 2)
                lot = price * sp["lot"]
                r[right] = dict(price=price, src=src, iv_pct=round(iv * 100, 2) if iv else None,
                                bid=q.get("bid") if q else None, ask=q.get("ask") if q else None,
                                listed=(q is not None) if has_chain else None,
                                per_lot=round(lot, 2), pct=round(lot / eq * 100, 2) if eq else None)
            r["source"] = _src_text(r)
            rows[off] = r
            table.append(r)
            if has_chain and r["CE"]["listed"] is False and r["PE"]["listed"] is False:
                res["warnings"].append(f"{e} strike {K:g} is not listed on NSE")
        st = _structures(rows, sp["lot"], cap, cash, free, breaker,
                         lambda side, units, px: charges(side, units, px, "OPT").total, e)
        res["expiries"].append(dict(label=label, expiry=e, days=(_date.fromisoformat(e) - _date.fromisoformat(market_day)).days,
                                    atm=atm, atm_iv_pct=round(chains.atm_iv(snap, e, S) * 100, 2) if has_chain and chains.atm_iv(snap, e, S) else None,
                                    rows=table, structures=st))
    return res


# ---------- US ----------
def _is_monthly(e: str) -> bool:
    d = _date.fromisoformat(e)
    return (d.weekday() == 4 and 15 <= d.day <= 21) or (d.weekday() == 3 and 14 <= d.day <= 20)


def build_us(market, book, market_day: str) -> dict:
    """Candidates from the US sleeve's market (us.UsMarket: its cached chains, so nothing is fetched twice)."""
    eq, cash = book.equity(), book.cash
    cap = config.DERIV_MAX_ORDER_RISK_PCT * eq
    free = config.DERIV_MAX_POSITIONS - len(book.positions)
    nxt = (_date.fromisoformat(market_day) + timedelta(days=1)).isoformat()
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
            out["underlyings"].append(_us_one(market, t, market_day, eq, cash, cap, free, breaker))
        except Exception as e:
            log.warning("US candidates for %s: %s", t, e)
            out["underlyings"].append(dict(underlying=t, error=str(e)))
    if over:
        out["notes"].append(f"Above the ${config.US_CANDIDATE_MAX_PRICE:g} price bar, not shown: " + ", ".join(over))
    out["reference"] = list(config.US_CANDIDATE_REFERENCE)
    srcs = {r.get("source") for x in out["underlyings"] for ex in x.get("expiries", []) for r in ex.get("rows", [])}
    out["sources"] = sorted(s for s in srcs if s)
    return out


def _us_one(market, t, market_day, eq, cash, cap, free, breaker) -> dict:
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
    strikes = sorted({float(k) for k in calls["strike"]} & {float(k) for k in puts["strike"]})
    i = min(range(len(strikes)), key=lambda j: abs(strikes[j] - S))
    tt = years_to(e, sday)
    raw = {}
    for off in OFFSETS:
        if not 0 <= i + off < len(strikes):
            continue
        K = strikes[i + off]
        for right, frame in (("CE", calls), ("PE", puts)):
            row = frame[(frame["strike"] - K).abs() < 1e-6]
            raw[(off, right)] = (K, row.iloc[0] if len(row) else None)
    # Outside US hours (the 17:33 IST run is US pre-market) Yahoo shows bid = ask = 0 and a meaningless IV
    # (0.00001 or a few %). Then one IV for the underlying is backed out of the latest session's last trades
    # near the money (median), and every strike is priced from it, so the structures are consistent.
    lt_ivs = []
    for (off, right), (K, q) in raw.items():
        if abs(off) <= 1 and q is not None and _traded_since(q, md):
            v = _implied_vol(float(q.get("lastPrice")), S, K, tt, config.US_RISK_FREE_RATE, "CALL" if right == "CE" else "PUT")
            if v:
                lt_ivs.append(v)
    lt_iv = sorted(lt_ivs)[len(lt_ivs) // 2] if lt_ivs else None
    rows, table = {}, []
    for off in OFFSETS:
        if (off, "CE") not in raw:
            continue
        r = dict(offset=off, strike=raw[(off, "CE")][0])
        for right, kind in (("CE", "CALL"), ("PE", "PUT")):
            K, q = raw[(off, right)]
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
            r[right] = dict(price=price, src=src, iv_pct=round(used_iv * 100, 2) if used_iv else None, bid=bid or None,
                            ask=ask or None, last=last, contract=str(q.get("contractSymbol", "")) if q is not None else "",
                            per_lot=per, pct=round(per / eq * 100, 2) if per is not None and eq else None)
        r["source"] = _src_text(r)
        rows[off] = r
        table.append(r)
    st = _structures(rows, config.US_CONTRACT_SIZE, cap, cash, free, breaker,
                     lambda side, units, px: us_charges(side, 1, px, option=True).total, e)
    return dict(underlying=t, spot=round(S, 2), spot_date=sday, lot=config.US_CONTRACT_SIZE, why=[],
                monthly=bool(monthly), warnings=[] if monthly else [f"no third-Friday expiry in range; using {e}"],
                expiries=[dict(label="monthly" if monthly else "nearest in range", expiry=e,
                               days=(_date.fromisoformat(e) - md).days, atm=strikes[i], rows=table, structures=st,
                               atm_iv_pct=round(lt_iv * 100, 2) if lt_iv else None)])


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
    name = (lambda r: "CALL" if r == "CE" else "PUT") if us else (lambda r: r)
    return " + ".join(f"{l['side']} {name(l['right'])} {_k(l['strike'])}" for l in s["legs"]) + f" ({s['expiry']})"


def _fits(q: dict, cap: float) -> bool:
    return bool(q) and q.get("per_lot") is not None and q["per_lot"] <= cap + 1e-9


def _fail_line(s: dict) -> str:
    return f"{s['name']} FAIL ({s['why']})" if s.get("verdict") == "FAIL" else f"{s['name']} n/a ({s.get('why', '')})"


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
                          f"{_m(cap, ccy)} limit; " + "; ".join(_fail_line(st) for st in ex["structures"]) if ex else ""))
            continue
        head = f"### {name} ({x.get('kind', 'US')}, {unit_word} {x['lot']})"
        why = [w for w in x.get("why") or [] if w != "index"]
        if why:
            head += " - " + "; ".join(why)
        lines += [head, ""]
        ch = x.get("chain")
        sp = f"Spot {x['spot']:,.2f} (Yahoo close {x['spot_date']})"
        if ch:
            sp += f"; NSE chain underlying {ch['underlying']:,.2f} at {ch['timestamp']} ({ch['source']})"
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
                lines += [f"**{ex['label'].title()} expiry {ex['expiry']}**: no structure fits: "
                          + "; ".join(_fail_line(st) for st in ex["structures"]) + ".", ""]
                continue
            iv = (f", {'IV from last trades' if ccy == 'USD' else 'chain ATM IV'} {ex['atm_iv_pct']:.2f}%"
                  if ex.get("atm_iv_pct") else "")
            lines += [f"**{ex['label'].title()} expiry {ex['expiry']}** ({ex['days']} days), ATM {_k(ex['atm'])}{iv}", ""]
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
            lines += ["", f"| Structure (1 {unit_word}) | Legs: put these in the order | Net debit | Max loss | Max gain | "
                          "Breakeven | 10% rule (long leg) | Free cash | Verdict |",
                      "|---|---|---|---|---|---|---|---|---|"]
            for st in passing:
                mg = "unlimited" if st["max_gain"] is None else _m(st["max_gain"], ccy)
                be = " / ".join(_k(v) for v in st["breakevens"])
                v = f"PASS (up to {st['max_lots']} {unit_word}{'' if st['max_lots'] == 1 else 's'})"
                lines.append(f"| {st['name']} | {_legs_text(st, ccy == 'USD')} | {_m(st['net_debit_lot'], ccy)} | {_m(st['max_loss'], ccy)} | {mg} | "
                             f"{be} | {_m(st['long_leg_max'], ccy)} = {st['long_leg_pct']:.1f}% {st['rule_10pct']} | "
                             f"needs {_m(st['cash_need'], ccy)} {st['free_cash']} | {v} |")
            fails = [st for st in ex["structures"] if st.get("verdict") != "PASS"]
            if fails:
                lines += [""] + [f"- {_fail_line(st)}" for st in fails]
            lines.append("")
    if nofit:
        lines += ["### Did not fit the 10% limit (one line each)", ""] + [f"- {n}" for n in nofit] + [""]
    return lines


def fno_lines(c: dict | None) -> list[str]:
    lines = ["## F&O candidates", ""]
    if not c:
        return lines + ["Not available this run (see the run log)."]
    lines += [f"For the reviewer (ROUTINE_PROMPT.md step 5c). Close of {c['date']}. Sleeve equity "
              f"{_m(c['equity'], 'INR')}; the 10% per-order limit is {_m(c['cap'], 'INR')}; free cash "
              f"{_m(c['cash'], 'INR')}; open positions {c['open_positions']} of {c['max_positions']}; circuit breaker "
              f"{c['breaker'] or 'off'}" + (f"; India VIX {c['india_vix']:.2f}" if c.get("india_vix") else "") + ".", "",
              "Pricing source on every row: **real NSE chain (mid)** = mid of NSE's best bid/ask at the chain's timestamp; "
              "**model (BS on NSE chain IV)** = one-sided quote, Black-Scholes on the chain's implied vol; "
              "**model (BS on VIX / realised vol)** = no chain today. These are close prices, not fills: an order fills "
              "at the next NSE open at the model price (Black-Scholes on the latest chain IV, +1% buy / -1% sell). "
              "Write the numeric strikes and the exact expiry date shown here in the order (\"ATM\" is re-resolved at "
              "the fill-day open). PASS/FAIL mirrors the engine's checks: each long leg's premium <= 10% of equity "
              "(the short leg of a debit spread is covered, margin 0), free cash covers the long premiums plus entry "
              "charges, the legs fit the free position slots. Max loss includes entry charges, not exit charges. Only what "
              "passes is tabled; each failing structure is one line.", ""]
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
    lines += [f"For the reviewer (ROUTINE_PROMPT.md step 5c). US market date {c['date']}, chains fetched "
              f"{c['fetched_at']}. Sleeve equity {_m(c['equity'], 'USD')}; the 10% per-order limit is "
              f"{_m(c['cap'], 'USD')}; free cash {_m(c['cash'], 'USD')}; open positions {c['open_positions']} of "
              f"{c['max_positions']}; circuit breaker {c['breaker'] or 'off'}. One contract = 100 shares.", "",
              "Pricing source on every row: **real yfinance chain (mid)** = mid of Yahoo's bid/ask (quotes of the latest "
              "US session); **model (BS on chain IV)** = no two-sided quote, Black-Scholes on Yahoo's implied vol; "
              "**no quote** = neither. An option order fills at the chain mid of the session after its decision_date. "
              "Expiry: the nearest monthly (third Friday) 14-42 days out; ATM = the listed strike nearest the close. "
              "PASS/FAIL as for F&O: each long leg's premium x 100 <= 10% of equity, free cash, slots. Only what "
              "passes is tabled; SPY and QQQ are reference only (one contract is above the limit at this size). "
              "**model (BS on IV implied by last trades)** = outside US hours (the 17:33 IST run is US pre-market) Yahoo "
              "shows no bid/ask, so one IV is backed out of the latest session's near-the-money last trades and every "
              "strike is priced from it: an estimate, not a quote; the fill will be at the next session's mid.", ""]
    lines += [f"- {n}" for n in c.get("notes", [])]
    lines += _lines_common(c, "USD", "contract")
    while lines and lines[-1] == "":
        lines.pop()
    return lines
