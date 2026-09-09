"""Unit tests for the Phase-1 analysis pipeline (no container, no network)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from harness.rewards import flatten_rewards  # noqa: E402
from scripts.ingest_harbor import parse_pi_trajectory, read_reward, terminal_reason  # noqa: E402
from scripts.select_eval100 import stratified_sample  # noqa: E402


def write_pi_log(tmp_path: Path, events: list[dict]) -> Path:
    path = tmp_path / "pi.txt"
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return path


def assistant(step_usage: dict, tool: str | None = None, text: str = "") -> dict:
    content: list[dict] = []
    if text:
        content.append({"type": "text", "text": text})
    if tool:
        content.append({"type": "toolCall", "name": tool, "arguments": {"command": "ls"}})
    return {
        "type": "message_end",
        "message": {"role": "assistant", "content": content, "usage": step_usage},
    }


def test_pi_input_tokens_include_cache(tmp_path):
    """pi reports `input` excluding cache; the common schema includes it."""
    log = write_pi_log(tmp_path, [
        {"type": "turn_start"},
        assistant({"input": 100, "output": 20, "cacheRead": 900, "cost": {"total": 0.01}}, tool="bash"),
    ])
    _, totals, steps = parse_pi_trajectory(log)
    assert totals["input"] == 1000
    assert totals["cached"] == 900
    assert totals["output"] == 20
    assert totals["cost_usd"] == 0.01
    assert steps == 1


def test_tool_results_are_not_duplicated(tmp_path):
    """toolResult message_end events must not double the tool output."""
    log = write_pi_log(tmp_path, [
        {"type": "turn_start"},
        assistant({"input": 10, "output": 5, "cacheRead": 0}, tool="bash"),
        {"type": "message_end", "message": {
            "role": "toolResult",
            "content": [{"type": "text", "text": "total 0"}],
        }},
        {"type": "tool_execution_end", "toolName": "bash", "result": {"output": "total 0"}},
        {"type": "turn_end"},
    ])
    events, totals, _ = parse_pi_trajectory(log)
    assert [e["role"] for e in events] == ["assistant", "tool"]
    assert totals["input"] == 10


def test_steps_count_turns_not_messages(tmp_path):
    log = write_pi_log(tmp_path, [
        {"type": "turn_start"},
        assistant({"input": 1, "output": 1, "cacheRead": 0}, tool="bash"),
        assistant({"input": 1, "output": 1, "cacheRead": 0}, text="thinking out loud"),
        {"type": "turn_end"},
        {"type": "turn_start"},
        assistant({"input": 1, "output": 1, "cacheRead": 0}, tool="bash"),
        {"type": "turn_end"},
    ])
    _, _, steps = parse_pi_trajectory(log)
    assert steps == 2


def test_malformed_lines_are_skipped(tmp_path):
    path = tmp_path / "pi.txt"
    path.write_text(
        "not json at all\n"
        '{"type": "turn_start"}\n'
        "{broken\n"
        + json.dumps(assistant({"input": 5, "output": 5, "cacheRead": 0}, tool="bash"))
        + "\n"
    )
    events, totals, steps = parse_pi_trajectory(path)
    assert steps == 1
    assert totals["input"] == 5
    assert len(events) == 1


def test_read_reward_prefers_json_and_keeps_scale(tmp_path):
    verifier = tmp_path / "verifier"
    verifier.mkdir()
    (verifier / "reward.json").write_text(json.dumps({"overall_score": 42.5, "passed": False}))
    (verifier / "reward.txt").write_text("0.00")
    reward, passed, raw = read_reward(tmp_path)
    assert reward == 42.5
    assert passed is False
    assert raw["overall_score"] == 42.5


def test_read_reward_falls_back_to_text(tmp_path):
    verifier = tmp_path / "verifier"
    verifier.mkdir()
    (verifier / "reward.txt").write_text("100.00\n")
    reward, passed, _ = read_reward(tmp_path)
    assert reward == 100.0
    assert passed is None


def test_terminal_reason_prefers_our_own_label():
    assert terminal_reason({}, [{}], {"terminal_reason": "step_cap"}) == "step_cap"


def test_terminal_reason_maps_harbor_exceptions():
    crashed = {"exception_info": {"exception_type": "RuntimeError"}}
    assert terminal_reason(crashed, [{}], None) == "crash"
    timed_out = {"exception_info": {"exception_type": "AgentTimeoutError"}}
    assert terminal_reason(timed_out, [{}], None) == "timeout"
    assert terminal_reason({}, [], None) == "crash"
    assert terminal_reason({}, [{}], None) == "finish"


def test_flatten_rewards_handles_nested_and_drops_non_numeric():
    flat = flatten_rewards({
        "overall_score": 12.5,
        "passed": True,
        "constraint": {"earned": 3, "total": 9},
        "rules": {"total": 4, "by_dimension": {"constraint": [{"status": "FAIL"}]}},
        "spend": {"optimality_score": None},
        "label": "ignored",
    })
    assert flat["overall_score"] == 12.5
    assert flat["passed"] == 1
    assert flat["constraint_earned"] == 3
    assert flat["rules_total"] == 4
    assert "rules_by_dimension" not in flat
    assert "spend_optimality_score" not in flat
    assert "label" not in flat


def test_stratified_sample_is_deterministic_and_covers_every_pattern():
    pools = {f"p{i}": [f"p{i}_t{j}" for j in range(8)] for i in range(29)}
    first = stratified_sample(pools, 100, 3, seed=0)
    second = stratified_sample(pools, 100, 3, seed=0)
    assert first == second
    assert len(set(first)) == 100
    assert {task.split("_")[0] for task in first} == set(pools)
    per_pattern = {p: sum(1 for t in first if t.startswith(f"{p}_")) for p in pools}
    assert min(per_pattern.values()) == 3
    assert max(per_pattern.values()) == 4


def test_stratified_sample_refuses_an_impossible_request():
    import pytest

    pools = {"only": ["a", "b"]}
    with pytest.raises(SystemExit):
        stratified_sample(pools, 10, 1, seed=0)
