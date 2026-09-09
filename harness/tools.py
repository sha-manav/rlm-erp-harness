"""Tool schemas exposed to the model. Which ones a config gets is set in configs/configs.yaml."""

from __future__ import annotations

PYTHON = {
    "type": "function",
    "function": {
        "name": "python",
        "description": (
            "Run Python in a persistent kernel inside the Odoo container. Variables and "
            "imports survive between calls. The harness library (erp, check, plan, ...) is "
            "already loaded. Print what you want to see; nothing is returned implicitly."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python source to execute."},
                "timeout": {
                    "type": "integer",
                    "description": "Seconds before the kernel is restarted (default 120).",
                },
            },
            "required": ["code"],
        },
    },
}

BASH = {
    "type": "function",
    "function": {
        "name": "bash",
        "description": (
            "Run a shell command in the Odoo container. Each call is a fresh shell: no "
            "directory or variable survives between calls."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "cmd": {"type": "string", "description": "Command to run."},
                "timeout": {
                    "type": "integer",
                    "description": "Seconds before the command is killed (default 120).",
                },
            },
            "required": ["cmd"],
        },
    },
}

SHOW = {
    "type": "function",
    "function": {
        "name": "show",
        "description": (
            "Read one page of an output that was truncated. Handles look like 'h3' and are "
            "named in the truncation notice."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "handle": {"type": "string", "description": "Handle from a truncation notice."},
                "page": {"type": "integer", "description": "1-based page number (default 1)."},
            },
            "required": ["handle"],
        },
    },
}

FINISH = {
    "type": "function",
    "function": {
        "name": "finish",
        "description": (
            "End the task. Runs the end-state checks first and refuses while a hard check "
            "fails, returning what is wrong so you can fix it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "What you changed and why, in a few sentences.",
                },
            },
            "required": ["summary"],
        },
    },
}

DELEGATE = {
    "type": "function",
    "function": {
        "name": "delegate",
        "description": (
            "Hand a self-contained question to a fresh read-only sub-agent with its own "
            "context and Python kernel (same library, same database, no writes). Returns "
            "its report, at most ~400 tokens. Spell out product codes, quantities, dates "
            "and what to return: it has no memory of this conversation."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "The question, self-contained."},
                "max_steps": {
                    "type": "integer",
                    "description": "Step cap for the sub-agent (default 40).",
                },
            },
            "required": ["task"],
        },
    },
}

ALL = {"python": PYTHON, "bash": BASH, "show": SHOW, "delegate": DELEGATE, "finish": FINISH}


def schemas_for(names: list[str]) -> list[dict]:
    """The tool schemas for a config's tool list, in the order given."""
    unknown = [name for name in names if name not in ALL]
    if unknown:
        raise ValueError(f"unknown tool(s) {unknown}; have {sorted(ALL)}")
    return [ALL[name] for name in names]
