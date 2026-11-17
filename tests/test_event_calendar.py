"""strategy_reviews/events.json: the event calendar merged into the F&O candidates' events (candidates.fno_events)."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from papertrade import candidates as C

DAY = "2026-11-17"


def _write(d: Path, payload) -> Path:
    p = d / "events.json"
    p.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    return p


RBI = {"slug": "rbi-mpc-december", "date": "2026-12-04", "sleeves": ["fno"], "symbols": [], "event": "RBI MPC decision"}
FOMC = {"slug": "fomc-december", "date": "2026-12-09", "sleeves": ["us"], "symbols": [], "event": "FOMC decision"}


class EventCalendarTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_merge_with_thesis_events(self):
        p = _write(self.d, {"events": [RBI, FOMC]})
        thesis = [dict(slug="some-thesis", date="2026-12-20", symbols=["BAJFINANCE"], origin="thesis")]
        with mock.patch.object(C, "fno_event_dates", return_value=list(thesis)):
            evs = C.fno_events(DAY, p)
        self.assertEqual([(e["slug"], e["date"], e["origin"]) for e in evs],
                         [("rbi-mpc-december", "2026-12-04", "calendar"), ("some-thesis", "2026-12-20", "thesis")])
        # the US event is not an F&O event; an index gets the calendar event, a stock only its own
        self.assertEqual([e["slug"] for e in C.fno_events_for("NIFTY", evs)], ["rbi-mpc-december", "some-thesis"])
        self.assertEqual([e["slug"] for e in C.fno_events_for("BAJFINANCE", evs)], ["some-thesis"])
        self.assertEqual([e["slug"] for e in C.calendar_events("us", DAY, p)], ["fomc-december"])
        self.assertEqual(C._event_name(evs[0]), "rbi-mpc-december 2026-12-04 (calendar)")
        self.assertEqual(C._event_name(evs[1]), "some-thesis 2026-12-20")

    def test_deduplicated_by_slug_and_date(self):
        p = _write(self.d, {"events": [RBI, dict(RBI)]})
        evs, warns = C.load_event_calendar(p)
        self.assertEqual(len(evs), 1)
        self.assertTrue(any("duplicate" in w for w in warns))
        thesis = [dict(slug="rbi-mpc-december", date="2026-12-04", symbols=[], origin="thesis")]
        with mock.patch.object(C, "fno_event_dates", return_value=list(thesis)):
            merged = C.fno_events(DAY, p)
        self.assertEqual([(e["slug"], e["origin"]) for e in merged], [("rbi-mpc-december", "thesis")])

    def test_past_events_dropped(self):
        past = dict(RBI, slug="rbi-mpc-october", date="2026-10-01")
        today = dict(RBI, slug="rbi-today", date=DAY)      # not after the market date: dropped too
        p = _write(self.d, {"events": [past, today, RBI]})
        with mock.patch.object(C, "fno_event_dates", return_value=[]):
            self.assertEqual([e["slug"] for e in C.fno_events(DAY, p)], ["rbi-mpc-december"])

    def test_malformed_file_ignored(self):
        for payload in ("{not json", json.dumps([RBI]), json.dumps({"events": "x"})):
            p = _write(self.d, payload)
            evs, warns = C.load_event_calendar(p)
            self.assertEqual(evs, [])
            self.assertEqual(len(warns), 1, payload)
            with mock.patch.object(C, "fno_event_dates", return_value=[]):
                self.assertEqual(C.fno_events(DAY, p), [])

    def test_bad_entry_skipped_with_warning(self):
        bad = [dict(RBI, date="4 Dec 2026"), dict(RBI, slug=""), dict(RBI, sleeves=["india"]), "x",
               dict(RBI, symbols="NIFTY"), dict(RBI, date=None)]
        p = _write(self.d, {"events": bad + [RBI]})
        evs, warns = C.load_event_calendar(p)
        self.assertEqual([e["slug"] for e in evs], ["rbi-mpc-december"])
        self.assertEqual(len(warns), len(bad))
        self.assertTrue(all("skipped" in w for w in warns))
        self.assertEqual(C.calendar_warnings(p), warns)

    def test_missing_file_ignored(self):
        p = self.d / "nope.json"
        self.assertEqual(C.load_event_calendar(p), ([], []))
        with mock.patch.object(C, "fno_event_dates", return_value=[]):
            self.assertEqual(C.fno_events(DAY, p), [])

    def test_header_lines(self):
        cal = [dict(slug="rbi-mpc-december", date="2026-12-04", event="RBI MPC decision", holds=[
            dict(underlying="NIFTY", label="event: rbi-mpc-december 2026-12-04 (calendar)", expiry="2026-12-29"),
            dict(underlying="BANKNIFTY", label="event: rbi-mpc-december 2026-12-04 (calendar)", expiry="2026-12-29")])]
        line = C._fno_calendar_line(cal)
        self.assertIn('NIFTY, BANKNIFTY -> "Event: rbi-mpc-december 2026-12-04 (calendar) expiry 2026-12-29"', line)
        us = [dict(slug="fomc-december", date="2026-12-09", event="FOMC decision",
                   held={"2026-12-18": ["AAPL", "F"]}, short={"2026-12-04": ["XYZ"]})]
        line = C._us_calendar_line(us)
        self.assertIn("held through by the tabled expiry 2026-12-18 (2 tickers)", line)
        self.assertIn("NOT held: expiry 2026-12-04 ends before it (XYZ)", line)
        self.assertIsNone(C._fno_calendar_line(None))


if __name__ == "__main__":
    unittest.main()
