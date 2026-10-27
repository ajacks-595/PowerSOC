"""Mattermost webhook delivery for the daily brief.

The webhook URL is stored Fernet-encrypted in settings (credential
MATTERMOST_WEBHOOK_URL); channel/username/icon are configurable with sensible
defaults. Incoming-webhook payload shape matches the SOC dashboard:
`{"text", "username", "icon_emoji", "channel"?}`.
"""
from __future__ import annotations

import logging

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

import config

log = logging.getLogger("powersoc.notifications")

_TIMEOUT = 15
DEFAULT_USERNAME = "PowerSOC"
DEFAULT_ICON = ":zap:"


def valid_webhook_url(url: str | None) -> bool:
    """http(s) URL with a host. (The Mattermost server is usually ON the LAN,
    so private addresses stay allowed — this only stops file:/gopher:/
    scheme-less values from reaching requests.)"""
    from urllib.parse import urlsplit
    if not url or not isinstance(url, str):
        return False
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def _build_payload(text: str, channel: str | None) -> dict:
    payload = {"text": text, "username": DEFAULT_USERNAME,
               "icon_emoji": DEFAULT_ICON}
    # Mattermost wants the channel handle without a leading '#'; a blank channel
    # means "use the channel this webhook was created against" (omit the field).
    # strip() FIRST: ' #chan' would otherwise keep its '#' and 404 the post.
    ch = (channel or "").strip().lstrip("#").strip()
    if ch:
        payload["channel"] = ch
    return payload


class _RetryableStatus(Exception):
    """Transient HTTP status (429/5xx) — retried like a connection error."""
    def __init__(self, resp: requests.Response):
        self.resp = resp
        super().__init__(f"HTTP {resp.status_code}")


_REDIRECTS = (301, 302, 303, 307, 308)


def _location_host(r) -> str | None:
    from urllib.parse import urljoin, urlsplit
    loc = (getattr(r, "headers", None) or {}).get("Location")
    return urlsplit(urljoin(getattr(r, "url", "") or "", loc)).hostname if loc else None


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=20),
       retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout,
                                      _RetryableStatus)),
       reraise=True)
def _post(url: str, payload: dict) -> requests.Response:
    """POST without letting requests follow redirects: it would re-send the
    brief to wherever a 307/308 points — another host included. A same-host
    307/308 (a path rewrite) is followed by hand, up to 3 hops; anything else
    comes back as the 3xx for post_brief to report."""
    from urllib.parse import urljoin, urlsplit
    host = urlsplit(url).hostname
    target = url
    hops: list = []
    for _ in range(4):
        r = requests.post(target, json=payload, timeout=_TIMEOUT,
                          allow_redirects=False)
        loc = (getattr(r, "headers", None) or {}).get("Location")
        if (r.status_code in (307, 308) and loc
                and urlsplit(urljoin(target, loc)).hostname == host
                and len(hops) < 3):
            hops.append(r)
            target = urljoin(target, loc)
            continue
        break
    # A transient 429/502 from a proxy in front of Mattermost used to get a
    # single attempt — an intraday alert sent during that blip was simply lost.
    if r.status_code == 429 or r.status_code >= 500:
        raise _RetryableStatus(r)
    if hops:
        r.history = hops
    return r


def _error_reason(r) -> str:
    """Short, safe description of a non-2xx reply: the status, plus the
    `message` of a Mattermost-style JSON error. Never the raw body — it may
    echo request data, and it went into the log verbatim."""
    reason = f"HTTP {r.status_code}"
    if r.status_code in _REDIRECTS:
        host = _location_host(r)
        return reason + (f": redirected to {host} — a login wall is blocking the webhook"
                         if host else ": unexpected redirect")
    try:
        import json as _json
        msg = _json.loads(r.text or "").get("message")
    except (ValueError, AttributeError):
        msg = None
    if isinstance(msg, str) and msg:
        clean = "".join(ch for ch in msg if ch.isprintable())[:120]
        reason += f": {clean}"
    return reason


def post_brief(text: str, *, webhook_url: str | None = None,
               channel: str | None = None) -> tuple[bool, str]:
    """Post a brief to Mattermost. Resolves URL/channel from settings if not
    given. Returns (ok, message). Never raises — a notification failure must
    not crash the sync."""
    settings = config.Settings.load()
    url = webhook_url or settings.mattermost_webhook_url
    channel = channel or settings.mattermost_channel
    if not url:
        log.warning("Mattermost webhook not configured — brief not sent")
        return False, "webhook not configured"
    if not valid_webhook_url(url):
        log.warning("Mattermost webhook URL is not an http(s) URL — not sent")
        return False, "invalid webhook URL (must be http/https)"
    try:
        r = _post(url, _build_payload(text, channel))
    except _RetryableStatus as e:
        reason = _error_reason(e.resp)
        log.warning("Mattermost returned %s after retries", reason)
        return False, reason
    except requests.RequestException as e:
        # Catch the whole family — MissingSchema/InvalidURL from a mistyped
        # webhook must honour the "never raises" contract. And never
        # interpolate the exception text: connection errors embed the full
        # webhook URL (a secret) which would leak into logs and UI details.
        log.warning("Mattermost post failed after retries: %s", type(e).__name__)
        return False, f"delivery failed: {type(e).__name__}"
    ok = 200 <= r.status_code < 300
    if not ok:
        reason = _error_reason(r)
        log.warning("Mattermost returned %s", reason)
        return False, reason
    wall = _not_a_webhook_reply(r, url)
    if wall:
        log.warning("Mattermost returned %s %s", r.status_code, wall)
        return False, f"{r.status_code} {wall}"
    return True, f"{r.status_code} {(r.text or '')[:40]}".strip()


def _not_a_webhook_reply(r: requests.Response, url: str) -> str | None:
    """Why a 2xx reply is NOT a delivery, or None.

    A login wall in front of the webhook (Cloudflare Access, a proxy's SSO)
    answers the POST with a redirect that requests follows to the sign-in
    page: 200, and nothing was posted — the Pi's briefs were "sent" that way
    for weeks. Webhooks reply with a short body ("ok", JSON or nothing),
    never an HTML page. Only the HOST of a redirect is reported: the full
    URL carries the webhook secret in its query string."""
    import re
    from urllib.parse import urlsplit
    body = (r.text or "")[:4096].lstrip("\ufeff \t\r\n")
    # A BOM or a leading <!-- comment --> used to hide the page from the check.
    while body.startswith("<!--") and "-->" in body:
        body = body[body.index("-->") + 3:].lstrip()
    body = body[:512].lower()
    final_host = urlsplit(r.url or url).hostname
    if r.history and final_host != urlsplit(url).hostname:
        return f"redirected to {final_host} — a login wall is blocking the webhook"
    if body.startswith(("<!doctype html", "<html", "<head", "<body")):
        title = re.search(r"<title[^>]*>([^<]{0,80})", r.text[:4096], re.I)
        page = title.group(1).strip() if title else "an HTML page"
        return f"got a web page ({page!r}), not a webhook reply — check for a login wall"
    return None


def test_webhook(webhook_url: str | None = None,
                 channel: str | None = None) -> tuple[bool, str]:
    """Send a configuration-check message — used by the Settings 'Test' button."""
    return post_brief(
        "### ⚡ PowerSOC test\nIf you can see this, your Mattermost webhook is "
        "wired up correctly.",
        webhook_url=webhook_url, channel=channel)
