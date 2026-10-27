"""Tests for Mattermost payload building (channel normalization)."""
from __future__ import annotations

import pytest

import notifications


def test_payload_strips_leading_hash():
    p = notifications._build_payload("hi", "#energy")
    assert p["channel"] == "energy"        # Mattermost wants no leading '#'
    assert p["text"] == "hi"
    assert p["username"] == "PowerSOC"


def test_payload_blank_channel_is_omitted():
    # Blank/None channel -> omit the field -> post to the webhook's own channel.
    assert "channel" not in notifications._build_payload("hi", "")
    assert "channel" not in notifications._build_payload("hi", None)
    assert "channel" not in notifications._build_payload("hi", "   ")
    assert "channel" not in notifications._build_payload("hi", "#")


def test_payload_plain_channel_passes_through():
    assert notifications._build_payload("hi", "town-square")["channel"] == "town-square"


def test_post_brief_no_webhook_returns_not_configured():
    # With no webhook configured anywhere, post_brief reports it cleanly rather
    # than raising (delivery failures must never crash the sync).
    sent, detail = notifications.post_brief("x", webhook_url="", channel="energy")
    assert sent is False
    assert "not configured" in detail


# ---------- a login wall's 200 is not a delivery (2026-10-25) ----------------

class _Reply:
    def __init__(self, status, text, url, history=()):
        self.status_code, self.text, self.url = status, text, url
        self.history = list(history)


HOOK = "https://chat.example.com/hooks/s3cr3t"
ACCESS_PAGE = ("<!DOCTYPE html><html><head><title>Sign in ・ Cloudflare Access"
               "</title></head><body>…</body></html>")


@pytest.mark.parametrize("reply,ok", [
    (_Reply(200, "ok", HOOK), True),                       # Mattermost
    (_Reply(200, "", HOOK), True),                         # bodyless 2xx
    (_Reply(204, "", HOOK), True),                         # Discord-style
    (_Reply(200, '{"ok":true}', HOOK), True),
    (_Reply(200, ACCESS_PAGE, HOOK), False),               # wall served in place
    (_Reply(200, ACCESS_PAGE,                              # wall via redirect
            "https://team.cloudflareaccess.com/cdn-cgi/access/login/chat"
            "?redirect_url=%2Fhooks%2Fs3cr3t", history=[object()]), False),
])
def test_post_brief_only_counts_real_webhook_replies(monkeypatch, reply, ok):
    monkeypatch.setattr(notifications, "_post", lambda url, payload: reply)
    sent, detail = notifications.post_brief("x", webhook_url=HOOK)
    assert sent is ok
    assert "s3cr3t" not in detail                         # never leak the secret
    if not ok:
        assert "login wall" in detail


def test_login_wall_detail_names_the_page(monkeypatch):
    monkeypatch.setattr(notifications, "_post",
                        lambda url, payload: _Reply(200, ACCESS_PAGE, HOOK))
    sent, detail = notifications.post_brief("x", webhook_url=HOOK)
    assert sent is False and "Cloudflare Access" in detail


def test_same_host_redirect_is_still_a_delivery(monkeypatch):
    """A 307/308 within the same server (e.g. a path rewrite) re-POSTs the
    body; requests follows it and the final reply is the real one."""
    monkeypatch.setattr(notifications, "_post", lambda url, payload: _Reply(
        200, "ok", HOOK.replace("/hooks/", "/api/hooks/"), history=[object()]))
    assert notifications.post_brief("x", webhook_url=HOOK)[0] is True
