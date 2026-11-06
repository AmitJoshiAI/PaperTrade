"""Real NSE option chains for the India F&O sleeve: fetched by the daily run (it has network), cached in
state/chains/<SYMBOL>.json (one file per underlying, the latest snapshot only, committed with the run).

Endpoints (checked 4 Nov 2026 from a home connection):
  /api/option-chain-contract-info?symbol=<SYM>   the listed expiry dates (DD-Mon-YYYY) and strikes
  /api/option-chain-v3?type=Indices|Equity&symbol=<SYM>&expiry=<DD-Mon-YYYY>
        one expiry per call: records.timestamp ("04-Nov-2026 15:40:00"), records.underlyingValue,
        records.data = [{strikePrice, CE: {buyPrice1, sellPrice1, lastPrice, impliedVolatility, ...}, PE: {...}}]
  legacy, tried only when contract-info fails: /api/option-chain-indices?symbol= and
        /api/option-chain-equities?symbol= (on 4 Nov 2026 they answered 404 and {}: NSE replaced them with v3
        in 2025; kept in case they come back)
  https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv   lot size per series, checked against config
Session: curl_cffi impersonating Chrome (as scanner.py does for NSE/BSE/Moneycontrol), cookies primed with a
GET of https://www.nseindia.com/ and of https://www.nseindia.com/option-chain; the JSON calls send
Accept: application/json and Referer: the option-chain page. NSE's Akamai front blocks plain urllib/requests
on the TLS fingerprint. Whether it also blocks GitHub's runner IPs shows only in the run's journal: every
failure here is soft (the sleeve falls back to the Black-Scholes model, labelled, and the journal says why).

Prices used: the MID of the best bid (buyPrice1) and best ask (sellPrice1) when both are > 0. NSE reports
impliedVolatility 0 for contracts it cannot solve (deep ITM, no trade); those are ignored.

A snapshot is used for a market date only if its own timestamp is that date (a chain fetched the next
morning, or before Yahoo has the day's bar, is not mixed with another day's spot).

Snapshot file (state/chains/<SYM>.json):
  {"symbol", "kind": "index"|"stock", "timestamp", "date": "YYYY-MM-DD", "fetched_at": "... IST",
   "source", "underlying_value", "expiries": [every listed expiry, ISO], "chains": {ISO expiry: {strike:
   {"CE": {"bid", "ask", "ltp", "iv", "oi"}, "PE": {...}}}}, "previous": {"date", "iv": {ISO expiry: ATM IV}}}
Strikes are kept within +/- config.CHAIN_STRIKE_BAND of the spot (file size). "previous" keeps the ATM implied
vol of the snapshot this one replaced, so a same-date re-run can still price a fill on the prior day's IV.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import time
from datetime import date as _date, datetime, timedelta, timezone

from . import config
from .derivbook import last_weekday

log = logging.getLogger(__name__)

NSE_HOME = "https://www.nseindia.com/"
NSE_OC_PAGE = "https://www.nseindia.com/option-chain"
CONTRACT_INFO = "https://www.nseindia.com/api/option-chain-contract-info?symbol={sym}"
V3 = "https://www.nseindia.com/api/option-chain-v3?type={type}&symbol={sym}&expiry={exp}"
LEGACY = {"index": "https://www.nseindia.com/api/option-chain-indices?symbol={sym}",
          "stock": "https://www.nseindia.com/api/option-chain-equities?symbol={sym}"}
LOTS_CSV = "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv"
JSON_HEADERS = {"Accept": "application/json, text/plain, */*", "Referer": NSE_OC_PAGE}
TIMEOUT = 20
PAUSE = 0.35                     # seconds between NSE calls (be polite; NSE rate-limits bursts)

CHAIN_LABEL = "real NSE chain (mid)"
ZERODHA_CHAIN_LABEL = "real Zerodha chain (mid)"   # a snapshot with source "zerodha" (papertrade/kite.py)
CHAIN_IV_LABEL = "model (BS on NSE chain IV)"
MODEL_LABEL = "model (BS on VIX / realised vol)"

IST = timezone(timedelta(hours=5, minutes=30))
_MON = {m: i for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


# ---------- dates ----------
def iso(d: str) -> str:
    """'23-Nov-2026' or '23-11-2026' -> '2026-11-23'."""
    p = str(d).strip().split("-")
    m = _MON.get(p[1][:3].title()) if not p[1].isdigit() else int(p[1])
    return _date(int(p[2]), int(m), int(p[0])).isoformat()


def dmy(iso_day: str) -> str:
    d = _date.fromisoformat(iso_day)
    return f"{d.day:02d}-{d.strftime('%b')}-{d.year}"


def snap_to_listed(computed: str, listed) -> str:
    """NSE moves an expiry that falls on a holiday to the trading day before. Return the latest listed expiry
    in [computed - 3 days, computed], else `computed` itself."""
    c = _date.fromisoformat(computed)
    near = [e for e in (listed or []) if c - timedelta(days=3) <= _date.fromisoformat(e) <= c]
    return max(near) if near else computed


def monthly_after(after: str, weekday: int, listed=None, skip: int = 0) -> str:
    """The first monthly expiry strictly after `after` (skip=1: the one after that), using NSE's listed dates
    where known (holiday shifts), else the last <weekday> of the month."""
    a = _date.fromisoformat(after)
    y, m = a.year, a.month
    found = -1
    for _ in range(24):
        e = snap_to_listed(last_weekday(y, m, weekday).isoformat(), listed)
        if e > after:
            found += 1
            if found == skip:
                return e
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    raise ValueError(f"no monthly expiry after {after}")


def event_expiry(event_day: str, weekday: int, listed=None) -> str:
    """The first monthly expiry ON or after `event_day` (an option expiring that day still holds through a
    policy announcement made in the morning). Shared by the fetch (refresh) and the candidates table so the
    two always name the same date."""
    before = (_date.fromisoformat(event_day) - timedelta(days=1)).isoformat()
    return monthly_after(before, weekday, listed)


def nearest_after(after: str, listed) -> str | None:
    later = sorted(e for e in (listed or []) if e > after)
    return later[0] if later else None


def is_zerodha(snap: dict | None) -> bool:
    return bool(snap) and snap.get("source") == "zerodha"


def chain_label(snap: dict | None) -> str:
    """The pricing label for a mid taken from this snapshot: Zerodha's quotes or NSE's scrape."""
    return ZERODHA_CHAIN_LABEL if is_zerodha(snap) else CHAIN_LABEL


def source_name(snap: dict | None) -> str:
    """'Zerodha' or 'NSE', for journal text such as "NSE chain IV 14.20%"."""
    return "Zerodha" if is_zerodha(snap) else "NSE"


def expiry_picker(sym: str, sp: dict, market_day: str, weekly=(), extra_expiries: dict | None = None,
                  event_dates: dict | None = None):
    """pick(listed ISO expiries) -> the ISO expiries to fetch for one underlying: the current monthly expiry,
    the nearest listed expiry for the symbols in `weekly`, any listed expiry in extra_expiries[sym] (open
    positions), and the event expiry (event_expiry) of each date in event_dates[sym]. Shared by the NSE
    refresh and the Zerodha snapshot (fno.load_chains) so both fetch the same expiries."""
    extra_expiries, event_dates = extra_expiries or {}, event_dates or {}

    def pick(listed):
        want = {monthly_after(market_day, sp["weekday"], listed)}
        if sym in weekly:
            n = nearest_after(market_day, listed)
            if n:
                want.add(n)
        want |= {e for e in extra_expiries.get(sym, ()) if e in listed}
        # only a listed expiry: an event months out maps to a date NSE has not opened yet, and asking v3
        # for it would fail the whole symbol (its current monthly chain too)
        want |= {e for d in event_dates.get(sym, ()) if (e := event_expiry(d, sp["weekday"], listed)) in listed}
        return sorted(want)
    return pick


# ---------- the session ----------
class NseClient:
    """One primed curl_cffi session per run. Raises on any failure; callers catch."""

    def __init__(self):
        self._sess = None
        self.calls = 0          # API calls attempted
        self.ok = 0             # API calls answered 200

    def _prime(self):
        from curl_cffi import requests as cffi_requests     # pinned in requirements.txt
        self._sess = cffi_requests.Session(impersonate="chrome")
        for u in (NSE_HOME, NSE_OC_PAGE):
            r = self._sess.get(u, timeout=TIMEOUT)
            if r.status_code != 200:
                raise RuntimeError(f"cookie priming: {u} answered HTTP {r.status_code}")

    def get(self, url: str, headers: dict | None = None):
        if self._sess is None:
            self._prime()
        if self.calls:
            time.sleep(PAUSE)
        self.calls += 1
        r = self._sess.get(url, headers=headers or JSON_HEADERS, timeout=TIMEOUT)
        if r.status_code in (401, 403):          # cookies expired: prime once more and retry
            self._prime()
            r = self._sess.get(url, headers=headers or JSON_HEADERS, timeout=TIMEOUT)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code} from {url.split('?')[0]}")
        self.ok += 1
        return r

    def json(self, url: str):
        r = self.get(url)
        try:
            return json.loads(r.content)
        except ValueError:
            raise RuntimeError(f"not JSON from {url.split('?')[0]} (blocked or changed?)")


# ---------- parsing ----------
def _num(v) -> float:
    try:
        x = float(v)
        return x if x == x else 0.0
    except (TypeError, ValueError):
        return 0.0


def _side(d: dict | None) -> dict | None:
    if not isinstance(d, dict):
        return None
    return {"bid": _num(d.get("buyPrice1", d.get("bidprice"))), "ask": _num(d.get("sellPrice1", d.get("askPrice"))),
            "ltp": _num(d.get("lastPrice")), "iv": _num(d.get("impliedVolatility")), "oi": _num(d.get("openInterest"))}


def _strike_key(k) -> str:
    return f"{float(k):g}"


def parse_rows(rows: list, spot: float, band: float | None = None) -> dict:
    band = config.CHAIN_STRIKE_BAND if band is None else band
    out = {}
    for r in rows or []:
        k = _num(r.get("strikePrice"))
        if k <= 0 or (spot > 0 and abs(k - spot) > band * spot):
            continue
        out[_strike_key(k)] = {"CE": _side(r.get("CE")), "PE": _side(r.get("PE"))}
    return out


def _ts_date(ts: str) -> str | None:
    try:
        return iso(str(ts).split()[0])
    except Exception:
        return None


# ---------- one underlying ----------
def fetch_snapshot(client: NseClient, sym: str, kind: str, pick) -> dict:
    """pick(listed ISO expiries) -> the ISO expiries to fetch. Returns a snapshot dict; raises on failure."""
    typ = "Indices" if kind == "index" else "Equity"
    try:
        info = client.json(CONTRACT_INFO.format(sym=sym))
        listed = sorted({iso(e) for e in info.get("expiryDates") or []})
        if not listed:
            raise RuntimeError("contract-info returned no expiry dates")
        chains, spot, ts, src = {}, 0.0, "", "NSE option-chain-v3"
        for e in pick(listed):
            d = client.json(V3.format(type=typ, sym=sym, exp=dmy(e)))
            rec = d.get("records") or {}
            rows = rec.get("data") or []
            if not rows:
                raise RuntimeError(f"option-chain-v3 {sym} {e}: no rows")
            spot = _num(rec.get("underlyingValue")) or _num((rows[0].get("CE") or rows[0].get("PE") or {}).get("underlyingValue"))
            ts = rec.get("timestamp") or ts
            chains[e] = parse_rows(rows, spot)
    except Exception as first:
        # legacy endpoint (all expiries in one call), only if NSE ever restores it
        try:
            d = client.json(LEGACY[kind].format(sym=sym))
            rec = d.get("records") or {}
            listed = sorted({iso(e) for e in rec.get("expiryDates") or []})
            if not listed or not rec.get("data"):
                raise RuntimeError("empty")
            spot, ts, src = _num(rec.get("underlyingValue")), rec.get("timestamp") or "", "NSE option-chain (legacy)"
            chains = {}
            for e in pick(listed):
                rows = [r for r in rec["data"] if iso(r.get("expiryDate", "01-Jan-1970")) == e]
                chains[e] = parse_rows(rows, spot)
        except Exception:
            raise first
    day = _ts_date(ts)
    if not day:
        raise RuntimeError(f"{sym}: chain has no timestamp")
    return {"symbol": sym, "kind": kind, "timestamp": ts, "date": day,
            "fetched_at": datetime.now(IST).strftime("%Y-%m-%d %H:%M IST"), "source": src,
            "underlying_value": spot, "expiries": listed, "chains": chains}


# ---------- cache ----------
def path(sym: str):
    return config.CHAINS_DIR / f"{sym}.json"


def load(sym: str) -> dict | None:
    p = path(sym)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("state/chains/%s.json unreadable: %s", sym, e)
        return None


def save(snap: dict, old: dict | None) -> None:
    if old and old.get("date") and old["date"] < snap["date"]:
        snap["previous"] = {"date": old["date"], "iv": {e: atm_iv(old, e) for e in old.get("chains", {})}}
    elif old and old.get("previous"):
        snap["previous"] = old["previous"]          # same-date re-run: keep the prior day's summary
    config.CHAINS_DIR.mkdir(parents=True, exist_ok=True)
    path(snap["symbol"]).write_text(json.dumps(snap, indent=1, sort_keys=True) + "\n", encoding="utf-8")


# ---------- reading a snapshot ----------
def quote(snap: dict | None, expiry: str, strike: float, right: str) -> dict | None:
    """The cached quote dict for one contract (right CALL/PUT or CE/PE), or None."""
    if not snap:
        return None
    side = "CE" if right.upper() in ("CALL", "CE") else "PE"
    row = (snap.get("chains", {}).get(expiry) or {}).get(_strike_key(strike))
    return (row or {}).get(side)


def mid(q: dict | None) -> float | None:
    if q and q.get("bid", 0) > 0 and q.get("ask", 0) > 0 and q["ask"] >= q["bid"]:
        return round((q["bid"] + q["ask"]) / 2, 2)
    return None


def atm_iv(snap: dict | None, expiry: str | None = None, spot: float | None = None) -> float | None:
    """Underlying's implied vol (decimal) for an expiry: the mean of the out-of-the-money side's IV at the
    two strikes on each side of the spot (calls above, puts below), ignoring NSE's 0 placeholders. With no
    expiry, the nearest cached expiry."""
    if not snap or not snap.get("chains"):
        return None
    if expiry not in snap["chains"]:
        expiry = min(snap["chains"])
    S = spot or snap.get("underlying_value") or 0
    rows = snap["chains"][expiry]
    ks = sorted(rows, key=lambda k: abs(float(k) - S))[:4]
    ivs = []
    for k in ks:
        q = rows[k].get("CE" if float(k) >= S else "PE") or {}
        if q.get("iv", 0) > 0:
            ivs.append(q["iv"])
    return round(sum(ivs) / len(ivs) / 100, 4) if ivs else None


def strike_iv(snap: dict | None, expiry: str, strike: float, right: str) -> float | None:
    q = quote(snap, expiry, strike, right)
    return round(q["iv"] / 100, 4) if q and q.get("iv", 0) > 0 else None


def listed_steps(snap: dict | None, expiry: str, spot: float) -> list[float]:
    """Gaps between listed strikes within 3% of the spot (to check config's strike step)."""
    if not snap or expiry not in snap.get("chains", {}):
        return []
    ks = sorted(float(k) for k in snap["chains"][expiry] if abs(float(k) - spot) <= 0.03 * spot)
    return sorted({round(b - a, 2) for a, b in zip(ks, ks[1:])})


# ---------- the daily refresh ----------
def lot_check(client: NseClient, specs: dict, market_day: str) -> list[str]:
    """Compare config lot sizes with NSE's fo_mktlots.csv for the current month's series. Warn-only."""
    text = client.get(LOTS_CSV, headers={"Referer": NSE_HOME}).text
    rows = list(csv.reader(io.StringIO(text)))
    head = [c.strip().upper() for c in rows[0]]
    d = _date.fromisoformat(market_day)
    col_name = f"{d.strftime('%b').upper()}-{str(d.year)[2:]}"
    if col_name not in head:
        return [f"lot-size check: no {col_name} column in NSE fo_mktlots.csv"]
    ci, si = head.index(col_name), head.index("SYMBOL")
    nse = {r[si].strip().upper(): r[ci].strip() for r in rows[1:] if len(r) > ci}
    out = []
    for sym, sp in specs.items():
        v = nse.get(sym)
        if v and v.isdigit() and int(v) != int(sp["lot"]):
            out.append(f"LOT SIZE MISMATCH {sym}: config {sp['lot']}, NSE fo_mktlots.csv {col_name} {v}. Fix config.FNO_*.")
        elif not v:
            out.append(f"lot-size check: {sym} not found in NSE fo_mktlots.csv {col_name}")
    return out


def refresh(specs: dict, market_day: str, extra_expiries: dict | None = None, weekly=(),
            lot_table: dict | None = None, event_dates: dict | None = None, no_save=()) -> tuple[dict, list[str]]:
    """Fetch and cache a snapshot for each underlying. specs: {SYM: {"kind", "weekday", "lot"}}. Fetches the
    current monthly expiry, the nearest listed expiry for the symbols in `weekly`, any expiry in
    extra_expiries[SYM] (open positions), and the event expiry (event_expiry) of each ISO date in
    event_dates[SYM] (catalysts after the current monthly expiry, candidates.fno_event_dates). The event
    expiry is resolved here, against NSE's own list, because a holiday can move it. Symbols in `no_save` are
    fetched but not written to state/chains/ (the Zerodha snapshot of the same symbol owns the file that day).
    Returns ({SYM: snapshot}, journal notes). Never raises."""
    client, snaps, notes = NseClient(), {}, []
    extra_expiries, event_dates = extra_expiries or {}, event_dates or {}
    for sym, sp in specs.items():
        old = load(sym)
        pick = expiry_picker(sym, sp, market_day, weekly, extra_expiries, event_dates)
        try:
            snap = fetch_snapshot(client, sym, sp["kind"], pick)
            if sym not in no_save:
                save(snap, old)
            snaps[sym] = snap
            if snap["date"] != market_day:
                notes.append(f"NSE chain {sym}: timestamp {snap['timestamp']} is not the market date {market_day}; "
                             f"cached but not used today (model prices instead)")
        except Exception as e:
            notes.append(f"NSE chain {sym} not fetched ({e}); its candidates and marks use the model")
            log.warning("NSE chain %s: %s", sym, e)
            if client.ok == 0:          # nothing has worked yet: NSE is blocking this run; do not retry 10 times
                rest = [s for s in specs if s != sym and s not in snaps]
                if rest:
                    notes.append(f"NSE unreachable from this run: skipped {', '.join(rest)} (model prices)")
                break
    try:
        if client.ok and snaps:
            notes += lot_check(client, lot_table or specs, market_day)
    except Exception as e:
        notes.append(f"lot-size check against NSE fo_mktlots.csv failed ({e})")
    return snaps, notes


def usable(snap: dict | None, market_day: str) -> dict | None:
    """The snapshot if it is of `market_day`, else None."""
    return snap if snap and snap.get("date") == market_day else None


def fill_iv(sym: str, prior: dict | None, day: str, expiry: str, strike: float, right: str,
            max_age_days: int = 7) -> tuple[float, str] | None:
    """Implied vol for a fill at the open of `day`: from the latest chain snapshot dated BEFORE `day` (no
    look-ahead), at most `max_age_days` old: the strike's own IV, else the expiry's ATM IV, else any cached
    ATM IV. `prior` is the cache as it was before today's fetch; its "previous" summary is used when the
    cache already holds the fill day's own snapshot (a same-date re-run)."""
    lo = (_date.fromisoformat(day) - timedelta(days=max_age_days)).isoformat()
    if prior and lo <= prior.get("date", "") < day:
        iv = strike_iv(prior, expiry, strike, right)
        if iv:
            return iv, f"{source_name(prior)} chain IV {iv*100:.2f}% at this strike (close of {prior['date']})"
        iv = atm_iv(prior, expiry if expiry in prior.get("chains", {}) else None)
        if iv:
            return iv, f"{source_name(prior)} chain ATM IV {iv*100:.2f}% (close of {prior['date']})"
    prev = (prior or {}).get("previous") or {}
    if prev and lo <= prev.get("date", "") < day:
        ivs = prev.get("iv") or {}
        iv = ivs.get(expiry) or next((v for v in ivs.values() if v), None)
        if iv:
            return iv, f"NSE chain ATM IV {iv*100:.2f}% (close of {prev['date']})"
    return None
