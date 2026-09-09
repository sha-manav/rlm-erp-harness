"""The purchase-split solver, on the numbers from the 2109 trial that lost on spend alone."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "harness"))

from lib.erp import _cheapest_split, _max_qty_from_notes  # noqa: E402


def offers(rows):
    return [{"vendor_id": i, "vendor": f"v{i}", "min_qty": mn, "price": p, "delay_days": d,
             "max_qty": mx} for i, (mn, p, d, mx) in enumerate(rows)]


def test_harmonic_drive_split_matches_the_reference():
    # The trial bought 66 @ 651.70 + 7 @ 941.05; the reference bought 63 + 10 @ 801.63.
    o = offers([(4, 941.05, 2, 9), (22, 721.76, 4, 29), (36, 651.7, 4, 66), (10, 801.63, 4, 58)])
    cost, lines = _cheapest_split(o, 73)
    assert sorted((l["vendor_id"], q) for l, q in lines) == [(2, 63), (3, 10)]
    assert abs(cost - (63 * 651.7 + 10 * 801.63)) < 0.01


def test_controller_cabinet_split_buys_exactly_what_is_needed():
    o = offers([(11, 2391.49, 2, 30), (18, 2231.49, 4, 18), (27, 1839.0, 4, 32), (42, 1919.66, 4, 52)])
    cost, lines = _cheapest_split(o, 73)
    assert sum(q for _, q in lines) == 73
    assert sorted((l["vendor_id"], q) for l, q in lines) == [(2, 31), (3, 42)]


def test_minimum_quantities_are_honoured_even_when_they_overshoot():
    o = offers([(50, 10.0, 1, None)])
    cost, lines = _cheapest_split(o, 5)
    assert lines == [(o[0], 50)] and cost == 500.0


def test_no_combination_reaches_the_quantity():
    assert _cheapest_split(offers([(1, 1.0, 1, 3), (1, 1.0, 1, 3)]), 10) is None


def test_max_qty_is_read_from_the_vendor_note_for_that_product():
    note = "<p>Maximum order quantity for P18378A2D42-SPP-CDP-008: 43 units</p>"
    assert _max_qty_from_notes(note, "P18378A2D42-SPP-CDP-008") == 43
    assert _max_qty_from_notes(note, "OTHER-CODE") is None       # capped product named, not this one
    assert _max_qty_from_notes("Maximum order quantity: 12 units", "ANY") == 12
    assert _max_qty_from_notes("Reliable vendor, ships weekly", "ANY") is None
