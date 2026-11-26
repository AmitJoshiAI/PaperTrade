"""'Today in brief' sentences on the dashboard (papertrade/brief.py).

Small hand-made dashboard dicts in the shape export.build_dashboard_data produces (cleaned). The real-data
check was run separately on a full export in a scratch copy of the repo.

Run:  python -m unittest discover -s tests
"""
import unittest

import pandas as pd

from papertrade import brief

AS_OF = "2026-11-26"
PREV = "2026-11-25"


def combined(prev: dict, last: dict, cap=2_000_000.0):
    rows = []
    for d, r in ((PREV, prev), (AS_OF, last)):
        row = dict(date=d, **r)
        row["equity"] = sum(r.values())
        rows.append(row)
    return dict(capital=cap, equity=rows[-1]["equity"], equity_curve=rows)


def base(prev=None, last=None):
    prev = prev or dict(momentum=500_000.0, thesis=500_000.0, fno=500_000.0, us=500_000.0)
    last = last or dict(prev)
    return dict(as_of=AS_OF, price_date=AS_OF, combined=combined(prev, last),
                portfolio=dict(positions=[]), trades=[], pending=dict(decision_date=AS_OF, orders=[]),
                thesis=dict(theses=[], second_order=[], closed=[], orders=[], trades=[]),
                fno=dict(positions=[], orders=[], trades=[]),
                us=dict(positions=[], orders=[], trades=[], equity_curve=[dict(date=PREV)],
                        day_pnl_inr=0.0, day_fx_pnl_inr=0.0))


def prices(closes: dict):
    """closes: {symbol: (previous close, last close)}"""
    rows = []
    for s, (a, b) in closes.items():
        rows += [dict(date=pd.Timestamp(PREV), symbol=s, close=a), dict(date=pd.Timestamp(AS_OF), symbol=s, close=b)]
    return pd.DataFrame(rows)


IRON_CONDOR = [("PE", "BUY", 24250), ("CE", "BUY", 26250), ("PE", "SELL", 24350), ("CE", "SELL", 26150)]


class Rupees(unittest.TestCase):
    def test_indian_grouping(self):
        self.assertEqual(brief.inr(2016495), "Rs 20,16,495")
        self.assertEqual(brief.inr(2000000), "Rs 20,00,000")
        self.assertEqual(brief.inr(-7134.6), "Rs 7,135")
        self.assertEqual(brief.inr(999), "Rs 999")
        self.assertEqual(brief.inr(12345678), "Rs 1,23,45,678")


class LosingDayWithMovers(unittest.TestCase):
    def setUp(self):
        d = base(last=dict(momentum=493_000.0, thesis=500_000.0, fno=499_700.0, us=500_000.0))
        d["portfolio"]["positions"] = [
            dict(symbol="KOTAKBANK", qty=200, entry_date="2026-10-16"),     # -20 x 200 = -4,000
            dict(symbol="DRREDDY", qty=100, entry_date="2026-10-16"),       # -25 x 100 = -2,500
            dict(symbol="ETERNAL", qty=100, entry_date="2026-10-16"),       # -5 x 100 = -500
        ]
        self.d = d
        self.p = prices({"KOTAKBANK": (500, 480), "DRREDDY": (1400, 1375), "ETERNAL": (370, 365), "TCS": (1, 1)})
        self.lines = brief.build(d, self.p)["lines"]

    def test_result(self):
        self.assertEqual(self.lines[0], "All four sleeves together lost Rs 7,300 today; "
                                        "the total is now Rs 19,92,700 on Rs 20,00,000.")

    def test_why_names_movers_in_order(self):
        why = self.lines[1]
        self.assertTrue(why.startswith("Momentum lost Rs 7,000: biggest moves KOTAKBANK down Rs 4,000, "
                                       "DRREDDY down Rs 2,500, ETERNAL down Rs 500"), why)
        # F&O moved Rs 300 = 4% of all moves: below both thresholds, not mentioned; thesis and US did not move
        self.assertNotIn("F&O", why)
        self.assertNotIn("Thesis", why)
        self.assertNotIn("US", why)

    def test_no_em_dash(self):
        self.assertFalse(any("—" in l for l in self.lines))

    def test_position_bought_today_uses_fill(self):
        self.d["portfolio"]["positions"].append(dict(symbol="TCS", qty=10, entry_date=AS_OF))
        self.d["trades"] = [dict(date=AS_OF, symbol="TCS", side="BUY", qty=10, price=3000.0)]
        p = prices({"KOTAKBANK": (500, 480), "DRREDDY": (1400, 1375), "TCS": (2900, 2950)})
        moves = brief.holding_moves([("TCS", 10, AS_OF)], self.d["trades"], AS_OF, *brief._closes(p, AS_OF))
        self.assertEqual(moves, [("TCS", -500.0, "bought today")])     # from the 3,000 fill, not the 2,900 close


class DayWithFills(unittest.TestCase):
    def setUp(self):
        d = base()
        d["trades"] = [dict(date=AS_OF, symbol="ICICIBANK", side="BUY", qty=56, price=1602.8),
                       dict(date=AS_OF, symbol="TECHM", side="SELL", qty=53, price=1718.9),
                       dict(date=PREV, symbol="OLD", side="BUY", qty=1, price=1.0)]
        d["fno"]["trades"] = [dict(date=AS_OF, underlying="NIFTY", instrument=f"NIFTY 2026-12-29 {k} {kind}",
                                   action="OPEN", side=side, position=f"NIFTY:2026-12-29:{kind}{k}")
                              for kind, side, k in IRON_CONDOR]
        d["us"]["trades"] = [dict(date=PREV, underlying="EEM", instrument="EEM 2026-12-18 48 PUT", action="OPEN",
                                  side="BUY")]
        self.line = brief.build(d, None)["lines"][-2]

    def test_trades(self):
        self.assertEqual(self.line,
                         "Today: Momentum bought ICICIBANK (56 shares) and sold TECHM (53 shares); "
                         "India F&O opened 4 option legs on NIFTY 29 Dec (iron condor). "
                         "In the US session of 25 Nov, US opened 1 option leg on EEM 18 Dec.")

    def test_no_leg_prices(self):
        self.assertNotIn("24250", self.line)
        self.assertNotIn("OLD", self.line)


class QueuedOrders(unittest.TestCase):
    def setUp(self):
        d = base()
        d["pending"]["orders"] = [dict(symbol="TECHM", side="SELL", qty=53), dict(symbol="ICICIBANK", side="BUY", qty=None)]
        d["thesis"]["theses"] = [dict(slug="bajaj-rbi", symbol="BAJFINANCE", qty=0)]
        d["thesis"]["orders"] = [dict(thesis="bajaj-rbi", side="BUY", amount=75000, decision_date=AS_OF),
                                 dict(thesis="bajaj-rbi", side="SELL", decision_date="2026-12-10")]
        d["fno"]["orders"] = [dict(action="OPEN", underlying="NIFTY", instrument=kind, side=side, lots=1, strike=k,
                                   expiry="2026-12-29", decision_date=AS_OF) for kind, side, k in IRON_CONDOR]
        d["us"]["orders"] = [dict(action="CLOSE", position="EEM:2026-12-18:P48:L", side="SELL", decision_date=AS_OF)]
        self.line = brief.build(d, None)["lines"][-1]

    def test_plan(self):
        self.assertEqual(self.line,
                         "Planned for the next session: Momentum will sell TECHM and buy ICICIBANK; "
                         "Thesis will buy BAJFINANCE; India F&O will open 4 option legs on NIFTY 29 Dec (iron condor); "
                         "US will close 1 option leg on EEM 18 Dec in the next US session. "
                         "1 more order waits for a later date.")


class AllQuiet(unittest.TestCase):
    def test_quiet(self):
        lines = brief.build(base(), prices({"KOTAKBANK": (500, 500)}))["lines"]
        self.assertEqual(lines, [
            "All four sleeves together did not change today; the total is now Rs 20,00,000 on Rs 20,00,000.",
            "No trades today.",
            "Nothing is planned for the next session: hold.",
        ])

    def test_first_day(self):
        d = base()
        d["combined"]["equity_curve"] = d["combined"]["equity_curve"][-1:]
        lines = brief.build(d, None)["lines"]
        self.assertEqual(lines[0], "This is the first recorded day, so there is no day change yet; "
                                   "the total is Rs 20,00,000 on Rs 20,00,000.")


class PricesMissing(unittest.TestCase):
    def test_other_lines_still_written(self):
        d = base(last=dict(momentum=493_000.0, thesis=500_000.0, fno=499_000.0, us=500_000.0))
        d["portfolio"]["positions"] = [dict(symbol="KOTAKBANK", qty=200, entry_date="2026-10-16")]
        d["fno"]["positions"] = [dict(id=f"NIFTY:2026-12-29:{kind}{k}:L", kind="PUT" if kind == "PE" else "CALL",
                                      expiry="2026-12-29") for kind, _s, k in IRON_CONDOR]
        d["pending"]["orders"] = [dict(symbol="TECHM", side="SELL", qty=53)]
        lines = brief.build(d, None)["lines"]
        self.assertEqual(len(lines), 4)
        self.assertEqual(lines[1], "Momentum lost Rs 7,000; India F&O lost Rs 1,000 on its 4 option legs on NIFTY 29 Dec.")
        self.assertNotIn("KOTAKBANK", lines[1])
        self.assertEqual(lines[3], "Planned for the next session: Momentum will sell TECHM.")

    def test_stale_prices_treated_as_missing(self):
        d = base(last=dict(momentum=493_000.0, thesis=500_000.0, fno=500_000.0, us=500_000.0))
        d["portfolio"]["positions"] = [dict(symbol="KOTAKBANK", qty=200, entry_date="2026-10-16")]
        p = prices({"KOTAKBANK": (500, 480)})
        p["date"] = p["date"] - pd.Timedelta(days=7)
        self.assertEqual(brief.build(d, p)["lines"][1], "Momentum lost Rs 7,000.")


class UsSession(unittest.TestCase):
    def test_us_session_named_when_earlier_than_india_day(self):
        # the India run of 26 Nov first sees the US session of 25 Nov; export._combined counts it on 26 Nov
        d = base(last=dict(momentum=496_000.0, thesis=500_000.0, fno=500_000.0, us=497_234.0))
        d["us"].update(day_pnl_inr=-2766.0, day_fx_pnl_inr=-575.0, equity_curve=[dict(date=PREV)])
        why = brief.build(d, None)["lines"][1]
        self.assertEqual(why, "Momentum lost Rs 4,000; US lost Rs 2,766 in its 25 Nov session "
                              "(Rs 575 of it from the dollar-rupee rate).")

    def test_us_holiday_does_not_repeat(self):
        # the latest US row is two India rows back (US holiday): the previous run already showed it
        d = base(last=dict(momentum=496_000.0, thesis=500_000.0, fno=500_000.0, us=500_000.0))
        d["us"].update(day_pnl_inr=-2766.0, day_fx_pnl_inr=-575.0, equity_curve=[dict(date="2026-11-24")],
                       trades=[dict(date="2026-11-24", underlying="EEM", instrument="EEM 2026-12-18 48 PUT",
                                    action="OPEN", side="BUY")])
        lines = brief.build(d, None)["lines"]
        self.assertEqual(lines[1], "Momentum lost Rs 4,000.")
        self.assertEqual(lines[2], "No trades today.")

    def test_us_move_mostly_rupee(self):
        d = base(last=dict(momentum=500_000.0, thesis=500_000.0, fno=500_000.0, us=498_000.0))
        d["us"].update(day_pnl_inr=-2000.0, day_fx_pnl_inr=-1900.0, equity_curve=[dict(date=AS_OF)])
        why = brief.build(d, None)["lines"][1]
        self.assertEqual(why, "US lost Rs 2,000, almost all of it from the dollar-rupee rate, not the trades.")


class CombinedDating(unittest.TestCase):
    """export._combined: a US session is counted on the India day whose run first saw it."""

    def test_us_session_counts_on_the_next_india_day(self):
        from papertrade import config
        from papertrade.export import _combined
        mom = pd.DataFrame([dict(date=d, equity=500_000.0) for d in ("2026-11-23", "2026-11-25", "2026-11-26", "2026-11-27")])
        us_rows = [dict(date="2026-11-20", equity_inr=493_000.0), dict(date="2026-11-24", equity_inr=491_000.0),
                   dict(date="2026-11-25", equity_inr=489_000.0)]            # 26 Nov: US holiday, no row
        out = dict(thesis=dict(equity=500_000.0, equity_curve=[]), fno=dict(equity_inr=500_000.0, equity_curve=[]),
                   us=dict(equity_inr=489_000.0, equity_curve=us_rows))
        us = {r["date"]: r["us"] for r in _combined(mom, out)["equity_curve"]}
        self.assertEqual(us, {"2026-11-23": 493_000.0,     # run of 23 Nov saw the 20 Nov session
                              "2026-11-25": 491_000.0,     # run of 25 Nov saw the 24 Nov session
                              "2026-11-26": 489_000.0,     # run of 26 Nov saw the 25 Nov session: Day -2,000
                              "2026-11-27": 489_000.0})    # US holiday on 26 Nov: Day 0
        self.assertEqual(config.US_CAPITAL, 500_000)


if __name__ == "__main__":
    unittest.main()
