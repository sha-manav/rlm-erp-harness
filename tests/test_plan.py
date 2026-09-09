"""The task ledger (pure unit tests)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "harness"))

from lib.plan import Plan  # noqa: E402


def test_set_replaces_and_numbers_from_one():
    plan = Plan().set(["read instruction", "price vendors", "create POs"])
    assert [i["id"] for i in plan.items] == [1, 2, 3]
    assert all(i["status"] == "todo" for i in plan.items)
    plan.set(["only this"])
    assert len(plan) == 1 and plan.items[0]["id"] == 1


def test_add_returns_the_new_id():
    plan = Plan().set(["a"])
    assert plan.add("b") == 2
    assert plan.items[1]["text"] == "b"


def test_update_sets_status_and_note():
    plan = Plan().set(["a", "b"])
    plan.update(2, "doing")
    assert plan.items[1]["status"] == "doing"
    plan.update(2, "done", "3 POs confirmed")
    assert plan.items[1]["status"] == "done"
    assert plan.items[1]["note"] == "3 POs confirmed"


def test_update_rejects_unknown_status_and_id():
    plan = Plan().set(["a"])
    with pytest.raises(ValueError):
        plan.update(1, "finished")
    with pytest.raises(KeyError):
        plan.update(9, "done")


def test_summary_leads_with_unfinished_work():
    plan = Plan().set(["a", "b", "c"])
    plan.update(1, "done")
    plan.update(2, "blocked", "vendor has no stock")
    summary = plan.summary()
    assert summary.splitlines()[0] == "1/3 done"
    assert "[blocked] 2. b — vendor has no stock" in summary
    assert "[todo] 3. c" in summary
    assert "1. a" not in summary          # completed work is collapsed to the count


def test_summary_is_bounded_for_reinjection():
    plan = Plan().set([f"step {i}" for i in range(50)])
    summary = plan.summary()
    assert len(summary.splitlines()) <= 10
    assert "(+42 more open)" in summary


def test_summary_on_an_empty_plan_says_what_to_do():
    assert "plan.set" in Plan().summary()


def test_completion_helpers():
    plan = Plan().set(["a", "b"])
    assert not plan.is_complete()
    assert len(plan.open_items()) == 2
    plan.update(1, "done")
    plan.update(2, "done")
    assert plan.is_complete()
    assert plan.open_items() == []
    assert not Plan().is_complete()      # an empty plan is not a complete one


def test_show_renders_a_table():
    text = str(Plan().set(["read instruction"]).show())
    assert "plan" in text and "read instruction" in text and "todo" in text


def test_objective_is_kept_and_echoed_in_the_summary():
    from lib.plan import Plan

    plan = Plan().set(["a", "b"], objective="keep as much shared workcenter capacity open as possible")
    assert plan.objective.startswith("keep as much")
    assert "objective: keep as much" in plan.summary()
    plan.update(1, "done")
    assert plan.summary().splitlines()[1].startswith("objective:")
