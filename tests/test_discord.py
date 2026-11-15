import json
import unittest
from unittest import mock

import requests

from market_pulse import discord


class FakeResponse:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


class PostWebhookTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(discord.time, "sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)

    def test_json_message(self):
        session = FakeSession(FakeResponse(204))
        discord.post_webhook("hook", {"content": "hi"}, session=session)
        self.assertEqual(session.calls[0]["json"], {"content": "hi"})
        self.sleep.assert_not_called()

    def test_file_with_payload_is_multipart(self):
        session = FakeSession(FakeResponse(200))
        discord.post_webhook("hook", {"embeds": []}, file=("card.png", b"png"), session=session)
        call = session.calls[0]
        self.assertEqual(json.loads(call["data"]["payload_json"]), {"embeds": []})
        self.assertEqual(call["files"]["file"], ("card.png", b"png", "image/png"))

    def test_multiple_files_are_sent_as_numbered_parts_with_guessed_types(self):
        session = FakeSession(FakeResponse(200))
        discord.post_webhook(
            "hook", {"embeds": []},
            files=[("card.png", b"png"), ("clip.mp4", b"mp4")], session=session,
        )
        files = session.calls[0]["files"]
        self.assertEqual(files["files[0]"], ("card.png", b"png", "image/png"))
        self.assertEqual(files["files[1]"], ("clip.mp4", b"mp4", "video/mp4"))

    def test_file_without_payload(self):
        session = FakeSession(FakeResponse(200))
        discord.post_webhook("hook", file=("table.png", b"png"), session=session)
        self.assertIsNone(session.calls[0]["data"])

    def test_retries_after_rate_limit(self):
        session = FakeSession(FakeResponse(429, {"retry_after": 0.5}), FakeResponse(204))
        discord.post_webhook("hook", {}, session=session)
        self.assertEqual(len(session.calls), 2)
        self.sleep.assert_called_once_with(1.5)

    def test_rate_limit_without_json_body_uses_default_wait(self):
        session = FakeSession(FakeResponse(429), FakeResponse(204))
        discord.post_webhook("hook", {}, session=session)
        self.sleep.assert_called_once_with(3.0)

    def test_gives_up_after_max_retries(self):
        session = FakeSession(*[FakeResponse(429, {"retry_after": 0})] * discord.MAX_RETRIES)
        with self.assertRaises(RuntimeError):
            discord.post_webhook("hook", {}, session=session)
        self.assertEqual(len(session.calls), discord.MAX_RETRIES)

    def test_other_errors_raise_immediately(self):
        session = FakeSession(FakeResponse(500))
        with self.assertRaises(requests.HTTPError):
            discord.post_webhook("hook", {}, session=session)
        self.assertEqual(len(session.calls), 1)


if __name__ == "__main__":
    unittest.main()
