# Truth Social tracker: investigation notes (Nov 2026)

Summary of an investigation into two problems with the Truth Social tracker
(`market_pulse/truth_social.py`): unstable translation, and images/videos not
showing up in Discord. Includes what was changed, what the logs showed, and
what is still open.

## Status at a glance

| Area | Status |
|---|---|
| Translation | Fixed. Gemini free tier is the primary backend; verified on GitHub Actions. |
| Media attachments | Code is in place (browser-TLS download, video attachments, size fallback) but **cannot work from GitHub Actions**: the media CDN answers HTTP 403. |
| Media fallback | Done. A one-line note links the post page instead of a raw CDN URL. |
| Images shown inside Discord | **Open.** Needs a decision, see "Open problem" below. |

## Root causes

### Translation was unstable

- The DeepL free quota (500k characters/month) was exhausted. An Actions run
  logged `QuotaExceededException: ... used all the characters included in your
  free DeepL API plan`.
- On any DeepL error the old code silently fell back to the English original and
  then marked the post as seen, so a post could never be translated afterwards.
- No retries, URLs were sent through the translator, and an untranslated echo
  from DeepL was accepted as a translation.

### Media never appeared

- The card image was built by downloading the media with plain `requests`.
  Failures were swallowed, so the card simply had no image. Videos were never
  uploaded at all, only a drawn play button.
- After the fix (browser-TLS client, then plain requests), the Actions log of a
  real image post shows both transports failing the same way:

  ```
  Media download via browser-tls got HTTP 403: https://static-assets-1.truthsocial.com/...jpg
  Media download via requests got HTTP 403: https://static-assets-1.truthsocial.com/...jpg
  ```

  So it is not a TLS fingerprint problem. `static-assets-1.truthsocial.com`
  refuses these requests from GitHub Actions. Discord's own servers cannot fetch
  the URL either (a bare link in the message was not unfurled into a preview).

## What changed

PR #1 (`77e1ded`)

- Media: download via `curl_cffi` (browser TLS) then `requests`; logs the
  transport and HTTP status of every failure. Videos and extra images are
  attached to the webhook message; anything not fetched or over the upload budget
  is linked instead; a 413 from Discord triggers a resend with links.
  `discord.post_webhook` gained `files=[...]`.
- Translation chain: Gemini free tier, then DeepL, then Google's keyless
  endpoint. Each backend is skipped if it has no key. URLs are kept out of the
  translated text, untranslated echoes are rejected, transient errors are retried,
  and a backend that fails is skipped for the rest of the run.
- A fresh post whose translation fails is held back (not marked seen) and
  retried by later runs. After 30 minutes the English original is sent with a
  notice.
- `python -m market_pulse.truth_social --check-translation [--probe-models]` and
  the manual workflow `translate-check.yml` test every backend without touching
  Discord or state.

PR #2 (`6e17e40`)

- When media cannot be attached, the message carries one line
  ("本帖含图片，无法自动获取，点击查看原帖") linking the Truth Social post page,
  instead of the raw CDN URL.

## Measurements

Gemini models, same sample post, run on GitHub Actions with the real key:

| Model | Result |
|---|---|
| `gemini-2.5-flash`, `gemini-2.5-flash-lite` | 404, retired |
| `gemini-3.7-flash`, `gemini-3.8-flash`, `gemini-flash-latest` | read timeout after 20 s (the alias pointed at 3.8) |
| `gemini-3.5-flash`, `gemini-3.6-flash` | works, 6 to 7 s |
| `gemini-3.1-flash-lite` | works, 0.5 s |
| `gemini-3.5-flash-lite` | works, 0.7 s |

The default order is therefore `gemini-3.1-flash-lite`, `gemini-3.5-flash-lite`,
`gemini-3.5-flash`; the next one is tried on a 404. Google retires and renames
models, so re-run the probe if Gemini starts returning 404s:

```bash
python -m market_pulse.truth_social --check-translation --probe-models
```

Google's keyless translate endpoint answered 429 on Actions after a few calls, so
it is only a last resort.

## Operations

- Secrets used by `news-trump.yml`: `DISCORD_WEBHOOK_URL_TRUMP`, `GEMINI_API_KEY`,
  and optionally `DEEPL_API_KEY`. Only the webhook is required.
- `GEMINI_MODEL` overrides the first model tried.
- To check translation after a change, run the "Check Truth Social translation
  backends" workflow from the Actions tab (it sends nothing to Discord).
- In a tracker run log, `Translated with gemini` confirms the primary path, and
  `Media download via ... got HTTP ...` lines show why media was not attached.

## Open problem: showing images inside the Discord message

Not solved, and not solvable from GitHub-hosted runners without working around
the CDN's refusal. That was deliberately not attempted: no further browser
impersonation, header tricks or third-party image proxies.

Options that do not involve getting around the block:

1. **Run the tracker on a self-hosted runner on a normal home network**
   (`runs-on: self-hosted`). Requests then come from an ordinary residential
   connection. Not verified, but cheap to check first. From the machine that
   would host the runner:

   ```bash
   curl -sI "https://static-assets-1.truthsocial.com/tmtg:prime-ts-assets/media_attachments/files/117/621/326/778/114/513/original/bb73d068a3bfddf1.jpg" | head -1
   ```

   `HTTP/2 200` means the approach should work. Caveat: if the runner is offline,
   scheduled runs queue and even the text-only posts stop, so keep the hosted
   job as a fallback rather than switching outright.
2. **Truth Social's authenticated API** with the owner's own account. Needs stored
   credentials and a check of their terms.
3. **Keep the current behaviour**: text card plus a one-line link to the post.

## Smaller known issues (not addressed)

- Emoji in the original post render as empty boxes on the card (DejaVu font).
- All-caps posts translate noticeably worse than normal-case text.
- Posts with identical text are dropped by the content-hash dedup for 30 days.
- A DeepL glossary (names, proper nouns) would improve consistency if DeepL is
  used again.
