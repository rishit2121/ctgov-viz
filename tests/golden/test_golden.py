"""Golden planner evaluation against the real LLM and real ClinicalTrials.gov (``-m live``).

    uv run pytest -m live tests/golden -q   (ANTHROPIC_API_KEY from the environment or .env)

Each case checks properties of the produced plan (see questions.yaml). A summary accuracy line
is printed at the end of the session.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from app.contracts.plan import QueryPlan
from app.ctgov.client import CTGovClient
from app.planner.base import PlannerError, PlannerResult
from app.planner.planner import make_planner
from app.settings import Settings

CASES: list[dict[str, Any]] = yaml.safe_load((Path(__file__).parent / "questions.yaml")
                                             .read_text())


def _llm_configured() -> bool:
    """Same lookup as the app: LLM_MODE plus its key, from the environment or .env."""
    s = Settings()
    return bool(s.anthropic_api_key if s.llm_mode == "anthropic" else
                s.openai_api_key if s.llm_mode == "openai" else False)


pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not _llm_configured(), reason="needs ANTHROPIC_API_KEY (or LLM_MODE="
                       "openai + OPENAI_API_KEY) in the environment or .env"),
]


def _texts(c: Any) -> str:
    return " ".join(filter(None, [c.condition, c.intervention, c.term, c.sponsor])).lower()


def check_plan(plan: QueryPlan, case: dict[str, Any]) -> list[str]:
    a, problems = plan.analysis, []

    def expect(name: str, actual: Any) -> None:
        if name in case and case[name] != actual:
            problems.append(f"{name}: expected {case[name]!r}, got {actual!r}")

    expect("kind", a.kind)
    expect("dimension", a.dimension.value if a.dimension else None)
    expect("series_by", a.series_by.value if a.series_by else None)
    expect("sort", a.sort.by)
    expect("n_cohorts", len(plan.cohorts))
    if "time" in case:
        if a.time is None:
            problems.append("time: expected a time axis")
        elif isinstance(case["time"], dict):
            expect_time = {k: getattr(a.time, k) for k in case["time"]}
            if expect_time != case["time"]:
                problems.append(f"time: expected {case['time']}, got {expect_time}")
    if "terms" in case:
        texts = [_texts(c) for c in plan.cohorts]
        for wanted in case["terms"]:
            alternatives = [w.lower() for w in wanted[0].split("|")]
            if not any(any(alt in t for alt in alternatives) for t in texts):
                problems.append(f"no cohort searches for {wanted[0]!r}: {texts}")
    for key in ("status", "phase"):
        if key in case:
            field = "overall_status" if key == "status" else "phase"
            for c in plan.cohorts:
                got = sorted(v.value for v in getattr(c.filters, field))
                if got != sorted(case[key]):
                    problems.append(f"{key}: expected {case[key]}, got {got} on {c.label}")
    if a.pair is not None:
        expect("pair", [a.pair.left.value, a.pair.right.value])
        expect("scope", a.pair.scope)
        expect("drugs_only", a.pair.drugs_only)
    elif "pair" in case:
        problems.append("expected a network pair")
    if "measures" in case:
        got = sorted(m.value for m in (a.x_measure, a.y_measure) if m)
        if got != sorted(case["measures"]):
            problems.append(f"measures: expected {case['measures']}, got {got}")
    return problems


RESULTS: list[bool] = []


@pytest.fixture
async def planner():  # type: ignore[no-untyped-def]
    settings = Settings()
    client = CTGovClient(settings.ctgov_base_url)
    yield make_planner(settings, client)
    await client.aclose()


@pytest.mark.parametrize("case", CASES, ids=[c["question"][:60] for c in CASES])
async def test_golden(planner: Any, case: dict[str, Any]) -> None:
    try:
        result: PlannerResult = await planner.plan(case["question"])
        decisions = case["decision"] if isinstance(case["decision"], list) else [
            case["decision"]]
        problems = [] if result.kind in decisions else [
            f"decision: expected {decisions}, got {result.kind}"]
        if not problems and result.kind == "plan" and result.plan is not None:
            problems = check_plan(result.plan, case)
    except PlannerError as e:
        problems = [f"planner error: {e.code}: {e.message} {e.detail or ''}".rstrip()]
    RESULTS.append(not problems)
    assert not problems, problems
