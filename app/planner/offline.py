"""Offline planner (``LLM_MODE=fake``): answers only the curated example questions.

Lets the full service run without an LLM key — e.g. for demos and CI — using the hand-checked
plans in ``examples/plans``. Any other question is reported as unsupported in this mode.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.contracts.plan import QueryPlan
from app.contracts.response import LLMInfo
from app.planner.base import PlannerResult

EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples" / "plans"


def _key(question: str) -> str:
    return " ".join(question.casefold().replace("?", " ").replace(".", " ").split())


class ExamplePlanner:
    def __init__(self, directory: Path = EXAMPLES_DIR):
        self.plans: dict[str, QueryPlan] = {}
        for path in sorted(directory.glob("*.json")):
            spec = json.loads(path.read_text())
            self.plans[_key(spec["question"])] = QueryPlan.model_validate(spec["plan"])

    async def plan(self, question: str) -> PlannerResult:
        info = LLMInfo(model="offline-examples")
        plan = self.plans.get(_key(question))
        if plan is None:
            return PlannerResult(
                kind="unsupported", llm=info,
                message="Offline mode (LLM_MODE=fake) only answers the example questions in "
                        "examples/plans. Configure OPENAI_API_KEY for arbitrary questions.")
        return PlannerResult(kind="plan", llm=info, plan=plan.model_copy(deep=True))
