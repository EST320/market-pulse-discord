import unittest
from datetime import date, timedelta
from unittest import mock

from market_pulse import dip


def series(*moves, start=100.0):
    """Daily closes from a list of percent moves, with one date per close."""
    closes = [start]
    for move in moves:
        closes.append(closes[-1] * (1 + move / 100))
    dates = [date(2010, 1, 4) + timedelta(days=i) for i in range(len(closes))]
    return dates, closes


class SymbolTest(unittest.TestCase):
    def test_normalised_for_yahoo(self):
        self.assertEqual(dip.normalise_symbol(" googl "), "GOOGL")
        self.assertEqual(dip.normalise_symbol("brk.b"), "BRK-B")
        self.assertEqual(dip.normalise_symbol("BRK/B"), "BRK-B")

    def test_rejects_anything_else(self):
        for raw in ("", "GOOG L", "$GOOG", "A" * 11, "../x", "goog;rm"):
            with self.assertRaises(dip.StudyError, msg=raw):
                dip.normalise_symbol(raw)

    def test_threshold_parsing(self):
        self.assertIsNone(dip.parse_threshold(""))
        self.assertEqual(dip.parse_threshold("12.5"), 12.5)
        for raw in ("abc", "0.5", "60"):
            with self.assertRaises(dip.StudyError, msg=raw):
                dip.parse_threshold(raw)


class FindDropsTest(unittest.TestCase):
    def test_drops_and_forward_returns_from_the_close_of_the_drop_day(self):
        moves = [1, -10] + [0] * 19 + [10] + [0] * 40
        dates, closes = series(*moves)
        (event,) = dip.find_drops(dates, closes, 8)
        self.assertEqual(event["index"], 2)
        self.assertAlmostEqual(event["change"], -10.0)
        self.assertAlmostEqual(event["forward"][1], 0.0)
        self.assertAlmostEqual(event["forward"][20], 10.0)
        self.assertAlmostEqual(event["forward"][60], 10.0)

    def test_threshold_is_inclusive(self):
        dates, closes = series(-8.0, -7.99)
        self.assertEqual([e["index"] for e in dip.find_drops(dates, closes, 8)], [1])

    def test_recent_drops_have_no_later_returns(self):
        dates, closes = series(*([0] * 70 + [-9, 1, 2]))
        (event,) = dip.find_drops(dates, closes, 8)
        self.assertAlmostEqual(event["forward"][1], 1.0)
        self.assertIsNone(event["forward"][20])
        self.assertIsNone(event["forward"][60])


class ChooseThresholdTest(unittest.TestCase):
    def test_default_when_the_count_is_readable(self):
        dates, closes = series(*([-9, 9] * 6))
        threshold, events = dip.choose_threshold(dates, closes)
        self.assertEqual((threshold, len(events)), (8.0, 6))

    def test_kept_at_eight_for_volatile_stocks(self):
        # Raising it would leave only the oldest crashes; the tables show the recent ones instead.
        dates, closes = series(*([-9, 9] * 30 + [-13, 13] * 5))
        threshold, events = dip.choose_threshold(dates, closes)
        self.assertEqual((threshold, len(events)), (8.0, 35))

    def test_lowered_for_steady_stocks(self):
        dates, closes = series(*([-6, 6] * 5))
        threshold, events = dip.choose_threshold(dates, closes)
        self.assertEqual((threshold, len(events)), (6.0, 5))

    def test_lowered_no_further_than_five(self):
        dates, closes = series(*([-1, 1] * 5))
        threshold, events = dip.choose_threshold(dates, closes)
        self.assertEqual((threshold, events), (5.0, []))

    def test_an_explicit_threshold_is_kept(self):
        dates, closes = series(*([-9, 9] * 30))
        threshold, events = dip.choose_threshold(dates, closes, 8)
        self.assertEqual((threshold, len(events)), (8.0, 30))


class ClassifyTest(unittest.TestCase):
    DAY = date(2020, 1, 15)

    def classify(self, calendars, drop_day=DAY):
        dates = [drop_day - timedelta(days=1), drop_day]
        events = [{"index": 1, "date": drop_day}]
        with mock.patch.object(dip, "fetch_calendars", return_value=calendars) as fetch:
            ok = dip.classify(events, dates, "GOOGL")
        return ok, events[0].get("earnings"), fetch

    def rows(self, symbol, hour):
        return [{"symbol": symbol, "hour": hour}]

    def test_before_the_open_on_the_drop_day(self):
        _, earnings, _ = self.classify({self.DAY: self.rows("GOOGL", "bmo"), self.DAY - timedelta(days=1): []})
        self.assertTrue(earnings)

    def test_after_the_close_the_day_before(self):
        _, earnings, _ = self.classify({self.DAY: [], self.DAY - timedelta(days=1): self.rows("GOOGL", "amc")})
        self.assertTrue(earnings)

    def test_a_release_after_the_drop_does_not_count(self):
        _, earnings, _ = self.classify({self.DAY: self.rows("GOOGL", "amc"), self.DAY - timedelta(days=1): []})
        self.assertFalse(earnings)

    def test_unstated_time_counts_on_either_day(self):
        _, earnings, _ = self.classify({self.DAY: [], self.DAY - timedelta(days=1): self.rows("GOOGL", "")})
        self.assertTrue(earnings)

    def test_old_ticker_aliases_match(self):
        _, earnings, _ = self.classify({self.DAY: [], self.DAY - timedelta(days=1): self.rows("GOOG", "amc")})
        self.assertTrue(earnings)

    def test_before_the_calendar_starts_it_is_unknown_and_not_fetched(self):
        ok, earnings, fetch = self.classify({}, drop_day=date(2006, 1, 20))
        self.assertTrue(ok)
        self.assertIsNone(earnings)
        self.assertEqual(list(fetch.call_args.args[0]), [])

    def test_an_unreadable_calendar_leaves_everything_unclassified(self):
        ok, earnings, _ = self.classify({self.DAY: None, self.DAY - timedelta(days=1): []})
        self.assertFalse(ok)
        self.assertIsNone(earnings)


class SummaryTest(unittest.TestCase):
    def test_headline_numbers(self):
        moves = [-10] + [1] * 60 + [-9] + [-0.5] * 60 + [-12]
        dates, closes = series(*moves)
        events = dip.find_drops(dates, closes, 8)
        summary = dip.summarise(events, dates, closes)
        self.assertEqual((summary["count"], summary["finished"], summary["winners"]), (3, 2, 1))
        self.assertAlmostEqual(summary["win_rate"], 50.0)
        self.assertEqual([e["index"] for e in summary["losers"]], [62])
        # The latest drop is the last bar and the worst day on record.
        self.assertEqual(summary["latest_rank"], 1)
        self.assertEqual(summary["worst"]["index"], 123)

    def test_notes(self):
        moves = [-10] + [1] * 60 + [-9] + [-0.5] * 60
        dates, closes = series(*moves)
        events = dip.find_drops(dates, closes, 8)
        events[0]["earnings"] = None
        notes = dip.build_notes(dip.summarise(events, dates, closes), "XYZ", events)
        headlines = [h for h, _ in notes]
        self.assertIn("1 of 2 were lower 60 trading days later", headlines)
        self.assertTrue(any(h.startswith("Biggest drop: -10.00%") for h in headlines))
        self.assertTrue(notes[-1][1].startswith("* Before 2008"))

    def test_plural(self):
        self.assertEqual(dip.plural(1, "EVENT"), "1 EVENT")
        self.assertEqual(dip.plural(8, "EVENT"), "8 EVENTS")
        self.assertEqual(dip.plural(2, "earlier event"), "2 earlier events")


class MainTest(unittest.TestCase):
    def test_a_user_facing_problem_becomes_a_plain_reply(self):
        env = {dip.INTERACTION_TOKEN_ENV: "tok", dip.APPLICATION_ID_ENV: "app", "SYMBOL": "NOPE"}
        with mock.patch.dict("os.environ", env, clear=True), \
                mock.patch.object(dip, "DRY_RUN", False), \
                mock.patch.object(dip, "fetch_history", side_effect=dip.StudyError("找不到股票代码 NOPE。")), \
                mock.patch.object(dip, "edit_interaction_response") as edit, \
                mock.patch("builtins.print"):
            dip.main([])
        edit.assert_called_once_with("app", "tok", {"content": "找不到股票代码 NOPE。"}, file=None)


if __name__ == "__main__":
    unittest.main()
