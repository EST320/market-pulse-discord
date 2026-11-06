import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from market_pulse import wallstreetcn as w

US = w.CHANNELS["us"]


def raw(news_id, age_seconds=60, title="t", content="c"):
    return {
        "id": news_id,
        "title": title,
        "content_text": content,
        "display_time": time.time() - age_seconds,
    }


def pages(*page_list):
    """Fake fetch_page serving the given pages; the cursor is the page index."""
    def fake(channel, cursor=0):
        items = page_list[cursor] if cursor < len(page_list) else []
        return items, cursor + 1
    return fake


class ItemToNewsTest(unittest.TestCase):
    def test_missing_id_is_dropped(self):
        self.assertIsNone(w.item_to_news({"title": "x"}))

    def test_title_from_bracketed_headline(self):
        news = w.item_to_news({"id": 1, "content_text": "【Headline】 body text"})
        self.assertEqual(news["title"], "Headline")
        self.assertEqual(news["content"], "body text")

    def test_title_from_first_line(self):
        news = w.item_to_news({"id": 1, "content_text": "first line\nrest"})
        self.assertEqual(news["title"], "first line")
        self.assertEqual(news["content"], "rest")

    def test_fallback_title_and_app_signature_stripped(self):
        news = w.item_to_news({"id": 1, "content_text": "（来自华尔街见闻APP）"})
        self.assertEqual(news["title"], w.FALLBACK_TITLE)
        self.assertEqual(news["content"], "")

    def test_missing_timestamp(self):
        news = w.item_to_news({"id": 1, "title": "x", "display_time": "bad"})
        self.assertIsNone(news["display_ts"])
        self.assertIsNone(news["timestamp"])


class StateTest(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "seen.json"

    def test_missing_file_is_empty(self):
        self.assertEqual(w.load_seen(self.path), {})

    def test_corrupt_file_is_empty(self):
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(w.load_seen(self.path), {})

    def test_save_prunes_expired_entries(self):
        now = time.time()
        w.save_seen(self.path, {"old": now - w.RETENTION_SECONDS - 10, "new": now})
        self.assertEqual(list(w.load_seen(self.path)), ["new"])


class CollectNewItemsTest(unittest.TestCase):
    def collect(self, seen_ids, *page_list):
        with mock.patch.object(w, "fetch_page", pages(*page_list)):
            return w.collect_new_items(US, seen_ids)

    def test_returns_unseen_oldest_first(self):
        result = self.collect(set(), [raw(3, 10), raw(2, 20), raw(1, 30)])
        self.assertEqual([n["id"] for n in result], ["1", "2", "3"])

    def test_skips_seen_stale_and_untimed(self):
        untimed = {"id": 9, "title": "x"}
        stale = raw(5, w.MAX_NEWS_AGE_SECONDS + 60)
        result = self.collect({"2"}, [raw(3, 10), untimed, raw(2, 20), stale])
        self.assertEqual([n["id"] for n in result], ["3"])

    def test_an_item_that_surfaces_late_is_still_collected(self):
        # Displayed 40 minutes ago, first seen now: the old 15-minute window dropped it.
        result = self.collect({"2"}, [raw(3, 10), raw(2, 20), raw(1, 40 * 60)])
        self.assertEqual([n["id"] for n in result], ["1", "3"])

    def test_keeps_paging_past_seen_items_until_the_window_edge(self):
        inside, outside = w.MAX_NEWS_AGE_SECONDS - 60, w.MAX_NEWS_AGE_SECONDS + 60
        fake = mock.Mock(side_effect=pages(
            [raw(3, 10), raw(2, 20)],
            [raw(1, inside), raw(0, outside)],
            [raw(-1, outside + 60)],
        ))
        with mock.patch.object(w, "fetch_page", fake):
            result = w.collect_new_items(US, {"2"})
        self.assertEqual([n["id"] for n in result], ["1", "3"])
        self.assertEqual(fake.call_count, 2)

    def test_the_window_stays_well_inside_the_retention(self):
        # Otherwise an item already sent could drop out of the state and be sent again.
        self.assertGreaterEqual(w.RETENTION_SECONDS, 2 * w.MAX_NEWS_AGE_SECONDS)

    def test_stops_paging_past_age_window(self):
        stale = raw(1, w.MAX_NEWS_AGE_SECONDS + 60)
        fake = mock.Mock(side_effect=pages([raw(2, 10), stale], [raw(0, 9999)]))
        with mock.patch.object(w, "fetch_page", fake):
            w.collect_new_items(US, set())
        self.assertEqual(fake.call_count, 1)


class RunTest(unittest.TestCase):
    def setUp(self):
        state_file = Path(tempfile.mkdtemp()) / "seen.json"
        self.channel = w.Channel(**{**US.__dict__, "state_file": state_file})
        patches = [
            mock.patch.dict("os.environ", {US.webhook_env: "https://example.invalid/hook"}),
            mock.patch.object(w.time, "sleep"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def run_with(self, items, post):
        with mock.patch.object(w, "fetch_page", pages(items)), \
                mock.patch.object(w, "post_to_discord", post):
            w.run(self.channel)

    def seed(self, *ids):
        self.channel.state_file.write_text(
            json.dumps({"seen": {i: time.time() for i in ids}}), encoding="utf-8"
        )

    def test_first_run_sends_latest_few_and_marks_all_seen(self):
        items = [raw(i, 100 - i) for i in range(20, 0, -1)]
        post = mock.Mock()
        self.run_with(items, post)
        self.assertEqual(post.call_count, w.FIRST_RUN_SEND)
        self.assertEqual(len(w.load_seen(self.channel.state_file)), 20)

    def test_failure_midway_keeps_delivered_items_only(self):
        self.seed("0")
        post = mock.Mock(side_effect=[None, RuntimeError("boom")])
        with self.assertRaises(RuntimeError):
            self.run_with([raw(2, 10), raw(1, 20), raw(0, 30)], post)
        self.assertEqual(set(w.load_seen(self.channel.state_file)), {"0", "1"})

    def test_fetch_failure_is_swallowed(self):
        def boom(channel, cursor=0):
            raise RuntimeError("api down")
        with mock.patch.object(w, "fetch_page", boom):
            w.run(self.channel)
        self.assertFalse(self.channel.state_file.exists())


if __name__ == "__main__":
    unittest.main()
