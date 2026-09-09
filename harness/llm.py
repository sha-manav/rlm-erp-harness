"""OpenAI-compatible chat client for OpenRouter: cache breakpoints, fp8 provider pin,
retries, per-call usage and cost.
"""

from __future__ import annotations

import http.client
import json
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504}

# 402 is transient when it mentions in-flight requests (per-request credit holds) and terminal otherwise.
TRANSIENT_402 = "in-flight"
MAX_ATTEMPTS = 8
BACKOFF_BASE = 2.0
BACKOFF_CAP = 90.0    # 8 attempts ≈ 5 min of waiting: a provider blip, not a request, is the unit of failure


class LLMError(RuntimeError):
    """A request that could not be completed after retries."""


class _RetryableBody(Exception):
    """An error the provider put in a 200 body that is worth another attempt."""


@dataclass
class Usage:
    input: int = 0
    cached: int = 0
    output: int = 0
    reasoning: int = 0
    cost_usd: float = 0.0

    def add(self, other: "Usage") -> None:
        self.input += other.input
        self.cached += other.cached
        self.output += other.output
        self.reasoning += other.reasoning
        self.cost_usd += other.cost_usd

    def as_dict(self) -> dict:
        return {"input": self.input, "cached": self.cached, "output": self.output}


@dataclass
class Reply:
    message: dict
    usage: Usage
    finish_reason: str | None
    provider: str | None
    latency_s: float
    raw: dict = field(repr=False, default_factory=dict)

    @property
    def tool_calls(self) -> list[dict]:
        return self.message.get("tool_calls") or []

    @property
    def text(self) -> str:
        return self.message.get("content") or ""


def _parse_usage(raw: dict) -> Usage:
    usage = raw.get("usage") or {}
    prompt_details = usage.get("prompt_tokens_details") or {}
    completion_details = usage.get("completion_tokens_details") or {}
    return Usage(
        input=usage.get("prompt_tokens", 0) or 0,          # includes cached
        cached=prompt_details.get("cached_tokens", 0) or 0,
        output=usage.get("completion_tokens", 0) or 0,
        reasoning=completion_details.get("reasoning_tokens", 0) or 0,
        cost_usd=usage.get("cost", 0.0) or 0.0,
    )


class LLM:
    def __init__(self, spec: dict, api_key: str, logger=None):
        self.spec = spec
        self.api_key = api_key
        self.logger = logger
        self.total = Usage()
        self.calls = 0
        self.providers: dict[str, int] = {}

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/sha-manav/erpharnessrlm",
            "X-Title": "ERP-Harness",
        }

    def _payload(self, messages: list[dict], tools: list[dict] | None) -> dict:
        messages, tools = apply_cache_control(messages, tools)
        payload: dict[str, Any] = {
            "model": self.spec["model"],
            "messages": messages,
            "max_tokens": self.spec.get("max_tokens", 4096),
        }
        # pi never sends temperature, so neither do we: the configs must differ by the
        # harness alone, not by sampling settings.
        if self.spec.get("temperature") is not None:
            payload["temperature"] = self.spec["temperature"]
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if self.spec.get("provider_routing"):
            payload["provider"] = self.spec["provider_routing"]
        if self.spec.get("reasoning"):
            # Optional reasoning budget per model; absent means provider default.
            payload["reasoning"] = self.spec["reasoning"]
        payload["usage"] = {"include": True}
        return payload

    def complete(self, messages: list[dict], tools: list[dict] | None = None) -> Reply:
        payload = self._payload(messages, tools)
        body = json.dumps(payload).encode()
        started = time.time()
        last_error = ""

        for attempt in range(1, MAX_ATTEMPTS + 1):
            request = urllib.request.Request(
                f"{self.spec['base_url'].rstrip('/')}/chat/completions",
                data=body, headers=self._headers(), method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=self.spec.get("timeout_s", 900)) as response:
                    raw = json.loads(response.read())
                if raw.get("error"):
                    # Upstream failures can arrive as a 200 with an error body.
                    err = raw["error"]
                    code = err.get("code") if isinstance(err, dict) else None
                    detail = str(err)[:300]
                    try:
                        code = int(code)
                    except (TypeError, ValueError):
                        code = None
                    retryable = code in RETRY_STATUS or (code == 402 and TRANSIENT_402 in detail)
                    if not retryable:
                        raise LLMError(detail)
                    last_error = f"body error {code}: {detail}"
                    raise _RetryableBody(last_error)
                choice = (raw.get("choices") or [{}])[0]
                usage = _parse_usage(raw)
                self.total.add(usage)
                self.calls += 1
                choice0 = (raw.get("choices") or [{}])[0] or {}
                message0 = choice0.get("message") or {}
                if choice0.get("finish_reason") == "error" and not message0.get("tool_calls"):
                    # The provider gave up mid-generation (seen after a 21-minute reasoning
                    # run). Nothing usable came back; retrying is cheaper than a nudge turn.
                    last_error = "provider ended the generation with finish_reason=error"
                    raise _RetryableBody(last_error)
                provider = raw.get("provider")
                if provider:
                    self.providers[provider] = self.providers.get(provider, 0) + 1
                return Reply(
                    message=choice.get("message") or {},
                    usage=usage,
                    finish_reason=choice.get("finish_reason"),
                    provider=provider,
                    latency_s=round(time.time() - started, 2),
                    raw=raw,
                )
            except _RetryableBody as exc:
                last_error = str(exc)
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode(errors="replace")[:300]
                last_error = f"HTTP {exc.code}: {detail}"
                retryable = exc.code in RETRY_STATUS or (
                    exc.code == 402 and TRANSIENT_402 in detail
                )
                if not retryable:
                    raise LLMError(last_error) from None
            except (OSError, http.client.HTTPException, ValueError) as exc:
                # OSError, not just URLError: connection resets are common with several long requests in flight.
                last_error = f"{type(exc).__name__}: {exc}"

            if attempt < MAX_ATTEMPTS:
                # Jittered exponential backoff: a whole fleet of trials retrying in step is
                # how a rate limit becomes an outage.
                delay = min(BACKOFF_CAP, BACKOFF_BASE ** attempt) * (0.5 + random.random())
                if "402" in last_error:
                    delay = max(delay, 30.0)   # waits for other trials' requests to settle
                if self.logger:
                    self.logger.warning(
                        "llm retry %d/%d in %.1fs: %s", attempt, MAX_ATTEMPTS, delay, last_error
                    )
                time.sleep(delay)

        raise LLMError(f"giving up after {MAX_ATTEMPTS} attempts: {last_error}")


CACHE_CONTROL = {"type": "ephemeral"}


def _mark(message: dict) -> bool:
    """Attach a cache breakpoint to a message's text content, in place. True if applied."""
    content = message.get("content")
    if isinstance(content, str):
        if not content:
            return False
        message["content"] = [
            {"type": "text", "text": content, "cache_control": CACHE_CONTROL}
        ]
        return True
    if isinstance(content, list) and content:
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                block["cache_control"] = CACHE_CONTROL
                return True
    return False


def apply_cache_control(messages: list[dict], tools: list[dict] | None):
    """Place cache breakpoints the way pi does, which is the way that actually works."""
    copied = [dict(message) for message in messages]
    for message in copied:
        content = message.get("content")
        if isinstance(content, list):
            message["content"] = [
                ({k: v for k, v in block.items() if k != "cache_control"}
                 if isinstance(block, dict) else block)
                for block in content
            ]

    for message in copied:
        if message.get("role") in ("system", "developer"):
            _mark(message)
            break

    for message in reversed(copied):
        if message.get("role") in ("user", "assistant", "tool") and _mark(message):
            break

    if tools:
        tools = [dict(tool) for tool in tools]
        tools[-1] = {**tools[-1], "cache_control": CACHE_CONTROL}
    return copied, tools
