"""Tests for derived energy calculations (sync.py pure helpers)."""
from __future__ import annotations

import pytest

from sync import net_cost_pence, saving_vs_flexible_pence, self_sufficiency_pct

# ---------- self-sufficiency ----------------------------------------------

def test_self_sufficiency_typical():
    # Imported 1.4 of 10 kWh load -> 86%.
    assert self_sufficiency_pct(1.4, 10.0) == pytest.approx(86.0)


def test_self_sufficiency_clamped():
    # Import exceeds load (CT-clamp vs meter mismatch) -> clamp to 0.
    assert self_sufficiency_pct(12.0, 10.0) == 0.0
    # Zero import -> 100%.
    assert self_sufficiency_pct(0.0, 10.0) == 100.0


def test_self_sufficiency_none_without_load():
    assert self_sufficiency_pct(5.0, 0) is None
    assert self_sufficiency_pct(5.0, None) is None


# ---------- net cost -------------------------------------------------------

def test_net_cost_combines_and_credits_export():
    # import 42 + standing 55 + gas 88 + standing 32 - export 27 = 190p.
    assert net_cost_pence(42, 55, 88, 32, 27) == pytest.approx(190.0)


def test_net_cost_handles_missing_parts():
    assert net_cost_pence(42, None, None, None, None) == pytest.approx(42.0)
    assert net_cost_pence(0, 55, 0, 0, 80) == pytest.approx(-25.0)  # net exporter


# ---------- saving vs Flexible --------------------------------------------

def test_saving_vs_flexible():
    # Flexible flat rate 28p over 4.3 kWh = 120.4p; actual import cost 46.4p.
    assert saving_vs_flexible_pence(28.0, 4.3, 46.4) == pytest.approx(73.99, abs=0.02)


def test_saving_vs_flexible_none_without_rate():
    assert saving_vs_flexible_pence(None, 4.3, 46.4) is None


def test_saving_can_be_negative():
    # If the smart tariff cost more than Flexible would have, saving is negative.
    assert saving_vs_flexible_pence(10.0, 4.0, 60.0) == pytest.approx(-20.0)
