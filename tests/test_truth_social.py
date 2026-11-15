import io
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from market_pulse import truth_social as ts


def raw(post_id, content="hello", age_seconds=60, **extra):
    created = datetime.fromtimestamp(time.time() - age_seconds, tz=timezone.utc)
    return {"id": post_id, "content": content, "created_at": created.isoformat(), **extra}


def empty_state():
    return {"seen": {}, "hashes": {}, "etag": None}


class ParsingTest(unittest.TestCase):
    def test_parse_timestamp_variants(self):
        ts_value, iso = ts.parse_timestamp("2026-10-05T16:32:00Z")
        self.assertEqual(iso, "2026-10-05T16:32:00+00:00")
        self.assertEqual(ts.parse_timestamp(ts_value)[1], iso)
        # Naive timestamps are taken as UTC.
        self.assertEqual(ts.parse_timestamp("2026-10-05T16:32:00")[0], ts_value)
        self.assertEqual(ts.parse_timestamp("not a date"), (None, None))
        self.assertEqual(ts.parse_timestamp(None), (None, None))

    def test_clean_html_content(self):
        cleaned = ts.clean_html_content("<p>Tom &amp; Jerry<br/>second   line</p><p>next</p>")
        self.assertEqual(cleaned, "Tom & Jerry\nsecond line\nnext")

    def test_media_type_detection(self):
        self.assertEqual(ts.get_first_media({"media": ["https://x/a.JPG?s=1"]})["type"], "image")
        video = ts.get_first_media({"media": [{"url": "https://x/a.mp4", "preview_url": "https://x/p.jpg"}]})
        self.assertEqual((video["type"], video["preview_url"]), ("video", "https://x/p.jpg"))
        self.assertIsNone(ts.get_first_media({"media": []}))

    def test_content_hash_ignores_case_and_whitespace(self):
        self.assertEqual(
            ts.make_content_hash("Hello   World\n", None),
            ts.make_content_hash("hello world", None),
        )
        self.assertNotEqual(
            ts.make_content_hash("hello", None),
            ts.make_content_hash("hello", {"url": "https://x/a.jpg", "preview_url": None}),
        )

    def test_item_without_id_or_content_is_dropped(self):
        self.assertIsNone(ts.item_to_post({"content": "x"}))
        self.assertIsNone(ts.item_to_post({"id": 1, "content": "<p> </p>"}))

    def test_post_url_falls_back_to_profile_link(self):
        post = ts.item_to_post(raw(42))
        self.assertEqual(post["url"], "https://truthsocial.com/@realDonaldTrump/42")


class CollectNewPostsTest(unittest.TestCase):
    def collect(self, raw_posts, state=None):
        self.log = io.StringIO()
        with redirect_stdout(self.log):
            return ts.collect_new_posts(raw_posts, state or empty_state())

    def ids(self, *args, **kwargs):
        return [p["id"] for p in self.collect(*args, **kwargs)]

    def test_returns_recent_posts_oldest_first(self):
        self.assertEqual(self.ids([raw(2, "b", 10), raw(1, "a", 20)]), ["1", "2"])

    def test_skips_seen_id(self):
        state = {"seen": {"1": time.time()}, "hashes": {}}
        self.assertEqual(self.ids([raw(1, "a"), raw(2, "b")], state), ["2"])

    def test_skips_repost_of_same_content_under_new_id(self):
        first = ts.item_to_post(raw(1, "same text"))
        state = {"seen": {}, "hashes": {first["content_hash"]: time.time()}}
        self.assertEqual(self.ids([raw(2, "Same   TEXT"), raw(3, "other")], state), ["3"])
        self.assertIn("duplicate content: 2", self.log.getvalue())

    def test_old_posts_are_dropped_silently(self):
        first = ts.item_to_post(raw(1, "same text"))
        state = {"seen": {}, "hashes": {first["content_hash"]: time.time()}}
        old = raw(2, "same text", ts.MAX_POST_AGE_SECONDS + 60)
        self.assertEqual(self.ids([old], state), [])
        self.assertEqual(self.log.getvalue(), "")

    def test_skips_unparseable_and_future_timestamps(self):
        untimed = {"id": 1, "content": "a", "created_at": "garbage"}
        future = raw(2, "b", -3600)
        self.assertEqual(self.ids([untimed, future, raw(3, "c")]), ["3"])


class StateTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(ts, "STATE_FILE", Path(tempfile.mkdtemp()) / "seen_trump.json")
        self.state_file = patcher.start()
        self.addCleanup(patcher.stop)

    def test_missing_or_corrupt_file_is_empty(self):
        self.assertEqual(ts.load_state(), empty_state())
        self.state_file.write_text("{broken", encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(ts.load_state(), empty_state())

    def test_wrong_shapes_are_reset(self):
        self.state_file.write_text('{"seen": [], "hashes": "x", "etag": 5}', encoding="utf-8")
        self.assertEqual(ts.load_state(), empty_state())

    def test_save_prunes_expired_entries(self):
        now = time.time()
        expired = now - ts.RETENTION_SECONDS - 10
        ts.save_state({"seen": {"old": expired, "new": now}, "hashes": {"h_old": expired, "h_new": now}})
        state = ts.load_state()
        self.assertEqual((list(state["seen"]), list(state["hashes"])), (["new"], ["h_new"]))

    def test_etag_round_trips(self):
        ts.save_state({"seen": {}, "hashes": {}, "etag": '"abc-3"'})
        self.assertEqual(ts.load_state()["etag"], '"abc-3"')


class FakeResponse:
    def __init__(self, status_code, payload=None, etag=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = {"ETag": etag} if etag else {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"status {self.status_code}")


class FetchPostsTest(unittest.TestCase):
    def fetch(self, response, etag=None):
        with mock.patch.object(ts.requests, "get", return_value=response) as get:
            result = ts.fetch_posts(etag)
        return result, get.call_args.kwargs["headers"]

    def test_first_fetch_sends_no_condition_and_returns_new_etag(self):
        result, headers = self.fetch(FakeResponse(200, [{"id": 1}], '"v1"'))
        self.assertEqual(result, ([{"id": 1}], '"v1"'))
        self.assertNotIn("If-None-Match", headers)

    def test_known_etag_is_sent_and_304_returns_no_posts(self):
        result, headers = self.fetch(FakeResponse(304), '"v1"')
        self.assertEqual(result, (None, '"v1"'))
        self.assertEqual(headers["If-None-Match"], '"v1"')

    def test_posts_wrapped_in_object(self):
        result, _ = self.fetch(FakeResponse(200, {"posts": [{"id": 1}]}, '"v2"'))
        self.assertEqual(result, ([{"id": 1}], '"v2"'))


class MainEtagTest(unittest.TestCase):
    """The ETag must only be remembered once the archive version is fully handled."""

    def setUp(self):
        self.state_file = Path(tempfile.mkdtemp()) / "seen_trump.json"
        self.posted = mock.Mock()
        patches = [
            mock.patch.object(ts, "STATE_FILE", self.state_file),
            mock.patch.object(ts, "post_to_discord", self.posted),
            mock.patch.object(ts.time, "sleep"),
            mock.patch.dict("os.environ", {ts.WEBHOOK_ENV: "hook"}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        # Start past the first-run baseline, with an older archive version on record.
        ts.save_state({"seen": {"0": time.time()}, "hashes": {}, "etag": '"old"'})

    def run_main(self, posts, etag='"new"'):
        with mock.patch.object(ts, "fetch_posts", return_value=(posts, etag)) as fetch, \
                redirect_stdout(io.StringIO()):
            try:
                ts.main()
            finally:
                self.fetched_with = fetch.call_args.args
        return ts.load_state()

    def test_unchanged_archive_sends_nothing_and_keeps_state(self):
        before = self.state_file.read_text(encoding="utf-8")
        state = self.run_main(None, '"old"')
        self.assertEqual(self.fetched_with, ('"old"',))
        self.posted.assert_not_called()
        self.assertEqual(self.state_file.read_text(encoding="utf-8"), before)
        self.assertEqual(state["etag"], '"old"')

    def test_etag_saved_after_everything_was_sent(self):
        state = self.run_main([raw(1, "a"), raw(2, "b")])
        self.assertEqual(self.posted.call_count, 2)
        self.assertEqual(state["etag"], '"new"')

    def test_etag_saved_when_archive_changed_but_nothing_is_new(self):
        state = self.run_main([raw(0, "already seen")])
        self.assertEqual(state["etag"], '"new"')

    def test_etag_cleared_when_sending_fails_midway(self):
        self.posted.side_effect = [None, RuntimeError("discord down")]
        with self.assertRaises(RuntimeError):
            self.run_main([raw(1, "a", 20), raw(2, "b", 10)])
        state = ts.load_state()
        self.assertIn("1", state["seen"])
        self.assertNotIn("2", state["seen"])
        self.assertIsNone(state["etag"])

    def test_etag_cleared_while_a_backlog_remains(self):
        backlog = [raw(i, f"post {i}", 100 + i) for i in range(1, ts.MAX_SEND_PER_RUN + 3)]
        state = self.run_main(backlog)
        self.assertEqual(self.posted.call_count, ts.MAX_SEND_PER_RUN)
        self.assertIsNone(state["etag"])


    def test_translation_failure_holds_back_the_post_and_everything_after_it(self):
        self.posted.side_effect = [ts.TranslationDeferred("1")]
        state = self.run_main([raw(1, "a", 20), raw(2, "b", 10)])
        self.assertEqual(self.posted.call_count, 1)
        self.assertNotIn("1", state["seen"])
        self.assertNotIn("2", state["seen"])
        self.assertIsNone(state["etag"])


class DescriptionTest(unittest.TestCase):
    def post(self, age_seconds):
        return {"id": "1", "content": "original", "media": None,
                "created_ts": time.time() - age_seconds}

    def test_fresh_post_is_deferred_when_translation_fails(self):
        with mock.patch.object(ts, "translate_text", return_value=None):
            with self.assertRaises(ts.TranslationDeferred):
                ts.build_description(self.post(60))

    def test_old_post_falls_back_to_original_with_notice(self):
        with mock.patch.object(ts, "translate_text", return_value=None):
            text = ts.build_description(self.post(ts.TRANSLATION_GRACE_SECONDS + 60))
        self.assertEqual(text, ts.TRANSLATION_FALLBACK_NOTE + "original")

    def test_long_translation_is_split_across_embeds(self):
        post = {"url": "https://x", "timestamp": None}
        embeds = ts.build_embeds(post, "x" * (ts.MAX_TRANSLATED_LEN + 5), "card.png")
        self.assertEqual([len(e["description"]) for e in embeds], [ts.MAX_TRANSLATED_LEN, 5])
        self.assertIn("image", embeds[0])
        self.assertNotIn("image", embeds[1])


class TranslateTextTest(unittest.TestCase):
    def setUp(self):
        self.translator = mock.Mock()
        for p in (
            mock.patch.object(ts, "get_translator", return_value=self.translator),
            mock.patch.object(ts, "TRANSLATORS", [("deepl", ts.translate_deepl)]),
            mock.patch.object(ts, "_DISABLED", set()),
            mock.patch.dict("os.environ", {ts.DEEPL_KEY_ENV: "key"}),
            mock.patch.object(ts.time, "sleep"),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.log = io.StringIO()
        redirect = redirect_stdout(self.log)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def result(self, text):
        return mock.Mock(text=text)

    def test_urls_are_kept_out_of_the_translation_and_appended(self):
        self.translator.translate_text.return_value = self.result("你好世界")
        out = ts.translate_text("Hello world https://example.com/a?b=1")
        self.assertEqual(out, "你好世界\nhttps://example.com/a?b=1")
        self.assertEqual(self.translator.translate_text.call_args.args[0], "Hello world")

    def test_text_without_words_is_not_sent_to_deepl(self):
        self.assertEqual(ts.translate_text("https://example.com 🇺🇸 47"), "https://example.com 🇺🇸 47")
        self.translator.translate_text.assert_not_called()

    def test_transient_error_is_retried(self):
        self.translator.translate_text.side_effect = [ConnectionError("reset"), self.result("好的")]
        self.assertEqual(ts.translate_text("All good"), "好的")
        self.assertEqual(self.translator.translate_text.call_count, 2)

    def test_untranslated_echo_counts_as_failure(self):
        self.translator.translate_text.return_value = self.result("All good")
        self.assertIsNone(ts.translate_text("All good"))
        self.assertEqual(self.translator.translate_text.call_count, ts.TRANSLATE_ATTEMPTS)

    def test_quota_error_is_not_retried(self):
        self.translator.translate_text.side_effect = ts.deepl.QuotaExceededException("quota")
        self.assertIsNone(ts.translate_text("All good"))
        self.assertEqual(self.translator.translate_text.call_count, 1)


class TranslatorChainTest(unittest.TestCase):
    def setUp(self):
        for p in (mock.patch.object(ts.time, "sleep"), mock.patch.object(ts, "_DISABLED", set())):
            p.start()
            self.addCleanup(p.stop)
        redirect = redirect_stdout(io.StringIO())
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def test_next_backend_is_used_when_the_first_fails(self):
        chain = [("a", lambda body: None), ("b", lambda body: "好"), ("c", mock.Mock())]
        with mock.patch.object(ts, "TRANSLATORS", chain):
            self.assertEqual(ts.translate_text("Good"), "好")
        chain[2][1].assert_not_called()

    def test_a_failed_backend_is_skipped_for_the_rest_of_the_run(self):
        first = mock.Mock(return_value=None)
        with mock.patch.object(ts, "TRANSLATORS", [("google", first), ("b", lambda body: "好")]):
            ts.translate_text("Good one")
            ts.translate_text("Good two")
        self.assertEqual(first.call_count, 1)

    def test_all_backends_failing_returns_none(self):
        with mock.patch.object(ts, "TRANSLATORS", [("a", lambda body: None), ("b", lambda body: "English")]):
            self.assertIsNone(ts.translate_text("Good"))

    def test_gemini_is_skipped_without_a_key(self):
        with mock.patch.dict("os.environ", {}, clear=True), mock.patch.object(ts.requests, "post") as post:
            self.assertIsNone(ts.translate_gemini("Hello"))
        post.assert_not_called()

    def test_gemini_request_and_response(self):
        reply = mock.Mock(status_code=200)
        reply.json.return_value = {"candidates": [{"content": {"parts": [{"text": "你好"}]}}]}
        with mock.patch.dict("os.environ", {ts.GEMINI_KEY_ENV: "k"}), \
                mock.patch.object(ts.requests, "post", return_value=reply) as post:
            self.assertEqual(ts.translate_gemini("Hello"), "你好")
        self.assertEqual(post.call_args.kwargs["headers"], {"x-goog-api-key": "k"})
        self.assertIn("<post>\nHello\n</post>", post.call_args.kwargs["json"]["contents"][0]["parts"][0]["text"])

    def test_gemini_retired_model_falls_through_to_the_next_one(self):
        gone = mock.Mock(status_code=404, text="no longer available")
        ok = mock.Mock(status_code=200)
        ok.json.return_value = {"candidates": [{"content": {"parts": [{"text": "你好"}]}}]}
        with mock.patch.dict("os.environ", {ts.GEMINI_KEY_ENV: "k", ts.GEMINI_MODEL_ENV: "old-model"}), \
                mock.patch.object(ts.requests, "post", side_effect=[gone, ok]) as post:
            self.assertEqual(ts.translate_gemini("Hello"), "你好")
        urls = [c.args[0] for c in post.call_args_list]
        self.assertIn("old-model", urls[0])
        self.assertIn(ts.GEMINI_FALLBACK_MODELS[0], urls[1])

    def test_gemini_bad_key_is_not_retried(self):
        reply = mock.Mock(status_code=403, text="denied")
        with mock.patch.dict("os.environ", {ts.GEMINI_KEY_ENV: "k"}), \
                mock.patch.object(ts.requests, "post", return_value=reply) as post:
            self.assertIsNone(ts.translate_gemini("Hello"))
        self.assertEqual(post.call_count, 1)

    def test_unofficial_google_segments_are_joined(self):
        reply = mock.Mock(status_code=200)
        reply.json.return_value = [[["你好，", "Hello, "], ["世界", "world"]], None, "en"]
        with mock.patch.object(ts.requests, "post", return_value=reply):
            self.assertEqual(ts.translate_google_free("Hello, world"), "你好，世界")


class MediaParsingTest(unittest.TestCase):
    def test_all_media_items_are_kept_in_order(self):
        items = ts.get_media_items({"media": [
            "https://x/a.jpg", {"url": "https://x/b.mp4", "preview_url": "https://x/b.jpg"},
        ]})
        self.assertEqual([i["type"] for i in items], ["image", "video"])
        self.assertEqual(items[1]["preview_url"], "https://x/b.jpg")

    def test_extensionless_url_uses_type_hint_else_unknown(self):
        items = ts.get_media_items({"media": [
            {"url": "https://x/files/1", "type": "video"}, "https://x/files/2",
        ]})
        self.assertEqual([i["type"] for i in items], ["video", "unknown"])

    def test_invalid_entries_are_skipped(self):
        self.assertEqual(ts.get_media_items({"media": ["", {"url": ""}, 5, "ftp://x"]}), [])

    def test_item_to_post_carries_every_item(self):
        post = ts.item_to_post(raw(1, "t", media=["https://x/a.jpg", "https://x/b.jpg"]))
        self.assertEqual(len(post["media_items"]), 2)
        self.assertEqual(post["media"]["url"], "https://x/a.jpg")


def png_bytes(size=(40, 30)):
    buffer = io.BytesIO()
    ts.Image.new("RGB", size, "red").save(buffer, format="PNG")
    return buffer.getvalue()


class PrepareMediaTest(unittest.TestCase):
    def prepare(self, media, downloads):
        """downloads maps URL -> (bytes, content_type) or None (a failed download)."""
        post = {"id": "7", "media_items": [
            {"url": u, "type": t, "preview_url": pv} for u, t, pv in media
        ]}
        with mock.patch.object(ts, "fetch_media", side_effect=lambda url, _max: downloads.get(url)), \
                redirect_stdout(io.StringIO()):
            return ts.prepare_media(post)

    def test_image_goes_into_the_card(self):
        bundle = self.prepare([("https://x/a.png", "image", None)],
                              {"https://x/a.png": (png_bytes(), "image/png")})
        self.assertIsNotNone(bundle["card_image"])
        self.assertEqual((bundle["files"], bundle["links"]), ([], []))

    def test_failed_image_download_becomes_a_link(self):
        bundle = self.prepare([("https://x/a.png", "image", None)], {})
        self.assertIsNone(bundle["card_image"])
        self.assertEqual(bundle["links"], ["https://x/a.png"])

    def test_video_is_attached_as_a_file(self):
        bundle = self.prepare([("https://x/v.mp4", "video", None)],
                              {"https://x/v.mp4": (b"mp4data", "video/mp4")})
        self.assertEqual([(n, c) for n, c, _ in bundle["files"]], [("trump_7_0.mp4", b"mp4data")])
        self.assertEqual(bundle["links"], [])

    def test_video_over_the_budget_or_failed_becomes_a_link(self):
        big = b"0" * (ts.MAX_UPLOAD_BYTES)
        bundle = self.prepare([("https://x/v.mp4", "video", None)], {"https://x/v.mp4": (big, "video/mp4")})
        self.assertEqual((bundle["files"], bundle["links"]), ([], ["https://x/v.mp4"]))

    def test_hls_playlist_is_never_downloaded(self):
        bundle = self.prepare([("https://x/v.m3u8", "video", None)], {})
        self.assertEqual(bundle["links"], ["https://x/v.m3u8"])

    def test_unknown_type_is_resolved_from_content_type(self):
        bundle = self.prepare([("https://x/files/1", "unknown", None)],
                              {"https://x/files/1": (png_bytes(), "image/png")})
        self.assertIsNotNone(bundle["card_image"])

    def test_video_preview_becomes_the_card_image(self):
        bundle = self.prepare(
            [("https://x/v.mp4", "video", "https://x/p.png")],
            {"https://x/v.mp4": (b"mp4", "video/mp4"), "https://x/p.png": (png_bytes(), "image/png")},
        )
        self.assertIsNotNone(bundle["card_image"])
        self.assertEqual(len(bundle["files"]), 1)


class FetchMediaTest(unittest.TestCase):
    class Resp:
        def __init__(self, status, body=b"", headers=None):
            self.status_code, self.body, self.headers = status, body, headers or {}

        def iter_content(self, size):
            yield self.body

        def close(self):
            pass

    def fetch(self, browser, plain, max_bytes=100):
        with mock.patch.object(ts, "browser_requests", mock.Mock(get=browser)), \
                mock.patch.object(ts.requests, "get", plain), redirect_stdout(io.StringIO()):
            return ts.fetch_media("https://x/a.jpg", max_bytes)

    def test_browser_transport_is_tried_first(self):
        browser = mock.Mock(return_value=self.Resp(200, b"ok", {"Content-Type": "image/jpeg; q=1"}))
        plain = mock.Mock()
        self.assertEqual(self.fetch(browser, plain), (b"ok", "image/jpeg"))
        plain.assert_not_called()
        self.assertEqual(browser.call_args.kwargs["impersonate"], "chrome")

    def test_falls_back_to_plain_requests_when_browser_transport_fails(self):
        browser = mock.Mock(side_effect=OSError("tls handshake"))
        plain = mock.Mock(return_value=self.Resp(200, b"ok"))
        self.assertEqual(self.fetch(browser, plain)[0], b"ok")

    def test_http_error_on_every_transport_returns_none(self):
        self.assertIsNone(self.fetch(mock.Mock(return_value=self.Resp(403)),
                                     mock.Mock(return_value=self.Resp(403))))

    def test_oversized_download_is_refused_without_trying_again(self):
        browser = mock.Mock(return_value=self.Resp(200, b"x", {"Content-Length": "999"}))
        plain = mock.Mock()
        self.assertIsNone(self.fetch(browser, plain))
        plain.assert_not_called()


class PostToDiscordTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for p in (
            mock.patch.object(ts, "CARD_DIR", self.tmp),
            mock.patch.dict("os.environ", {ts.WEBHOOK_ENV: "hook"}),
            mock.patch.object(ts, "build_description", return_value="你好"),
        ):
            p.start()
            self.addCleanup(p.stop)

    def post(self):
        return {"id": "9", "content": "hi", "url": "https://t/9", "created_ts": time.time(),
                "timestamp": None, "media": {"url": "https://x/v.mp4", "type": "video", "preview_url": None},
                "media_items": [{"url": "https://x/v.mp4", "type": "video", "preview_url": None}]}

    def test_video_is_uploaded_next_to_the_card(self):
        with mock.patch.object(ts, "fetch_media", return_value=(b"mp4", "video/mp4")), \
                mock.patch.object(ts, "post_webhook") as hook:
            ts.post_to_discord(self.post())
        names = [n for n, _ in hook.call_args.kwargs["files"]]
        self.assertEqual(names, ["trump_9.png", "trump_9_0.mp4"])
        self.assertNotIn("content", hook.call_args.args[1])
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_too_large_upload_is_resent_with_links(self):
        too_big = ts.requests.HTTPError("413", response=mock.Mock(status_code=413))
        with mock.patch.object(ts, "fetch_media", return_value=(b"mp4", "video/mp4")), \
                mock.patch.object(ts, "post_webhook", side_effect=[too_big, None]) as hook:
            ts.post_to_discord(self.post())
        retry = hook.call_args_list[1]
        self.assertEqual([n for n, _ in retry.kwargs["files"]], ["trump_9.png"])
        self.assertIn("https://x/v.mp4", retry.args[1]["content"])

    def test_other_http_errors_are_raised(self):
        failure = ts.requests.HTTPError("500", response=mock.Mock(status_code=500))
        with mock.patch.object(ts, "fetch_media", return_value=(b"mp4", "video/mp4")), \
                mock.patch.object(ts, "post_webhook", side_effect=failure):
            with self.assertRaises(ts.requests.HTTPError):
                ts.post_to_discord(self.post())


if __name__ == "__main__":
    unittest.main()
