"""Question -> QueryPlan via a bounded, tool-assisted LLM loop.

    1. The model sees a registry-generated prompt and must answer in the strict PlannerOutput
       schema. It may first call read-only tools (probe_cohort, validate_plan), at most
       ``planner_max_tool_calls`` times; after that tool calls are disabled and it must answer.
    2. The answer is converted and validated in code. If invalid, the model gets exactly one
       repair turn with the full error list. Still invalid -> ``plan_invalid`` (never a guess).
    3. Assumption sentences that repeat a probed study count are dropped: counts only ever come
       from the pipeline.
"""

from __future__ import annotations

import re
from typing import Any

from openai.lib._pydantic import to_strict_json_schema
from pydantic import ValidationError

from app.contracts.plan import QueryPlan
from app.contracts.response import Clarification, ClarificationOption, LLMInfo
from app.ctgov.client import CTGovClient
from app.planner.base import PlannerError, PlannerResult, QuestionPlanner
from app.planner.llm import (
    AnthropicLLM,
    LLMClient,
    Message,
    OpenAIChatLLM,
    ToolResult,
    UserMessage,
)
from app.planner.prompt import system_prompt
from app.planner.schema import PlanConversionError, PlanDraft, PlannerOutput, to_plan
from app.planner.tools import TOOL_SPECS, Tools
from app.registry.validator import validate_plan
from app.settings import Settings

OUTPUT_SCHEMA = to_strict_json_schema(PlannerOutput)
_NUMBER_RE = re.compile(r"\d+(?:,\d{3})*")


class LLMPlanner:
    def __init__(self, llm: LLMClient, client: CTGovClient, settings: Settings):
        self.llm = llm
        self.client = client
        self.max_tool_calls = settings.planner_max_tool_calls
        self.max_studies = settings.max_studies_per_cohort

    async def plan(self, question: str) -> PlannerResult:
        tools = Tools(self.client, self.max_studies)
        info = LLMInfo(model=self.llm.model)
        system = system_prompt()
        messages: list[Message] = [UserMessage(question)]
        repaired = False
        while True:
            budget_left = self.max_tool_calls - len(info.tool_calls)
            reply = await self.llm.complete(system, messages, TOOL_SPECS, OUTPUT_SCHEMA,
                                            allow_tools=budget_left > 0)
            messages.append(reply)
            if reply.tool_calls:
                results: list[ToolResult] = []
                for call in reply.tool_calls:
                    if len(info.tool_calls) >= self.max_tool_calls:
                        payload: dict[str, Any] = {"error": "tool budget exhausted; answer now"}
                    else:
                        info.tool_calls.append(call.name)
                        payload = await tools.run(call.name, call.arguments)
                    results.append(ToolResult(call.id, payload))
                messages.append(results)
                continue

            errors, result = self._interpret(reply.content, info, tools.probe_totals)
            if result is not None:
                info.repaired = repaired
                return result
            if repaired:
                raise PlannerError("plan_invalid", "Could not turn the question into a valid "
                                   "query plan.", errors)
            repaired = True
            messages.append(UserMessage(
                "That output is not valid. Fix every problem below and answer again:\n- "
                + "\n- ".join(errors)))

    # ------------------------------------------------------------------ interpretation

    def _interpret(self, content: str | None, info: LLMInfo,
                   probe_totals: set[int]) -> tuple[list[str], PlannerResult | None]:
        try:
            out = PlannerOutput.model_validate_json(content or "")
        except ValidationError as e:
            return [f"output does not match the schema: {err['msg']} at "
                    f"{'.'.join(str(p) for p in err['loc'])}" for err in e.errors()[:5]], None

        if out.decision == "unsupported":
            reason = out.unsupported_reason or "This question cannot be answered from "\
                "ClinicalTrials.gov registration data."
            return [], PlannerResult(kind="unsupported", llm=info, message=reason)

        if out.decision == "plan":
            if out.plan is None:
                return ["decision='plan' requires `plan`"], None
            plan, errors = self._checked(out.plan)
            if plan is None:
                return errors, None
            plan.assumptions[:] = _scrub(plan.assumptions, probe_totals)
            return [], PlannerResult(kind="plan", llm=info, plan=plan)

        if out.clarification is None or not out.clarification.options:
            return ["decision='clarify' requires `clarification` with 2-3 options"], None
        options, errors = [], []
        for i, opt in enumerate(out.clarification.options):
            plan, problems = self._checked(opt.plan)
            if plan is None:
                errors += [f"clarification option {i + 1}: {p}" for p in problems]
            else:
                options.append(ClarificationOption(label=opt.label,
                                                   description=opt.description, plan=plan))
        if errors:
            return errors, None
        return [], PlannerResult(kind="clarify", llm=info, clarification=Clarification(
            question=out.clarification.question, options=options))

    @staticmethod
    def _checked(draft: PlanDraft) -> tuple[QueryPlan | None, list[str]]:
        try:
            plan = to_plan(draft)
        except PlanConversionError as e:
            return None, e.errors
        errors = validate_plan(plan)
        return (None, errors) if errors else (plan, [])


def _scrub(assumptions: list[str], probe_totals: set[int]) -> list[str]:
    """Drop assumption sentences that restate a probed study count.

    Small numbers and year-like numbers are ignored so "Phase 3" or "since 2015" survive.
    """
    counts = {n for n in probe_totals if n >= 10 and not 1900 <= n <= 2100}

    def mentions_count(text: str) -> bool:
        return any(int(n.replace(",", "")) in counts for n in _NUMBER_RE.findall(text))
    return [a for a in assumptions if not mentions_count(a)]


def make_planner(settings: Settings, client: CTGovClient) -> QuestionPlanner | None:
    """Pick the planner from ``LLM_MODE``: anthropic (default), openai, or fake (offline)."""
    llm: LLMClient
    if settings.llm_mode == "fake":
        from app.planner.offline import ExamplePlanner

        return ExamplePlanner()
    if settings.llm_mode == "anthropic":
        if not settings.anthropic_api_key:
            return None
        llm = AnthropicLLM(settings.anthropic_api_key, settings.anthropic_model,
                           settings.llm_timeout_s, settings.anthropic_effort)
    else:
        if not settings.openai_api_key:
            return None
        llm = OpenAIChatLLM(settings.openai_api_key, settings.openai_model,
                            settings.llm_timeout_s, settings.openai_reasoning_effort)
    return LLMPlanner(llm, client, settings)
