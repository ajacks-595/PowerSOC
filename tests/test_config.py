"""Tests for crypto-key stability (matters for credential persistence)."""
from __future__ import annotations

import config


def test_machine_id_override(monkeypatch):
    monkeypatch.setenv("POWERSOC_MACHINE_ID", "fixed-id-123")
    assert config._machine_id() == "fixed-id-123"


def test_encrypt_decrypt_stable_across_calls(monkeypatch):
    # With a stable machine id, a token encrypted now still decrypts later
    # (i.e. credentials survive a restart). fernet() re-derives the key each call.
    monkeypatch.setenv("POWERSOC_MACHINE_ID", "stable-xyz")
    token = config.encrypt("s3cret")
    assert config.decrypt(token) == "s3cret"


def test_different_machine_id_cannot_decrypt(monkeypatch):
    monkeypatch.setenv("POWERSOC_MACHINE_ID", "id-A")
    token = config.encrypt("data")
    monkeypatch.setenv("POWERSOC_MACHINE_ID", "id-B")
    assert config.decrypt(token) is None   # wrong key -> None, not an exception
