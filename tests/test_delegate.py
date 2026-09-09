"""Delegation: the read-only sandbox, the sub-loop, and the accounting."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from harness.delegate import Delegator, clip_report, filter_library_docs  # noqa: E402
from harness.lib.delegate import (  # noqa: E402
    DELEGATE_SENTINEL, MAX_REPORT_CHARS, SUB_NAMESPACE_SETUP, ErpReadOnly, _ReadOnlyModule,
)
from harness.llm import Usage  # noqa: E402
from harness.loop import Loop, Trajectory  # noqa: E402
from tests.test_loop import FakeLLM, FakeReply  # noqa: E402


# -- the read-only client ---------------------------------------------------
class FakeErp:
    db = "bench"

    def __init__(self):
        self.write_log = []
        self.calls = []

    def search_read(self, model, domain, fields, limit=80, order=None):
        self.calls.append(("search_read", model))
        return [{"id": 1, "name": "S00001"}]

    def suppliers(self, product_ids=None):
        return "offers"

    def cheapest_buy(self, product_id, qty, need_by=None, order_date=None):
        return f"split {product_id} {qty}"

    def create_po(self, vendor_id, lines, **kw):
        self.write_log.append(("create_po",))
        return 99

    def confirm_po(self, po_id):
        self.write_log.append(("confirm_po",))

    def call(self, model, method, args=(), kwargs=None):
        self.calls.append((method, model))
        if method not in ("read", "search", "search_read", "search_count", "fields_get"):
            self.write_log.append((model, method))
        return "called"

    def on(self, db):
        other = FakeErp()
        other.db = db
        return other


def test_reads_and_planning_helpers_pass_through():
    ro = ErpReadOnly(FakeErp())
    assert ro.search_read("sale.order", [], ["name"])[0]["name"] == "S00001"
    assert ro.suppliers() == "offers"
    assert ro.cheapest_buy(7, 40, "2026-09-20") == "split 7 40"
    assert ro.db == "bench"
    assert ro.write_log == []


def test_every_write_helper_raises_permission_error():
    real = FakeErp()
    ro = ErpReadOnly(real)
    with pytest.raises(PermissionError) as excinfo:
        ro.create_po(1, [(2, 3)])
    assert "read-only" in str(excinfo.value)
    with pytest.raises(PermissionError):
        ro.confirm_po(5)
    assert real.write_log == [], "nothing reached the real client"


def test_raw_call_allows_reads_and_refuses_writes():
    real = FakeErp()
    ro = ErpReadOnly(real)
    assert ro.call("sale.order", "search_read", [[]], {"fields": ["name"]}) == "called"
    with pytest.raises(PermissionError):
        ro.call("sale.order", "action_confirm", [[1]])
    with pytest.raises(PermissionError):
        ro.call("purchase.order", "write", [[1], {"origin": "x"}])
    assert real.write_log == []


def test_snapshot_clients_are_read_only_too():
    ro = ErpReadOnly(FakeErp())
    other = ro.on("bench_s1")
    assert isinstance(other, ErpReadOnly) and other.db == "bench_s1"
    with pytest.raises(PermissionError):
        other.create_po(1, [])


def test_client_cannot_be_reassigned_or_unwrapped():
    ro = ErpReadOnly(FakeErp())
    with pytest.raises(PermissionError):
        ro.write_log = []
    with pytest.raises(AttributeError):
        ro.no_such_method()
    assert ro.write_log == []          # a copy of the real log, never the list itself


def test_read_only_module_refuses_named_attributes():
    import types
    mod = types.SimpleNamespace(all=lambda c=None: "table", register=lambda r: r, __name__="check")
    ro = _ReadOnlyModule(mod, ("register", "clear"))
    assert ro.all() == "table"
    with pytest.raises(PermissionError):
        ro.register("rule")


# -- the sub-loop ------------------------------------------------------------
class FakeKernel:
    """Records namespaces and code; `python` returns whatever `outputs` scripts."""

    def __init__(self, outputs=None):
        self.outputs = list(outputs or [])
        self.runs: list[tuple[str, str]] = []
        self.resets: list[str] = []

    def reset(self, ns="root"):
        self.resets.append(ns)
        return {"ok": True}

    def run(self, code, timeout=120, ns="root"):
        self.runs.append((ns, code))
        if code == SUB_NAMESPACE_SETUP:
            return {"ok": True, "stdout": "", "stderr": ""}
        out = self.outputs.pop(0) if self.outputs else "ok"
        return {"ok": True, "stdout": out, "stderr": ""}


def make_delegator(tmp_path, replies, **kw):
    llm = FakeLLM(replies)
    trajectory = Trajectory(tmp_path / "trajectory.jsonl")
    kernel = FakeKernel(kw.pop("outputs", None))
    delegator = Delegator(llm=llm, kernel=kernel, trajectory=trajectory,
                          system_prompt="SUB SYSTEM", token_cap=10_000_000, **kw)
    return delegator, llm, kernel, trajectory


def events(tmp_path):
    return [json.loads(l) for l in (tmp_path / "trajectory.jsonl").read_text().splitlines()]


def test_sub_agent_answers_in_its_own_namespace_and_logs_with_agent_id(tmp_path):
    replies = [FakeReply("python", {"code": "print(erp.suppliers())"}),
               FakeReply(text="Vendor A: 40 @ 3.10, arrives 2026-09-15. Recommend A.")]
    delegator, llm, kernel, traj = make_delegator(tmp_path, replies, outputs=["offers"])
    report = delegator.run("Sourcing analysis for product 7, 40 units by 2026-09-20")
    traj.close()

    assert report.startswith("[sub-1 report — 2 steps]")
    assert "Recommend A" in report
    assert delegator.count == 1
    assert kernel.resets == ["sub-1"]
    assert kernel.runs[0] == ("sub-1", SUB_NAMESPACE_SETUP), "namespace locked down first"
    assert kernel.runs[1][0] == "sub-1", "the python tool runs in the sub namespace"

    evs = events(tmp_path)
    sub = [e for e in evs if e["agent_id"] == "sub-1"]
    assert {e["role"] for e in sub} >= {"system", "user", "assistant", "tool"}
    assert any(e["role"] == "assistant" and e.get("usage") for e in sub), "sub usage is logged"
    summary = [e for e in evs if e["agent_id"] == "root" and e.get("kind") == "delegation"]
    assert len(summary) == 1 and summary[0]["usage"]["input"] == 200
    assert delegator.total_usage["input"] == 200
    assert llm.total.input == 200, "the sub-agent's tokens count in the shared total"


def test_sub_agent_cannot_finish_and_has_only_python(tmp_path):
    replies = [FakeReply("finish", {"summary": "done"}),
               FakeReply(text="report")]
    delegator, _, _, traj = make_delegator(tmp_path, replies)
    delegator.run("anything")
    traj.close()
    tool_events = [e for e in events(tmp_path) if e["role"] == "tool"]
    assert "only the python tool" in tool_events[0]["output"]


def test_step_cap_returns_a_partial_report(tmp_path):
    replies = [FakeReply("python", {"code": f"print({i})"}, text=f"thinking {i}") for i in range(10)]
    delegator, _, _, traj = make_delegator(tmp_path, replies)
    report = delegator.run("long task", max_steps=3)
    traj.close()
    assert "[sub-agent stopped: step_cap after 3 steps]" in report
    assert "thinking 2" in report


def test_max_steps_is_bounded_by_the_ceiling(tmp_path):
    replies = [FakeReply("python", {"code": f"print({i})"}) for i in range(100)]
    delegator, _, _, traj = make_delegator(tmp_path, replies, max_sub_steps=5)
    report = delegator.run("long task", max_steps=500)
    traj.close()
    assert "step_cap after 5 steps" in report


def test_report_is_clipped_to_400_tokens():
    long = "x" * (MAX_REPORT_CHARS * 3)
    clipped = clip_report(long)
    assert len(clipped) <= MAX_REPORT_CHARS + 10
    assert clipped.endswith("characters]")
    assert clip_report("short") == "short"


def test_sentinel_in_python_output_runs_the_sub_agent_and_delivers(tmp_path):
    replies = [FakeReply(text="the report")]
    delegator, _, _, traj = make_delegator(tmp_path, replies)
    delivered = []
    request = json.dumps({"n": 1, "task": "feasibility of S00003", "max_steps": 10})
    output = f"before\n{DELEGATE_SENTINEL}{request}\nafter"
    resolved = delegator.resolve_output(output, lambda n, r: delivered.append((n, r)))
    traj.close()
    assert resolved.splitlines()[0] == "before" and resolved.splitlines()[-1] == "after"
    assert "the report" in resolved and DELEGATE_SENTINEL not in resolved
    assert delivered and delivered[0][0] == 1 and "the report" in delivered[0][1]


def test_empty_task_is_refused_without_a_model_call(tmp_path):
    delegator, llm, _, traj = make_delegator(tmp_path, [FakeReply(text="x")])
    assert "empty" in delegator.run("   ")
    traj.close()
    assert llm.calls == 0


def test_loop_answer_ends_only_when_asked(tmp_path):
    llm = FakeLLM([FakeReply(text="plain answer"), FakeReply("finish", {"summary": "s"})])
    traj = Trajectory(tmp_path / "t.jsonl")
    loop = Loop(llm=llm, tools=[], system_prompt="S", instruction="I",
                run_tool=lambda n, a: "__ERP_HARNESS_FINISHED__", trajectory=traj, answer_ends=True)
    result = loop.run()
    traj.close()
    assert result.terminal_reason == "answer" and result.summary == "plain answer"
    assert llm.calls == 1


def test_library_docs_are_filtered_to_loaded_modules():
    docs = "# Preloaded library\n\nintro\n\n## `erp`\n\n- a\n\n## `state`\n\n- b\n\n## `delegate`\n\n- c\n"
    kept = filter_library_docs(docs, ["erp", "delegate"])
    assert "## `erp`" in kept and "## `delegate`" in kept
    assert "## `state`" not in kept and "- b" not in kept
    assert kept.startswith("# Preloaded library")


def test_configs_differ_by_delegation_only():
    import yaml
    from scripts.run import resolve_config
    configs = yaml.safe_load((REPO_ROOT / "configs/configs.yaml").read_text())
    full, minus = resolve_config(configs, "C_full"), resolve_config(configs, "C_minus_del")
    assert "delegate" in full["tools"] and "delegate" not in minus["tools"]
    assert "delegate" in full["lib"] and "delegate" not in minus["lib"]
    assert "playbook_delegate" in full["prompts"] and "playbook_delegate" not in minus["prompts"]
    same = {k for k in full if k not in ("tools", "lib", "prompts", "id")}
    assert all(full[k] == minus[k] for k in same), "every other key is identical"


def test_ingest_counts_distinct_sub_agents(tmp_path):
    from scripts.ingest_harbor import parse_our_trajectory
    path = tmp_path / "trajectory.jsonl"
    lines = [
        {"t": 1, "agent_id": "root", "role": "assistant", "usage": {"input": 10, "cached": 0, "output": 1}},
        {"t": 1, "agent_id": "sub-1", "role": "assistant", "usage": {"input": 5, "cached": 0, "output": 1}},
        {"t": 2, "agent_id": "sub-1", "role": "assistant", "usage": {"input": 5, "cached": 0, "output": 1}},
        {"t": 1, "agent_id": "sub-2", "role": "assistant", "usage": {"input": 5, "cached": 0, "output": 1}},
        {"t": 2, "agent_id": "root", "role": "assistant", "usage": {"input": 10, "cached": 0, "output": 1}},
    ]
    path.write_text("\n".join(json.dumps(l) for l in lines) + "\n")
    events_, totals, steps, delegations = parse_our_trajectory(path)
    assert delegations == 2 and steps == 2 and totals["input"] == 35


# -- live: a real kernel namespace on the dev container -----------------------
@pytest.fixture(scope="module")
def sub_kernel(container):
    from harness.container import Kernel
    CONTAINER_ENV = {"ODOO_URL": "http://127.0.0.1:8069", "ODOO_DB": "bench", "ODOO_USER": "admin", "PGHOST": "127.0.0.1", "PGPORT": "5432", "PGUSER": "odoo", "PGPASSWORD": "odoo", "PGDATABASE": "bench"}

    kern = Kernel(container, port=8821, lib_modules=["erp", "db", "state", "check", "plan", "brief", "delegate"])
    kern.start(env=CONTAINER_ENV)
    yield kern
    kern.stop()


@pytest.mark.live
def test_live_sub_namespace_is_read_only_and_root_is_not(sub_kernel):
    assert sub_kernel.run("print(erp.suppliers()[:1])", ns="root")["ok"]
    prep = sub_kernel.run(SUB_NAMESPACE_SETUP, ns="sub-1")
    assert prep["ok"], prep["stderr"]

    reads = sub_kernel.run(
        "print(type(erp).__name__); print(len(erp.suppliers().all()) > 0); "
        "print(len(check.all(erp).all()) > 5); print(len(db.sql('select id from res_partner').all()) > 0)",
        ns="sub-1", timeout=180)
    assert reads["ok"], reads["stderr"]
    assert reads["stdout"].split() == ["ErpReadOnly", "True", "True", "True"]

    write = sub_kernel.run(
        "o = erp.suppliers().all()[0]\n"
        "erp.create_po(o['vendor_id'], [(o['product_id'], o['min_qty'] or 1)])", ns="sub-1")
    assert write["ok"] is False and "PermissionError" in write["stderr"], write
    raw = sub_kernel.run("erp.call('sale.order', 'action_confirm', [[1]])", ns="sub-1")
    assert "PermissionError" in raw["stderr"]
    rule = sub_kernel.run("check.register(Rule('r', 'd', lambda c: (True, '')))", ns="sub-1")
    assert "PermissionError" in rule["stderr"]
    missing = sub_kernel.run("print([n for n in ('state', 'finish', 'Erp', 'delegate') if n in globals()])",
                             ns="sub-1")
    assert missing["stdout"].strip() == "[]"

    root = sub_kernel.run("print(type(erp).__name__, len(check.registered()), 'delegate' in globals())",
                          ns="root")
    assert root["stdout"].split() == ["Erp", "0", "True"], "the root namespace is untouched"


@pytest.mark.live
def test_live_delegate_function_prints_a_request_and_receives_the_report(sub_kernel):
    out = sub_kernel.run("print(delegate('what is on hand?', max_steps=5))", ns="root")
    assert out["ok"], out["stderr"]
    line = next(l for l in out["stdout"].splitlines() if l.startswith(DELEGATE_SENTINEL))
    request = json.loads(line[len(DELEGATE_SENTINEL):])
    assert request["task"] == "what is on hand?" and request["max_steps"] == 5
    n = request["n"]
    deliver = sub_kernel.run(f"from lib.delegate import _deliver; _deliver({n}, 'stock is fine')\n"
                             f"print(delegate.reports[{n}])", ns="root")
    assert deliver["stdout"].strip() == "stock is fine"


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("ERP_LIVE_LLM"), reason="set ERP_LIVE_LLM=1 to spend a few cents")
def test_live_sub_agent_completes_a_sourcing_analysis(sub_kernel, tmp_path):
    """A real sub-agent on the dev task: it must use the read-only client, produce a
    report naming a vendor and an arrival date, and never write."""
    import logging

    import yaml

    PROMPT_DIR = REPO_ROOT / "harness" / "prompts"
    from harness.llm import LLM
    from scripts.run import load_env

    env = load_env(REPO_ROOT / ".env")
    key = env.get("MODEL_BIG_API_KEY") or env.get("OPENROUTER_API_KEY") or os.environ.get("OPENROUTER_API_KEY")
    assert key, "no API key"
    spec = yaml.safe_load((REPO_ROOT / "configs/models.yaml").read_text())["big"]
    llm = LLM(spec, key, logger=logging.getLogger("live"))

    parts = []
    for name in ("delegate_contract", "playbook", "schema_card", "library_docs"):
        text = (PROMPT_DIR / f"{name}.md").read_text().strip()
        if name == "library_docs":
            text = filter_library_docs(text, ["erp", "db", "check", "plan", "brief", "fmt"])
        parts.append(text)
    system_prompt = "\n\n---\n\n".join(parts) + (
        "\n\n---\n\nAvailable in your Python kernel right now: **brief, check, db, erp, fmt, plan** "
        "(erp is read-only). Nothing else is loaded.")

    offer = sub_kernel.run(
        "o = erp.suppliers().all()[0]; print(o['product_id'], o['product'])", ns="root")["stdout"].split(maxsplit=1)
    product_id, product = int(offer[0]), offer[1].strip()
    before = sub_kernel.run("print(len(erp.write_log))", ns="root")["stdout"].strip()

    trajectory = Trajectory(tmp_path / "trajectory.jsonl")
    delegator = Delegator(llm=llm, kernel=sub_kernel, trajectory=trajectory, system_prompt=system_prompt,
                          token_cap=3_000_000, kernel_timeout=300, logger=logging.getLogger("live"))
    report = delegator.run(
        f"Sourcing analysis for product id {product_id} ({product}): we need 20 units on hand by "
        "30 days from today. List every vendor offer (vendor, MOQ, tier price, lead time in days, "
        "arrival date if ordered today), then give the min-cost feasible purchase split "
        "(erp.cheapest_buy) and its total. Return: the recommended vendor(s), quantities, "
        "unit prices, date_planned to use, and the total spend.", max_steps=15)
    trajectory.close()

    after = sub_kernel.run("print(len(erp.write_log))", ns="root")["stdout"].strip()
    record = delegator.records[0]
    print("\nREPORT:\n", report, "\nRECORD:", record)
    (REPO_ROOT / "runs" / "p3.4_live_delegate.json").write_text(json.dumps(
        {"report": report, "record": record, "product": product}, indent=2))

    assert record["terminal"] == "answer", record
    assert before == after, "the sub-agent wrote to the main database"
    assert len(report) <= MAX_REPORT_CHARS + 80
    assert any(token in report.lower() for token in ("vendor", "moq", "price"))
    sub_events = [json.loads(l) for l in (tmp_path / "trajectory.jsonl").read_text().splitlines()
                  if '"sub-1"' in l]
    assert sub_events and all(e["agent_id"] == "sub-1" for e in sub_events)


# -- C_full_audit: the mandatory audit delegation --------------------------------
def _resolved(name):
    import yaml
    from scripts.run import resolve_config
    return resolve_config(yaml.safe_load((REPO_ROOT / "configs/configs.yaml").read_text()), name)


def test_c_full_audit_differs_from_c_full_only_by_the_audit_requirement():
    full, audit = _resolved("C_full"), _resolved("C_full_audit")
    assert audit["finish_requires_delegation"] is True and not full.get("finish_requires_delegation")
    assert audit["prompts"] == ["contract_audit" if p == "contract" else p for p in full["prompts"]]
    same = {k for k in full if k not in ("prompts", "finish_requires_delegation", "id")}
    assert same == {k for k in audit if k not in ("prompts", "finish_requires_delegation", "id")}
    assert all(full[k] == audit[k] for k in same), "tools, lib, caps and delegation settings are identical"


def test_contract_audit_differs_from_contract_only_in_step_4():
    import difflib
    prompts = REPO_ROOT / "harness" / "prompts"
    a = (prompts / "contract.md").read_text().splitlines()
    b = (prompts / "contract_audit.md").read_text().splitlines()
    changed = [l for l in difflib.unified_diff(a, b, lineterm="", n=0) if l[:1] in "+-" and not l.startswith(("+++", "---"))]
    assert changed, "the audit contract must differ"
    removed = [l[1:] for l in changed if l.startswith("-")]
    assert all("**4." in l or l.startswith("call `finish") or "harness telling you" in l for l in removed), removed
    # Every removed line belongs to step 4's paragraph; nothing before step 4 changed.
    step4 = a.index(next(l for l in a if l.startswith("**4.")))
    assert a[:step4] == b[:step4]
    assert "delegate(" in "\n".join(b) and "refuses until an audit has run" in "\n".join(b)


def test_c_minus_del_equals_c_full_minus_delegation_exactly():
    full, minus = _resolved("C_full"), _resolved("C_minus_del")
    assert minus["tools"] == [t for t in full["tools"] if t != "delegate"]
    assert minus["lib"] == [m for m in full["lib"] if m != "delegate"]
    assert minus["prompts"] == [p for p in full["prompts"] if p != "playbook_delegate"]
    same = {k for k in full if k not in ("tools", "lib", "prompts", "id")}
    assert all(full[k] == minus[k] for k in same)


def test_finish_refuses_until_a_delegation_has_run(monkeypatch, tmp_path):
    from harness.lib import check as check_module
    from harness.lib import delegate as delegate_module
    from harness.lib import finish as finish_module
    from harness.lib.fmt import Table

    monkeypatch.setattr(check_module, "all", lambda client=None: Table([], ["check", "status", "hard", "evidence"]))
    monkeypatch.setattr(finish_module, "_main_write_count", lambda: 1)
    monkeypatch.setattr(finish_module, "SUMMARY_PATH", str(tmp_path / "summary.md"))
    monkeypatch.setattr(delegate_module, "reports", {})
    finish_module.reset()

    # Without the requirement a clean state finishes at once.
    assert finish_module.FINISH_SENTINEL in finish_module.finish("done", gate=True)

    finish_module.reset()
    finish_module.require_delegation(True)
    first = finish_module.finish("done", gate=True)
    assert "no audit has run" in first and finish_module.FINISH_SENTINEL not in first
    second = finish_module.finish("done", gate=True)
    assert "attempt 2/3" in second and finish_module.FINISH_SENTINEL not in second
    # A report arriving lifts the refusal.
    delegate_module._deliver(1, "clean")
    assert finish_module.FINISH_SENTINEL in finish_module.finish("done", gate=True)

    # Never delegating still terminates after the usual three attempts.
    finish_module.reset(); finish_module.require_delegation(True)
    monkeypatch.setattr(delegate_module, "reports", {})
    finish_module.finish("x", gate=True); finish_module.finish("x", gate=True)
    assert finish_module.FINISH_SENTINEL in finish_module.finish("x", gate=True)
    finish_module.reset()
