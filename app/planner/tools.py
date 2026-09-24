"""Read-only tools the planner may call to ground its plan.

Tool results inform the *plan only*. Nothing a tool returns is copied into a response: the
pipeline re-retrieves and recomputes everything itself.
"""

from __future__ import annotations

from typing import Any

from openai.lib._pydantic import to_strict_json_schema
from pydantic import BaseModel, ConfigDict, ValidationError

from app.contracts.plan import Cohort
from app.ctgov.client import CTGovClient
from app.ctgov.compiler import compile_cohort
from app.ctgov.errors import CTGovError
from app.planner.llm import ToolSpec
from app.planner.schema import (
    ClarificationDraft,
    CohortDraft,
    DeclareUnsupportedArgs,
    PlanConversionError,
    PlanDraft,
    SubmitPlanArgs,
    drop_nulls,
    to_plan,
)
from app.registry.validator import validate_plan


class ProbeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cohort: CohortDraft


class ValidateArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan: PlanDraft


# Research tools (read-only, budgeted) and answer tools (end the loop). Schemas small enough are
# strict (grammar-constrained decoding). The plan-carrying schemas are too large to compile as
# grammars, so they are validated in code instead: Pydantic + the plan validator + a repair turn.
TOOL_SPECS: list[ToolSpec] = [
    ToolSpec("probe_cohort", "Count the ClinicalTrials.gov studies a cohort matches and show "
             "3 titles.", to_strict_json_schema(ProbeArgs), strict=True),
    ToolSpec("validate_plan", "Check a draft plan; returns a list of problems to fix (empty = "
             "valid).", to_strict_json_schema(ValidateArgs)),
    ToolSpec("submit_plan", "Final answer: the query plan for the question.",
             to_strict_json_schema(SubmitPlanArgs)),
    ToolSpec("ask_clarification", "Final answer when the question is genuinely ambiguous: a "
             "clarifying question with 2-3 complete alternative plans.",
             to_strict_json_schema(ClarificationDraft)),
    ToolSpec("declare_unsupported", "Final answer when the question cannot be answered from "
             "ClinicalTrials.gov registration data.",
             to_strict_json_schema(DeclareUnsupportedArgs), strict=True),
]
RESEARCH_TOOLS = frozenset({"probe_cohort", "validate_plan"})
ANSWER_TOOLS = frozenset({"submit_plan", "ask_clarification", "declare_unsupported"})


class Tools:
    def __init__(self, client: CTGovClient, max_studies: int):
        self.client = client
        self.max_studies = max_studies
        self.probe_totals: set[int] = set()

    async def run(self, name: str, arguments: str) -> dict[str, Any]:
        try:
            if name == "probe_cohort":
                return await self._probe(ProbeArgs.model_validate_json(arguments).cohort)
            if name == "validate_plan":
                return self._validate(ValidateArgs.model_validate_json(arguments).plan)
        except ValidationError as e:
            return {"error": f"invalid arguments: {e.errors()[:3]}"}
        return {"error": f"unknown tool {name!r}"}

    async def _probe(self, draft: CohortDraft) -> dict[str, Any]:
        try:
            cohort = Cohort.model_validate(drop_nulls(draft.model_dump(mode="json")))
        except ValidationError as e:
            return {"error": f"invalid cohort: {e.errors()[:3]}"}
        params = compile_cohort(cohort).params
        try:
            total = await self.client.count(params)
            sample = await self.client.sample(params, 3) if total else []
        except CTGovError as e:
            return {"error": f"ClinicalTrials.gov rejected this search: {e}"}
        self.probe_totals.add(total)
        titles = [s.get("protocolSection", {}).get("identificationModule", {}).get("briefTitle")
                  for s in sample]
        result: dict[str, Any] = {"matches": total, "sample_titles": titles}
        if total == 0:
            result["hint"] = "No matches: try another field, a synonym or broader wording."
        elif total > self.max_studies:
            result["hint"] = (f"Too broad (limit {self.max_studies:,}): add a filter such as "
                              "status, phase or start date, or clarify.")
        return result

    @staticmethod
    def _validate(draft: PlanDraft) -> dict[str, Any]:
        try:
            errors = validate_plan(to_plan(draft))
        except PlanConversionError as e:
            errors = e.errors
        return {"valid": not errors, "errors": errors}
