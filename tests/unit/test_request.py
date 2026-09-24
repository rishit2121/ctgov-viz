"""Request contract parsing and deterministic application of structured fields."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from pydantic import ValidationError

from app.contracts.plan import OverallStatus, Phase, QueryPlan
from app.contracts.request import QueryRequest
from app.registry.request_fields import (
    RequestFieldConflict,
    apply_request_fields,
    constraints_text,
)


@pytest.mark.parametrize(("raw", "phases"), [
    ("Phase 3", [Phase.PHASE3]), ("phase 2/3", [Phase.PHASE2, Phase.PHASE3]),
    ("PHASE3", [Phase.PHASE3]), ("3", [Phase.PHASE3]), ("Early Phase 1", [Phase.EARLY_PHASE1]),
    ("N/A", [Phase.NA]), (["Phase 1", "phase 1"], [Phase.PHASE1]),
])
def test_trial_phase_is_parsed_leniently(raw: Any, phases: list[Phase]) -> None:
    assert QueryRequest(query="q", trial_phase=raw).trial_phase == phases


@pytest.mark.parametrize(("raw", "statuses"), [
    ("recruiting", [OverallStatus.RECRUITING]),
    ("Active, not recruiting", [OverallStatus.ACTIVE_NOT_RECRUITING]),
    (["completed", "TERMINATED"], [OverallStatus.COMPLETED, OverallStatus.TERMINATED]),
])
def test_status_is_parsed_leniently(raw: Any, statuses: list[OverallStatus]) -> None:
    assert QueryRequest(query="q", status=raw).status == statuses


@pytest.mark.parametrize("bad", [
    {"query": ""}, {"query": "q", "trial_phase": "Phase 9"}, {"query": "q", "status": "ongoing"},
    {"query": "q", "start_year": 2020, "end_year": 2010}, {"query": "q", "start_year": 1500},
    {"query": "q", "drug": "x"}, {"trial_phase": "Phase 3"},
])
def test_invalid_requests_are_rejected(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        QueryRequest(**bad)


def test_blank_strings_mean_not_supplied() -> None:
    req = QueryRequest(query="q", drug_name="  ", condition="")
    assert req.structured_fields() == {}
    assert constraints_text(req) is None


PHASES = {"kind": "aggregate", "dimension": "phase"}


def plan(cohorts: list[dict[str, Any]], analysis: dict[str, Any] | None = None) -> QueryPlan:
    series = {"series_by": "cohort"} if len(cohorts) > 1 else {}
    return QueryPlan.model_validate({"cohorts": cohorts, "analysis": analysis or {**PHASES,
                                                                                    **series}})


def test_fields_are_applied_to_every_cohort() -> None:
    p = plan([{"label": "Breast", "condition": "breast cancer"},
              {"label": "Prostate", "condition": "prostate cancer"}])
    req = QueryRequest(query="q", trial_phase="Phase 3", country="Germany", start_year=2018,
                       end_year=2022, sponsor="NCI")
    out = apply_request_fields(p, req)
    for c in out.cohorts:
        assert c.filters.phase == [Phase.PHASE3] and c.filters.country == "Germany"
        assert c.filters.start_date_from == date(2018, 1, 1)
        assert c.filters.start_date_to == date(2022, 12, 31)
        assert c.sponsor == "NCI"
    assert [c.condition for c in out.cohorts] == ["breast cancer", "prostate cancer"]
    assert len(out.assumptions) == 5
    assert p.cohorts[0].filters.phase == []  # the input plan is not mutated


def test_explicit_field_overrides_the_interpretation_and_says_so() -> None:
    out = apply_request_fields(plan([{"label": "Drug", "intervention": "nivolumab"}]),
                               QueryRequest(query="q", drug_name="Pembrolizumab"))
    assert out.cohorts[0].intervention == "Pembrolizumab"
    assert "replacing the interpreted intervention in Drug" in out.assumptions[0]


def test_list_fields_override_existing_filters() -> None:
    p = plan([{"label": "Lung", "condition": "lung cancer",
               "filters": {"phase": ["PHASE2"], "overall_status": ["RECRUITING"]}}])
    out = apply_request_fields(p, QueryRequest(query="q", trial_phase="Phase 3",
                                               status="recruiting"))
    assert out.cohorts[0].filters.phase == [Phase.PHASE3]
    assert "replacing the interpreted phase in Lung" in out.assumptions[0]
    assert "replacing" not in out.assumptions[1]  # same status as interpreted


def test_years_also_set_the_trend_window() -> None:
    p = plan([{"label": "X", "condition": "x"}], {"kind": "aggregate", "time": {}})
    out = apply_request_fields(p, QueryRequest(query="q", start_year=2015))
    assert out.analysis.time is not None and out.analysis.time.from_year == 2015


def test_field_that_would_merge_a_comparison_is_a_conflict() -> None:
    p = plan([{"label": "A", "intervention": "pembrolizumab"},
              {"label": "B", "intervention": "nivolumab"}])
    with pytest.raises(RequestFieldConflict, match="drug_name"):
        apply_request_fields(p, QueryRequest(query="q", drug_name="Keytruda"))


def test_no_fields_is_a_no_op() -> None:
    p = plan([{"label": "X", "condition": "x"}])
    assert apply_request_fields(p, QueryRequest(query="q")) is p
