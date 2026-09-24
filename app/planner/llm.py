"""Provider-neutral LLM interface plus the Anthropic (default) and OpenAI adapters.

The planner speaks only in the neutral types below; each adapter translates them to its own wire
format. A model reply keeps the provider's raw content (``LLMReply.raw``) so adapters can send it
back verbatim — Claude requires its thinking blocks to be returned unchanged in a tool loop.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.planner.base import PlannerError


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]  # strict JSON schema


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # JSON text


@dataclass
class LLMReply:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: Any = None  # provider-native assistant content, echoed back on the next request


@dataclass
class UserMessage:
    text: str


@dataclass
class ToolResult:
    call_id: str
    payload: dict[str, Any]


# A conversation is a list of these, in order.
Message = UserMessage | LLMReply | list[ToolResult]


class LLMClient(Protocol):
    model: str

    async def complete(self, system: str, messages: list[Message], tools: list[ToolSpec],
                       schema: dict[str, Any], allow_tools: bool = True) -> LLMReply:
        """``allow_tools=False`` keeps the tool definitions (history may reference them) but
        forbids new calls, forcing a final answer."""
        ...


# --------------------------------------------------------------------------- Anthropic


class AnthropicLLM:
    """Claude Messages API: client tools + strict JSON-schema final output.

    Adaptive thinking is on by default for Claude Opus 5; its thinking blocks are carried in
    ``LLMReply.raw`` and returned unchanged. Server-side refusal fallbacks are enabled so a
    safety-classifier decline is retried on Anthropic's recommended model instead of failing.
    """

    FALLBACK_BETA = "server-side-fallback-2026-07-01"

    def __init__(self, api_key: str, model: str, timeout_s: float, effort: str | None = None,
                 workspace_id: str | None = None):
        import anthropic

        self.model = model
        self.effort = effort
        # Keys that aren't scoped to a workspace must name one on every request.
        headers = {"anthropic-workspace-id": workspace_id} if workspace_id else None
        self._client = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout_s,
                                                max_retries=2, default_headers=headers)

    @staticmethod
    def to_wire(messages: list[Message]) -> list[dict[str, Any]]:
        wire: list[dict[str, Any]] = []
        for m in messages:
            if isinstance(m, UserMessage):
                wire.append({"role": "user", "content": m.text})
            elif isinstance(m, LLMReply):
                wire.append({"role": "assistant",
                             "content": m.raw if m.raw is not None else (m.content or "")})
            else:  # all tool results of one turn go back in a single user message
                wire.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": r.call_id,
                     "content": json.dumps(r.payload), "is_error": "error" in r.payload}
                    for r in m]})
        return wire

    async def complete(self, system: str, messages: list[Message], tools: list[ToolSpec],
                       schema: dict[str, Any], allow_tools: bool = True) -> LLMReply:
        import anthropic

        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": schema}}
        if self.effort:
            output_config["effort"] = self.effort
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 16000,
            "system": system,
            "messages": self.to_wire(messages),
            "tools": [{"name": t.name, "description": t.description,
                       "input_schema": t.input_schema, "strict": True} for t in tools],
            "tool_choice": {"type": "auto" if allow_tools else "none"},
            "output_config": output_config,
            "betas": [self.FALLBACK_BETA],
            "fallbacks": "default",
        }
        create: Any = self._client.beta.messages.create  # request built as a plain dict
        try:
            response = await create(**request)
        except anthropic.AuthenticationError as e:
            raise PlannerError("llm_unavailable", "The Anthropic API key was rejected; check "
                               "ANTHROPIC_API_KEY.") from e
        except anthropic.APIStatusError as e:
            raise PlannerError("llm_unavailable",
                               f"The Claude API call failed ({e.status_code}): {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise PlannerError("llm_unavailable", f"Could not reach the Claude API: {e}") from e

        if response.stop_reason == "refusal":
            raise PlannerError("llm_refused", "The model declined to plan this question.")
        if response.stop_reason == "max_tokens":
            raise PlannerError("llm_unavailable", "The model ran out of output tokens.")
        calls = [ToolCall(b.id, b.name, json.dumps(b.input))
                 for b in response.content if b.type == "tool_use"]
        text = "".join(b.text for b in response.content if b.type == "text") or None
        return LLMReply(content=text, tool_calls=calls, raw=response.content)


# --------------------------------------------------------------------------- OpenAI


class OpenAIChatLLM:
    """Chat Completions with strict JSON-schema output and function tools."""

    def __init__(self, api_key: str, model: str, timeout_s: float,
                 reasoning_effort: str | None = None):
        from openai import AsyncOpenAI

        self.model = model
        self.reasoning_effort = reasoning_effort
        self._client = AsyncOpenAI(api_key=api_key, timeout=timeout_s, max_retries=2)

    @staticmethod
    def to_wire(system: str, messages: list[Message]) -> list[dict[str, Any]]:
        wire: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for m in messages:
            if isinstance(m, UserMessage):
                wire.append({"role": "user", "content": m.text})
            elif isinstance(m, LLMReply):
                msg: dict[str, Any] = {"role": "assistant", "content": m.content or ""}
                if m.tool_calls:
                    msg["tool_calls"] = [{"id": c.id, "type": "function", "function": {
                        "name": c.name, "arguments": c.arguments}} for c in m.tool_calls]
                wire.append(msg)
            else:
                wire += [{"role": "tool", "tool_call_id": r.call_id,
                          "content": json.dumps(r.payload)} for r in m]
        return wire

    async def complete(self, system: str, messages: list[Message], tools: list[ToolSpec],
                       schema: dict[str, Any], allow_tools: bool = True) -> LLMReply:
        import openai

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": self.to_wire(system, messages),
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "planner_output", "schema": schema, "strict": True}},
        }
        if tools:
            kwargs["tools"] = [{"type": "function", "function": {
                "name": t.name, "description": t.description, "parameters": t.input_schema,
                "strict": True}} for t in tools]
            kwargs["tool_choice"] = "auto" if allow_tools else "none"
        if self.reasoning_effort:
            kwargs["reasoning_effort"] = self.reasoning_effort
        try:
            resp = await self._client.chat.completions.create(**kwargs)
        except openai.APIError as e:
            raise PlannerError("llm_unavailable", f"The language model call failed: {e}") from e
        msg = resp.choices[0].message
        calls = [ToolCall(tc.id, tc.function.name, tc.function.arguments)
                 for tc in msg.tool_calls or [] if tc.type == "function"]
        return LLMReply(content=msg.content, tool_calls=calls)


# --------------------------------------------------------------------------- tests


class ScriptedLLM:
    """Replays prepared replies in order and records what it was sent (tests)."""

    def __init__(self, replies: list[LLMReply], model: str = "scripted"):
        self.model = model
        self.replies = list(replies)
        self.calls: list[tuple[list[Message], bool]] = []  # (messages sent, tools allowed)

    async def complete(self, system: str, messages: list[Message], tools: list[ToolSpec],
                       schema: dict[str, Any], allow_tools: bool = True) -> LLMReply:
        self.calls.append((list(messages), allow_tools))
        if not self.replies:
            raise AssertionError("ScriptedLLM ran out of replies")
        return self.replies.pop(0)
