"""'Today in brief': a few plain-English sentences for the dashboard, built from the finished dashboard data.

Read-only. It takes the cleaned dashboard dict (export.build_dashboard_data) and the momentum price frame,
and returns {"date": as_of, "lines": [...]}: what the four sleeves did together today, which sleeve and
holdings moved it, what was bought and sold, and what is queued for the next session. It states only what
the data shows (amounts and names), never a market reason.

Sleeve day moves come from the last two rows of combined.equity_curve, the same numbers the four sleeve
cards on the dashboard show, so the sentences always add up to the headline day change.
"""
from __future__ import annotations

from datetime import date as _date

import pandas as pd

SLEEVE_NAMES = {"momentum": "Momentum", "thesis": "Thesis", "fno": "India F&O", "us": "US"}
SLEEVES = ("momentum", "thesis", "fno", "us")
MIN_MOVE = 500.0          # a sleeve's day move worth a mention (Rs) ...
MIN_SHARE = 0.10          # ... or this share of all sleeves' absolute moves
OPTION_KINDS = {"PE": "put", "PUT": "put", "CE": "call", "CALL": "call"}


# ---------- formatting ----------
def inr(x) -> str:
    """Rupees, whole, Indian grouping as on the dashboard: 2016495 -> 'Rs 20,16,495'. Sign is the caller's job."""
    n = int(round(abs(float(x))))
    s = str(n)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups + [tail])
    return "Rs " + s


def _day(s) -> str:
    """'2026-12-29' -> '29 Dec'; anything else is returned as given."""
    try:
        d = _date.fromisoformat(str(s)[:10])
    except ValueError:
        return str(s)
    return f"{d.day} {d.strftime('%b')}"


def _num(x):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if x != x else x


def _join(items: list[str]) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


# ---------- day moves ----------
def sleeve_moves(d: dict) -> dict | None:
    """{sleeve: day change in Rs} from the last two combined rows, or None with fewer than two rows."""
    curve = (d.get("combined") or {}).get("equity_curve") or []
    if len(curve) < 2:
        return None
    a, b = curve[-2], curve[-1]
    out = {}
    for s in SLEEVES:
        x, y = _num(a.get(s)), _num(b.get(s))
        if x is not None and y is not None:
            out[s] = y - x
    return out


def _matters(move: float, moves: dict) -> bool:
    total = sum(abs(v) for v in moves.values())
    return abs(move) >= 1 and (abs(move) >= MIN_MOVE or (total > 0 and abs(move) >= MIN_SHARE * total))


def result_line(d: dict) -> str | None:
    cb = d.get("combined") or {}
    curve = cb.get("equity_curve") or []
    cap = _num(cb.get("capital"))
    if not curve:
        return None
    eq = _num(cb.get("equity"))
    if eq is None:
        eq = _num(curve[-1].get("equity"))
    if eq is None:
        return None
    on = f" on {inr(cap)}" if cap else ""
    if len(curve) < 2:
        return f"This is the first recorded day, so there is no day change yet; the total is {inr(eq)}{on}."
    prev, last = _num(curve[-2].get("equity")), _num(curve[-1].get("equity"))
    if prev is None or last is None:
        return f"The total is {inr(eq)}{on}."
    ch = last - prev
    if abs(ch) < 1:
        head = "All four sleeves together did not change today"
    else:
        head = f"All four sleeves together {'lost' if ch < 0 else 'gained'} {inr(ch)} today"
    return f"{head}; the total is now {inr(eq)}{on}."


def _today_fills(trades: list, day: str, sides: tuple) -> dict:
    """{symbol: average fill price} of today's trades with these sides."""
    qty, val = {}, {}
    for t in trades or []:
        if str(t.get("date")) != day or str(t.get("side", "")).upper() not in sides:
            continue
        q, p = _num(t.get("qty")), _num(t.get("price"))
        if q is None or p is None:
            continue
        s = str(t.get("symbol"))
        qty[s] = qty.get(s, 0.0) + abs(q)
        val[s] = val.get(s, 0.0) + abs(q) * p
    return {s: val[s] / qty[s] for s in qty if qty[s]}


def holding_moves(holdings: list, trades: list, day: str, last: pd.Series, prev: pd.Series) -> list:
    """[(symbol, Rs move, note)] for today. holdings: [(symbol, signed qty, entry_date)]. A position entered
    today is measured from its fill price; one closed today from the previous close to its exit price."""
    entries = _today_fills(trades, day, ("BUY", "SHORT"))
    out = []
    held = set()
    for sym, qty, entry in holdings:
        held.add(sym)
        c = _num(last.get(sym))
        if c is None or not qty:
            continue
        note = ""
        if str(entry) == day:
            base = entries.get(sym)
            note = "bought today" if qty > 0 else "sold short today"
        else:
            base = _num(prev.get(sym))
        if base is None:
            continue
        out.append((sym, qty * (c - base), note))
    for side, sign, note in (("SELL", 1.0, "sold today"), ("COVER", -1.0, "bought back today")):
        for t in trades or []:
            if str(t.get("date")) != day or str(t.get("side", "")).upper() != side:
                continue
            sym = str(t.get("symbol"))
            q, p, b = _num(t.get("qty")), _num(t.get("price")), _num(prev.get(sym))
            if sym in held or q is None or p is None or b is None:
                continue
            out.append((sym, sign * abs(q) * (p - b), note))
    return sorted(out, key=lambda m: -abs(m[1]))


def _movers_text(moves: list, n: int = 3) -> str:
    parts = []
    for sym, rs, note in moves[:n]:
        if abs(rs) < 1:
            continue
        parts.append(f"{sym} {'down' if rs < 0 else 'up'} {inr(rs)}" + (f" ({note})" if note else ""))
    return ", ".join(parts)


def _closes(prices, as_of):
    """(last, previous) close Series when the price frame reaches as_of, else (None, None)."""
    if prices is None or len(prices) == 0:
        return None, None
    closes = prices.pivot(index="date", columns="symbol", values="close").sort_index().ffill()
    if len(closes) < 2 or pd.Timestamp(closes.index[-1]).strftime("%Y-%m-%d") != str(as_of):
        return None, None
    return closes.iloc[-1], closes.iloc[-2]


def _thesis_holdings(th: dict) -> list:
    rows = []
    for r in th.get("theses") or []:
        if _num(r.get("qty")) and (r.get("currency") or "INR") == "INR":
            rows.append((str(r.get("symbol")), _num(r.get("qty")), r.get("entry_date")))
    for so in th.get("second_order") or []:
        for l in so.get("legs") or []:
            if _num(l.get("qty")) and (l.get("currency") or "INR") == "INR":
                rows.append((str(l.get("symbol")), _num(l.get("qty")), l.get("entry_date")))
    return rows


def _option_groups(positions: list) -> list:
    """['4 option legs on NIFTY 29 Dec', ...] from open F&O / US positions."""
    groups = {}
    for p in positions or []:
        pid = str(p.get("id", ""))
        und = p.get("underlying") or pid.split(":")[0]
        exp = p.get("expiry") or (pid.split(":")[1] if pid.count(":") >= 1 else "")
        key = (und, exp)
        groups.setdefault(key, []).append(p)
    out = []
    for (und, exp), ps in groups.items():
        opt = all(str(p.get("kind", "")).upper() in ("PUT", "CALL") for p in ps)
        out.append(f"{_plural(len(ps), 'option leg' if opt else 'leg')} on {und}" + (f" {_day(exp)}" if exp else ""))
    return out


def why_line(d: dict, prices) -> str | None:
    moves = sleeve_moves(d)
    if not moves:
        return None
    as_of = str(d.get("as_of"))
    last, prev = _closes(prices, as_of)
    clauses = []
    for s in sorted(moves, key=lambda k: -abs(moves[k])):
        mv = moves[s]
        if not _matters(mv, moves):
            continue
        head = f"{SLEEVE_NAMES[s]} {'lost' if mv < 0 else 'gained'} {inr(mv)}"
        detail = ""
        if s == "momentum" and last is not None:
            hold = [(p["symbol"], _num(p.get("qty")), p.get("entry_date")) for p in (d.get("portfolio") or {}).get("positions") or []]
            detail = _movers_text(holding_moves(hold, d.get("trades"), as_of, last, prev))
            detail = f": biggest moves {detail}" if detail else ""
        elif s == "thesis" and last is not None and d.get("thesis"):
            th = d["thesis"]
            detail = _movers_text(holding_moves(_thesis_holdings(th), th.get("trades"), as_of, last, prev))
            detail = f": biggest moves {detail}" if detail else ""
        elif s == "fno" and d.get("fno"):
            g = _option_groups(d["fno"].get("positions"))
            detail = f" on its {_join(g)}" if g else ""
        elif s == "us" and d.get("us"):
            us_day = _us_day(d["us"])
            # the US session the India run saw is an earlier date than the India day: name it
            detail = (f" in its {_day(us_day)} session" if us_day and str(us_day) < as_of else "") + _us_detail(d["us"], mv)
        clauses.append(head + detail)
    text = "; ".join(clauses) + "." if clauses else ""
    return text or None


def _us_detail(us: dict, mv: float) -> str:
    """Rupee-rate share of the US move, only when the combined move is the latest US row's own day move."""
    day, fxp = _num(us.get("day_pnl_inr")), _num(us.get("day_fx_pnl_inr"))
    if day is None or fxp is None or abs(day - mv) > 1 or abs(mv) < 1:
        return ""
    share = fxp / mv
    if share >= 0.9:
        return ", almost all of it from the dollar-rupee rate, not the trades"
    if share >= 0.5:
        return f", mostly from the dollar-rupee rate ({inr(fxp)} of it)"
    if abs(fxp) >= 1:
        return f" ({inr(fxp)} of it from the dollar-rupee rate)"
    return ""


# ---------- trades and plans ----------
def _deriv_kind(o: dict) -> str:
    k = str(o.get("instrument", "")).upper().split()
    k = k[-1] if k else ""
    if k in OPTION_KINDS:
        return OPTION_KINDS[k]
    parts = str(o.get("position", "")).upper().split(":")    # a CLOSE order: "EEM:2026-12-18:P48:L"
    if len(parts) > 2:
        for pre, kind in (("PE", "put"), ("CE", "call"), ("P", "put"), ("C", "call")):
            if parts[2].startswith(pre) and parts[2][len(pre):].replace(".", "", 1).isdigit():
                return kind
    return "other"


def _deriv_groups(rows: list, verb_of) -> list:
    """Group option legs by (verb, underlying, expiry): ['opened 4 option legs on NIFTY 29 Dec (iron condor)']."""
    groups = {}
    for r in rows:
        pid = str(r.get("position", ""))
        parts = pid.split(":")
        und = r.get("underlying") or (parts[0] if parts and parts[0] else "?")
        exp = r.get("expiry")
        if not exp:
            inst = str(r.get("instrument", "")).split()
            exp = inst[1] if len(inst) > 2 else (parts[1] if len(parts) > 1 else "")
        groups.setdefault((verb_of(r), und, str(exp)), []).append(r)
    out = []
    for (verb, und, exp), rs in groups.items():
        kinds = [_deriv_kind(r) for r in rs]
        opt = all(k in ("put", "call") for k in kinds)
        sides = [str(r.get("side", "")).upper() for r in rs]
        tag = ""
        if len(rs) == 4 and kinds.count("put") == 2 and kinds.count("call") == 2 and sides.count("BUY") == 2 \
                and sides.count("SELL") == 2 and verb.startswith("open"):
            tag = " (iron condor)"
        out.append(f"{verb} {_plural(len(rs), 'option leg' if opt else 'leg')} on {und}"
                   + (f" {_day(exp)}" if exp else "") + tag)
    return out


def _deriv_trade_verb(t: dict) -> str:
    a = str(t.get("action", "")).upper()
    return {"OPEN": "opened", "CLOSE": "closed", "EXPIRE": "let expire"}.get(a, a.lower() or "traded")


def _deriv_order_verb(o: dict) -> str:
    a = str(o.get("action") or ("CLOSE" if o.get("position") else "OPEN")).upper()
    return {"OPEN": "open", "CLOSE": "close"}.get(a, a.lower())


def _stock_trades(trades: list, day: str) -> str:
    words = {"BUY": "bought", "SELL": "sold", "SHORT": "sold short", "COVER": "bought back"}
    by = {}
    for t in trades or []:
        if str(t.get("date")) != day:
            continue
        side = str(t.get("side", "")).upper()
        q = _num(t.get("qty"))
        item = f"{t.get('symbol')}" + (f" ({int(abs(q)):,} shares)" if q else "")
        by.setdefault(words.get(side, side.lower()), []).append(item)
    return _join([f"{verb} {_join(items)}" for verb, items in by.items()])


def trades_line(d: dict) -> str:
    as_of = str(d.get("as_of"))
    parts = []
    m = _stock_trades(d.get("trades"), as_of)
    if m:
        parts.append(f"Momentum {m}")
    th = d.get("thesis") or {}
    t = _stock_trades(th.get("trades"), as_of)
    if t:
        parts.append(f"Thesis {t}")
    fno = d.get("fno") or {}
    f = _deriv_groups([r for r in fno.get("trades") or [] if str(r.get("date")) == as_of], _deriv_trade_verb)
    if f:
        parts.append(f"India F&O {_join(f)}")
    india = ("Today: " + "; ".join(parts) + ".") if parts else ""
    us = d.get("us") or {}
    us_day = _us_day(us)
    if us_day and not _us_is_new(d, us_day):
        us_day = None
    u = _deriv_groups([r for r in us.get("trades") or [] if us_day and str(r.get("date")) == us_day], _deriv_trade_verb)
    us_txt = f"In the US session of {_day(us_day)}, US {_join(u)}." if u else ""
    if not india and not us_txt:
        return "No trades today."
    if not india:
        return "No trades in India today. " + us_txt
    return (india + " " + us_txt).strip()


def _us_is_new(d: dict, us_day: str) -> bool:
    """False when the latest US session is older than the previous India row: the previous run already showed
    it (a US holiday, e.g. Thanksgiving, leaves the same US date as the latest on two India days)."""
    curve = (d.get("combined") or {}).get("equity_curve") or []
    return len(curve) < 2 or str(us_day) >= str(curve[-2].get("date"))


def _us_day(us: dict) -> str | None:
    curve = us.get("equity_curve") or []
    if curve:
        return str(curve[-1].get("date"))
    return (us.get("last_run") or {}).get("date")


def _due(o: dict, as_of: str) -> bool:
    """Fills at the next session: decided on or before as_of (or not dated yet: the run stamps it today)."""
    dd = o.get("decision_date")
    return not dd or str(dd) <= as_of


def _thesis_symbol(th: dict, o: dict) -> str:
    if o.get("leg"):
        return str(o["leg"])
    slug = o.get("thesis")
    for r in (th.get("theses") or []) + (th.get("closed") or []):
        if r.get("slug") == slug and r.get("symbol"):
            return str(r["symbol"])
    for r in th.get("second_order") or []:
        if r.get("slug") == slug and r.get("label"):
            return str(r["label"])
    return str(slug or "?")


def plan_line(d: dict) -> str:
    as_of = str(d.get("as_of"))
    words = {"BUY": "buy", "SELL": "sell", "SHORT": "sell short", "COVER": "buy back"}
    parts, later = [], 0

    pend = d.get("pending") or {}
    mo = pend.get("orders") or []
    if mo:
        by = {}
        for o in mo:
            by.setdefault(words.get(str(o.get("side", "")).upper(), str(o.get("side", "")).lower()), []).append(str(o.get("symbol")))
        parts.append("Momentum will " + _join([f"{v} {_join(s)}" for v, s in by.items()]))

    th = d.get("thesis") or {}
    by = {}
    for o in th.get("orders") or []:
        if not _due(o, as_of):
            later += 1
            continue
        by.setdefault(words.get(str(o.get("side", "")).upper(), str(o.get("side", "")).lower()), []).append(_thesis_symbol(th, o))
    if by:
        parts.append("Thesis will " + _join([f"{v} {_join(s)}" for v, s in by.items()]))

    for key, label in (("fno", "India F&O"), ("us", "US")):
        orders = (d.get(key) or {}).get("orders") or []
        due = [o for o in orders if _due(o, as_of)]
        later += len(orders) - len(due)
        g = _deriv_groups(due, _deriv_order_verb)
        if g:
            parts.append(f"{label} will {_join(g)}" + (" in the next US session" if key == "us" else ""))

    if parts:
        text = "Planned for the next session: " + "; ".join(parts) + "."
    else:
        text = "Nothing is planned for the next session: hold."
    if later:
        text += f" {_plural(later, 'more order')} {'waits' if later == 1 else 'wait'} for a later date."
    return text


def build(d: dict, prices=None) -> dict:
    """The brief for the cleaned dashboard dict `d`. prices: the momentum long price frame, or None (then the
    per-holding movers are left out and everything else is still written)."""
    lines = [result_line(d), why_line(d, prices), trades_line(d), plan_line(d)]
    return dict(date=d.get("as_of"), lines=[l for l in lines if l])
