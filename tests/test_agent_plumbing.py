"""The full ErpAgent path with a scripted model against live Odoo."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

pytestmark = pytest.mark.live

try:
    import harbor
    HARBOR = True
except ImportError:
    HARBOR = False


def _script(steps):
    """Build a fake LLM class replaying `steps`: (tool, args) or ('text', content)."""
    from harness.llm import Usage

    class Reply:
        def __init__(self, tool, args):
            if tool == "text":
                self.message = {"role": "assistant", "content": args}
            else:
                self.message = {"role": "assistant", "content": "", "tool_calls": [{
                    "id": f"call_{tool}", "type": "function",
                    "function": {"name": tool, "arguments": json.dumps(args)},
                }]}
            self.usage = Usage(input=200, cached=100, output=20, cost_usd=0.001)
            self.finish_reason = "stop" if tool == "text" else "tool_calls"
            self.provider = "Scripted"
            self.latency_s = 0.0
            self.raw = {}

        @property
        def tool_calls(self):
            return self.message.get("tool_calls") or []

        @property
        def text(self):
            return self.message.get("content") or ""

    class ScriptedLLM:
        def __init__(self, *_a, **_k):
            self.total = Usage()
            self.calls = 0
            self.providers = {}
            self.seen = []

        def complete(self, messages, tools=None):
            self.seen.append(messages[-1])
            tool, args = steps[min(self.calls, len(steps) - 1)]
            self.calls += 1
            reply = Reply(tool, args)
            self.total.add(reply.usage)
            return reply

    return ScriptedLLM


@pytest.mark.skipif(not HARBOR, reason="harness.agent imports harbor; run inside harbor's venv")
def test_full_agent_path_with_a_scripted_model(dev_container_name, tmp_path, monkeypatch):
    import harness.agent as agent_module
    from harness.container import DockerContainer

    seed_late = """
o = erp.suppliers().all()[0]
partner = erp.search_read('res.partner', [('customer_rank', '>', 0)], ['name'], limit=1)[0]['id']
so = erp.call('sale.order', 'create', [{'partner_id': partner, 'commitment_date': '2026-09-10 12:00:00',
      'order_line': [(0, 0, {'product_id': o['product_id'], 'product_uom_qty': 5})]}])
erp.call('sale.order', 'action_confirm', [[so]])
so_name = erp.get('sale.order', [so], ['name'])[0]['name']
# force=True: the write-time origin guard would refuse a PO landing after the order it feeds.
po = erp.create_po(o['vendor_id'], [(o['product_id'], max(o['min_qty'], 5))], date_planned='2026-09-25 12:00:00',
                   origin=so_name, force=True)
erp.confirm_po(po)
print('seeded late PO', po)
"""
    # A real repair: move the due date past the arrival the lead time implies.
    fix_late = """
erp.call('sale.order', 'write', [[so], {'commitment_date': '2026-10-30 12:00:00'}])
print('moved receipt earlier')
"""
    steps = [
        ("python", {"code": "plan.set(['read', 'act']); print(len(erp.suppliers()))"}),
        ("python", {"code": seed_late}),
        ("finish", {"summary": "done?"}),                 # must be REFUSED: receipt is late
        ("python", {"code": fix_late}),
        ("finish", {"summary": "receipt moved before the due date"}),   # must pass
    ]
    monkeypatch.setattr(agent_module, "LLM", _script(steps))
    monkeypatch.setenv("OPENROUTER_API_KEY", "scripted")

    # Start from no confirmed orders: other modules leave origin-less POs and forced
    # receipts behind, and this test's refusal/acceptance must be about ITS scenario.
    from harness.container import Kernel
    pre = Kernel(DockerContainer(dev_container_name), port=8812, lib_modules=["erp"])
    pre.start(env=agent_module.CONTAINER_ENV)
    try:
        pre.run("""
for po in erp.search_read('purchase.order', [('state','not in',('cancel','done'))], ['id'], limit=100):
    erp.cancel('purchase.order', po['id'])
for so in erp.search_read('sale.order', [('state','not in',('cancel',))], ['id'], limit=100):
    erp.cancel('sale.order', so['id'])
""", timeout=120)
    finally:
        pre.stop()

    agent = agent_module.ErpAgent(
        logs_dir=tmp_path, model_name="z-ai/glm-5.1", logger=logging.getLogger("t"),
        config="C_full", model_key="big")

    class Ctx:
        n_input_tokens = n_cache_tokens = n_output_tokens = cost_usd = metadata = None

    agent._run_sync("Do the obvious thing.", DockerContainer(dev_container_name), Ctx())

    result = json.loads((tmp_path / "result.json").read_text())
    assert result["terminal_reason"] == "finish", result
    assert result["steps"] == 5, "expected: seed, refused finish, fix, accepted finish"
    assert set(result["lib_active"]) >= {"erp", "check", "plan", "state", "db", "brief", "finish"}, result["lib_active"]
    assert result["tokens"]["input"] == 1000 and result["tokens"]["cached"] == 500

    events = [json.loads(l) for l in (tmp_path / "trajectory.jsonl").read_text().splitlines()]
    first_user = next(e for e in events if e["role"] == "user")
    assert "Environment at task start" in first_user["content"], "briefing was not injected"
    assert "Vendor offers" in first_user["content"]

    tool_outputs = [e["output"] for e in events if e["role"] == "tool"]
    assert tool_outputs[0].strip().isdigit(), tool_outputs[0]          # suppliers count printed
    assert "seeded late PO" in tool_outputs[1]
    assert "finish refused" in tool_outputs[2], tool_outputs[2]
    assert "timeline_feasible" in tool_outputs[2]
    assert "2026-09-25" in tool_outputs[2] and "2026-09-10" in tool_outputs[2]
    assert "Fix:" in tool_outputs[2], "the refusal must say how to repair it"
    assert "moved receipt earlier" in tool_outputs[3]
    assert "__ERP_HARNESS_FINISHED__" in tool_outputs[4], tool_outputs[4]
    assert "refused" not in tool_outputs[4]

    # Leave the shared dev container as we found it for the tests that run after this.
    from harness.container import Kernel
    kern = Kernel(DockerContainer(dev_container_name), port=8811, lib_modules=["erp"])
    kern.start(env=agent_module.CONTAINER_ENV)
    try:
        kern.run("""
for po in erp.search_read('purchase.order', [('state','=','purchase')], ['id'], limit=50):
    erp.cancel('purchase.order', po['id'])
for so in erp.search_read('sale.order', [('state','=','sale')], ['id'], limit=50):
    erp.cancel('sale.order', so['id'])
""", timeout=120)
    finally:
        kern.stop()
