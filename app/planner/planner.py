"""Question -> QueryPlan via a bounded, tool-assisted LLM loop.

    1. The model sees a registry-generated prompt and a fixed tool set. It may call read-only
       research tools (probe_cohort, validate_plan) at most ``planner_max_tool_calls`` times,
       and must finish by calling exactly one answer tool: submit_plan, ask_clarification or
       declare_unsupported.
    2. The answer is converted and validated in code. If invalid, the model gets exactly one
       repair turn with the full error list. Still invalid -> ``plan_invalid`` (never a guess).
    3. Assumption sentences that repeat a probed study count are dropped: counts only ever come
       from the pipeline.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ValidationError

from app.contracts.plan import QueryPlan
from app.contracts.response import Clarification, ClarificationOption, LLMInfo
from app.ctgov.client import CTGovClient
from app.planner.base import PlannerError, PlannerResult, QuestionPlanner
from app.planner.llm import (
    AnthropicLLM,
    LLMClient,
    Message,
    OpenAIChatLLM,
    ToolCall,
    ToolResult,
    UserMessage,
)
from app.planner.prompt import system_prompt
from app.planner.schema import (
    ClarificationDraft,
    DeclareUnsupportedArgs,
    PlanConversionError,
    PlanDraft,
    SubmitPlanArgs,
    to_plan,
)
from app.planner.tools import ANSWER_TOOLS, TOOL_SPECS, Tools
from app.registry.validator import validate_plan
from app.settings import Settings

_NUMBER_RE = re.compile(r"\d+(?:,\d{3})*")
NO_ANSWER = ("Finish by calling exactly one answer tool: submit_plan, ask_clarification or "
             "declare_unsupported.")


class LLMPlanner:
    def __init__(self, llm: LLMClient, client: CTGovClient, settings: Settings):
        self.llm = llm
        self.client = client
        self.max_tool_calls = settings.planner_max_tool_calls
        self.max_studies = settings.max_studies_per_cohort

    async def plan(self, question: str, constraints: str | None = None) -> PlannerResult:
        tools = Tools(self.client, self.max_studies)
        info = LLMInfo(model=self.llm.model)
        system = system_prompt()
        messages: list[Message] = [
            UserMessage(f"{question}\n\n{constraints}" if constraints else question)]
        repaired = False
        # research turns + one answer + one repair, with slack for batched calls
        for _ in range(self.max_tool_calls + 3):
            reply = await self.llm.complete(system, messages, TOOL_SPECS)
            messages.append(reply)
            answer = next((c for c in reply.tool_calls if c.name in ANSWER_TOOLS), None)
            errors: list[str] = []
            if answer is not None:
                errors, result = self._interpret(answer, info, tools.probe_totals)
                if result is not None:
                    info.repaired = repaired
                    return result
            elif not reply.tool_calls:
                errors = [NO_ANSWER]
            if errors:
                if repaired:
                    raise PlannerError("plan_invalid", "Could not turn the question into a "
                                       "valid query plan.", errors)
                repaired = True
            if not reply.tool_calls:
                messages.append(UserMessage(NO_ANSWER))
                continue

            # Every tool call gets a result, in one message.
            results: list[ToolResult] = []
            for call in reply.tool_calls:
                payload: dict[str, Any]
                if call is answer:
                    payload = {"error": "This answer is not valid. Fix every problem and "
                               "answer again.", "problems": errors}
                elif call.name in ANSWER_TOOLS:
                    payload = {"error": "Only one answer tool may be called."}
                elif len(info.tool_calls) >= self.max_tool_calls:
                    payload = {"error": f"Research budget used up. {NO_ANSWER}"}
                else:
                    info.tool_calls.append(call.name)
                    payload = await tools.run(call.name, call.arguments)
                results.append(ToolResult(call.id, payload))
            messages.append(results)
        raise PlannerError("plan_invalid", "The model did not produce an answer.", [NO_ANSWER])

    # ------------------------------------------------------------------ interpretation

    def _interpret(self, call: ToolCall, info: LLMInfo,
                   probe_totals: set[int]) -> tuple[list[str], PlannerResult | None]:
        if call.name == "declare_unsupported":
            unsupported = _parse(DeclareUnsupportedArgs, call.arguments)
            if isinstance(unsupported, list):
                return unsupported, None
            return [], PlannerResult(kind="unsupported", llm=info, message=unsupported.reason)

        if call.name == "submit_plan":
            submitted = _parse(SubmitPlanArgs, call.arguments)
            if isinstance(submitted, list):
                return submitted, None
            plan, errors = self._checked(submitted.plan)
            if plan is None:
                return errors, None
            plan.assumptions[:] = _scrub(plan.assumptions, probe_totals)
            return [], PlannerResult(kind="plan", llm=info, plan=plan)

        clarification = _parse(ClarificationDraft, call.arguments)
        if isinstance(clarification, list):
            return clarification, None
        if not clarification.options:
            return ["ask_clarification needs 2-3 options, each with a complete plan"], None
        options, errors = [], []
        for i, opt in enumerate(clarification.options):
            plan, problems = self._checked(opt.plan)
            if plan is None:
                errors += [f"option {i + 1}: {p}" for p in problems]
            else:
                options.append(ClarificationOption(label=opt.label,
                                                   description=opt.description, plan=plan))
        if errors:
            return errors, None
        return [], PlannerResult(kind="clarify", llm=info, clarification=Clarification(
            question=clarification.question, options=options))

    @staticmethod
    def _checked(draft: PlanDraft) -> tuple[QueryPlan | None, list[str]]:
        try:
            plan = to_plan(draft)
        except PlanConversionError as e:
            return None, e.errors
        errors = validate_plan(plan)
        return (None, errors) if errors else (plan, [])


def _parse[M: BaseModel](model: type[M], arguments: str) -> M | list[str]:
    """Validate answer-tool arguments; readable errors (for the repair turn) on failure."""
    try:
        return model.model_validate_json(arguments or "{}")
    except ValidationError as e:
        return [f"{'.'.join(str(p) for p in err['loc']) or 'arguments'}: {err['msg']}"
                for err in e.errors()[:8]]


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
                           settings.llm_timeout_s, settings.anthropic_effort,
                           settings.anthropic_workspace_id)
    else:
        if not settings.openai_api_key:
            return None
        llm = OpenAIChatLLM(settings.openai_api_key, settings.openai_model,
                            settings.llm_timeout_s, settings.openai_reasoning_effort)
    return LLMPlanner(llm, client, settings)
