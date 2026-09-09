"""Sub-agents (host side): a fresh Loop with a python-only tool in its own kernel
namespace, read-only erp, and a report capped at ~400 tokens. Shares the root's LLM totals.
"""

from __future__ import annotations

import json
import time
from typing import Callable

from harness.lib.delegate import DELEGATE_SENTINEL, DEFAULT_MAX_STEPS, MAX_REPORT_CHARS, SUB_NAMESPACE_SETUP
from harness.lib.fmt import PageStore
from harness.loop import Loop
from harness.tools import schemas_for

SUB_PAGE_HINT = ("no show tool here: filter rows or aggregate in the kernel and print "
                 "only what you need")


def filter_library_docs(text: str, active: list[str]) -> str:
    """Keep only the "## `module`" sections of library_docs.md whose module is loaded."""
    keep = set(active)
    out, keeping = [], True
    for line in text.splitlines():
        if line.startswith("## `") and line.rstrip().endswith("`"):
            keeping = line[4:-1] in keep
        if keeping:
            out.append(line)
    return "\n".join(out).rstrip()


def clip_report(text: str, limit: int = MAX_REPORT_CHARS) -> str:
    """At most ~400 tokens; a cut is marked so the root knows it happened."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 40].rstrip() + f"\n[report cut at {limit} characters]"


class Delegator:
    def __init__(
        self,
        llm,
        kernel,
        trajectory,
        system_prompt: str,
        token_cap: int,
        deadline: Callable[[], float] | None = None,
        kernel_timeout: int = 120,
        default_max_steps: int = DEFAULT_MAX_STEPS,
        max_sub_steps: int = 60,
        logger=None,
    ):
        self.llm = llm
        self.kernel = kernel
        self.trajectory = trajectory
        self.system_prompt = system_prompt
        self.token_cap = token_cap
        self.deadline = deadline          # seconds of wall-clock remaining, or None
        self.kernel_timeout = kernel_timeout
        self.default_max_steps = default_max_steps
        self.max_sub_steps = max_sub_steps
        self.logger = logger
        self.count = 0
        self.records: list[dict] = []

    # -- entry points ---------------------------------------------------------
    def run(self, task: str, max_steps: int | None = None, n: int | None = None) -> str:
        """Run one sub-agent to completion and return its report."""
        self.count += 1
        n = n or self.count
        agent_id = f"sub-{n}"
        steps = max(1, min(int(max_steps or self.default_max_steps), self.max_sub_steps))
        task = (task or "").strip()
        if not task:
            return "delegate: the task is empty; say what to investigate and what to return."

        # A fresh namespace, preloaded with the library, then locked down.
        self.kernel.reset(agent_id)
        prep = self.kernel.run(SUB_NAMESPACE_SETUP, timeout=60, ns=agent_id)
        if not prep.get("ok"):
            return f"delegate: could not prepare the sub-agent namespace: {prep.get('stderr', '')[-400:]}"

        remaining = self.deadline() if self.deadline else 0
        budget = max(60.0, remaining) if remaining else 0
        started = time.time()
        usage_before = _snapshot(self.llm.total)

        def run_tool(name: str, args: dict) -> str:
            if name != "python":
                return f"sub-agents have only the python tool (asked for {name!r})"
            reply = self.kernel.run(args.get("code", ""),
                                    timeout=int(args.get("timeout") or self.kernel_timeout),
                                    ns=agent_id)
            parts = [reply.get("stdout", "").rstrip(), reply.get("stderr", "").rstrip()]
            return "\n".join(p for p in parts if p) or "(no output)"

        sub = Loop(
            llm=self.llm,
            tools=schemas_for(["python"]),
            system_prompt=self.system_prompt,
            instruction=f"## Delegated task from the root agent\n\n{task}",
            run_tool=run_tool,
            trajectory=self.trajectory.for_agent(agent_id),
            step_cap=steps,
            token_cap=self.token_cap,     # shared with the root: the cap is per trial
            time_budget_s=budget,
            answer_ends=True,
            pages=PageStore(hint=SUB_PAGE_HINT),
            logger=self.logger,
        )
        result = sub.run()
        used = _delta(usage_before, self.llm.total)

        if result.terminal_reason == "answer":
            report = clip_report(result.summary)
        else:
            last = _last_assistant_text(sub.messages)
            report = clip_report(
                f"[sub-agent stopped: {result.terminal_reason} after {result.steps} steps] "
                + (last or "(no partial answer)"))

        record = {
            "agent_id": agent_id, "steps": result.steps, "terminal": result.terminal_reason,
            "usage": used, "elapsed_s": round(time.time() - started, 1),
            "report_chars": len(report),
        }
        self.records.append(record)
        self.trajectory.write(
            t=None, role="system", kind="delegation", content=json.dumps(record),
            tool="delegate", args={"task": task[:500], "max_steps": steps},
            output=report, usage=used, latency_s=record["elapsed_s"])
        if self.logger:
            self.logger.info("delegation %s: %s after %d steps, %d input tokens, $%.3f",
                             agent_id, result.terminal_reason, result.steps,
                             used["input"], used["cost_usd"])
        return f"[{agent_id} report — {result.steps} steps]\n{report}"

    def resolve_output(self, output: str, deliver: Callable[[int, str], None]) -> str:
        """Replace every `delegate(...)` sentinel line in a python result with the report."""
        if DELEGATE_SENTINEL not in output:
            return output
        lines = []
        for line in output.splitlines():
            if not line.startswith(DELEGATE_SENTINEL):
                lines.append(line)
                continue
            try:
                request = json.loads(line[len(DELEGATE_SENTINEL):])
            except ValueError:
                lines.append("[delegate: malformed request]")
                continue
            report = self.run(request.get("task", ""), request.get("max_steps"))
            try:
                deliver(int(request.get("n") or self.count), report)
            except Exception as exc:
                if self.logger:
                    self.logger.warning("could not deliver report to the kernel: %s", exc)
            lines.append(report)
        return "\n".join(lines)

    @property
    def total_usage(self) -> dict:
        totals = {"input": 0, "cached": 0, "output": 0, "cost_usd": 0.0}
        for record in self.records:
            for key in totals:
                totals[key] += record["usage"].get(key, 0)
        totals["cost_usd"] = round(totals["cost_usd"], 6)
        return totals


def _snapshot(usage) -> dict:
    return {"input": usage.input, "cached": usage.cached, "output": usage.output,
            "cost_usd": usage.cost_usd}


def _delta(before: dict, usage) -> dict:
    now = _snapshot(usage)
    return {key: (round(now[key] - before[key], 6) if key == "cost_usd" else now[key] - before[key])
            for key in now}


def _last_assistant_text(messages: list[dict]) -> str:
    for message in reversed(messages):
        if message.get("role") == "assistant" and message.get("content"):
            return str(message["content"])
    return ""
