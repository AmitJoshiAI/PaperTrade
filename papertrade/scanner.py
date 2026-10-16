"""Catalyst scanner: catch market-moving news while it happens, not after.

Every run (GitHub Actions, every 30 minutes in market hours; see .github/workflows/scanner.yml):

  1. pulls free news feeds: NSE announcements and results (RSS), BSE corporate announcements (BSE has no
     announcements RSS; its public JSON endpoint is used), Economic Times markets and stocks, Moneycontrol
     business, CNBC top news;
  2. pulls Yahoo Finance daily quotes for the watchlist (watchlist.yaml: Nifty 50 + every symbol in an open
     or proposed thesis + user-added names) and flags a move of more than 4% against the previous close or
     volume above 3x the 20-day average;
  3. scores each headline: watchlist symbols/names it mentions, catalyst keywords (results, order win,
     demerger, IPO, buyback, SEBI, RBI, tariff, capex, stake sale, ...), whether the symbol is also moving,
     whether it touches a thesis;
  4. appends new items to alerts/<YYYY-MM-DD>.json (IST date; deduped by headline hash against today and
     yesterday), rewrites alerts/latest.md (top items for the reviewer routine, SCANNER_PROMPT.md) and
     docs/alerts.json (the dashboard's Alerts section).

Fails soft: a feed that errors or is blocked is recorded in the output ("feeds") and skipped; the run
still writes whatever the other feeds returned. It never reads or writes trading state and never places
or queues an order.

Run: python -m papertrade.scanner            (all feeds + quotes)
     python -m papertrade.scanner --no-quotes (feeds only, faster)
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import yaml

from . import config
from .universe import NIFTY50

log = logging.getLogger("papertrade.scanner")

IST = timezone(timedelta(hours=5, minutes=30))
WATCHLIST_FILE = config.ROOT / "watchlist.yaml"
ALERTS_DIR = config.ROOT / "alerts"
LATEST_MD = ALERTS_DIR / "latest.md"
NOTES_DIR = ALERTS_DIR / "notes"   # written by the hourly reviewer routine (SCANNER_PROMPT.md), read back here
DOCS_ALERTS_FILE = config.ROOT / "docs" / "alerts.json"

FETCH_TIMEOUT = 12          # seconds per feed request; the whole run must stay under ~2 minutes
MAX_AGE_HOURS = 36          # ignore feed items published longer ago than this
MIN_SCORE = 2               # items scoring below this are dropped (no watchlist name and no catalyst keyword)
HIGH_SCORE = 6              # "high" alerts: the ones the reviewer routine writes a second-order note for
LATEST_TOP_N = 15           # items in alerts/latest.md
DOCS_MAX_ALERTS = 150       # items in docs/alerts.json (today + yesterday, newest first)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/128.0 Safari/537.36")
BROWSER_HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
                   "Accept": "application/rss+xml, application/xml, text/xml, application/json, */*"}

# primary=True: an exchange filing (the company itself said it), scored +1 over a press headline.
FEEDS = [
    {"id": "nse", "name": "NSE announcements", "kind": "nse_rss", "primary": True,
     "url": "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml"},
    {"id": "nse_results", "name": "NSE results", "kind": "nse_rss", "primary": True,
     "url": "https://nsearchives.nseindia.com/content/RSS/Financial_Results.xml"},
    {"id": "bse", "name": "BSE announcements", "kind": "bse_json", "primary": True,
     "url": "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w?pageno={page}&strCat=-1"
            "&strPrevDate={prev}&strScrip=&strSearch=P&strToDate={to}&strType=C&subcategory=-1",
     "headers": {"Referer": "https://www.bseindia.com/", "Origin": "https://www.bseindia.com",
                 "Accept": "application/json"}},
    {"id": "et_markets", "name": "ET Markets", "kind": "rss",
     "url": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"},
    {"id": "et_stocks", "name": "ET Stocks", "kind": "rss",
     "url": "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms"},
    {"id": "bs_markets", "name": "Business Standard", "kind": "rss",
     "url": "https://www.business-standard.com/rss/markets-106.rss"},
    {"id": "bl_markets", "name": "BusinessLine", "kind": "rss",
     "url": "https://www.thehindubusinessline.com/markets/feeder/default.rss"},
    # Optional: used whenever it responds. On 2026-10-17 every moneycontrol.com URL (all /rss/ feeds and the
    # home page) returned 403 to urllib and curl with full browser headers, and the FeedBurner address
    # redirected to a consent page: a site-wide bot block, not a wrong URL. Its failure is reported
    # separately ("optional_unavailable") and does not count as a feed down.
    {"id": "moneycontrol", "name": "Moneycontrol", "kind": "rss", "optional": True,
     "url": "https://www.moneycontrol.com/rss/business.xml",
     "headers": {"Referer": "https://www.moneycontrol.com/", "Cache-Control": "no-cache"}},
    {"id": "cnbc", "name": "CNBC top news", "kind": "rss",
     "url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114"},
]

# (label, weight, pattern). Case-insensitive. Each label counts once per headline.
KEYWORDS = [
    ("results", 2, r"\bQ[1-4]\b|quarterly|\bresults?\b|earnings|net profit|\bprofit (rises|falls|jumps|surges|"
                   r"drops|declines|grows|soars|slumps)|financial results"),
    ("order win", 3, r"\border wins?\b|\b(wins?|bags?|secures?|receives?|bagged|secured|won) (an? |\w+ )?"
                     r"(orders?|contracts?)\b|\border (worth|from|of rs|valued)\b|letter of award|\bLoA\b"),
    ("demerger", 3, r"\bdemerg\w*|spin-?off|hive-?off"),
    ("IPO", 2, r"\bIPO\b|initial public offer"),
    ("listing", 1, r"\blisting\b|\bdebut\b|\blists? (on|at)\b"),
    ("buyback", 2, r"\bbuy-?backs?\b"),
    ("dividend", 1, r"\bdividend"),
    ("SEBI", 2, r"\bSEBI\b"),
    ("RBI", 2, r"\bRBI\b|repo rate|monetary policy"),
    ("budget", 2, r"\b(union )?budget\b"),
    ("tariff", 2, r"\btariffs?\b|anti-?dumping|(import|export|customs|safeguard) duty"),
    ("capex", 2, r"\bcapex\b|capacity expansion|new plant|greenfield|brownfield|capital expenditure"),
    ("guidance", 2, r"\bguidance\b|\boutlook\b|\bforecasts?\b"),
    ("rating", 1, r"\bupgrades?\b|\bdowngrades?\b|\brating\b|target price|price target"),
    ("block deal", 3, r"\bblock deals?\b|\bbulk deals?\b"),
    ("stake sale", 3, r"\bstake\b|offer for sale|\bOFS\b|promoter (sells?|selling|pledge)"),
    ("M&A", 2, r"\bacquir\w*|\bacquisition\b|\bmerger\b|\btakeover\b|open offer"),
    ("enters/launches business", 2, r"\benters?\b|\bentry into\b|\bforays?\b|\blaunch(es|ed)?\b|"
                                    r"\brent(s|ing)? out\b|new business|\bunveils?\b"),
    ("report/sources", 1, r"\bsources (said|say)\b|\breport(s|ed)? that\b|\bBloomberg\b|\bReuters\b|"
                          r"people familiar"),
]
_KEYWORD_RES = [(label, w, re.compile(p, re.I)) for label, w, p in KEYWORDS]
KEYWORD_CAP = 6
SYMBOL_POINTS, SYMBOL_CAP = 3, 6
THESIS_POINTS = 3
MOVING_POINTS = 2
PRIMARY_POINTS = 1
# Round-ups and live blogs mention many names but carry no new catalyst; they score this much less.
ROUNDUP_RE = re.compile(r"market wrap|top gainers|top losers|gainers and losers|(share|stock) price (highlights|"
                        r"history|live|today)|live updates|stocks? to watch|stocks in (the )?news|buzzing stocks",
                        re.I)
ROUNDUP_POINTS = -3


# ---------------------------------------------------------------- helpers

def now_ist() -> datetime:
    return datetime.now(IST)


def yahoo_ticker(key: str) -> str:
    """Same convention as the thesis sleeve: NSE symbol -> SYMBOL.NS; "US:XXX" -> XXX; a ticker that already
    contains a '.' is used as is."""
    if key.startswith("US:"):
        return key[3:]
    if "." in key:
        return key
    return f"{key}.NS"


def display(key: str) -> str:
    return key[3:] if key.startswith("US:") else key


def headline_id(headline: str) -> str:
    norm = re.sub(r"[^a-z0-9]+", " ", headline.lower()).strip()
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:12]


def _clean(s) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(s or ""))).strip()


def _fetch(url: str, extra_headers: dict | None = None, retries: int = 1) -> bytes:
    """GET with browser-like headers. One short retry (1.5 s) on any error, 403 included: some sites
    block the first request from a new IP and let the second through."""
    headers = dict(BROWSER_HEADERS)
    headers.update(extra_headers or {})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=FETCH_TIMEOUT) as r:
                return r.read()
        except Exception:
            if attempt == retries:
                raise
            time.sleep(1.5)


def _parse_time(s: str) -> datetime | None:
    s = _clean(s)
    if not s:
        return None
    for fmt in ("%d-%b-%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=IST)   # NSE and BSE times are IST, no zone
        except ValueError:
            pass
    try:
        dt = parsedate_to_datetime(s)
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(IST)
    except (TypeError, ValueError):
        return None


def _rss_items(raw: bytes) -> list[dict]:
    root = ET.fromstring(raw.lstrip(b"\xef\xbb\xbf").strip())
    out = []
    for it in root.iter("item"):
        out.append({c.tag.split("}")[-1]: (c.text or "") for c in it})
    return out


# ---------------------------------------------------------------- feeds

def _from_nse(feed: dict, raw: bytes) -> list[dict]:
    items = []
    for it in _rss_items(raw):
        company = _clean(it.get("title"))
        desc = _clean(it.get("description"))
        if "RELATING TO:" in desc:   # Financial_Results.xml
            period = desc.split("RELATING TO:", 1)[1].split("|", 1)[0].strip()
            what = f"financial results ({period})"
        else:   # "<Co> has informed the Exchange regarding <what> |SUBJECT: <subject>"
            body, _, subject = desc.partition("|SUBJECT:")
            what = re.sub(r"^.*?has informed the Exchange (regarding|about|that)\s*", "", body, flags=re.I).strip()
            what = what or subject.strip()
        items.append({"title": f"{company}: {what}" if what else company, "summary": desc,
                      "link": _clean(it.get("link")), "published": _parse_time(it.get("pubDate")),
                      "symbol_text": f"{company} {desc}"})
    return items


def _from_bse(feed: dict, now: datetime) -> list[dict]:
    # The endpoint only answers a single-day query (prev == to; a two-day range returns nothing). 50 rows a
    # page, newest first. Today pages 1-2 cover a busy half hour; early in the day, top up with yesterday.
    today = now.strftime("%Y%m%d")
    yday = (now - timedelta(days=1)).strftime("%Y%m%d")
    items, seen, today_rows = [], set(), 0
    for day, page in ((today, 1), (today, 2), (yday, 1)):
        if (day, page) == (today, 2) and today_rows < 50:
            continue   # today has no second page
        if day == yday and today_rows >= 50:
            break
        url = feed["url"].format(page=page, prev=day, to=day)
        resp = json.loads(_fetch(url, feed.get("headers")))
        if not isinstance(resp, dict) or "Table" not in resp:
            raise ValueError("BSE response has no 'Table' (format changed?)")
        rows = resp.get("Table") or []
        if day == today:
            today_rows += len(rows)
        for r in rows:
            nid = r.get("NEWSID")
            if nid in seen:
                continue
            seen.add(nid)
            company = _clean(r.get("SLONGNAME"))
            parts = _clean(r.get("NEWSSUB")).split(" - ", 2)
            subject = (parts[2] if len(parts) == 3 else _clean(r.get("NEWSSUB"))).replace("_", " ")
            detail = _clean(r.get("HEADLINE"))
            att = _clean(r.get("ATTACHMENTNAME"))
            link = (f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{att}" if att
                    else _clean(r.get("NSURL")) or "https://www.bseindia.com/corporates/ann.html")
            items.append({"title": f"{company}: {subject}", "summary": f"{_clean(r.get('CATEGORYNAME'))}. {detail}",
                          "link": link, "published": _parse_time(r.get("DT_TM") or r.get("NEWS_DT")),
                          "symbol_text": f"{company} {subject} {detail}"})
    return items


def _from_rss(feed: dict, raw: bytes) -> list[dict]:
    items = []
    for it in _rss_items(raw):
        title = _clean(it.get("title"))
        if not title:
            continue
        summary = _clean(re.sub(r"<[^>]+>", " ", it.get("description") or ""))
        items.append({"title": title, "summary": summary[:300], "link": _clean(it.get("link")),
                      "published": _parse_time(it.get("pubDate")), "symbol_text": title})
    return items


def fetch_feed(feed: dict, now: datetime) -> tuple[dict, list[dict]]:
    """Returns (status, items). Never raises."""
    t0 = time.monotonic()
    status = {"id": feed["id"], "name": feed["name"], "ok": False, "items": 0, "error": None,
              "optional": bool(feed.get("optional"))}
    try:
        if feed["kind"] == "bse_json":
            items = _from_bse(feed, now)
        else:
            raw = _fetch(feed["url"], feed.get("headers"))
            items = _from_nse(feed, raw) if feed["kind"] == "nse_rss" else _from_rss(feed, raw)
        for it in items:
            it["source"] = feed["name"]
            it["source_id"] = feed["id"]
            it["primary"] = bool(feed.get("primary"))
        status.update(ok=True, items=len(items))
    except Exception as e:   # fail soft: blocked, timeout, bad XML, changed format
        items = []
        status["error"] = f"{type(e).__name__}: {str(e)[:160]}"
        log.warning("feed %s failed: %s", feed["id"], status["error"])
    status["seconds"] = round(time.monotonic() - t0, 1)
    return status, items


# ---------------------------------------------------------------- watchlist and theses

def _front_matter(text: str) -> dict:
    m = re.match(r"^﻿?---\s*\n(.*?)\n---\s*(\n|$)", text, re.S)
    if not m:
        return {}
    try:
        fm = yaml.safe_load(m.group(1))
        return fm if isinstance(fm, dict) else {}
    except yaml.YAMLError:
        # fall back to the plain "key: value" lines (enough for symbol/status/market)
        fm = {}
        for line in m.group(1).splitlines():
            mm = re.match(r"^(\w+):\s*(.*)$", line)
            if mm:
                fm[mm.group(1)] = mm.group(2).strip()
        return fm


def _thesis_key(symbol, market) -> str | None:
    sym = _clean(symbol).upper()
    if not sym or sym == "SYMBOL":
        return None
    if _clean(market).upper() == "US" and not sym.startswith("US:"):
        sym = "US:" + sym
    return sym


def load_theses(theses_dir: Path | None = None) -> list[dict]:
    """Open and proposed theses in theses/*.md (files starting with '_' are templates/examples and skipped;
    theses/proposed/ holds unapproved drafts from the scanner routine and is not read)."""
    theses_dir = theses_dir or config.THESES_DIR
    out = []
    for path in sorted(Path(theses_dir).glob("*.md")):
        if path.name.startswith("_"):
            continue
        try:
            fm = _front_matter(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        status = _clean(fm.get("status")).lower() or "proposed"
        if status not in ("open", "proposed"):
            continue
        keys = []
        k = _thesis_key(fm.get("symbol"), fm.get("market"))
        if k:
            keys.append(k)
        for leg in fm.get("legs") or []:
            if isinstance(leg, dict):
                k = _thesis_key(leg.get("symbol"), leg.get("market"))
                if k and k not in keys:
                    keys.append(k)
        if keys:
            out.append({"slug": path.stem, "status": status, "type": _clean(fm.get("type")) or "theme",
                        "symbols": keys, "thesis": _clean(fm.get("thesis"))[:200]})
    return out


def load_watchlist(path: Path | None = None, theses: list[dict] | None = None) -> dict:
    path = path or WATCHLIST_FILE
    cfg = {}
    try:
        cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        log.warning("watchlist %s unreadable (%s); using Nifty 50 only", path, e)
    names_map = cfg.get("names") or {}
    symbols: dict[str, dict] = {}

    def add(key, names=(), source=""):
        key = _clean(key).upper()
        if not key:
            return
        entry = symbols.setdefault(key, {"key": key, "display": display(key), "names": [], "sources": []})
        for n in names or []:
            n = _clean(n)
            if n and n not in entry["names"]:
                entry["names"].append(n)
        if source and source not in entry["sources"]:
            entry["sources"].append(source)

    if cfg.get("include_nifty50", True):
        for s in NIFTY50:
            add(s, names_map.get(s), "nifty50")
    for e in cfg.get("extra") or []:
        if isinstance(e, dict):
            add(e.get("symbol"), e.get("names"), "watchlist")
        else:
            add(e, (), "watchlist")
    for th in theses or []:
        for k in th["symbols"]:
            add(k, names_map.get(k), "thesis")
    return {"symbols": symbols, "ignore": [_clean(x) for x in cfg.get("ignore") or [] if _clean(x)],
            "move_pct": float(cfg.get("move_pct", 4.0)), "volume_x": float(cfg.get("volume_x", 3.0))}


def _word_re(alias: str) -> re.Pattern:
    forms = {re.escape(alias), re.escape(alias.upper())}
    return re.compile(r"(?<![A-Za-z0-9&])(" + "|".join(sorted(forms)) + r")(?![A-Za-z0-9&])")


def build_matcher(watch: dict):
    pairs = []   # (alias, key)
    for key, e in watch["symbols"].items():
        aliases = list(e["names"])
        if len(e["display"]) >= 3:
            aliases.append(e["display"])
        for a in aliases:
            pairs.append((a, key))
    pairs.sort(key=lambda p: -len(p[0]))   # longer names first, then blanked out
    compiled = [(_word_re(a), key) for a, key in pairs]
    ignore = [re.compile(r"(?<![A-Za-z0-9])" + re.escape(x) + r"(?![A-Za-z0-9])", re.I) for x in watch["ignore"]]

    def match(text: str) -> list[str]:
        for rx in ignore:
            text = rx.sub(" ", text)
        found = []
        for rx, key in compiled:
            if rx.search(text):
                text = rx.sub(" ", text)
                if key not in found:
                    found.append(key)
        return found
    return match


# Legal boilerplate in nearly every exchange filing title ("Regulation 30 Of SEBI (Listing Obligations And
# Disclosure Requirements) Regulations, 2015"); removed before keyword matching so it does not score as
# "SEBI" + "listing".
BOILERPLATE_RE = re.compile(r"(Securities and Exchange Board of India|SEBI)\s*\([^)]*\)(\s*Regulations?,?(\s*\d{4})?)?"
                            r"|\(LODR\)|LODR|Reg(ulation|\.)\s*\d+\s*(\(\d+\))?", re.I)


def keyword_hits(text: str) -> list[tuple[str, int]]:
    text = BOILERPLATE_RE.sub(" ", text)
    return [(label, w) for label, w, rx in _KEYWORD_RES if rx.search(text)]


# ---------------------------------------------------------------- quotes

def _intraday_last(yf, tickers: list[str]) -> dict:
    """Last 5-minute close per ticker per date: {ticker: {date: price}}. Used when Yahoo's daily bar for the
    latest session has volume but no Close (seen on NSE symbols, e.g. the 2026-10-16 bar the next morning)."""
    out = {}
    try:
        raw = yf.download(tickers, period="5d", interval="5m", auto_adjust=False, progress=False,
                          group_by="ticker", threads=True, timeout=20)
    except Exception as e:
        log.warning("intraday quotes failed: %s", e)
        return out
    for t in tickers:
        try:
            df = (raw[t] if len(tickers) > 1 else raw).dropna(subset=["Close"])
            if df.index.tz is not None:
                df.index = df.index.tz_convert("Asia/Kolkata" if t.endswith((".NS", ".BO")) else "America/New_York")
            out[t] = {d: float(g["Close"].iloc[-1]) for d, g in df.groupby(df.index.date)}
        except Exception:
            continue
    return out


def scan_quotes(watch: dict, now: datetime) -> tuple[dict, list[dict], str | None]:
    """Yahoo daily bars for every watched symbol. Returns (quotes by key, movers, error).
    During market hours the last bar is today's partial bar (Yahoo NSE quotes are delayed ~15 minutes); its
    volume is the volume so far, compared with the full-day average of the previous 20 sessions. When that
    bar has no Close, the last 5-minute price for the same date is used instead (price_source "5m")."""
    try:
        import yfinance as yf
        keys = list(watch["symbols"])
        tick = {yahoo_ticker(k): k for k in keys}
        raw = yf.download(list(tick), period="2mo", interval="1d", auto_adjust=False, progress=False,
                          group_by="ticker", threads=True, timeout=20)
    except Exception as e:
        log.warning("quotes failed: %s", e)
        return {}, [], f"{type(e).__name__}: {str(e)[:160]}"
    frames = {}
    for t in tick:
        try:
            df = (raw[t] if len(tick) > 1 else raw).dropna(subset=["Volume"])
            df = df[(df["Volume"] > 0) | df["Close"].notna()]
            if len(df) >= 2:
                frames[t] = df
        except Exception:
            continue
    missing = [t for t, df in frames.items() if df["Close"].isna().iloc[-1]]
    intraday = _intraday_last(yf, missing) if missing else {}

    quotes, movers = {}, []
    stale_before = (now - timedelta(days=5)).date()
    for t, df in frames.items():
        key = tick[t]
        try:
            bar_date = df.index[-1].date()
            if bar_date < stale_before:
                continue
            last, source = df["Close"].iloc[-1], "daily"
            if last != last:   # NaN
                last, source = intraday.get(t, {}).get(bar_date), "5m"
            if last is None:   # no price for the latest session at all: judge the previous one
                df, source = df.iloc[:-1], "daily"
                bar_date, last = df.index[-1].date(), df["Close"].iloc[-1]
            prevs = df["Close"].iloc[:-1].dropna()
            if prevs.empty or last != last:
                continue
            last, prev = float(last), float(prevs.iloc[-1])
            chg = (last / prev - 1) * 100 if prev else 0.0
            avg = float(df["Volume"].iloc[-21:-1].mean())
            vol = float(df["Volume"].iloc[-1])
            vx = vol / avg if avg > 0 else 0.0
        except Exception:
            continue
        q = {"key": key, "symbol": display(key), "date": bar_date.isoformat(), "close": round(last, 2),
             "change_pct": round(chg, 2), "volume_x": round(vx, 2), "price_source": source}
        quotes[key] = q
        q["move"] = abs(chg) > watch["move_pct"]
        q["volume"] = vx > watch["volume_x"]
        if q["move"] or q["volume"]:
            movers.append(q)
    return quotes, movers, None


# ---------------------------------------------------------------- scoring

def _thesis_refs(keys: list[str], theses: list[dict]) -> list[dict]:
    refs = []
    for th in theses:
        if any(k in th["symbols"] for k in keys):
            refs.append({"slug": th["slug"], "status": th["status"], "type": th["type"]})
    return refs


def score_item(it: dict, match, theses: list[dict], movers_by_key: dict) -> dict | None:
    keys = match(it["symbol_text"])
    kw = keyword_hits(f"{it['title']} {it['summary'] if it['primary'] else ''}")
    kw_points = min(KEYWORD_CAP, sum(w for _, w in kw))
    sym_points = min(SYMBOL_CAP, SYMBOL_POINTS * len(keys))
    refs = _thesis_refs(keys, theses)
    moving = [k for k in keys if k in movers_by_key]
    roundup = bool(ROUNDUP_RE.search(it["title"]))
    score = (kw_points + sym_points + (THESIS_POINTS if refs else 0) + (MOVING_POINTS if moving else 0)
             + (PRIMARY_POINTS if it["primary"] and (keys or kw) else 0) + (ROUNDUP_POINTS if roundup else 0))
    if score < MIN_SCORE:
        return None
    pub = it["published"]
    return {
        "id": headline_id(it["title"]), "kind": "news",
        "time": pub.isoformat(timespec="minutes") if pub else None,
        "source": it["source"], "headline": it["title"], "summary": it["summary"][:240], "link": it["link"],
        "symbols": [display(k) for k in keys], "symbol_keys": keys,
        "keywords": [label for label, _ in kw] + (["round-up"] if roundup else []),
        "moving": [display(k) for k in moving],
        "score": score, "high": score >= HIGH_SCORE, "theses": refs, "second_order_candidates": [],
    }


def price_alert(q: dict, theses: list[dict], news: list[dict], now: datetime) -> dict:
    why = []
    if q["move"]:
        why.append(f"{q['change_pct']:+.1f}% vs previous close")
    if q["volume"]:
        why.append(f"volume {q['volume_x']:.1f}x 20-day average")
    refs = _thesis_refs([q["key"]], theses)
    score = 4 + (2 if q["move"] and q["volume"] else 0) + (2 if abs(q["change_pct"]) >= 8 else 0) \
        + (THESIS_POINTS if refs else 0)
    related = [n["headline"] for n in news if q["key"] in n["symbol_keys"]][:3]
    return {
        "id": hashlib.sha1(f"price|{q['key']}|{q['date']}".encode()).hexdigest()[:12], "kind": "price",
        "time": now.isoformat(timespec="minutes"), "source": "Yahoo quotes",
        "headline": f"{q['symbol']} {', '.join(why)} (close {q['close']}, bar {q['date']})",
        "summary": ("Headlines this run: " + " | ".join(related)) if related else "No matching headline in this run's feeds.",
        "link": f"https://finance.yahoo.com/quote/{yahoo_ticker(q['key'])}",
        "symbols": [q["symbol"]], "symbol_keys": [q["key"]], "keywords": ["price move" if q["move"] else "volume spike"],
        "moving": [q["symbol"]], "score": score, "high": score >= HIGH_SCORE, "theses": refs,
        "second_order_candidates": [], "change_pct": q["change_pct"], "volume_x": q["volume_x"],
    }


# ---------------------------------------------------------------- output

def _day_file(d) -> Path:
    return ALERTS_DIR / f"{d.isoformat()}.json"


def _load_day(d) -> dict:
    p = _day_file(d)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.warning("%s unreadable; starting it again", p)
    return {"date": d.isoformat(), "alerts": []}


NOTE_HEAD_RE = re.compile(r"^###\s*\[([0-9a-f]{12})\]")


def load_notes(d) -> dict:
    """Second-order notes from alerts/notes/<date>.md, keyed by alert id: {id: ["Winner: ...", ...]}.
    Format (SCANNER_PROMPT.md): a heading "### [<alert id>] <headline>" followed by "- " lines."""
    p = NOTES_DIR / f"{d.isoformat()}.md"
    notes, cur = {}, None
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return notes
    for line in lines:
        m = NOTE_HEAD_RE.match(line.strip())
        if m:
            cur = notes.setdefault(m.group(1), [])
        elif line.startswith("#"):
            cur = None
        elif cur is not None and line.strip().startswith("- "):
            cur.append(line.strip()[2:].strip())
    return notes


def _sort_key(a: dict) -> str:
    return a.get("time") or a.get("first_seen") or ""


def _short_time(iso: str | None) -> str:
    return iso[:16].replace("T", " ") if iso else "?"


def write_latest_md(alerts: list[dict], status: dict, now: datetime) -> None:
    cutoff = (now - timedelta(hours=24)).isoformat(timespec="minutes")
    recent = [a for a in alerts if (_sort_key(a) >= cutoff)]
    top = sorted(recent, key=lambda a: (a["score"], _sort_key(a)), reverse=True)[:LATEST_TOP_N]
    ok = [f["name"] for f in status["feeds"] if f["ok"]]
    bad = [f"{f['name']} ({f['error']})" for f in status["feeds"] if not f["ok"]]
    lines = [
        f"# Catalyst scanner: top alerts, last 24 hours",
        "",
        f"Generated {now.strftime('%Y-%m-%d %H:%M')} IST. High score = {HIGH_SCORE}+. "
        f"Feeds OK: {', '.join(ok) or 'none'}." + (f" Failed: {'; '.join(bad)}." if bad else "")
        + (" Optional, unavailable: " + "; ".join(f"{f['name']} ({f['error']})" for f in status["optional_unavailable"])
           + "." if status.get("optional_unavailable") else ""),
        f"Quotes: {status['quotes_checked']} symbols checked, {status['movers']} moving"
        + (f" (quote error: {status['quotes_error']})" if status.get("quotes_error") else "") + ".",
        "",
        "Paper trading only. Nothing here is an order; the reviewer routine (SCANNER_PROMPT.md) only writes notes "
        "and draft theses for the user to approve.",
        "",
    ]
    if not top:
        lines.append("No alerts in the last 24 hours.")
    for i, a in enumerate(top, 1):
        th = ", ".join(f"{t['slug']} ({t['status']})" for t in a["theses"]) or "none"
        lines += [
            f"## {i}. [{a['score']}{' HIGH' if a['high'] else ''}] {a['headline']}",
            "",
            f"- Time: {_short_time(a.get('time') or a.get('first_seen'))} IST",
            f"- Source: {a['source']}",
            f"- Link: {a['link']}",
            f"- Matched symbols: {', '.join(a['symbols']) or 'none'}"
            + (f" (moving today: {', '.join(a['moving'])})" if a.get("moving") else ""),
            f"- Keywords: {', '.join(a['keywords']) or 'none'}",
            f"- Thesis match: {th}",
            f"- Score: {a['score']}",
            f"- Alert id: {a['id']}",
            "- Second-order candidates: " + " / ".join(a.get("second_order_candidates") or []),
            "",
        ]
    LATEST_MD.write_text("\n".join(lines), encoding="utf-8")


def run(do_quotes: bool = True, now: datetime | None = None) -> dict:
    t0 = time.monotonic()
    now = now or now_ist()
    theses = load_theses()
    watch = load_watchlist(theses=theses)
    match = build_matcher(watch)

    with ThreadPoolExecutor(max_workers=len(FEEDS) + 1) as pool:
        quote_job = pool.submit(scan_quotes, watch, now) if do_quotes else None
        feed_jobs = [pool.submit(fetch_feed, f, now) for f in FEEDS]
        results = [j.result() for j in feed_jobs]
        quotes, movers, quotes_error = quote_job.result() if quote_job else ({}, [], "skipped (--no-quotes)")

    movers_by_key = {q["key"]: q for q in movers}
    oldest = now - timedelta(hours=MAX_AGE_HOURS)
    scored = []
    for _, items in results:
        for it in items:
            if it["published"] and it["published"] < oldest:
                continue
            a = score_item(it, match, theses, movers_by_key)
            if a:
                scored.append(a)
    for q in movers:
        scored.append(price_alert(q, theses, scored, now))

    today, yesterday = now.date(), (now - timedelta(days=1)).date()
    day = _load_day(today)
    prev = _load_day(yesterday) if _day_file(yesterday).exists() else {"alerts": []}
    by_id = {a["id"]: a for a in day["alerts"]}
    seen = set(by_id) | {a["id"] for a in prev["alerts"]}
    stamp = now.isoformat(timespec="minutes")
    new = 0
    for a in scored:
        if a["kind"] == "price" and a["id"] in by_id:   # a mover seen earlier today: refresh its numbers
            old = by_id[a["id"]]
            a["first_seen"] = old.get("first_seen", stamp)
            a["time"] = old.get("time", a["time"])
            a["second_order_candidates"] = old.get("second_order_candidates", [])
            old.update(a)
            continue
        if a["id"] in seen:
            continue
        seen.add(a["id"])
        a["first_seen"] = stamp
        day["alerts"].append(a)
        by_id[a["id"]] = a
        new += 1
    day["updated_at"] = stamp

    status = {
        # an optional feed that failed is listed apart, so the dashboard does not show it as a feed down
        "generated_at": stamp, "feeds": [s for s, _ in results if s["ok"] or not s["optional"]],
        "optional_unavailable": [s for s, _ in results if not s["ok"] and s["optional"]],
        "quotes_checked": len(quotes),
        "movers": len(movers), "quotes_error": quotes_error, "new_alerts": new,
        "high_score": HIGH_SCORE, "seconds": round(time.monotonic() - t0, 1),
    }
    # the reviewer routine's second-order notes (alerts/notes/<date>.md) fill each alert's candidates field
    notes = {**load_notes(yesterday), **load_notes(today)}
    both = day["alerts"] + prev["alerts"]
    for a in both:
        if a["id"] in notes:
            a["second_order_candidates"] = notes[a["id"]]

    ALERTS_DIR.mkdir(parents=True, exist_ok=True)
    _day_file(today).write_text(json.dumps(day, indent=1, ensure_ascii=False), encoding="utf-8")
    write_latest_md(both, status, now)
    newest = sorted(both, key=_sort_key, reverse=True)[:DOCS_MAX_ALERTS]
    payload = dict(status, theses=[{"slug": t["slug"], "status": t["status"], "symbols": [display(k) for k in t["symbols"]]}
                                   for t in theses],
                   watchlist_size=len(watch["symbols"]), alerts=newest)
    DOCS_ALERTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    DOCS_ALERTS_FILE.write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
    status["day_total"] = len(day["alerts"])
    return status


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="python -m papertrade.scanner", description=__doc__.split("\n\n")[0])
    ap.add_argument("--no-quotes", action="store_true", help="skip the Yahoo quote check (feeds only)")
    args = ap.parse_args(argv)
    st = run(do_quotes=not args.no_quotes)
    for f in st["feeds"] + st["optional_unavailable"]:
        tag = "OK  " if f["ok"] else ("OPT " if f["optional"] else "FAIL")
        print(f"  {tag} {f['name']:<18} items {f['items']:>3}  {f['seconds']}s"
              + (f"  {f['error']}" if f["error"] else ""))
    print(f"Quotes: {st['quotes_checked']} checked, {st['movers']} moving"
          + (f"  ({st['quotes_error']})" if st["quotes_error"] else ""))
    print(f"New alerts: {st['new_alerts']}  (today's file now {st['day_total']})  in {st['seconds']}s")
    print(f"Wrote {_day_file(now_ist().date()).relative_to(config.ROOT)}, {LATEST_MD.relative_to(config.ROOT)}, "
          f"{DOCS_ALERTS_FILE.relative_to(config.ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
