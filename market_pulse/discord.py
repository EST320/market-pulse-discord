"""Shared Discord webhook client with bounded rate-limit retries."""

import json
import mimetypes
import time

import requests

MAX_RETRIES = 5


def _multipart_files(file, files):
    if files:
        return {
            f"files[{index}]": (name, content, mimetypes.guess_type(name)[0] or "application/octet-stream")
            for index, (name, content) in enumerate(files)
        }
    filename, content = file
    return {"file": (filename, content, "image/png")}


def post_webhook(webhook_url, payload=None, file=None, *, files=None, session=None, timeout=30):
    """Post a message to a Discord webhook.

    payload is the JSON message body. file is an optional (filename, bytes)
    PNG attachment; embeds reference it as attachment://<filename>. files is a
    list of (filename, bytes) attachments of any type (images, mp4 video); the
    content type is guessed from the extension.

    On HTTP 429 this waits for the returned retry_after and tries again, up to
    MAX_RETRIES attempts in total, then raises RuntimeError. Any other error
    status raises requests.HTTPError.
    """
    http = session or requests

    for attempt in range(1, MAX_RETRIES + 1):
        if file is None and not files:
            response = http.post(webhook_url, json=payload, timeout=timeout)
        else:
            data = None
            if payload is not None:
                data = {"payload_json": json.dumps(payload, ensure_ascii=False)}
            response = http.post(
                webhook_url,
                data=data,
                files=_multipart_files(file, files),
                timeout=timeout,
            )

        if response.status_code != 429:
            response.raise_for_status()
            return

        if attempt < MAX_RETRIES:
            try:
                retry_after = float(response.json().get("retry_after", 2))
            except (ValueError, TypeError, AttributeError):
                retry_after = 2.0
            time.sleep(retry_after + 1)

    raise RuntimeError(f"Discord returned 429 {MAX_RETRIES} times in a row, giving up on this message")


INTERACTION_URL = "https://discord.com/api/v10/webhooks/{application_id}/{token}/messages/@original"


def edit_interaction_response(application_id, token, payload, file=None, *, timeout=60):
    """Fill in the deferred reply to a slash command.

    After a command is acknowledged with a "thinking" placeholder, Discord
    accepts edits to that message for 15 minutes through the interaction
    token. file is an optional (filename, bytes) PNG attachment.
    """
    url = INTERACTION_URL.format(application_id=application_id, token=token)
    if file is None:
        response = requests.patch(url, json=payload, timeout=timeout)
    else:
        filename, content = file
        body = {**payload, "attachments": [{"id": 0, "filename": filename}]}
        response = requests.patch(
            url,
            data={"payload_json": json.dumps(body, ensure_ascii=False)},
            files={"files[0]": (filename, content, "image/png")},
            timeout=timeout,
        )
    response.raise_for_status()
