"""Minimal LLM client interface plus the OpenAI adapter.

The planner depends only on ``LLMClient``; swapping providers means writing one adapter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from app.planner.base import PlannerError

Message = dict[str, Any]


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # JSON text


@dataclass
class LLMReply:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)


class LLMClient(Protocol):
    model: str

    async def complete(self, messages: list[Message], tools: list[dict[str, Any]],
                       schema: dict[str, Any]) -> LLMReply: ...


class OpenAIChatLLM:
    """Chat Completions with strict JSON-schema output and function tools."""

    def __init__(self, api_key: str, model: str, timeout_s: float,
                 reasoning_effort: str | None = None):
        from openai import AsyncOpenAI

        self.model = model
        self.reasoning_effort = reasoning_effort
        self._client = AsyncOpenAI(api_key=api_key, timeout=timeout_s, max_retries=2)

    async def complete(self, messages: list[Message], tools: list[dict[str, Any]],
                       schema: dict[str, Any]) -> LLMReply:
        import openai

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "planner_output", "schema": schema, "strict": True}},
        }
        if tools:
            kwargs["tools"] = tools
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


class ScriptedLLM:
    """Replays prepared replies in order and records what it was sent (tests)."""

    def __init__(self, replies: list[LLMReply], model: str = "scripted"):
        self.model = model
        self.replies = list(replies)
        self.calls: list[tuple[list[Message], list[dict[str, Any]]]] = []

    async def complete(self, messages: list[Message], tools: list[dict[str, Any]],
                       schema: dict[str, Any]) -> LLMReply:
        self.calls.append(([dict(m) for m in messages], tools))
        if not self.replies:
            raise AssertionError("ScriptedLLM ran out of replies")
        return self.replies.pop(0)
