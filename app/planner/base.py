"""Planner interface: natural-language question -> plan, clarification, or refusal."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from app.contracts.plan import QueryPlan
from app.contracts.response import Clarification, LLMInfo


@dataclass
class PlannerResult:
    kind: Literal["plan", "clarify", "unsupported"]
    llm: LLMInfo
    plan: QueryPlan | None = None
    clarification: Clarification | None = None
    message: str | None = None


class PlannerError(Exception):
    """The planner could not produce a valid plan (after its repair attempt)."""

    def __init__(self, code: str, message: str, detail: object = None):
        super().__init__(message)
        self.code, self.message, self.detail = code, message, detail


class QuestionPlanner(Protocol):
    async def plan(self, question: str) -> PlannerResult: ...
