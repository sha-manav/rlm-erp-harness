"""Harbor entry point. Runs on the host: starts the kernel inside the task container,
builds the prompt from the config, runs the loop, writes trajectory.jsonl and result.json.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from harness.container import HarborContainer, Kernel
from harness.delegate import Delegator, filter_library_docs
from harness.lib.delegate import DELEGATE_SENTINEL
from harness.llm import LLM
from harness.loop import FINISH_SENTINEL, Loop, Trajectory
from harness.tools import schemas_for

REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPT_DIR = REPO_ROOT / "harness" / "prompts"

# Exported into the kernel so lib/erp.py can reach Odoo and Postgres without the agent
# having to discover them. Values are the ERP-Bench image's.
CONTAINER_ENV = {
    "ODOO_URL": "http://127.0.0.1:8069",
    "ODOO_DB": "bench",
    "ODOO_USER": "admin",
    "PGHOST": "127.0.0.1",
    "PGPORT": "5432",
    "PGUSER": "odoo",
    "PGPASSWORD": "odoo",
    "PGDATABASE": "bench",
}

SETUP_MARKER = "/tmp/saas_setup_complete"
SETUP_TIMEOUT_S = 1500   # sixteen simultaneous Odoo seedings took >600 s on 18 cores; the wait counts against the task hour


class ErpAgent(BaseAgent):
    SUPPORTS_ATIF = False

    def __init__(self, logs_dir: Path, model_name: str | None = None, logger=None,
                 config: str = "C_full", model_key: str = "big", **kwargs):
        super().__init__(logs_dir=logs_dir, model_name=model_name, logger=logger, **kwargs)
        self.config_id = config
        self.model_key = model_key
        self.configs = yaml.safe_load((REPO_ROOT / "configs/configs.yaml").read_text())
        self.models = yaml.safe_load((REPO_ROOT / "configs/models.yaml").read_text())
        self.config = self._resolve(config)
        self.lib_active: list[str] = []
        self.result: dict[str, Any] = {}

    # -- configuration --------------------------------------------------------
    def _resolve(self, name: str) -> dict:
        if name not in self.configs:
            raise ValueError(f"unknown config {name!r}")
        resolved = dict(self.configs.get("defaults", {}))
        spec = self.configs[name]
        if spec.get("inherit"):
            resolved.update(self._resolve(spec["inherit"]))
        resolved.update({k: v for k, v in spec.items() if k != "inherit"})
        resolved["id"] = name
        return resolved

    def _system_prompt(self, names: list[str] | None = None,
                       lib_active: list[str] | None = None) -> str:
        parts = []
        for name in (self.config.get("prompts", []) if names is None else names):
            path = PROMPT_DIR / f"{name}.md"
            if not path.exists():
                self.logger.warning("prompt %s missing; skipping", path)
                continue
            text = path.read_text().strip()
            if name == "library_docs" and lib_active is not None:
                text = filter_library_docs(text, lib_active)
            parts.append(text)
        return "\n\n---\n\n".join(parts)

    @staticmethod
    def name() -> str:
        return "erp-harness"

    def version(self) -> str:
        return "0.1.0"

    async def setup(self, environment: BaseEnvironment) -> None:
        return

    # -- lifecycle ------------------------------------------------------------
    async def run(self, instruction: str, environment: BaseEnvironment,
                  context: AgentContext) -> None:
        loop = asyncio.get_running_loop()
        container = HarborContainer(environment, loop)
        await asyncio.to_thread(self._run_sync, instruction, container, context)

    def _wait_for_odoo(self, container) -> bool:
        """Odoo loads its scenario after the container starts; acting before it lands
        gives the agent an empty database and a wasted trial."""
        deadline = time.time() + SETUP_TIMEOUT_S
        while time.time() < deadline:
            if container.exec(f"test -f {SETUP_MARKER}", timeout=30).ok:
                return True
            time.sleep(5)
        return False

    def _run_sync(self, instruction: str, container, context: AgentContext) -> None:
        started = time.time()
        trajectory_path = Path(self.logs_dir) / "trajectory.jsonl"
        trajectory = Trajectory(trajectory_path)
        terminal_reason = "crash"
        result = None
        kernel = None

        try:
            if not self._wait_for_odoo(container):
                raise RuntimeError(f"{SETUP_MARKER} did not appear within {SETUP_TIMEOUT_S}s")

            api_key = self._api_key()
            spec = self.models[self.model_key]
            llm = LLM(spec, api_key, logger=self.logger)

            tools = self.config.get("tools", ["python", "bash", "show", "finish"])
            kernel_needed = "python" in tools or "finish" in tools
            if kernel_needed:
                # lib: [] means ship nothing; only an absent key means ship everything.
                lib_modules = self.config["lib"] if "lib" in self.config else None
                kernel = Kernel(container, lib_modules=lib_modules)
                kernel.start(env=CONTAINER_ENV)
                status = kernel.lib_status()
                self.lib_active = status.get("loaded", [])
                if status.get("failed"):
                    self.logger.warning("lib modules failed to load: %s", status["failed"])

            system_prompt = self._system_prompt(
                lib_active=self.lib_active if kernel is not None else None)
            if self.lib_active:
                # Tell the model exactly which modules loaded; naming absent ones produces NameErrors.
                names = ", ".join(sorted(self.lib_active))
                system_prompt += (
                    f"\n\n---\n\nAvailable in your Python kernel right now: **{names}**. "
                    "Nothing else is loaded — do not call modules that are not on this list.")

            if kernel is not None and self.config.get("finish_requires_delegation"):
                reply = kernel.run("from lib.finish import require_delegation as _rd\n_rd(True)\n"
                                   "print('finish requires a delegation')", timeout=30)
                if not reply.get("ok"):
                    self.logger.warning("could not arm the delegation requirement: %s",
                                        reply.get("stderr", "")[:200])

            if kernel is not None and "check" in self.lib_active:
                # Checks that already fail on the untouched scenario are recorded so the
                # finish gate holds the agent to what it changed, not to what it inherited.
                reply = kernel.run(
                    "from lib.finish import record_baseline as _rb\n"
                    "print(_rb())", timeout=180)
                if reply.get("ok"):
                    self.logger.info("check baseline: %s", reply.get("stdout", "").strip())
                else:
                    self.logger.warning("baseline failed: %s", reply.get("stderr", "")[:200])

            briefing = ""
            if self.config.get("briefing") and kernel is not None and "brief" in self.lib_active:
                reply = kernel.run("print(brief())", timeout=180)
                if reply.get("ok") and reply.get("stdout", "").strip():
                    briefing = reply["stdout"].strip()
                else:
                    self.logger.warning("briefing failed: %s", reply.get("stderr", "")[:200])

            delegator = None
            if "delegate" in tools and kernel is not None and "delegate" in self.lib_active:
                # The sub-agent's prompt: its own contract plus the shared domain prompts,
                # with the library docs cut to what its namespace actually binds.
                sub_active = [m for m in self.lib_active if m not in ("state", "finish", "delegate")]
                sub_prompt = self._system_prompt(
                    self.config.get("delegate_prompts", ["delegate_contract", "library_docs"]),
                    lib_active=sub_active)
                names = ", ".join(sorted(sub_active))
                sub_prompt += (f"\n\n---\n\nAvailable in your Python kernel right now: **{names}** "
                               "(erp is read-only). Nothing else is loaded.")
                budget = float(self.config.get("time_budget_s", 0) or 0)
                delegator = Delegator(
                    llm=llm, kernel=kernel, trajectory=trajectory, system_prompt=sub_prompt,
                    token_cap=self.config.get("token_cap", 1_500_000),
                    deadline=(lambda: budget - (time.time() - started)) if budget else None,
                    kernel_timeout=self.config.get("kernel_timeout_s", 120),
                    default_max_steps=self.config.get("delegate_max_steps", 40),
                    max_sub_steps=self.config.get("delegate_step_ceiling", 60),
                    logger=self.logger)
            elif "delegate" in tools:
                self.logger.warning("delegate tool requested but lib.delegate is not active")
            self.delegator = delegator

            dispatch = self._make_dispatch(container, kernel, delegator)
            agent_loop = Loop(
                llm=llm,
                tools=schemas_for(tools),
                system_prompt=system_prompt,
                instruction=instruction,
                run_tool=dispatch,
                trajectory=trajectory,
                step_cap=self.config.get("step_cap", 150),
                token_cap=self.config.get("token_cap", 1_500_000),
                ledger_k=self.config.get("ledger_k", 0),
                time_budget_s=self.config.get("time_budget_s", 0),
                ledger_summary=self._ledger_summary(kernel) if kernel else None,
                briefing=briefing,
                logger=self.logger,
            )
            result = agent_loop.run()
            terminal_reason = result.terminal_reason
            if delegator is not None:
                result.delegations = delegator.count
        except Exception as exc:
            self.logger.exception("agent crashed: %s", exc)
            trajectory.write(t=-1, role="system", content=f"crash: {type(exc).__name__}: {exc}",
                             tool=None, args=None, output=None, usage=None, latency_s=None)
        finally:
            if kernel:
                try:
                    kernel.stop()
                except Exception:
                    pass
            trajectory.close()

        usage = result.usage if result else None
        delegator = getattr(self, "delegator", None)
        self.result = {
            "config": self.config_id,
            "model": self.models[self.model_key]["model"],
            "model_key": self.model_key,
            "terminal_reason": terminal_reason,
            "steps": result.steps if result else 0,
            "delegations": delegator.count if delegator else 0,
            "delegation_usage": delegator.total_usage if delegator else None,
            "delegation_records": delegator.records if delegator else [],
            "tokens": usage.as_dict() if usage else {"input": 0, "cached": 0, "output": 0},
            "cost_usd": round(usage.cost_usd, 6) if usage else 0.0,
            "wallclock_s": round(time.time() - started, 2),
            "lib_active": self.lib_active,
            "providers": result.providers if result else {},
            "summary": result.summary if result else "",
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        (Path(self.logs_dir) / "result.json").write_text(json.dumps(self.result, indent=2))

        if usage:
            context.n_input_tokens = usage.input
            context.n_cache_tokens = usage.cached
            context.n_output_tokens = usage.output
            context.cost_usd = usage.cost_usd
        context.metadata = {k: v for k, v in self.result.items() if k != "summary"}

    def _api_key(self) -> str:
        spec = self.models[self.model_key]
        key = self._get_env(spec["api_key_env"], "OPENROUTER_API_KEY")
        if not key:
            raise RuntimeError(f"{spec['api_key_env']} is not set for the agent")
        return key

    # -- tools ----------------------------------------------------------------
    def _make_dispatch(self, container, kernel, delegator=None):
        bash_timeout = self.config.get("bash_timeout_s", 120)
        kernel_timeout = self.config.get("kernel_timeout_s", 120)
        gate = bool(self.config.get("finish_gate", False))

        def deliver(n: int, report: str) -> None:
            kernel.run("from lib.delegate import _deliver as _d\n"
                       f"_d({int(n)}, {json.dumps(report)})", timeout=30)

        def dispatch(name: str, args: dict) -> str:
            if name == "python":
                if kernel is None:
                    return "the python tool is not enabled for this configuration"
                reply = kernel.run(args.get("code", ""),
                                   timeout=int(args.get("timeout") or kernel_timeout))
                output = self._render_kernel(reply)
                if DELEGATE_SENTINEL in output:
                    # `delegate(...)` was called from python: run the sub-agent(s) now and
                    # put each report where the sentinel line was.
                    if delegator is None:
                        output = output.replace(DELEGATE_SENTINEL,
                                                "[delegation is not enabled in this configuration] ")
                    else:
                        output = delegator.resolve_output(output, deliver)
                if FINISH_SENTINEL in output:
                    # A printed sentinel is a finish only if the gate actually passed.
                    probe = kernel.run(
                        "from lib.finish import _state as _s; print(bool(_s.get('finished')))",
                        timeout=30)
                    if "True" not in (probe.get("stdout") or ""):
                        output = output.replace(FINISH_SENTINEL, "[sentinel text, not a finish]")
                return output

            if name == "bash":
                result = container.exec(args.get("cmd", ""),
                                        timeout=int(args.get("timeout") or bash_timeout))
                body = result.stdout or ""
                if result.stderr.strip():
                    body += ("\n" if body else "") + f"[stderr]\n{result.stderr}"
                if result.rc != 0:
                    body += f"\n[exit {result.rc}]"
                return body or "(no output)"

            if name == "delegate":
                if delegator is None:
                    return "delegation is not enabled in this configuration"
                report = delegator.run(args.get("task", ""), args.get("max_steps"))
                deliver(delegator.count, report)
                return report

            if name == "finish":
                summary = json.dumps(args.get("summary", ""))
                if kernel is None:
                    # No kernel means no checks to run: honour the finish immediately.
                    return f"{FINISH_SENTINEL}\n(no check gate in this configuration)"
                code = (
                    "from lib.finish import finish as _finish\n"
                    f"print(_finish({summary}, gate={gate}))\n"
                )
                # The intent is unambiguous, so transport failures are retried here.
                reply = None
                for _ in range(3):
                    reply = kernel.run(code, timeout=kernel_timeout)
                    if not reply.get("transport_failure"):
                        break
                    time.sleep(3)
                return self._render_kernel(reply)

            return f"unknown tool {name!r}"

        return dispatch

    @staticmethod
    def _render_kernel(reply: dict) -> str:
        parts = []
        if reply.get("stdout"):
            parts.append(reply["stdout"].rstrip())
        if reply.get("stderr"):
            parts.append(reply["stderr"].rstrip())
        if not parts:
            parts.append("(no output)")
        return "\n".join(parts)

    def _ledger_summary(self, kernel):
        def summary() -> str:
            reply = kernel.run("print(plan.summary())", timeout=30)
            return (reply.get("stdout") or "").strip() or "(no plan set)"

        return summary
