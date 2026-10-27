"""Tests for the stdlib TOTP implementation (RFC 6238)."""
from __future__ import annotations

import totp


def test_rfc6238_known_vector():
    # RFC 6238 test secret "12345678901234567890" (ASCII) in base32, at a known
    # time, yields a known SHA1 6-digit code.
    import base64
    secret = base64.b32encode(b"12345678901234567890").decode().rstrip("=")
    # t=59s -> counter 1 -> RFC sample (SHA1, 6 digits) = 287082.
    assert totp._hotp(secret, 1) == "287082"
    assert totp.verify(secret, "287082", at=59, window=0) is True


def test_generate_and_roundtrip():
    s = totp.generate_secret()
    assert len(s) >= 32
    now = 1_700_000_000
    code = totp._hotp(s, int(now // 30))
    assert totp.verify(s, code, at=now) is True
    assert totp.verify(s, code, at=now + 300) is False     # different step
    # A provably-wrong code (the real code with one digit flipped) must fail —
    # the old `in (True, False)` assertion could never fail.
    wrong = str((int(code[0]) + 1) % 10) + code[1:]
    assert totp.verify(s, wrong, at=now, window=0) is False


def test_verify_counter_returns_matched_step():
    s = totp.generate_secret()
    now = 1_700_000_000
    step = int(now // 30)
    code_prev = totp._hotp(s, step - 1)
    assert totp.verify_counter(s, code_prev, at=now) == step - 1
    assert totp.verify_counter(s, "", at=now) is None
    assert totp.verify_counter(s, "12345", at=now) is None   # wrong length


def test_window_tolerates_skew():
    s = totp.generate_secret()
    now = 1_700_000_000
    prev = totp._hotp(s, int(now // 30) - 1)
    assert totp.verify(s, prev, at=now, window=1) is True     # one step late, ok
    assert totp.verify(s, prev, at=now, window=0) is False    # no tolerance


def test_rejects_malformed():
    s = totp.generate_secret()
    assert totp.verify(s, "") is False
    assert totp.verify(s, "12ab56") is False
    assert totp.verify(s, "1234567") is False                 # wrong length
    assert totp.verify("", "123456") is False


def test_provisioning_uri():
    uri = totp.provisioning_uri("ABCD2345", "admin")
    assert uri.startswith("otpauth://totp/PowerSOC:admin?")
    assert "secret=ABCD2345" in uri and "issuer=PowerSOC" in uri
