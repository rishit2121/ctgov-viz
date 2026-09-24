"""Read-only tools the planner may call to ground its plan.

Tool results inform the *plan only*. Nothing a tool returns is copied into a response: the
pipeline re-retrieves and recomputes everything itself.
"""

from __future__ import annotations

import json
from typing import Any

import openai
from pydantic import BaseModel, ConfigDict, ValidationError

from app.contracts.plan import Cohort
from app.ctgov.client import CTGovClient
from app.ctgov.compiler import compile_cohort
from app.ctgov.errors import CTGovError
from app.planner.schema import CohortDraft, PlanConversionError, PlanDraft, drop_nulls, to_plan
from app.registry.validator import validate_plan


class ProbeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cohort: CohortDraft


class ValidateArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan: PlanDraft


TOOL_SPECS: list[dict[str, Any]] = [
    dict(openai.pydantic_function_tool(
        ProbeArgs, name="probe_cohort",
        description="Count the ClinicalTrials.gov studies a cohort matches and show 3 titles.")),
    dict(openai.pydantic_function_tool(
        ValidateArgs, name="validate_plan",
        description="Check a draft plan; returns a list of problems to fix (empty = valid).")),
]


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


def tool_message(call_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call_id, "content": json.dumps(payload)}
