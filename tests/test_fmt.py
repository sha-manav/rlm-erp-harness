"""Table rendering and paging (pure unit tests, no container)."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "harness"))

from lib.fmt import PageStore, Table  # noqa: E402


def rows(n: int) -> list[dict]:
    return [{"id": i, "product": f"part-{i}", "qty": i * 1.5} for i in range(n)]


def test_small_table_shows_every_row():
    text = str(Table(rows(3)))
    assert "part-0" in text and "part-2" in text
    assert "more rows" not in text


def test_large_table_is_capped_and_says_how_to_see_the_rest():
    table = Table(rows(120))
    text = str(table)
    assert text.count("part-") == 40
    assert "(80 more rows; use .all() for every row)" in text
    assert len(table.all()) == 120
    assert len(table) == 120


def test_columns_follow_first_seen_order_unless_given():
    assert Table([{"b": 1, "a": 2}]).cols == ["b", "a"]
    assert Table([{"b": 1, "a": 2}], cols=["a"]).cols == ["a"]


def test_odoo_shapes_render_readably():
    text = str(Table([{
        "partner": [7, "Axis Assemblies"],   # many2one read
        "note": False,                        # Odoo's empty
        "ok": True,
        "price": 317.80,
        "tags": ["a", "b"],
    }]))
    assert "Axis Assemblies" in text
    assert "yes" in text
    assert "317.8" in text
    assert "a, b" in text
    # False renders as empty, never as the word "False"
    assert "False" not in text


def test_long_cells_are_truncated_not_wrapped():
    text = str(Table([{"note": "x" * 500}]))
    assert "…" in text
    assert max(len(line) for line in text.splitlines()) < 120


def test_empty_table_is_explicit():
    assert str(Table([], cols=["a"])) == "(0 rows)"
    assert str(Table([], cols=["a"], title="stock")) == "stock (0 rows)"
    assert not Table([])


def test_helpers_operate_on_the_full_data():
    table = Table(rows(10))
    assert table.total("id") == 45
    assert len(table.where(lambda r: r["id"] > 7)) == 2
    assert table.sort("id", reverse=True)[0]["id"] == 9
    assert table.column("id")[:3] == [0, 1, 2]


def test_short_output_passes_through_unpaged():
    store = PageStore()
    assert store.capture("small") == "small"
    assert store.handles() == []


def test_long_output_is_stored_and_first_page_shown():
    store = PageStore(threshold=100, page_size=50)
    shown = store.capture("y" * 500)
    assert shown.startswith("y" * 50)
    assert 'show("h1", 2)' in shown
    assert store.handles() == ["h1"]
    # The model paid for ~one page, not for 500 characters.
    assert len(shown) < 250


def test_pages_are_contiguous_and_complete():
    store = PageStore(threshold=10, page_size=10)
    original = "".join(str(i % 10) for i in range(95))
    store.capture(original)
    rebuilt = ""
    for page in range(1, store.n_pages("h1") + 1):
        body = store.show("h1", page)
        rebuilt += body.split("\n[page")[0]
    assert rebuilt == original


def test_last_page_says_it_is_the_end():
    store = PageStore(threshold=10, page_size=10)
    store.capture("z" * 25)
    assert "end of output" in store.show("h1", 3)


def test_bad_handle_and_page_are_reported_not_raised():
    store = PageStore(threshold=10, page_size=10)
    store.capture("z" * 25)
    assert "no such handle" in store.show("nope", 1)
    assert "has 3 page(s)" in store.show("h1", 9)
    assert "has 3 page(s)" in store.show("h1", 0)


def test_handles_increment_across_captures():
    store = PageStore(threshold=5, page_size=5)
    store.capture("a" * 20)
    store.capture("b" * 20)
    assert store.handles() == ["h1", "h2"]
    assert store.show("h1", 1).startswith("aaaaa")
    assert store.show("h2", 1).startswith("bbbbb")
