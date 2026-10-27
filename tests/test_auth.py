"""Unit tests for password hashing + the rehash-on-login upgrade policy."""
from __future__ import annotations

import base64
import hashlib

import auth


def _legacy_hash(password: str, iters: int = 200_000) -> str:
    salt = b"0123456789abcdef"
    raw = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iters)
    return f"pbkdf2${iters}${base64.b64encode(salt).decode()}${base64.b64encode(raw).decode()}"


def test_hash_roundtrip_and_iteration_count():
    h = auth.hash_password("correct horse battery staple")
    assert h.startswith("pbkdf2$")
    assert int(h.split("$")[1]) == auth._PBKDF2_ITERATIONS == 600_000
    assert auth.verify_password("correct horse battery staple", h)
    assert not auth.verify_password("wrong", h)


def test_legacy_lower_iteration_hash_still_verifies():
    # A hash minted at the old 200k count must still verify (the iteration
    # count is read from the stored hash) and be flagged for upgrade.
    legacy = _legacy_hash("hunter2longpw", 200_000)
    assert auth.verify_password("hunter2longpw", legacy)
    assert auth.needs_rehash(legacy) is True


def test_needs_rehash_policy():
    assert auth.needs_rehash(auth.hash_password("freshpassword")) is False
    assert auth.needs_rehash(_legacy_hash("x", 200_000)) is True
    assert auth.needs_rehash(_legacy_hash("x", 600_000)) is False
    assert auth.needs_rehash("not-a-valid-hash") is True
    assert auth.needs_rehash("bcrypt$12$abc$def") is True
