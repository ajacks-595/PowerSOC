"""RFC 6238 TOTP (time-based one-time passwords) — standard library only.

Avoids a pyotp dependency: HOTP is HMAC-SHA1 over the time counter, truncated
to 6 digits (the de-facto standard every authenticator app implements).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

_DIGITS = 6
_PERIOD = 30


def generate_secret() -> str:
    """A fresh base32 TOTP secret (160-bit, unpadded — as apps expect)."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _hotp(secret_b32: str, counter: int) -> str:
    key = base64.b32decode(secret_b32 + "=" * (-len(secret_b32) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** _DIGITS)).zfill(_DIGITS)


def verify_counter(secret_b32: str, code: str, *, at: float | None = None,
                   window: int = 1) -> int | None:
    """Constant-time check of a 6-digit code, tolerating ±`window` time steps
    (clock skew). Returns the MATCHED time counter (for RFC 6238 §5.2 single-
    use enforcement — reject counters <= the last accepted one), or None on
    mismatch/malformed input."""
    if not secret_b32 or not code:
        return None
    code = code.strip().replace(" ", "")
    # isascii() first: str.isdigit() also accepts Unicode digits (e.g.
    # Arabic-Indic), and hmac.compare_digest raises TypeError on non-ASCII
    # str — that was a 500 on /login for anyone who knew the password.
    if not (code.isascii() and code.isdigit() and len(code) == _DIGITS):
        return None
    counter = int((time.time() if at is None else at) // _PERIOD)
    for w in range(-window, window + 1):
        if hmac.compare_digest(_hotp(secret_b32, counter + w), code):
            return counter + w
    return None


def verify(secret_b32: str, code: str, *, at: float | None = None,
           window: int = 1) -> bool:
    """Boolean wrapper around verify_counter (kept for callers that don't
    track the replay counter)."""
    return verify_counter(secret_b32, code, at=at, window=window) is not None


def provisioning_uri(secret_b32: str, account: str, issuer: str = "PowerSOC") -> str:
    """otpauth:// URI for QR enrolment (and manual entry of the secret)."""
    label = f"{quote(issuer)}:{quote(account)}"
    return (f"otpauth://totp/{label}?secret={secret_b32}"
            f"&issuer={quote(issuer)}&digits={_DIGITS}&period={_PERIOD}")
