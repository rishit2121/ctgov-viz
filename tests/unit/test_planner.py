"""Planner loop with a scripted LLM: tools, budget, repair, clarify, unsupported, scrubbing."""

from __future__ import annotations

import json
import types
from typing import Any, Union, get_args, get_origin

import pytest
from pydantic import BaseModel

from app.planner.base import PlannerError
from app.planner.llm import LLMReply, ScriptedLLM, ToolCall
from app.planner.offline import ExamplePlanner
from app.planner.planner import OUTPUT_SCHEMA, LLMPlanner
from app.planner.prompt import EXAMPLES, system_prompt
from app.planner.schema import CohortDraft, PlannerOutput, to_plan
from app.registry.fields import REGISTRY
from app.registry.validator import validate_plan
from app.settings import Settings
from tests.conftest import study
from tests.integration.fake_ctgov import FakeCTGov

# ------------------------------------------------------------------ building strict outputs


def _fill(model: type[BaseModel], data: dict[str, Any]) -> dict[str, Any]:
    """Expand a compact dict into the full strict shape: missing fields -> null / [] / False."""
    out: dict[str, Any] = {}
    for name, info in model.model_fields.items():
        ann = info.annotation
        optional = get_origin(ann) in (Union, types.UnionType) and type(None) in get_args(ann)
        inner = next(a for a in get_args(ann) if a is not type(None)) if optional else ann
        value = data.get(name)
        if isinstance(inner, type) and issubclass(inner, BaseModel):
            if value is None and not optional:
                value = {}
            out[name] = None if value is None else _fill(inner, value)
        elif get_origin(inner) is list:
            item = get_args(inner)[0]
            is_model = isinstance(item, type) and issubclass(item, BaseModel)
            out[name] = [_fill(item, v) if is_model else v for v in value or []]
        elif inner is bool:
            out[name] = bool(value)
        else:
            out[name] = value
    return out


def output(compact: dict[str, Any]) -> str:
    full = _fill(PlannerOutput, compact)
    PlannerOutput.model_validate(full)  # the helper itself must produce schema-valid output
    return json.dumps(full)


PHASE_PLAN = {"cohorts": [{"label": "Melanoma", "condition": "melanoma"}],
              "analysis": {"kind": "aggregate", "dimension": "phase"}, "assumptions": []}


def planner(replies: list[LLMReply], studies: int = 3, **settings: Any) -> tuple[LLMPlanner,
                                                                                ScriptedLLM]:
    fake = FakeCTGov([study(f"NCT{i:08}", conditions=["Melanoma"]) for i in range(studies)],
                     page_size=10)
    llm = ScriptedLLM(replies)
    return LLMPlanner(llm, fake.client(), Settings(**settings)), llm


def answer(compact: dict[str, Any]) -> LLMReply:
    return LLMReply(content=output(compact))


def probe(call_id: str, condition: str = "melanoma") -> ToolCall:
    cohort = _fill(CohortDraft, {"label": "X", "condition": condition})
    return ToolCall(call_id, "probe_cohort", json.dumps({"cohort": cohort}))


# ------------------------------------------------------------------ happy paths


async def test_direct_plan() -> None:
    p, llm = planner([answer({"decision": "plan", "plan": PHASE_PLAN})])
    result = await p.plan("Melanoma trials by phase?")
    assert result.kind == "plan" and result.plan is not None
    assert result.plan.analysis.dimension is not None
    assert result.llm.tool_calls == [] and not result.llm.repaired
    system, user = llm.calls[0][0][:2]
    assert system["role"] == "system" and user == {"role": "user",
                                                   "content": "Melanoma trials by phase?"}


async def test_probe_tool_result_is_fed_back() -> None:
    p, llm = planner([LLMReply(content=None, tool_calls=[probe("c1")]),
                      answer({"decision": "plan", "plan": PHASE_PLAN})], studies=3)
    result = await p.plan("q")
    assert result.llm.tool_calls == ["probe_cohort"]
    tool_msg = llm.calls[1][0][-1]
    assert tool_msg["role"] == "tool" and tool_msg["tool_call_id"] == "c1"
    payload = json.loads(tool_msg["content"])
    assert payload["matches"] == 3 and len(payload["sample_titles"]) == 3


async def test_probe_reports_zero_and_too_broad() -> None:
    p, llm = planner([LLMReply(None, [probe("a", "nothing"), probe("b")]),
                      answer({"decision": "plan", "plan": PHASE_PLAN})],
                     studies=5, max_studies_per_cohort=4)
    await p.plan("q")
    msgs = [json.loads(m["content"]) for m in llm.calls[1][0] if m["role"] == "tool"]
    assert msgs[0]["matches"] == 0 and "No matches" in msgs[0]["hint"]
    assert msgs[1]["matches"] == 5 and "Too broad" in msgs[1]["hint"]


async def test_tool_budget_is_enforced() -> None:
    p, llm = planner([LLMReply(None, [probe(f"c{i}") for i in range(3)]),
                      LLMReply(None, [probe("c3"), probe("c4")]),
                      answer({"decision": "plan", "plan": PHASE_PLAN})],
                     planner_max_tool_calls=4)
    result = await p.plan("q")
    assert len(result.llm.tool_calls) == 4
    last_tool = json.loads(llm.calls[2][0][-1]["content"])
    assert "budget exhausted" in last_tool["error"]
    assert llm.calls[2][1] == []  # tools withdrawn once the budget is spent


async def test_validate_plan_tool() -> None:
    bad = {**PHASE_PLAN, "analysis": {"kind": "aggregate"}}
    draft = json.loads(output({"decision": "plan", "plan": bad}))["plan"]
    p, llm = planner([LLMReply(None, [ToolCall("v", "validate_plan",
                                               json.dumps({"plan": draft}))]),
                      answer({"decision": "plan", "plan": PHASE_PLAN})])
    await p.plan("q")
    payload = json.loads(llm.calls[1][0][-1]["content"])
    assert payload["valid"] is False and any("exactly one" in e for e in payload["errors"])


# ------------------------------------------------------------------ repair


async def test_invalid_plan_gets_one_repair_turn() -> None:
    bad = {**PHASE_PLAN, "analysis": {"kind": "aggregate", "dimension": "phase",
                                      "series_by": "cohort"}}
    p, llm = planner([answer({"decision": "plan", "plan": bad}),
                      answer({"decision": "plan", "plan": PHASE_PLAN})])
    result = await p.plan("q")
    assert result.kind == "plan" and result.llm.repaired
    feedback = llm.calls[1][0][-1]
    assert feedback["role"] == "user" and "requires at least two cohorts" in feedback["content"]


async def test_second_failure_raises_plan_invalid() -> None:
    p, _ = planner([LLMReply("not json"), LLMReply("{}")])
    with pytest.raises(PlannerError) as exc:
        await p.plan("q")
    assert exc.value.code == "plan_invalid"


async def test_missing_plan_body_is_repaired() -> None:
    p, _ = planner([answer({"decision": "plan"}),
                    answer({"decision": "plan", "plan": PHASE_PLAN})])
    assert (await p.plan("q")).llm.repaired


# ------------------------------------------------------------------ other outcomes


async def test_clarification_options_are_validated_plans() -> None:
    clarify = {"decision": "clarify", "clarification": {
        "question": "Which view?",
        "options": [{"label": "Phases", "description": "By phase", "plan": PHASE_PLAN},
                    {"label": "Trend", "description": "By year", "plan": {
                        **PHASE_PLAN, "analysis": {"kind": "aggregate", "time": {}}}}]}}
    p, _ = planner([answer(clarify)])
    result = await p.plan("q")
    assert result.kind == "clarify" and result.clarification is not None
    assert [o.label for o in result.clarification.options] == ["Phases", "Trend"]


async def test_unsupported() -> None:
    p, _ = planner([answer({"decision": "unsupported",
                            "unsupported_reason": "Outcomes are not analyzed."})])
    result = await p.plan("Which drug works best?")
    assert result.kind == "unsupported" and result.message == "Outcomes are not analyzed."


async def test_assumptions_repeating_probe_counts_are_dropped() -> None:
    plan = {**PHASE_PLAN, "assumptions": ["There are 1,234 melanoma studies.",
                                          "Phase 3 includes Phase 2/3 studies.",
                                          "Trend starts in 2015."]}
    p, _ = planner([LLMReply(None, [probe("c")]),
                    answer({"decision": "plan", "plan": plan})], studies=1234)
    result = await p.plan("q")
    assert result.plan is not None
    assert result.plan.assumptions == ["Phase 3 includes Phase 2/3 studies.",
                                       "Trend starts in 2015."]


# ------------------------------------------------------------------ prompt & schema


def test_prompt_is_generated_from_the_registry() -> None:
    prompt = system_prompt()
    for f in REGISTRY.values():
        assert f"- {f.name.value}: {f.title}" in prompt


@pytest.mark.parametrize(("question", "compact"), EXAMPLES, ids=[q for q, _ in EXAMPLES])
def test_prompt_examples_are_valid_outputs(question: str, compact: dict[str, Any]) -> None:
    out = PlannerOutput.model_validate_json(output(compact))
    drafts = [out.plan] if out.plan else [o.plan for o in
                                          (out.clarification.options if out.clarification
                                           else [])]
    for draft in drafts:
        assert validate_plan(to_plan(draft)) == [], question


def test_output_schema_is_strict() -> None:
    def walk(node: Any) -> None:
        if isinstance(node, dict):
            assert "default" not in node
            if "properties" in node:
                assert set(node["required"]) == set(node["properties"])
                assert node["additionalProperties"] is False
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(OUTPUT_SCHEMA)


async def test_offline_planner_answers_only_examples() -> None:
    offline = ExamplePlanner()
    known = await offline.plan("How many breast cancer trials started each year since 2015?")
    assert known.kind == "plan" and known.plan is not None
    assert known.plan.analysis.time is not None
    assert (await offline.plan("Something else entirely")).kind == "unsupported"
