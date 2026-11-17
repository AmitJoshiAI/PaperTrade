"""Credit structures in the candidates table (candidates.py): the maths, the strike choice, the liquidity labels,
and that PASS/FAIL, margin and cash mirror the engine (derivbook.run_orders + check_open + DerivBook.open).

Synthetic prices only; the real-chain checks were run by hand on a scratch copy (see the session report).
Nothing here touches state/: the one test that lets the engine write its ledger points config at a temp dir.

Run:  python -m unittest discover -s tests      or      python -m unittest tests.test_credit_structures
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd

from papertrade import candidates as C, config
from papertrade.costs import charges, us_charges
from papertrade.derivbook import DPos, bs_price, position_id, run_orders
from papertrade.fno import FnoBook
from papertrade.us import UsBook

EXP = "2026-11-23"
DAY = "2026-11-16"
T = 7 / 365


def india_quote_fn(S=25291.1, sigma=0.15, listed=None, overrides=None):
    """quote_at(K, right) for a NIFTY-like chain priced by Black-Scholes; overrides {(K, right): dict}."""
    overrides = overrides or {}

    def q(K, right):
        if (K, right) in overrides:
            return overrides[(K, right)]
        if listed is not None and K not in listed:
            return dict(price=1.0, src="model", listed=False)
        p = round(bs_price(S, K, T, config.CARRY_RATE, sigma, "CALL" if right == "CE" else "PUT"), 2)
        return dict(price=p, src=C.chains.CHAIN_LABEL, bid=round(p * 0.995, 2), ask=round(p * 1.005, 2), listed=True)
    return q


def india_rows(q, atm=25300.0, step=50.0):
    return {o: dict(offset=o, strike=atm + o * step, CE=q(atm + o * step, "CE"), PE=q(atm + o * step, "PE"))
            for o in (-2, -1, 0, 1, 2)}


def india_ctx(book, lot=65, fill_day="2026-11-17", expiry=EXP):
    return dict(book=book, underlying="NIFTY", lot=lot, expiry=expiry, letters=("CE", "PE"), fill_day=fill_day,
                charge_fn=lambda side, lots, px: charges(side, lots * lot, px, "OPT").total, eq=book.equity(),
                ccy="INR", us=False, atm_real=True, strike_step=50.0, min_price=config.CANDIDATE_MIN_LEG_PRICE["INR"])


def build(book, q=None, listed=None, **kw):
    q = q or india_quote_fn()
    grid = listed if listed is not None else [25300.0 + 50 * i for i in range(-60, 61)]
    st = C._structures(india_rows(q), india_ctx(book, **kw), q, C.Ladder(grid, 25300.0, 50.0))
    return {s["name"]: s for s in st}


def fresh_book(cash=500_000.0):
    return FnoBook(cash=cash, starting_capital=500_000.0)


class Maths(unittest.TestCase):
    def test_bull_put_numbers(self):
        s = build(fresh_book())["Bull put spread"]
        self.assertEqual(s["verdict"], "PASS", s.get("why"))
        wing, short = s["legs"]
        self.assertEqual((wing["side"], wing["right"], short["side"], short["right"]), ("BUY", "PE", "SELL", "PE"))
        self.assertLess(wing["strike"], short["strike"])
        self.assertLess(short["strike"], 25300.0)
        credit = short["price"] - wing["price"]
        gap = short["strike"] - wing["strike"]
        ch = charges("SELL", 65, short["price"], "OPT").total + charges("BUY", 65, wing["price"], "OPT").total
        self.assertAlmostEqual(s["net_credit"], credit, places=6)
        self.assertAlmostEqual(s["net_credit_lot"], round(credit * 65, 2), places=2)
        self.assertAlmostEqual(s["entry_charges"], round(ch, 2), places=2)
        self.assertAlmostEqual(s["max_loss"], round((gap - credit) * 65 + ch, 2), places=2)
        self.assertAlmostEqual(s["max_gain"], round(credit * 65 - ch, 2), places=2)
        self.assertEqual(s["breakevens"], [round(short["strike"] - credit, 2)])
        self.assertAlmostEqual(s["margin"], gap * 65, places=2)      # engine: strike gap x units
        self.assertEqual(gap, 2 * 50)                                # wing CREDIT_WING_STEPS beyond

    def test_short_strike_is_nearest_atm_minus_expected_move(self):
        q = india_quote_fn()
        rows = india_rows(q)
        em = rows[0]["CE"]["price"] + rows[0]["PE"]["price"]
        s = build(fresh_book(), q)
        put_short = s["Bull put spread"]["legs"][1]["strike"]
        call_short = s["Bear call spread"]["legs"][1]["strike"]
        grid = [25300.0 + 50 * i for i in range(-60, 61)]
        self.assertEqual(put_short, min([k for k in grid if k <= 25250], key=lambda k: abs(k - (25300 - em))))
        self.assertEqual(call_short, min([k for k in grid if k >= 25350], key=lambda k: abs(k - (25300 + em))))
        self.assertEqual(s["Bull put spread"]["expected_move"], round(em, 4))

    def test_condor_numbers_use_wider_gap_and_total_credit(self):
        s = build(fresh_book())
        ic, bp, bc = s["Iron condor"], s["Bull put spread"], s["Bear call spread"]
        self.assertEqual([l["side"] for l in ic["legs"]], ["BUY", "BUY", "SELL", "SELL"])   # covers first
        total = bp["net_credit"] + bc["net_credit"]
        ch = bp["entry_charges"] + bc["entry_charges"]
        gap = max(bp["legs"][1]["strike"] - bp["legs"][0]["strike"], bc["legs"][0]["strike"] - bc["legs"][1]["strike"])
        self.assertAlmostEqual(ic["net_credit"], total, places=6)
        self.assertAlmostEqual(ic["max_loss"], round((gap - total) * 65 + ch, 2), places=1)
        self.assertAlmostEqual(ic["max_gain"], round(total * 65 - ch, 2), places=1)
        self.assertEqual(ic["breakevens"], [round(bp["legs"][1]["strike"] - total, 2), round(bc["legs"][1]["strike"] + total, 2)])
        self.assertAlmostEqual(ic["margin"], bp["margin"] + bc["margin"], places=2)   # both sides blocked
        self.assertGreater(ic["margin"], ic["max_loss"])

    def test_inconsistent_prices(self):
        q0 = india_quote_fn()
        s = build(fresh_book())
        wing_k, short_k = s["Bull put spread"]["legs"][0]["strike"], s["Bull put spread"]["legs"][1]["strike"]
        bad = dict(price=q0(short_k, "PE")["price"], src=C.chains.CHAIN_LABEL, listed=True)
        wing = dict(price=bad["price"] + 1, src=C.chains.CHAIN_LABEL, listed=True)    # wing dearer than short
        s2 = build(fresh_book(), india_quote_fn(overrides={(wing_k, "PE"): wing}))
        self.assertEqual(s2["Bull put spread"]["verdict"], "n/a")
        self.assertIn("inconsistent prices", s2["Bull put spread"]["why"])
        self.assertEqual(s2["Iron condor"]["verdict"], "n/a")
        self.assertEqual(s2["Bear call spread"]["verdict"], "PASS")

    def test_wing_falls_back_to_one_step_then_toward_atm(self):
        s = build(fresh_book())
        short_k = s["Bull put spread"]["legs"][1]["strike"]
        grid = [25300.0 + 50 * i for i in range(-60, 61) if 25300.0 + 50 * i != short_k - 100]
        s1 = build(fresh_book(), listed=grid)
        bp = s1["Bull put spread"]
        self.assertEqual(bp["legs"][0]["strike"], short_k - 50)
        self.assertIn("wing 1 step", bp["strike_note"])
        grid2 = [k for k in grid if k not in (short_k - 50,)]
        bp2 = build(fresh_book(), listed=grid2)["Bull put spread"]
        self.assertEqual([l["strike"] for l in bp2["legs"]], [25200.0, 25250.0])    # ATM-2 wing, ATM-1 short
        self.assertIn("fallback to ATM-1 / ATM-2", bp2["strike_note"])

    def test_wing_below_min_price_is_not_used(self):
        s = build(fresh_book())
        wing_k = s["Bull put spread"]["legs"][0]["strike"]
        cheap = dict(price=0.05, src=C.chains.CHAIN_LABEL, listed=True)
        bp = build(fresh_book(), india_quote_fn(overrides={(wing_k, "PE"): cheap}))["Bull put spread"]
        self.assertNotEqual(bp["legs"][0]["strike"], wing_k)

    def test_expiry_not_after_fill_day_is_na(self):
        s = build(fresh_book(), fill_day="2026-11-17", expiry="2026-11-17")
        self.assertTrue(all(x["verdict"] == "n/a" and "fill day" in x["why"] for x in s.values()), s)

    def test_fill_estimate_and_label(self):
        base = india_quote_fn()

        def q(K, right):
            d = dict(base(K, right))
            d["fill_est_buy"], d["fill_est_sell"] = round(d["price"] * 1.01, 2), round(d["price"] * 0.99, 2)
            return d
        bp = build(fresh_book(), q)["Bull put spread"]
        wing, short = bp["legs"]
        est = round(short["price"] * 0.99, 2) - round(wing["price"] * 1.01, 2)
        self.assertAlmostEqual(bp["fill_est_credit"], est, places=4)
        self.assertAlmostEqual(bp["fill_est_lot"], round(est * 65, 2), places=2)
        self.assertFalse(any("fill est" in x for x in bp["quotes"]))

        def q2(K, right):            # the fill model prices every leg equal: zero credit at the fill
            d = dict(base(K, right))
            d["fill_est_buy"] = d["fill_est_sell"] = 10.0
            return d
        bp2 = build(fresh_book(), q2)["Bull put spread"]
        self.assertTrue(any(x.startswith("engine fill est. credit 0.00") for x in bp2["quotes"]), bp2["quotes"])
        self.assertEqual(bp2["verdict"], "PASS")       # a label, not a block

    def test_event_size_marker_and_pct(self):
        s = build(fresh_book())
        for x in s.values():
            if x["verdict"] != "PASS":
                continue
            self.assertAlmostEqual(x["max_loss_pct"], round(x["max_loss"] / 500_000 * 100, 2))
            self.assertEqual(x["event_size"], "fits" if x["max_loss"] <= 0.02 * 500_000 else "too big")
        self.assertEqual(s["Long straddle"]["event_size"], "too big")
        self.assertEqual(s["Bull put spread"]["event_size"], "fits")


class EngineMirror(unittest.TestCase):
    """The table's verdict, margin and cash change against the real engine loop on a real FnoBook."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.saved = {k: getattr(config, k) for k in ("STATE_DIR", "FNO_TRADES_FILE")}
        config.STATE_DIR = self.tmp
        config.FNO_TRADES_FILE = self.tmp / "fno_trades.csv"

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(config, k, v)
        shutil.rmtree(self.tmp, ignore_errors=True)

    class Market:
        def __init__(self, prices):
            self.prices = prices

        def fill_day(self, o):
            return "2026-11-17"

        def pos_id(self, p):
            return position_id(p.underlying, p.kind, p.side, p.expiry, p.strike, ("CE", "PE"))

        def open_contract(self, o, fill_day, side):
            kind = "CALL" if o["instrument"] == "CE" else "PUT"
            px = self.prices[(float(o["strike"]), o["instrument"])]
            pos = DPos(id="", underlying="NIFTY", kind=kind, side=side, lots=int(o["lots"]), lot_size=65, entry_price=px,
                       entry_date=fill_day, strike=float(o["strike"]), expiry=o["expiry"])
            return pos, px, charges("BUY" if side == "LONG" else "SELL", int(o["lots"]) * 65, px, "OPT"), ""

    def engine(self, book, legs, lots):
        orders = [dict(action="OPEN", underlying="NIFTY", instrument=l["right"], side=l["side"], lots=lots,
                       strike=l["strike"], expiry=EXP, decision_date=DAY, reason="t", risk_check="t") for l in legs]
        prices = {(l["strike"], l["right"]): l["price"] for l in legs}
        cash0 = book.cash
        fills, notes, keep, executed = run_orders(book, orders, self.Market(prices), DAY)
        return dict(ok=all(e["status"] == "filled" for e in executed),
                    rejected=sorted(e["order"]["side"] + " " + e["order"]["instrument"] + " " + C._k(e["order"]["strike"])
                                    for e in executed if e["status"] == "rejected"),
                    margin=round(sum(f["margin"] for f in fills), 2), cash_change=round(book.cash - cash0, 2))

    def books(self):
        """(label, factory) over cash, equity, slots, breaker and an existing same-expiry long put."""
        def held(n):
            def f():
                b = fresh_book()
                for i in range(n):
                    p = DPos(id=f"X{i}", underlying="BANKNIFTY", kind="CALL", side="LONG", lots=1, lot_size=30,
                             entry_price=100.0, entry_date=DAY, strike=70000 + 100 * i, expiry=EXP, mark=100.0)
                    b.positions[p.id] = p
                    b.cash -= 3000.0
                return b
            return f

        def tripped():
            b = fresh_book()
            b.breaker = dict(tripped_on=DAY, until="2026-11-23", day_pnl=-16000.0, limit=15000.0)
            return b

        def other_long_put():
            b = fresh_book()
            p = DPos(id=position_id("NIFTY", "PUT", "LONG", EXP, 25300.0, ("CE", "PE")), underlying="NIFTY", kind="PUT",
                     side="LONG", lots=1, lot_size=65, entry_price=190.0, entry_date=DAY, strike=25300.0, expiry=EXP, mark=190.0)
            b.positions[p.id] = p
            b.cash -= 190.0 * 65
            return b
        def cash_poor(cash):
            def f():          # equity high (a held long worth Rs 4.9 lakh), cash low: the free-cash rule binds
                b = FnoBook(cash=cash, starting_capital=500_000.0)
                p = DPos(id="BIG", underlying="BANKNIFTY", kind="CALL", side="LONG", lots=1, lot_size=30,
                         entry_price=16_400.0, entry_date=DAY, strike=50_000.0, expiry=EXP, mark=16_400.0)
                b.positions[p.id] = p
                return b
            return f
        out = [("fresh", fresh_book), ("3 held", held(3)), ("4 held", held(4)), ("breaker", tripped),
               ("other long put", other_long_put)]
        for cash in (9_000.0, 5_000.0, 3_500.0):
            out.append((f"cash-poor {cash:g}", cash_poor(cash)))
        for cash in (60_000.0, 12_000.0, 6_000.0, 5_000.0, 2_000.0):
            out.append((f"cash {cash:g}", lambda c=cash: fresh_book(c)))
        out.append(("small equity", lambda: FnoBook(cash=55_000.0, starting_capital=55_000.0)))
        return out

    def test_verdict_margin_cash_match_engine(self):
        checked = 0
        for label, factory in self.books():
            table = build(factory())
            for name, s in table.items():
                if s["verdict"] == "n/a":
                    continue
                for lots in (1, 2, 7, 8):
                    sim = C.simulate(factory(), s["legs"], lots, india_ctx(factory()))
                    got = self.engine(factory(), s["legs"], lots)
                    with self.subTest(book=label, structure=name, lots=lots):
                        self.assertEqual(sim["ok"], got["ok"], (sim["rejects"], got["rejected"]))
                        self.assertEqual(sorted(r.split(":")[0] for r in sim["rejects"]), got["rejected"])
                        self.assertAlmostEqual(sim["margin"], got["margin"], places=2)
                        self.assertAlmostEqual(sim["cash_change"], got["cash_change"], places=2)
                        if lots == 1:
                            self.assertEqual(s["verdict"] == "PASS", got["ok"])
                            if got["ok"]:
                                self.assertAlmostEqual(s["margin"], got["margin"], places=2)
                    checked += 1
        self.assertGreater(checked, 100)

    def test_max_lots_is_the_engine_limit(self):
        for label, factory in self.books():
            for name, s in build(factory()).items():
                if s["verdict"] != "PASS":
                    continue
                n = s["max_lots"]
                with self.subTest(book=label, structure=name):
                    self.assertTrue(self.engine(factory(), s["legs"], n)["ok"])
                    self.assertFalse(self.engine(factory(), s["legs"], n + 1)["ok"])

    def test_condor_needs_four_slots(self):
        s = build(self.books()[2][1]())          # 4 positions held: 1 slot free
        self.assertEqual(s["Iron condor"]["verdict"], "FAIL")
        self.assertIn("open positions", s["Iron condor"]["why"])
        s = build(self.books()[1][1]())          # 3 held: a 2-leg spread fits, the condor does not
        self.assertEqual(s["Bull put spread"]["verdict"], "PASS")
        self.assertEqual(s["Iron condor"]["verdict"], "FAIL")

    def test_uncovered_short_is_rejected_by_engine(self):
        legs = [dict(side="SELL", right="CE", strike=26000.0, price=20.0)]
        got = self.engine(fresh_book(), legs, 1)
        self.assertFalse(got["ok"])
        sim = C.simulate(fresh_book(), legs, 1, india_ctx(fresh_book()))
        self.assertFalse(sim["ok"])
        self.assertIn("naked short option", sim["rejects"][0])

    def test_short_written_first_still_fills_after_its_cover(self):
        s = build(fresh_book())["Bull put spread"]
        self.assertTrue(self.engine(fresh_book(), list(reversed(s["legs"])), 1)["ok"])

    # ---- after the open: closes and expiry (what the reviewer's exit rule relies on) ----
    class CloseMarket(Market):
        def __init__(self, prices, close_prices):
            super().__init__(prices)
            self.close_prices = close_prices

        def close_price(self, p, o, fill_day):
            px = self.close_prices[(p.strike, "CE" if p.kind == "CALL" else "PE")]
            side = "SELL" if p.side == "LONG" else "BUY"
            return px, charges(side, p.units, px, "OPT"), "test", ""

    def opened_bull_put(self):
        s = build(fresh_book())["Bull put spread"]
        book = fresh_book()
        self.engine(book, s["legs"], 1)
        self.assertEqual(len(book.positions), 2)
        return s, book

    def close(self, book, s, orders):
        prices = {(l["strike"], l["right"]): l["price"] for l in s["legs"]}
        closes = {(l["strike"], l["right"]): l["price"] * 0.5 for l in s["legs"]}
        return run_orders(book, orders, self.CloseMarket(prices, closes), "2026-11-18")

    def ids(self, s):
        return {l["side"]: position_id("NIFTY", "PUT", "LONG" if l["side"] == "BUY" else "SHORT", EXP, l["strike"],
                                       ("CE", "PE")) for l in s["legs"]}

    def test_closing_the_cover_alone_is_rejected(self):
        s, book = self.opened_bull_put()
        ids = self.ids(s)
        fills, notes, keep, ex = self.close(book, s, [dict(action="CLOSE", position=ids["BUY"], decision_date="2026-11-17")])
        self.assertEqual(ex[0]["status"], "rejected")
        self.assertIn("naked", ex[0]["detail"])
        self.assertEqual(keep, [])                         # dropped from the queue, not kept
        self.assertEqual(len(book.positions), 2)

    def test_both_closes_with_ids_in_any_order(self):
        s, book = self.opened_bull_put()
        ids = self.ids(s)
        eq0 = book.equity()
        orders = [dict(action="CLOSE", position=ids["BUY"], decision_date="2026-11-17"),     # long written first
                  dict(action="CLOSE", position=ids["SELL"], decision_date="2026-11-17")]
        fills, notes, keep, ex = self.close(book, s, orders)
        self.assertEqual([e["status"] for e in ex], ["filled", "filled"])
        self.assertEqual(ex[0]["order"]["position"], ids["SELL"])     # the engine closed the short first
        self.assertEqual(book.positions, {})
        self.assertAlmostEqual(book.margin_used(), 0.0)
        self.assertAlmostEqual(fills[0]["margin"], s["margin"], places=2)   # margin released on the short's close
        self.assertAlmostEqual(book.cash, book.equity())

    def test_closes_without_ids_long_first_drops_the_long(self):
        s, book = self.opened_bull_put()
        orders = [dict(action="CLOSE", underlying="NIFTY", instrument="PE", strike=l["strike"], expiry=EXP,
                       decision_date="2026-11-17") for l in s["legs"]]                      # BUY (long) first
        fills, notes, keep, ex = self.close(book, s, orders)
        self.assertEqual([e["status"] for e in ex], ["rejected", "filled"])
        self.assertIn("naked", ex[0]["detail"])
        self.assertEqual(list(book.positions), [self.ids(s)["BUY"]])      # the long wing is left open

    def test_expiry_below_the_wing_realises_max_loss_plus_exercise_stt(self):
        from papertrade import fno
        s, book = self.opened_bull_put()
        cash_open = book.cash + s["margin"]          # what cash would be with the margin back
        wing, short = s["legs"][0]["strike"], s["legs"][1]["strike"]
        S = wing - 300.0

        class Mkt:
            def spot_close(self, ticker, day):
                return S, EXP
        fills, notes, rolls = fno.expire_positions(book, Mkt(), EXP)
        self.assertEqual(book.positions, {})
        stt = round((wing - S) * 65 * 0.0015, 2)                 # the long wing settles ITM: exercise STT
        realised_total = sum(f["realised_pnl"] for f in fills)
        self.assertAlmostEqual(realised_total, -s["max_loss"] - stt, places=1)
        self.assertAlmostEqual(book.cash - 500_000.0, -s["max_loss"] - stt, places=1)
        self.assertTrue(cash_open)


class QuoteLabels(unittest.TestCase):
    def leg(self, src, bid, ask, **kw):
        return dict(side="SELL", right="PE", strike=24900.0, price=(bid + ask) / 2 if bid and ask else 10.0,
                    src=src, bid=bid, ask=ask, **kw)

    def test_wide_one_sided_no_quote(self):
        real = C.chains.CHAIN_LABEL
        self.assertEqual(C._quote_flags([self.leg(real, 100, 105)], "INR", True, False), [])
        f = C._quote_flags([self.leg(real, 100, 120)], "INR", True, False)
        self.assertTrue(f and f[0].startswith("wide quotes: SELL PE 24,900 bid/ask 100.00/120.00"), f)
        f = C._quote_flags([self.leg(C.chains.CHAIN_IV_LABEL, 0, 12.5)], "INR", True, False)
        self.assertIn("one-sided", f[0])
        f = C._quote_flags([self.leg(C.chains.CHAIN_IV_LABEL, 0, 0)], "INR", True, False)
        self.assertTrue(f[0].startswith("no quote"))
        self.assertEqual(C._quote_flags([self.leg(C.chains.CHAIN_IV_LABEL, 0, 0)], "INR", False, False), [])
        # floor: a 2-tick spread on a cheap option is not wide
        self.assertEqual(C._quote_flags([self.leg(real, 0.80, 0.90)], "INR", True, False), [])

    def test_us_last_trade(self):
        l = self.leg(C.US_LT_LABEL, 0, 0, last=None)
        self.assertIn("no trade in the last session", C._quote_flags([l], "USD", False, True)[0])
        l = self.leg(C.US_LT_LABEL, 0, 0, last=1.2)
        self.assertEqual(C._quote_flags([l], "USD", False, True), [])


class UsBuilder(unittest.TestCase):
    """_us_one end to end on a fake yfinance market: listed-strike steps, the $0.50 grid filter, contracts."""

    class Market:
        def __init__(self):
            ks = [k / 2 for k in range(60, 141)] + [41.17, 43.67]          # 30..70 by 0.5, plus two adjusted
            S, t = 50.0, 32 / 365

            def frame(kind):
                rows = []
                for k in ks:
                    p = bs_price(S, k, t, 0.04, 0.30, kind)
                    rows.append(dict(strike=k, bid=round(max(p - 0.02, 0.01), 2), ask=round(p + 0.02, 2),
                                     impliedVolatility=0.30, lastPrice=round(p, 2),
                                     lastTradeDate=pd.Timestamp("2026-11-16 20:00"), contractSymbol=f"X{kind[0]}{k}"))
                return pd.DataFrame(rows)
            self.frames = {"CALL": frame("CALL"), "PUT": frame("PUT")}

        def last_close(self, t, day=None):
            return 50.0, "2026-11-16"

        def expiries(self, t):
            return ["2026-12-18"]

        def chain(self, t, e, kind):
            return self.frames[kind]

    def test_us_rows_and_credit(self):
        book = UsBook(cash=5289.62, starting_capital=5289.62)
        x = C._us_one(self.Market(), "XYZ", "2026-11-16", book.equity(), book)
        ex = x["expiries"][0]
        sts = {s["name"]: s for s in ex["structures"]}
        ks = [l["strike"] for s in sts.values() for l in s.get("legs", [])]
        self.assertFalse(any(k in (41.17, 43.67) for k in ks), ks)       # adjusted strikes never used
        bp = sts["Bull put spread"]
        self.assertEqual(bp["verdict"], "PASS", bp.get("why"))
        wing, short = bp["legs"]
        self.assertAlmostEqual(short["strike"] - wing["strike"], 0.5)     # US wing: 1 listed strike of 0.5
        self.assertAlmostEqual(bp["margin"], 50.0)
        credit = short["price"] - wing["price"]
        ch = us_charges("SELL", 1, short["price"], option=True).total
        self.assertAlmostEqual(bp["max_loss"], round((0.5 - credit) * 100 + ch, 2), places=2)
        self.assertEqual(bp["quotes"], [])
        json.dumps(x)                                                     # state file must serialise

    def test_us_breaker_is_mirrored(self):
        book = UsBook(cash=5289.62, starting_capital=5289.62)
        book.breaker = dict(tripped_on="2026-11-16", until="2026-11-23", day_pnl=-200.0, limit=158.7)
        x = C._us_one(self.Market(), "XYZ", "2026-11-16", book.equity(), book)
        sts = x["expiries"][0]["structures"]
        self.assertTrue(all(s["verdict"] in ("FAIL", "n/a") for s in sts), [s["verdict"] for s in sts])
        self.assertTrue(any("circuit breaker" in s.get("why", "") for s in sts))

    def test_fill_check_uses_engine_open_price(self):
        """The fake market has no open_option_price: the check is skipped; a market whose engine price is NotYet
        for one leg turns the row FAIL."""
        book = UsBook(cash=5289.62, starting_capital=5289.62)
        from papertrade.derivbook import NotYet

        class M(self.Market):
            def open_option_price(self, p, day):
                if p.side == "LONG":
                    raise NotYet("no quote and no implied vol")
                return 1.0, "x", "real option chain (mid of bid/ask)", 0.3
        x = C._us_one(M(), "XYZ", "2026-11-16", book.equity(), book)
        bp = {s["name"]: s for s in x["expiries"][0]["structures"]}["Bull put spread"]
        self.assertEqual(bp["verdict"], "FAIL")
        self.assertIn("not filled", bp["why"])
        self.assertEqual(x["fill_check_fails"], 6 - sum(1 for s in x["expiries"][0]["structures"] if s["verdict"] == "n/a"))

    def test_journal_renders(self):
        book = UsBook(cash=5289.62, starting_capital=5289.62)
        x = C._us_one(self.Market(), "XYZ", "2026-11-16", book.equity(), book)
        c = dict(date="2026-11-16", fetched_at="now", equity=5289.62, cap=528.96, cash=5289.62, open_positions=0,
                 max_positions=5, breaker=None, notes=[], underlyings=[x], reference=[])
        text = "\n".join(C.us_lines(c))
        self.assertIn("| Bull put spread |", text)
        self.assertIn("credit $", text)
        self.assertIn("Event-trade size", text)


def us_premarket_market(book, traded=True, session_in_progress=None):
    """A real us.UsMarket on a fake Yahoo chain as it looks at the 17:33 IST run: bid = ask = 0 everywhere, a
    stray impliedVolatility of 0.0625, last trades of the 16 Nov session priced at 30% vol (or none)."""
    from papertrade.us import UsMarket
    S, t, E = 50.0, 32 / 365, "2026-12-18"
    px = pd.DataFrame([dict(date=pd.Timestamp(d), open=S, high=S, low=S, close=S, volume=1, symbol="XYZ")
                       for d in ("2026-11-13", "2026-11-16")])
    px.attrs["session_in_progress"] = session_in_progress
    m = UsMarket(px, book, online=True)
    when = pd.Timestamp("2026-11-16 20:00", tz="UTC") if traded else pd.Timestamp("2026-11-10 20:00", tz="UTC")

    def frame(kind):
        rows = []
        for k in [x / 2 for x in range(60, 141)]:
            last = bs_price(S, k, t, 0.04, 0.30, kind)
            rows.append(dict(strike=k, bid=0.0, ask=0.0, impliedVolatility=0.0625, lastPrice=round(last, 2),
                             lastTradeDate=when, contractSymbol=f"XYZ261218{kind[0]}{int(k * 1000):08d}"))
        return pd.DataFrame(rows)
    m._expiries["XYZ"] = [E]
    m._chains[("XYZ", E)] = {"CALL": frame("CALL"), "PUT": frame("PUT")}
    return m


class UsPreMarketFills(unittest.TestCase):
    """us.UsMarket.quote / open_option_price before the US open (item A)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.saved = {k: getattr(config, k) for k in ("STATE_DIR", "US_TRADES_FILE")}
        config.STATE_DIR = self.tmp
        config.US_TRADES_FILE = self.tmp / "us_trades.csv"

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(config, k, v)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def pos(self, kind, K, side="LONG"):
        return DPos(id="", underlying="XYZ", kind=kind, side=side, lots=1, lot_size=100, entry_price=0.0,
                    entry_date="2026-11-16", strike=K, expiry="2026-12-18")

    def test_stray_iv_never_prices_a_leg_at_zero(self):
        book = UsBook(cash=5289.62, starting_capital=5289.62)
        m = us_premarket_market(book)
        stray = bs_price(50.0, 45.0, 32 / 365, 0.04, 0.0625, "PUT")
        self.assertLess(stray, 0.005)                                   # what the old code filled at: 0.00
        px, how, label, iv = m.open_option_price(self.pos("PUT", 45.0), "2026-11-16")
        self.assertGreater(px, 0.05)
        self.assertAlmostEqual(iv, 0.30, places=2)                      # backed out of the last trades
        self.assertEqual(label, "model (BS on IV implied by last trades)")
        self.assertIn("MODEL, not a quote", how)

    def test_under_five_cents_waits(self):
        from papertrade.derivbook import NotYet
        m = us_premarket_market(UsBook(cash=5289.62, starting_capital=5289.62))
        with self.assertRaises(NotYet) as cm:
            m.open_option_price(self.pos("PUT", 32.0), "2026-11-16")
        self.assertIn("under $0.05", str(cm.exception))

    def test_no_last_trades_order_waits_mark_uses_stored_iv(self):
        from papertrade.derivbook import NotYet
        m = us_premarket_market(UsBook(cash=5289.62, starting_capital=5289.62), traded=False)
        with self.assertRaises(NotYet):
            m.open_option_price(self.pos("PUT", 48.0), "2026-11-16")
        held = self.pos("PUT", 48.0)
        held.iv = 0.25
        px, how, label, iv = m.quote(held, "2026-11-16")              # a mark: stored IV, as before
        self.assertIn("stored IV", how)
        self.assertGreater(px, 0)

    def test_session_in_progress_still_waits(self):
        from papertrade.derivbook import NotYet
        from papertrade.us import SESSION_WAIT
        m = us_premarket_market(UsBook(cash=5289.62, starting_capital=5289.62), session_in_progress="2026-11-17")
        o = dict(action="OPEN", underlying="XYZ", instrument="PUT", side="BUY", lots=1, strike=45,
                 expiry="2026-12-18", decision_date="2026-11-13")
        with self.assertRaises(NotYet) as cm:
            m.fill_day(o)
        self.assertEqual(str(cm.exception), SESSION_WAIT)

    def test_table_rows_fill_through_the_engine_at_the_table_prices(self):
        for name in ("Bull put spread", "Bear call spread", "Iron condor", "Bull call spread"):
            book = UsBook(cash=5289.62, starting_capital=5289.62)
            m = us_premarket_market(book)
            x = C._us_one(m, "XYZ", "2026-11-16", book.equity(), book)
            st = {s["name"]: s for s in x["expiries"][0]["structures"]}[name]
            with self.subTest(structure=name):
                self.assertEqual(st["verdict"], "PASS", st.get("why"))
                self.assertTrue(any("MODEL price" in q for q in st["quotes"]), st["quotes"])
                orders = [dict(action="OPEN", underlying="XYZ", instrument="CALL" if l["right"] == "CE" else "PUT",
                               side=l["side"], lots=1, strike=l["strike"], expiry="2026-12-18",
                               decision_date="2026-11-13", reason="t", risk_check="t") for l in st["legs"]]
                fills, notes, keep, ex = run_orders(book, orders, m, "2026-11-16")
                self.assertEqual([e["status"] for e in ex], ["filled"] * len(orders), notes)
                by = {(f["instrument"]): f["price"] for f in fills}
                for l in st["legs"]:
                    inst = f"XYZ 2026-12-18 {l['strike']:g} {'CALL' if l['right'] == 'CE' else 'PUT'}"
                    self.assertAlmostEqual(by[inst], l["price"], places=4)       # table price = fill price
                self.assertAlmostEqual(book.margin_used(), st["margin"], places=2)
                self.assertAlmostEqual(book.equity(), book.cash + sum(p.value(p.mark) for p in book.positions.values()))


class IndiaFillIv(unittest.TestCase):
    """fno.FnoMarket.fill_sigma: the mid-implied IV first (item B), and the table's fill est. = the fill."""
    E = "2026-11-23"

    def market(self):
        from papertrade.fno import FnoMarket
        px = pd.DataFrame([dict(date=pd.Timestamp(d), open=o, high=o, low=o, close=c, volume=1, symbol=sym)
                           for d, o, c, sym in (("2026-11-16", 61500.0, 61575.9, "^NSEBANK"),
                                                ("2026-11-17", 61559.95, 61600.0, "^NSEBANK"),
                                                ("2026-11-16", 15.0, 15.68, "^INDIAVIX"),
                                                ("2026-11-17", 15.5, 15.6, "^INDIAVIX"))])
        snap = {"symbol": "BANKNIFTY", "kind": "index", "date": "2026-11-16", "timestamp": "16-Nov-2026 15:40:00",
                "source": "NSE option-chain-v3", "underlying_value": 61575.9, "expiries": [self.E],
                "chains": {self.E: {"62700": {"CE": {"bid": 141.25, "ask": 144.7, "ltp": 140, "iv": 13.67, "oi": 1},
                                              "PE": None},
                                    "62900": {"CE": {"bid": 0.0, "ask": 114.15, "ltp": 110, "iv": 14.97, "oi": 1},
                                              "PE": None}}}}
        return FnoMarket(px, "2026-11-17", fresh_book(), prior={"BANKNIFTY": snap}, today={}, listed={"BANKNIFTY": [self.E]})

    def test_mid_implied_iv_first_then_listed(self):
        from papertrade.fno import spec
        m = self.market()
        iv, how = m.fill_sigma(spec("BANKNIFTY"), "2026-11-17", self.E, 62700.0, "CALL")
        want = C._implied_vol(C.chains.mid({"bid": 141.25, "ask": 144.7}), 61575.9, 62700.0, 7 / 365, config.CARRY_RATE, "CALL")
        self.assertAlmostEqual(iv, want, places=6)
        self.assertNotAlmostEqual(iv, 0.1367, places=3)
        self.assertIn("mid-implied", how)
        iv2, how2 = m.fill_sigma(spec("BANKNIFTY"), "2026-11-17", self.E, 62900.0, "CALL")   # one-sided
        self.assertAlmostEqual(iv2, 0.1497, places=6)
        self.assertIn("listed IV", how2)
        iv3, how3 = m.fill_sigma(spec("BANKNIFTY"), "2026-11-17", self.E, 63000.0, "CALL")   # not in the cache
        self.assertIn("ATM IV", how3)

    def test_open_fill_uses_it_and_fill_est_agrees(self):
        from papertrade.fno import spec
        m = self.market()
        o = dict(underlying="BANKNIFTY", instrument="CE", lots=1, strike=62700, expiry=self.E)
        pos, price, chg, label = m.open_contract(o, "2026-11-17", "SHORT")
        self.assertIn("mid-implied", pos.pricing)
        est = C._fno_fill_est(m, spec("BANKNIFTY"), "BANKNIFTY", self.E, 62700.0, "CALL", 61559.95, "2026-11-17")
        self.assertEqual(est["fill_est_sell"], price)          # same function, same spot: identical


class IndiaJournal(unittest.TestCase):
    def test_fno_lines_render_credit_rows(self):
        book = fresh_book()
        sts = list(build(book).values())
        x = dict(underlying="NIFTY", kind="index", lot=65, why=["index"], spot=25291.1, spot_date=DAY, chain=None,
                 warnings=[], expiries=[dict(label="monthly", expiry=EXP, days=7, atm=25300.0, atm_iv_pct=None,
                                             expected_move=400.0, rows=list(india_rows(india_quote_fn()).values()),
                                             structures=sts)])
        c = dict(date=DAY, equity=500000.0, cap=50000.0, cash=500000.0, open_positions=0, max_positions=5, breaker=None,
                 notes=[], underlyings=[x], sources=[])
        text = "\n".join(C.fno_lines(c))
        for want in ("| Iron condor |", "| Bull put spread |", "| Bear call spread |", "Margin blocked", "fits",
                     "BUY PE", "SELL PE", "Expected move (ATM straddle): 400.00"):
            self.assertIn(want, text)
        row = next(l for l in text.splitlines() if l.startswith("| Iron condor"))
        self.assertLess(row.index("BUY PE"), row.index("SELL PE"))      # covers printed first


if __name__ == "__main__":
    unittest.main()
