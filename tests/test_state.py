"""Read-only SQL and snapshots, against a live dev container."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "harness"))

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def kernel(dev_container_name):
    """These modules talk to Postgres on the container's loopback, so they must run there."""
    from harness.container import DockerContainer, Kernel

    container = DockerContainer(dev_container_name)
    kern = Kernel(container, port=8810, lib_modules=["erp", "db", "state", "check", "plan", "brief"])
    kern.start(env={
        "ODOO_URL": "http://127.0.0.1:8069", "ODOO_DB": "bench", "ODOO_USER": "admin",
        "PGHOST": "127.0.0.1", "PGPORT": "5432", "PGUSER": "odoo",
        "PGPASSWORD": "odoo", "PGDATABASE": "bench",
    })
    # Leftover confirmed orders from other modules would trip the one-PO-per-offer guard
    # inside this module's rehearsal plans.
    kern.run("""
for po in erp.search_read('purchase.order', [('state', 'in', ('purchase', 'sent', 'to approve'))], ['id'], limit=200):
    erp.cancel('purchase.order', po['id'])
""", timeout=120)
    yield kern
    kern.run("state.drop('t_snap')", timeout=60)
    kern.stop()


def run(kernel, code, timeout=120):
    reply = kernel.run(code, timeout=timeout)
    assert reply["ok"], reply["stderr"]
    return reply["stdout"]


# -- db.py --------------------------------------------------------------------
def test_select_returns_rows(kernel):
    out = run(kernel, "print(db.sql('select count(*) as n from res_partner'))")
    assert "n" in out


def test_writes_are_refused_before_the_round_trip(kernel):
    out = run(kernel, """
try:
    db.sql("delete from res_partner where id = 999999")
    print("NOT REFUSED")
except Exception as exc:
    print(type(exc).__name__, exc)
""")
    assert "SqlRefused" in out and "reads only" in out


def test_postgres_itself_refuses_a_write_that_slips_past_the_prefix_check(kernel):
    """The guarantee is the role's grants, not the string check in front of them."""
    out = run(kernel, """
try:
    db.sql("with x as (delete from res_partner where id=999999 returning 1) select * from x")
    print("NOT REFUSED")
except Exception as exc:
    print(type(exc).__name__, str(exc)[:120])
""")
    assert "NOT REFUSED" not in out
    assert "permission denied" in out.lower() or "read-only" in out.lower()


def test_limit_is_added_when_absent(kernel):
    out = run(kernel, "print(len(db.sql('select id from res_partner', limit=3).all()))")
    assert out.strip() == "3"


def test_explicit_limit_is_respected(kernel):
    out = run(kernel, "print(len(db.sql('select id from res_partner limit 2').all()))")
    assert out.strip() == "2"


# -- state.py -----------------------------------------------------------------
def test_snapshot_is_reachable_over_rpc_and_isolated(kernel):
    """A clone the agent can rehearse against, whose changes do not reach main."""
    out = run(kernel, """
state.snapshot('t_snap')
clone = erp.on('t_snap')
print('rpc ok:', bool(clone.search_read('res.company', [], ['name'], limit=1)))
before_main = erp.count('purchase.order')
before_clone = clone.count('purchase.order')
offer = erp.suppliers().all()[0]
po = clone.create_po(offer['vendor_id'], [(offer['product_id'], max(offer['min_qty'], 1))])
clone.confirm_po(po)
print('clone grew:', clone.count('purchase.order') == before_clone + 1)
print('main unchanged:', erp.count('purchase.order') == before_main)
""", timeout=180)
    assert "rpc ok: True" in out
    assert "clone grew: True" in out
    assert "main unchanged: True" in out


def test_diff_reports_the_mutation(kernel):
    out = run(kernel, "print(state.diff('bench', 't_snap'))", timeout=180)
    assert "purchase.order" in out


def test_list_and_drop(kernel):
    assert "t_snap" in run(kernel, "print(state.list())", timeout=60)
    run(kernel, "state.drop('t_snap')", timeout=120)
    assert "t_snap" not in run(kernel, "print(state.list())", timeout=60)


def test_a_bad_snapshot_name_is_refused(kernel):
    out = run(kernel, """
try:
    state.snapshot('bench"; drop database bench; --')
    print("NOT REFUSED")
except ValueError as exc:
    print("refused:", exc)
""")
    assert "refused:" in out and "NOT REFUSED" not in out


# -- brief.py -----------------------------------------------------------------
def test_briefing_renders_within_budget_without_raising(kernel):
    """Front-loads what every plan starts from; must never sink a trial and must stay small."""
    out = run(kernel, """
text = brief()
print(len(text))
print('Vendor offers' in text, 'Products' in text, 'Bills of materials' in text)
""", timeout=180)
    lines = out.strip().splitlines()
    from lib.brief import CHAR_BUDGET
    assert int(lines[0]) <= CHAR_BUDGET + 100, f"briefing is {lines[0]} chars, over budget"
    assert lines[1] == "True True True"


def test_rehearse_runs_the_plan_on_a_clone_and_returns_checks(kernel):
    """One call for what the contract used to ask the agent to orchestrate by hand."""
    out = run(kernel, """
before = erp.count('purchase.order')
def plan_fn(client):
    o = client.suppliers().all()[0]
    po = client.create_po(o['vendor_id'], [(o['product_id'], max(o['min_qty'], 1))],
                          date_planned='2026-12-01 08:00:00')
    client.confirm_po(po)
table = state.rehearse(plan_fn)
print('rows:', len(table.all()) > 0)
print('main unchanged:', erp.count('purchase.order') == before)
print('clone dropped:', 'rehearsal' not in [r['database'] for r in state.list().all()])
""", timeout=240)
    assert "rows: True" in out
    assert "main unchanged: True" in out
    assert "clone dropped: True" in out


def test_rehearse_flags_a_plan_that_writes_to_main(kernel):
    """The likely mistake: plan_fn ignores `client` and calls the global `erp`."""
    out = run(kernel, """
def leaky_plan(client):
    o = erp.suppliers().all()[0]                       # uses erp, not client
    po = erp.create_po(o['vendor_id'], [(o['product_id'], max(o['min_qty'], 1))],
                       date_planned='2026-12-01 08:00:00')
    erp.cancel('purchase.order', po)                   # tidy, but the write happened
table = state.rehearse(leaky_plan)
row = next(r for r in table.all() if r['check'] == 'rehearsal_isolation')
print(row['status'], '|', row['evidence'][:60])
""", timeout=240)
    assert out.startswith("FAIL"), out
    assert "MAIN database" in out
