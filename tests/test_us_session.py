"""US sleeve: a partial daily bar of a New York session still in progress is ignored (papertrade/us.py).

GitHub starts the scheduled run hours late, e.g. 01:28 IST on 11 Nov 2026 = 14:58 New York on 10 Nov, while
the US session is open; Yahoo then returns a partial bar for 10 Nov. These tests use synthetic frames; the
real-data check was run separately on a fresh Yahoo download.

Run:  python -m unittest discover -s tests
"""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import pandas as pd

from papertrade import config, us

NY = ZoneInfo("America/New_York")
IST = timezone(timedelta(hours=5, minutes=30))


def frame(dates, symbols=("SPY", "XLF")):
    rows = [dict(date=pd.Timestamp(d), open=1.0 + i, high=2.0 + i, low=0.5 + i, close=1.5 + i, volume=100, symbol=s)
            for s in symbols for i, d in enumerate(dates)]
    return pd.DataFrame(rows)


class UnfinishedDate(unittest.TestCase):
    def test_mid_session(self):
        self.assertEqual(us.us_unfinished_date(datetime(2026, 11, 10, 14, 0, tzinfo=NY)), "2026-11-10")

    def test_after_close_and_buffer(self):
        self.assertIsNone(us.us_unfinished_date(datetime(2026, 11, 10, 16, 30, tzinfo=NY)))

    def test_buffer_edges(self):
        self.assertEqual(us.us_unfinished_date(datetime(2026, 11, 10, 16, 19, tzinfo=NY)), "2026-11-10")
        self.assertIsNone(us.us_unfinished_date(datetime(2026, 11, 10, 16, 20, tzinfo=NY)))

    def test_pre_market(self):
        self.assertEqual(us.us_unfinished_date(datetime(2026, 11, 10, 7, 0, tzinfo=NY)), "2026-11-10")

    def test_ist_date_a_day_ahead(self):
        # 01:28 IST on 11 Nov = 19:58 UTC on 10 Nov = 14:58 New York on 10 Nov (EST, UTC-5)
        ist = datetime(2026, 11, 11, 1, 28, tzinfo=IST)
        self.assertEqual(us.us_unfinished_date(ist), "2026-11-10")
        self.assertEqual(us.us_unfinished_date(ist.astimezone(timezone.utc)), "2026-11-10")

    def test_17_33_ist_run_is_pre_market(self):
        # the intended schedule: 17:33 IST 11 Nov = 07:03 New York 11 Nov; no bar for 11 Nov exists yet
        self.assertEqual(us.us_unfinished_date(datetime(2026, 11, 11, 17, 33, tzinfo=IST)), "2026-11-11")


class DropUnfinished(unittest.TestCase):
    dates = ["2026-11-06", "2026-11-09", "2026-11-10"]

    def test_mid_session_drops_that_day(self):
        out = us.drop_unfinished_session(frame(self.dates), datetime(2026, 11, 10, 14, 58, tzinfo=NY))
        self.assertEqual(out["date"].max(), pd.Timestamp("2026-11-09"))
        self.assertEqual(len(out), 4)
        self.assertEqual(out.attrs["session_in_progress"], "2026-11-10")
        self.assertEqual(out.attrs["session_seen_at"], "14:58")

    def test_after_close_keeps_everything(self):
        out = us.drop_unfinished_session(frame(self.dates), datetime(2026, 11, 10, 16, 30, tzinfo=NY))
        self.assertEqual(len(out), 6)
        self.assertIsNone(out.attrs["session_in_progress"])

    def test_pre_market_without_a_bar_is_not_in_progress(self):
        out = us.drop_unfinished_session(frame(self.dates), datetime(2026, 11, 11, 7, 3, tzinfo=NY))
        self.assertEqual(len(out), 6)
        self.assertIsNone(out.attrs["session_in_progress"])

    def test_empty_frame(self):
        empty = pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "symbol"])
        out = us.drop_unfinished_session(empty, datetime(2026, 11, 10, 14, 58, tzinfo=NY))
        self.assertEqual(len(out), 0)
        self.assertIsNone(out.attrs["session_in_progress"])

    def test_market_latest(self):
        f = us.drop_unfinished_session(frame(self.dates), datetime(2026, 11, 10, 14, 58, tzinfo=NY))
        m = us.UsMarket(f, us.UsBook(cash=5000.0, starting_capital=5000.0), online=True)
        self.assertEqual(m.latest, "2026-11-09")
        self.assertEqual(m.session_in_progress, "2026-11-10")


class LoadPrices(unittest.TestCase):
    """load_prices with the download patched and the cache in a temp dir (the real cache is never touched)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.patches = [mock.patch.object(config, "CACHE_DIR", d),
                        mock.patch.object(config, "US_PRICES_FILE", d / "us_prices.parquet"),
                        mock.patch.object(us.data, "download_tickers",
                                          return_value=frame(["2026-11-06", "2026-11-09", "2026-11-10"]))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_online_mid_session(self):
        out = us.load_prices(["SPY", "XLF"], True, now=datetime(2026, 11, 10, 14, 58, tzinfo=NY))
        self.assertEqual(out["date"].max(), pd.Timestamp("2026-11-09"))
        self.assertEqual(out.attrs["session_in_progress"], "2026-11-10")
        self.assertEqual(out.attrs["dropped"], [])
        # the partial bar never reaches the cache
        cached = pd.read_parquet(config.US_PRICES_FILE)
        self.assertEqual(cached["date"].max(), pd.Timestamp("2026-11-09"))

    def test_offline_after_mid_session_run(self):
        us.load_prices(["SPY", "XLF"], True, now=datetime(2026, 11, 10, 14, 58, tzinfo=NY))
        # a later offline run after the close reads the cache: no in-progress flag carried over from it
        out = us.load_prices(["SPY", "XLF"], False, now=datetime(2026, 11, 10, 17, 0, tzinfo=NY))
        self.assertEqual(out["date"].max(), pd.Timestamp("2026-11-09"))
        self.assertIsNone(out.attrs["session_in_progress"])

    def test_online_after_close(self):
        out = us.load_prices(["SPY", "XLF", "QQQ"], True, now=datetime(2026, 11, 10, 16, 30, tzinfo=NY))
        self.assertEqual(out["date"].max(), pd.Timestamp("2026-11-10"))
        self.assertIsNone(out.attrs["session_in_progress"])
        self.assertEqual(out.attrs["dropped"], ["QQQ"])


class InProgressMarket(unittest.TestCase):
    """With a session in progress: option orders wait, stock orders fill at a completed session's open,
    option marks use the stored-vol fallback (no chain fetched)."""

    def setUp(self):
        f = us.drop_unfinished_session(frame(["2026-11-06", "2026-11-09", "2026-11-10"]),
                                       datetime(2026, 11, 10, 14, 58, tzinfo=NY))
        self.m = us.UsMarket(f, us.UsBook(cash=5000.0, starting_capital=5000.0), online=True)

    def test_option_order_waits(self):
        o = dict(action="OPEN", underlying="SPY", instrument="CALL", side="BUY", lots=1, strike="ATM",
                 expiry="2026-12-18", decision_date="2026-11-06")
        with self.assertRaises(us.NotYet) as cm:
            self.m.fill_day(o)
        self.assertIn("US session in progress at run time", str(cm.exception))

    def test_stock_order_fills_at_completed_open(self):
        o = dict(action="OPEN", underlying="SPY", instrument="STK", side="BUY", lots=1, decision_date="2026-11-06")
        self.assertEqual(self.m.fill_day(o), "2026-11-09")
        o["decision_date"] = "2026-11-09"
        self.assertIsNone(self.m.fill_day(o))       # the 10 Nov open is not used: that bar was dropped

    def test_option_mark_uses_fallback_without_chain(self):
        p = us.DPos(id="SPY:2026-12-18:C2:L", underlying="SPY", kind="CALL", side="LONG", lots=1, lot_size=100,
                    entry_price=1.0, entry_date="2026-11-06", strike=2.0, expiry="2026-12-18", iv=0.2)
        with mock.patch.object(self.m, "chain", side_effect=AssertionError("chain must not be fetched")):
            price, how, label, iv = self.m.quote(p, self.m.latest)
        self.assertIn("FALLBACK Black-Scholes", how)
        self.assertIn("US session of 2026-11-10 in progress at run time", how)
        self.assertIn("(2026-11-09)", how)


if __name__ == "__main__":
    unittest.main(verbosity=2)
