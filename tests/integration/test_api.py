"""End-to-end HTTP tests: FastAPI app + real pipeline + fake ClinicalTrials.gov."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.contracts.plan import QueryPlan
from app.contracts.response import Clarification, ClarificationOption, LLMInfo
from app.planner.base import PlannerResult
from app.settings import Settings
from tests.conftest import study
from tests.integration.fake_ctgov import FakeCTGov

STUDIES = [
    study("NCT00000001", conditions=["Melanoma"], phases=["PHASE1"],
          interventions=[("Pembrolizumab", "DRUG")], countries=["France", "France"]),
    study("NCT00000002", conditions=["Melanoma"], phases=["PHASE3"],
          interventions=[("Keytruda", "DRUG"), ("Nivolumab", "DRUG")], countries=["Spain"]),
    study("NCT00000003", conditions=["Melanoma"], phases=["PHASE2", "PHASE3"],
          interventions=[("Nivolumab", "DRUG")], status="COMPLETED"),
    study("NCT00000004", conditions=["Lung Cancer"], phases=["PHASE3"],
          interventions=[("Pembrolizumab 200 mg", "DRUG")], countries=["France"]),
    study("NCT00000005", conditions=["Melanoma"], phases=[],
          interventions=[("Nivolumab", "DRUG"), ("Ipilimumab", "DRUG")]),
]


def phase_plan(**cohort: Any) -> dict[str, Any]:
    return {"cohorts": [{"label": "Melanoma", "condition": "melanoma", **cohort}],
            "analysis": {"kind": "aggregate", "dimension": "phase"}}


class ScriptedPlanner:
    def __init__(self, result: PlannerResult):
        self.result = result
        self.questions: list[str] = []
        self.constraints: list[str | None] = []

    async def plan(self, question: str, constraints: str | None = None) -> PlannerResult:
        self.questions.append(question)
        self.constraints.append(constraints)
        return self.result


def make_client(fake: FakeCTGov, planner: Any = None, **settings: Any) -> TestClient:
    s = Settings(llm_mode="openai", openai_api_key=None, **settings)  # no LLM unless injected
    return TestClient(create_app(s, client=fake.client(max_retries=1), planner=planner))


@pytest.fixture
def fake() -> FakeCTGov:
    return FakeCTGov(STUDIES, page_size=2)


@pytest.fixture
def api(fake: FakeCTGov) -> Iterator[TestClient]:
    with make_client(fake) as client:
        yield client


# ------------------------------------------------------------------ happy paths


def test_plan_to_verified_chart_with_evidence(api: TestClient) -> None:
    r = api.post("/query", json={"query": "q", "plan": phase_plan()})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    viz = body["visualization"]
    assert viz["type"] == "bar"
    assert [(d["phase"], d["study_count"]) for d in viz["data"]] == [
        ("Phase 1", 1), ("Phase 2/3", 1), ("Phase 3", 1), ("Not Reported", 1)]
    meta = body["meta"]
    assert meta["studies_analyzed"] == 4
    assert meta["api_queries"][0]["pages"] == 2 and meta["api_queries"][0]["complete"]
    assert "query.cond=melanoma" in meta["api_queries"][0]["url"]
    assert meta["data_timestamp"] == "2026-09-23T09:00:05"

    ref = viz["data"][2]["evidence"]["ref"]
    page = api.get(ref).json()
    assert page["total"] == 1
    assert page["items"][0]["nct_id"] == "NCT00000002"
    assert page["items"][0]["fields_used"]["designModule.phases"] == ["PHASE3"]
    assert page["items"][0]["url"] == "https://clinicaltrials.gov/study/NCT00000002"

    again = api.post("/query", json={"query": "q", "plan": phase_plan()}).json()
    assert again["query_id"] == body["query_id"]
    assert api.get(f"/query/{body['query_id']}").json()["query_id"] == body["query_id"]


def test_comparison_reports_cohort_overlap(api: TestClient) -> None:
    plan = {"cohorts": [{"label": "Pembrolizumab", "intervention": "pembrolizumab"},
                        {"label": "Nivolumab", "intervention": "nivolumab"}],
            "analysis": {"kind": "aggregate", "dimension": "phase", "series_by": "cohort"}}
    body = api.post("/query", json={"query": "q", "plan": plan}).json()
    assert body["status"] == "ok"
    # the fake API does substring search, so "Keytruda" is not found by "pembrolizumab"
    assert body["meta"]["cohort_overlap"] == {"Pembrolizumab ∩ Nivolumab": 0}
    assert body["visualization"]["type"] == "grouped_bar"


def test_structured_filters_are_reverified_locally(api: TestClient) -> None:
    plan = phase_plan(filters={"phase": ["PHASE3"]})
    body = api.post("/query", json={"query": "q", "plan": plan}).json()
    # fake API ignores filter.advanced; the pipeline drops non-Phase-3 studies itself
    assert body["meta"]["excluded"]["failed_local_filter"] == 2
    assert {d["phase"] for d in body["visualization"]["data"]} == {"Phase 2/3", "Phase 3"}
    assert any("Phase 2/3" in a for a in body["meta"]["assumptions"])


def test_network_endpoint_shapes(api: TestClient) -> None:
    plan = {"cohorts": [{"label": "Melanoma", "condition": "melanoma"}],
            "analysis": {"kind": "cooccurrence",
                         "pair": {"left": "intervention", "right": "intervention"}}}
    viz = api.post("/query", json={"query": "q", "plan": plan}).json()["visualization"]
    assert viz["type"] == "network"
    edges = {(e["source"], e["target"]): e["weight"] for e in viz["edges"]}
    assert edges == {("intervention:nivolumab", "intervention:pembrolizumab"): 1,
                     ("intervention:ipilimumab", "intervention:nivolumab"): 1}


# ------------------------------------------------------------------ honest non-answers


def test_zero_matches_is_empty_not_an_error(api: TestClient) -> None:
    body = api.post("/query", json={"query": "q", "plan": {
        "cohorts": [{"label": "X", "condition": "no such disease"}],
        "analysis": {"kind": "aggregate", "dimension": "phase"}}}).json()
    assert body["status"] == "empty"
    assert "No ClinicalTrials.gov studies match" in body["message"]
    assert "visualization" not in body


def test_too_broad_cohort_asks_to_narrow(fake: FakeCTGov) -> None:
    # 4 melanoma studies, 3 recruiting; the fake ignores filter.advanced, so only the
    # status narrowing reduces its count below the cap
    with make_client(fake, max_studies_per_cohort=3) as api:
        body = api.post("/query", json={"query": "q", "plan": phase_plan()}).json()
    assert body["status"] == "needs_clarification"
    assert "exceeds the 3-study limit" in body["message"]
    options = body["clarification"]["options"]
    assert [o["label"] for o in options] == ["Recruiting only"]  # the only narrowing that fits
    assert options[0]["plan"]["cohorts"][0]["filters"]["overall_status"] == ["RECRUITING"]


def test_failed_later_page_gives_partial_status(fake: FakeCTGov) -> None:
    fake.fail_page("2")
    with make_client(fake) as api:
        body = api.post("/query", json={"query": "q", "plan": phase_plan()}).json()
    assert body["status"] == "partial"
    assert not body["meta"]["completeness"]["complete"]
    assert "page 2 failed" in body["meta"]["completeness"]["reason"]


# ------------------------------------------------------------------ errors


@pytest.mark.parametrize(("status", "code", "http"), [
    (400, "upstream_rejected", 502), (503, "upstream_unavailable", 502)])
def test_upstream_errors_map_to_clear_codes(fake: FakeCTGov, status: int, code: str,
                                            http: int) -> None:
    fake.fail_all_studies(status)
    with make_client(fake) as api:
        r = api.post("/query", json={"query": "q", "plan": phase_plan()})
    assert r.status_code == http
    assert r.json()["code"] == code


def test_invalid_plans_and_requests(api: TestClient) -> None:
    bad = {"cohorts": [{"label": "A", "condition": "a"}], "analysis": {"kind": "aggregate"}}
    r = api.post("/query", json={"query": "q", "plan": bad})
    assert r.status_code == 422 and r.json()["code"] == "plan_invalid"
    assert any("exactly one" in e for e in r.json()["detail"])
    r = api.post("/query", json={"question": "x", "plan": phase_plan()})  # `query` missing
    assert r.status_code == 422 and r.json()["code"] == "invalid_request"
    r = api.post("/query", json={"query": "x", "trial_phase": "Phase 9"})
    assert r.status_code == 422 and "trial_phase" in str(r.json()["detail"])
    r = api.post("/query", json={"query": "q", "plan": {**phase_plan(), "counts": [1]}})
    assert r.status_code == 422


def test_question_without_llm(api: TestClient) -> None:
    r = api.post("/query", json={"query": "How are melanoma trials distributed by phase?"})
    assert r.status_code == 503 and r.json()["code"] == "llm_unavailable"


def test_expired_and_unknown_evidence(api: TestClient) -> None:
    assert api.get("/query/q_missing/evidence/r0").json()["code"] == "query_not_found"
    qid = api.post("/query", json={"query": "q", "plan": phase_plan()}).json()["query_id"]
    assert api.get(f"/query/{qid}/evidence/r999").json()["code"] == "evidence_not_found"


# ------------------------------------------------------------------ planner integration


def test_question_goes_through_planner(fake: FakeCTGov) -> None:
    llm = LLMInfo(model="scripted", tool_calls=["probe_cohort"])
    planner = ScriptedPlanner(PlannerResult(kind="plan", llm=llm,
                                            plan=QueryPlan.model_validate(phase_plan())))
    with make_client(fake, planner=planner) as api:
        body = api.post("/query", json={"query": "Melanoma trials by phase?"}).json()
    assert body["status"] == "ok"
    assert body["query"] == "Melanoma trials by phase?"
    assert body["meta"]["llm"]["tool_calls"] == ["probe_cohort"]


def test_planner_clarification_is_passed_through(fake: FakeCTGov) -> None:
    option = ClarificationOption(label="Start year", description="Trend by start year",
                                 plan=QueryPlan.model_validate(phase_plan()))
    planner = ScriptedPlanner(PlannerResult(
        kind="clarify", llm=LLMInfo(model="scripted"),
        clarification=Clarification(question="Which date?", options=[option])))
    with make_client(fake, planner=planner) as api:
        body = api.post("/query", json={"query": "Melanoma trials in 2020"}).json()
    assert body["status"] == "needs_clarification"
    assert body["clarification"]["options"][0]["plan"]["analysis"]["dimension"] == "phase"


# ------------------------------------------------------------------ discovery


def test_capabilities_schema_health(api: TestClient) -> None:
    caps = api.get("/capabilities").json()
    assert {d["name"] for d in caps["dimensions"]} >= {"phase", "country", "intervention"}
    schemas = api.get("/schema").json()
    assert {"QueryRequest", "QueryPlan", "QueryResponse"} <= set(schemas)
    health = api.get("/health").json()
    assert health["ctgov"]["reachable"] and health["llm"] is False


# ------------------------------------------------------------------ structured request fields


def test_structured_fields_constrain_the_plan(fake: FakeCTGov) -> None:
    llm = LLMInfo(model="scripted")
    planner = ScriptedPlanner(PlannerResult(kind="plan", llm=llm, plan=QueryPlan.model_validate(
        {"cohorts": [{"label": "Trials", "condition": "cancer"}],
         "analysis": {"kind": "aggregate", "dimension": "phase"}})))
    with make_client(fake, planner=planner) as api:
        body = api.post("/query", json={"query": "How are these trials distributed by phase?",
                                        "condition": "Melanoma", "status": "recruiting"}).json()
    assert body["status"] == "ok"
    # the planner was told, and the fields were enforced regardless of its reading
    assert "condition=Melanoma" in (planner.constraints[0] or "")
    assert body["plan"]["cohorts"][0]["condition"] == "Melanoma"
    assert body["meta"]["filters"] == {"Trials": {"condition": "Melanoma",
                                                  "overall_status": ["RECRUITING"]}}
    assert body["meta"]["request_fields"] == {"condition": "Melanoma",
                                              "status": ["RECRUITING"]}
    assert any("replacing the interpreted condition" in a for a in body["meta"]["assumptions"])
    assert sum(d["study_count"] for d in body["visualization"]["data"]) == 3  # recruiting melanoma


def test_field_that_collapses_a_comparison_is_rejected(fake: FakeCTGov) -> None:
    plan = QueryPlan.model_validate({
        "cohorts": [{"label": "Pembrolizumab", "intervention": "pembrolizumab"},
                    {"label": "Nivolumab", "intervention": "nivolumab"}],
        "analysis": {"kind": "aggregate", "dimension": "phase", "series_by": "cohort"}})
    planner = ScriptedPlanner(PlannerResult(kind="plan", llm=LLMInfo(model="s"), plan=plan))
    with make_client(fake, planner=planner) as api:
        r = api.post("/query", json={"query": "Compare them by phase", "drug_name": "Keytruda"})
    assert r.status_code == 422 and r.json()["code"] == "conflicting_fields"


def test_demo_page_is_served(api: TestClient) -> None:
    r = api.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert "vega-embed" in r.text and "d3" in r.text


# ------------------------------------------------------------------ public-deployment protections


def test_identical_questions_reuse_the_plan(fake: FakeCTGov) -> None:
    planner = ScriptedPlanner(PlannerResult(kind="plan", llm=LLMInfo(model="scripted"),
                                            plan=QueryPlan.model_validate(phase_plan())))
    with make_client(fake, planner=planner) as api:
        first = api.post("/query", json={"query": "Melanoma trials by phase?"}).json()
        again = api.post("/query", json={"query": "  melanoma TRIALS by phase?"}).json()
        other = api.post("/query", json={"query": "Melanoma trials by phase?",
                                         "status": "recruiting"}).json()
    assert len(planner.questions) == 2  # the repeat was served from the plan cache
    assert first["meta"]["llm"]["cached"] is False and again["meta"]["llm"]["cached"] is True
    assert again["visualization"]["data"] == first["visualization"]["data"]
    assert other["meta"]["llm"]["cached"] is False  # different fields -> new plan


def test_new_questions_are_rate_limited_but_repeats_are_not(fake: FakeCTGov) -> None:
    planner = ScriptedPlanner(PlannerResult(kind="plan", llm=LLMInfo(model="scripted"),
                                            plan=QueryPlan.model_validate(phase_plan())))
    with make_client(fake, planner=planner, llm_requests_per_client_per_hour=1) as api:
        assert api.post("/query", json={"query": "q1"}).status_code == 200
        limited = api.post("/query", json={"query": "q2"})
        assert limited.status_code == 429 and limited.json()["code"] == "rate_limited"
        assert int(limited.headers["Retry-After"]) > 0
        assert api.post("/query", json={"query": "q1"}).status_code == 200  # cached plan


def test_daily_cap(fake: FakeCTGov) -> None:
    planner = ScriptedPlanner(PlannerResult(kind="plan", llm=LLMInfo(model="scripted"),
                                            plan=QueryPlan.model_validate(phase_plan())))
    with make_client(fake, planner=planner, llm_requests_per_day=2) as api:
        codes = [api.post("/query", json={"query": f"q{i}"}).status_code for i in range(3)]
    assert codes == [200, 200, 429]
