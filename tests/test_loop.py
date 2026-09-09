"""The loop's caps, paging, ledger, loop detection and finish semantics (scripted model)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from harness.llm import LLMError, Usage  # noqa: E402
from harness.loop import FINISH_SENTINEL, Loop, Trajectory  # noqa: E402


class FakeReply:
    def __init__(self, tool=None, args=None, text="", usage=None):
        self.message = {"role": "assistant", "content": text}
        if tool:
            self.message["tool_calls"] = [{
                "id": f"call_{tool}",
                "type": "function",
                "function": {"name": tool, "arguments": json.dumps(args or {})},
            }]
        self.usage = usage or Usage(input=100, cached=0, output=10, cost_usd=0.001)
        self.finish_reason = "tool_calls" if tool else "stop"
        self.provider = "FakeProvider"
        self.latency_s = 0.01
        self.raw = {}

    @property
    def tool_calls(self):
        return self.message.get("tool_calls") or []

    @property
    def text(self):
        return self.message.get("content") or ""


class FakeLLM:
    """Returns a scripted sequence; repeats the last item forever."""

    def __init__(self, replies, per_call_input=100):
        self.replies = list(replies)
        self.calls = 0
        self.total = Usage()
        self.providers = {}
        self.per_call_input = per_call_input
        self.seen_messages = []

    def complete(self, messages, tools=None):
        self.seen_messages.append(list(messages))
        reply = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        if isinstance(reply, Exception):
            raise reply
        self.total.add(Usage(input=self.per_call_input, cached=0, output=10, cost_usd=0.001))
        return reply


def build(tmp_path, replies, run_tool=None, **kwargs):
    llm = FakeLLM(replies, per_call_input=kwargs.pop("per_call_input", 100))
    trajectory = Trajectory(tmp_path / "trajectory.jsonl")
    loop = Loop(
        llm=llm, tools=[], system_prompt="SYSTEM", instruction="INSTRUCTION",
        run_tool=run_tool or (lambda name, args: "ok"),
        trajectory=trajectory, **kwargs,
    )
    return loop, llm, trajectory


def events(tmp_path):
    return [json.loads(line) for line in (tmp_path / "trajectory.jsonl").read_text().splitlines()]


# -- terminal reasons ---------------------------------------------------------
def test_finish_sentinel_ends_the_episode(tmp_path):
    loop, _, traj = build(
        tmp_path, [FakeReply("finish", {"summary": "all done"})],
        run_tool=lambda name, args: f"{FINISH_SENTINEL}\nsummary written")
    result = loop.run()
    traj.close()
    assert result.terminal_reason == "finish"
    assert result.summary == "all done"
    assert result.steps == 1


def test_step_cap_is_enforced(tmp_path):
    # Distinct commands: identical ones would trip loop detection first, which is itself
    # the behaviour asserted in test_three_identical_calls_stop_the_loop.
    replies = [FakeReply("bash", {"cmd": f"echo {i}"}) for i in range(10)]
    loop, llm, traj = build(tmp_path, replies, step_cap=4)
    result = loop.run()
    traj.close()
    assert result.terminal_reason == "step_cap"
    assert result.steps == 4
    assert llm.calls == 4


def test_token_cap_is_enforced(tmp_path):
    replies = [FakeReply("bash", {"cmd": f"echo {i}"}) for i in range(10)]
    loop, _, traj = build(
        tmp_path, replies, step_cap=100, token_cap=350, per_call_input=100)
    result = loop.run()
    traj.close()
    assert result.terminal_reason == "token_cap"
    assert result.steps == 4          # stops once cumulative input crosses the cap


def test_api_error_is_terminal_not_fatal(tmp_path):
    loop, _, traj = build(tmp_path, [LLMError("giving up after 5 attempts")])
    result = loop.run()
    traj.close()
    assert result.terminal_reason == "api_error"
    assert any("api_error" in (e.get("content") or "") for e in events(tmp_path))


def test_three_identical_calls_stop_the_loop(tmp_path):
    loop, _, traj = build(tmp_path, [FakeReply("bash", {"cmd": "same"})], step_cap=50)
    result = loop.run()
    traj.close()
    assert result.terminal_reason == "loop"
    assert result.steps == 3
    assert any(e.get("kind") == "loop_warning" for e in events(tmp_path))


def test_varied_calls_do_not_trigger_loop_detection(tmp_path):
    replies = [FakeReply("bash", {"cmd": f"echo {i}"}) for i in range(5)]
    replies.append(FakeReply("finish", {"summary": "done"}))
    loop, _, traj = build(
        tmp_path, replies, step_cap=50,
        run_tool=lambda name, args: FINISH_SENTINEL if name == "finish" else "ok")
    result = loop.run()
    traj.close()
    assert result.terminal_reason == "finish"


# -- message construction -----------------------------------------------------
def test_cache_breakpoint_moves_to_the_end_of_the_transcript():
    """The breakpoint must move, or only the system block is ever cached."""
    from harness.llm import apply_cache_control

    def marked(messages):
        return [i for i, m in enumerate(messages)
                if isinstance(m.get("content"), list)
                and any(isinstance(b, dict) and "cache_control" in b for b in m["content"])]

    transcript = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "U"}]
    out, _ = apply_cache_control(transcript, None)
    assert marked(out) == [0, 1]

    transcript += [{"role": "assistant", "content": "A"},
                   {"role": "tool", "tool_call_id": "1", "content": "RESULT"}]
    out, _ = apply_cache_control(transcript, None)
    assert marked(out) == [0, 3], "the marker must follow the end of the conversation"

    # The stored transcript keeps plain content, so breakpoints never accumulate.
    assert transcript[0]["content"] == "SYS"
    assert transcript[-1]["content"] == "RESULT"


def test_last_tool_definition_is_cache_marked():
    from harness.llm import apply_cache_control

    _, tools = apply_cache_control(
        [{"role": "system", "content": "S"}],
        [{"type": "function", "function": {"name": "a"}},
         {"type": "function", "function": {"name": "b"}}])
    assert "cache_control" not in tools[0]
    assert tools[-1]["cache_control"] == {"type": "ephemeral"}


def test_an_unmarkable_last_message_falls_back_to_an_earlier_one():
    """An empty tool result cannot carry a marker; the search continues backwards."""
    from harness.llm import apply_cache_control

    out, _ = apply_cache_control(
        [{"role": "system", "content": "S"},
         {"role": "user", "content": "U"},
         {"role": "tool", "tool_call_id": "1", "content": ""}], None)
    assert isinstance(out[1]["content"], list)
    assert out[2]["content"] == ""


def test_transcript_is_append_only(tmp_path):
    replies = [FakeReply("bash", {"cmd": f"echo {i}"}) for i in range(4)]
    loop, llm, traj = build(tmp_path, replies, step_cap=4)
    loop.run()
    traj.close()
    # Every snapshot the LLM saw must be a prefix of the next: the cached prefix is only
    # worth anything if earlier messages are never rewritten.
    for earlier, later in zip(llm.seen_messages, llm.seen_messages[1:]):
        assert later[: len(earlier)] == earlier


def test_step_cap_is_stated_to_the_model(tmp_path):
    loop, _, traj = build(tmp_path, [FakeReply("finish", {"summary": ""})],
                          run_tool=lambda n, a: FINISH_SENTINEL, step_cap=42)
    loop.run()
    traj.close()
    assert "Step cap: 42." in loop.messages[1]["content"]


def test_briefing_is_folded_into_the_first_user_message(tmp_path):
    loop, _, traj = build(tmp_path, [FakeReply("finish", {"summary": ""})],
                          run_tool=lambda n, a: FINISH_SENTINEL,
                          briefing="stock: 27 units")
    loop.run()
    traj.close()
    text = loop.messages[1]["content"]
    assert "Environment at task start" in text and "stock: 27 units" in text


# -- paging and the ledger ----------------------------------------------------
def test_long_tool_output_is_paged_before_it_reaches_the_model(tmp_path):
    loop, _, traj = build(
        tmp_path, [FakeReply("bash", {"cmd": "dump"})], step_cap=1,
        run_tool=lambda name, args: "x" * 50_000)
    loop.run()
    traj.close()
    tool_message = [m for m in loop.messages if m["role"] == "tool"][0]
    assert len(tool_message["content"]) < 4000
    assert 'show("h1", 2)' in tool_message["content"]


def test_short_tool_output_is_untouched(tmp_path):
    loop, _, traj = build(tmp_path, [FakeReply("bash", {"cmd": "ls"})], step_cap=1,
                          run_tool=lambda name, args: "small output")
    loop.run()
    traj.close()
    assert [m for m in loop.messages if m["role"] == "tool"][0]["content"] == "small output"


def test_ledger_is_reinjected_every_k_steps(tmp_path):
    replies = [FakeReply("bash", {"cmd": f"echo {i}"}) for i in range(7)]
    loop, _, traj = build(tmp_path, replies, step_cap=6, ledger_k=2,
                          ledger_summary=lambda: "1/3 done")
    loop.run()
    traj.close()
    ledgers = [e for e in events(tmp_path) if e.get("kind") == "ledger"]
    assert len(ledgers) == 3                      # after steps 2, 4 and 6
    assert "1/3 done" in ledgers[0]["content"]
    assert "step 2/6" in ledgers[0]["content"]


def test_ledger_failure_does_not_break_the_loop(tmp_path):
    def broken():
        raise RuntimeError("no plan")

    replies = [FakeReply("bash", {"cmd": f"echo {i}"}) for i in range(3)]
    loop, _, traj = build(tmp_path, replies, step_cap=2, ledger_k=1, ledger_summary=broken)
    result = loop.run()
    traj.close()
    assert result.terminal_reason == "step_cap"
    assert any("ledger unavailable" in (e.get("content") or "") for e in events(tmp_path))


def test_no_tool_call_gets_a_nudge_rather_than_ending(tmp_path):
    loop, _, traj = build(tmp_path, [FakeReply(text="I think I am done.")], step_cap=2)
    result = loop.run()
    traj.close()
    assert result.terminal_reason == "step_cap"
    assert any(e.get("kind") == "nudge" for e in events(tmp_path))


# -- logging ------------------------------------------------------------------
def test_trajectory_records_usage_and_provider(tmp_path):
    loop, _, traj = build(tmp_path, [FakeReply("bash", {"cmd": "ls"})], step_cap=1)
    loop.run()
    traj.close()
    assistant = [e for e in events(tmp_path) if e["role"] == "assistant"][0]
    assert assistant["usage"] == {"input": 100, "cached": 0, "output": 10}
    assert assistant["provider"] == "FakeProvider"
    tool_event = [e for e in events(tmp_path) if e["role"] == "tool"][0]
    assert tool_event["tool"] == "bash"
    assert tool_event["args"] == {"cmd": "ls"}


def test_a_raising_tool_is_reported_to_the_model(tmp_path):
    def explode(name, args):
        raise ValueError("bad domain")

    loop, _, traj = build(tmp_path, [FakeReply("python", {"code": "x"})], step_cap=1,
                          run_tool=explode)
    loop.run()
    traj.close()
    tool_message = [m for m in loop.messages if m["role"] == "tool"][0]
    assert "ValueError" in tool_message["content"]
    assert "bad domain" in tool_message["content"]


def test_show_serves_pages_from_the_loops_own_store(tmp_path):
    """`show` is answered by the loop, not the dispatcher: the loop minted the handle."""
    replies = [
        FakeReply("bash", {"cmd": "dump"}),
        FakeReply("show", {"handle": "h1", "page": 2}),
        FakeReply("show", {"handle": "nope", "page": 1}),
    ]
    calls = []

    def dispatch(name, args):
        calls.append(name)
        return "y" * 50_000 if name == "bash" else "dispatcher should not see this"

    loop, _, traj = build(tmp_path, replies, step_cap=3, run_tool=dispatch)
    loop.run()
    traj.close()

    assert calls == ["bash"]                       # show never reached the dispatcher
    tool_messages = [m["content"] for m in loop.messages if m["role"] == "tool"]
    assert tool_messages[1].startswith("y")
    assert "page 2 of" in tool_messages[1]
    assert "no such handle" in tool_messages[2]


def test_tool_call_arguments_are_repaired_before_being_echoed(tmp_path):
    """Providers reject an echoed tool_call whose arguments are not a JSON string."""
    reply = FakeReply("python", {"code": "x"})
    reply.message["tool_calls"] = [
        {"id": "a", "type": "function", "function": {"name": "python", "arguments": {"code": "x"}}},
        {"id": "b", "type": "function", "function": {"name": "python", "arguments": ""}},
        {"id": "c", "type": "function", "function": {"name": "python", "arguments": None}},
    ]
    loop, _, traj = build(tmp_path, [reply], step_cap=1)
    loop.run()
    traj.close()

    echoed = [m for m in loop.messages if m.get("role") == "assistant"][0]
    for call in echoed["tool_calls"]:
        arguments = call["function"]["arguments"]
        assert isinstance(arguments, str)
        json.loads(arguments)          # must parse, not merely be a string
    assert json.loads(echoed["tool_calls"][0]["function"]["arguments"]) == {"code": "x"}
    assert echoed["tool_calls"][1]["function"]["arguments"] == "{}"
    assert echoed["tool_calls"][2]["function"]["arguments"] == "{}"


def test_connection_reset_is_retried_not_fatal(tmp_path, monkeypatch):
    """A reset socket must cost a retry, not the trajectory."""
    import harness.llm as llm_module

    attempts = {"n": 0}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({
                "choices": [{"message": {"role": "assistant", "content": "ok"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
                "provider": "Fake",
            }).encode()

    def fake_urlopen(request, timeout=None):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConnectionResetError(54, "Connection reset by peer")
        return FakeResponse()

    monkeypatch.setattr(llm_module.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(llm_module.time, "sleep", lambda _s: None)

    client = llm_module.LLM({"base_url": "https://example.invalid", "model": "m"}, "key")
    reply = client.complete([{"role": "user", "content": "hi"}])
    assert reply.text == "ok"
    assert attempts["n"] == 3          # two resets absorbed, third attempt succeeded


def test_truncated_tool_call_is_neutralised_and_flagged(tmp_path):
    """A call cut off by max_tokens leaves half-written JSON in `arguments`."""
    reply = FakeReply("python", {"code": "x"})
    reply.message["tool_calls"] = [{
        "id": "cut", "type": "function",
        "function": {"name": "python", "arguments": '{"code": "erp.create_po(8, [(2, 24'},
    }]
    reply.finish_reason = "length"

    loop, _, traj = build(tmp_path, [reply], step_cap=1)
    loop.run()
    traj.close()

    echoed = [m for m in loop.messages if m.get("role") == "assistant"][0]
    arguments = echoed["tool_calls"][0]["function"]["arguments"]
    json.loads(arguments)                      # the echo is valid whatever the model sent
    assert arguments == "{}"

    kinds = [e.get("kind") for e in events(tmp_path)]
    assert "truncated" in kinds, "the model must be told its call was cut off"


def test_transient_402_is_retried_but_a_real_one_is_not(monkeypatch):
    """OpenRouter uses 402 for both "wait your turn" and "you are out of money"."""
    import urllib.error

    import harness.llm as llm_module

    def make_402(detail: str):
        return urllib.error.HTTPError(
            "u", 402, "Payment Required", {},
            __import__("io").BytesIO(detail.encode()))

    transient = ('{"error":{"message":"This request would exceed your available credits '
                 'given your current in-flight requests. Retry after in-flight requests '
                 'settle, or add credits.","code":402}}')
    terminal = ('{"error":{"message":"This request requires more credits, or fewer '
                'max_tokens.","code":402}}')

    for detail, expected_attempts in ((transient, llm_module.MAX_ATTEMPTS), (terminal, 1)):
        attempts = {"n": 0}

        def fake_urlopen(request, timeout=None, _d=detail):
            attempts["n"] += 1
            raise make_402(_d)

        monkeypatch.setattr(llm_module.urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(llm_module.time, "sleep", lambda _s: None)
        client = llm_module.LLM({"base_url": "https://example.invalid", "model": "m"}, "k")
        try:
            client.complete([{"role": "user", "content": "hi"}])
        except llm_module.LLMError:
            pass
        assert attempts["n"] == expected_attempts, (detail[:40], attempts["n"])


def test_a_504_inside_a_200_body_is_retried(monkeypatch):
    """OpenRouter can hand back an upstream failure as a 200 with an error body."""
    import harness.llm as llm_module

    attempts = {"n": 0}

    class Resp:
        def __init__(self, body): self.body = body
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps(self.body).encode()

    def fake_urlopen(request, timeout=None):
        attempts["n"] += 1
        if attempts["n"] < 3:
            return Resp({"error": {"message": "A Timeout Occurred", "code": 504}})
        return Resp({"choices": [{"message": {"role": "assistant", "content": "ok"},
                                  "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": 5, "completion_tokens": 1}, "provider": "F"})

    monkeypatch.setattr(llm_module.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(llm_module.time, "sleep", lambda _s: None)
    client = llm_module.LLM({"base_url": "https://example.invalid", "model": "m"}, "k")
    assert client.complete([{"role": "user", "content": "hi"}]).text == "ok"
    assert attempts["n"] == 3

    # A non-retryable body error still fails fast.
    attempts["n"] = 0
    monkeypatch.setattr(llm_module.urllib.request, "urlopen",
                        lambda r, timeout=None: Resp({"error": {"message": "bad request", "code": 400}}))
    with pytest.raises(llm_module.LLMError):
        client.complete([{"role": "user", "content": "hi"}])


def test_sentinel_in_bash_output_does_not_end_the_episode(tmp_path):
    """Pass 3: a trial `cat`ed the finish module over bash, the sentinel literal came back
    in the output, and the loop declared the episode finished with nothing executed."""
    replies = [FakeReply("bash", {"cmd": "cat finish.py"}),
               FakeReply("finish", {"summary": "done"})]
    loop, _, traj = build(tmp_path, replies,
                          run_tool=lambda name, args: f"FINISH_SENTINEL = '{FINISH_SENTINEL}'"
                          if name == "bash" else FINISH_SENTINEL)
    result = loop.run()
    assert result.terminal_reason == "finish"
    assert result.steps == 2, "the bash output must not have ended the episode at step 1"


def test_a_generation_ended_with_finish_reason_error_is_retried(monkeypatch):
    """dev40's first attempt: a 21-minute reasoning run the provider ended with
    finish_reason "error" and no message. Retrying is cheaper than a nudge turn."""
    import harness.llm as llm_module

    attempts = {"n": 0}

    class Resp:
        def __init__(self, body): self.body = body
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps(self.body).encode()

    def fake_urlopen(request, timeout=None):
        attempts["n"] += 1
        if attempts["n"] < 2:
            return Resp({"choices": [{"message": {"role": "assistant", "content": ""},
                                      "finish_reason": "error"}],
                         "usage": {"prompt_tokens": 5, "completion_tokens": 13570}, "provider": "F"})
        return Resp({"choices": [{"message": {"role": "assistant", "content": "ok"},
                                  "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": 5, "completion_tokens": 1}, "provider": "F"})

    monkeypatch.setattr(llm_module.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(llm_module.time, "sleep", lambda _s: None)
    client = llm_module.LLM({"base_url": "https://example.invalid", "model": "m"}, "k")
    assert client.complete([{"role": "user", "content": "hi"}]).text == "ok"
    assert attempts["n"] == 2


def test_reasoning_budget_from_the_model_spec_is_sent():
    import harness.llm as llm_module

    client = llm_module.LLM({"base_url": "https://example.invalid", "model": "m",
                             "reasoning": {"max_tokens": 6000}}, "k")
    assert client._payload([{"role": "user", "content": "hi"}], None)["reasoning"] == {"max_tokens": 6000}
    plain = llm_module.LLM({"base_url": "https://example.invalid", "model": "m"}, "k")
    assert "reasoning" not in plain._payload([{"role": "user", "content": "hi"}], None)


def test_time_budget_warnings_fire_once_each(tmp_path, monkeypatch):
    """At 70% of the budget the model is told to execute now; at 88% to finish now."""
    import harness.loop as loop_module

    clock = {"t": 1000.0}
    monkeypatch.setattr(loop_module.time, "time", lambda: clock["t"])
    replies = [FakeReply("python", {"code": f"print({i})"}) for i in range(5)] + [FakeReply("finish", {"summary": "d"})]
    loop, _, traj = build(tmp_path, replies, time_budget_s=100,
                          run_tool=lambda name, args: FINISH_SENTINEL if name == "finish" else "ok")
    original = loop.llm.complete

    def advancing(messages, tools):
        clock["t"] += 20          # each step costs 20 s of a 100 s budget
        return original(messages, tools)
    loop.llm.complete = advancing
    result = loop.run()
    user_msgs = [m["content"] for m in loop.messages if m["role"] == "user"]
    warns = [m for m in user_msgs if m.startswith("[time]")]
    assert len(warns) == 2 and "Execute the best" in warns[0] and "call finish" in warns[1]
    assert "Time budget: 2 minutes" in user_msgs[0]
    assert result.terminal_reason == "finish"
