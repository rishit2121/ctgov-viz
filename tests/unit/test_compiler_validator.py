"""Plan -> API query compilation, local predicates, and semantic plan validation."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from pydantic import ValidationError

from app.contracts.plan import (
    Cohort,
    CohortFilters,
    OverallStatus,
    Phase,
    QueryPlan,
    TimeSpec,
)
from app.ctgov.compiler import compile_cohort, missing_start_params, sanitize_text
from app.registry.validator import PlanValidationError, ensure_valid, validate_plan
from tests.conftest import trial


def plan(**analysis: Any) -> QueryPlan:
    cohorts = analysis.pop("cohorts", [{"label": "Melanoma", "condition": "melanoma"}])
    return QueryPlan.model_validate({"cohorts": cohorts, "analysis": analysis})


# ------------------------------------------------------------------ compiler


def test_compile_pushes_down_structured_filters() -> None:
    c = Cohort(label="Lung", condition="lung cancer", filters=CohortFilters(
        overall_status=[OverallStatus.RECRUITING], phase=[Phase.PHASE3]))
    q = compile_cohort(c)
    assert q.params["query.cond"] == "lung cancer"
    assert q.params["filter.overallStatus"] == "RECRUITING"
    assert q.params["filter.advanced"] == "AREA[Phase]PHASE3"
    assert "NCTId" in q.params["fields"]


def test_compile_multiple_phases_uses_or() -> None:
    c = Cohort(label="x", intervention="nivolumab",
               filters=CohortFilters(phase=[Phase.PHASE2, Phase.PHASE3]))
    assert compile_cohort(c).params["filter.advanced"] == "AREA[Phase](PHASE2 OR PHASE3)"


def test_time_window_is_intersected_with_date_filters() -> None:
    c = Cohort(label="x", condition="breast cancer",
               filters=CohortFilters(start_date_from=date(2018, 6, 1)))
    q = compile_cohort(c, TimeSpec(from_year=2015, to_year=2020))
    assert q.params["filter.advanced"] == "AREA[StartDate]RANGE[2018-06-01,2020-12-31]"
    q = compile_cohort(c.model_copy(update={"filters": CohortFilters()}), TimeSpec(from_year=2015))
    assert q.params["filter.advanced"] == "AREA[StartDate]RANGE[2015-01-01,MAX]"


def test_missing_start_query_keeps_other_filters() -> None:
    c = Cohort(label="x", condition="breast cancer", filters=CohortFilters(
        phase=[Phase.PHASE3], start_date_from=date(2015, 1, 1)))
    params = missing_start_params(c)
    assert params["filter.advanced"] == "AREA[Phase]PHASE3 AND AREA[StartDate]MISSING"
    assert "fields" not in params


def test_search_text_cannot_inject_advanced_syntax() -> None:
    assert sanitize_text("melanoma AREA[Phase]PHASE1") == "melanoma PHASE1"
    assert sanitize_text("lung\x00 cancer\nRANGE[2000,2001]") == "lung cancer"
    c = Cohort(label="x", condition="AREA[StudyType]")
    assert "query.cond" not in compile_cohort(c).params


def test_local_predicate_rechecks_every_structured_filter() -> None:
    c = Cohort(label="x", condition="lung cancer", filters=CohortFilters(
        overall_status=[OverallStatus.RECRUITING], phase=[Phase.PHASE3],
        start_date_from=date(2019, 1, 1), country="Korea, Republic of"))
    ok = compile_cohort(c).predicate
    base: dict[str, Any] = dict(status="RECRUITING", phases=["PHASE2", "PHASE3"],
                                start="2019-03", countries=["South Korea"])
    assert ok(trial("NCT1", **base))
    assert not ok(trial("NCT2", **{**base, "status": "COMPLETED"}))
    assert not ok(trial("NCT3", **{**base, "phases": ["PHASE2"]}))
    assert not ok(trial("NCT4", **{**base, "start": "2018-12-31"}))
    assert not ok(trial("NCT5", **{**base, "start": None}))
    assert not ok(trial("NCT6", **{**base, "countries": ["Georgia"]}))


@pytest.mark.parametrize("wanted", ["turkey", "Turkey", "Türkiye", "TUR"])
def test_country_filter_matches_by_iso_code(wanted: str) -> None:
    ok = compile_cohort(Cohort(label="x", condition="c",
                               filters=CohortFilters(country=wanted))).predicate
    assert ok(trial("NCT1", countries=["Turkey (Türkiye)"]))
    assert not ok(trial("NCT2", countries=["Greece"]))


# ------------------------------------------------------------------ plan schema + validator


def test_plan_schema_forbids_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        plan(kind="aggregate", dimension="phase", counts=[1, 2])
    with pytest.raises(ValidationError):
        plan(kind="aggregate", dimension="not_a_field")


@pytest.mark.parametrize(
    ("analysis", "message"),
    [
        ({"kind": "aggregate"}, "exactly one of `dimension` or `time`"),
        ({"kind": "aggregate", "dimension": "phase", "time": {}}, "exactly one"),
        ({"kind": "aggregate", "dimension": "country", "series_by": "intervention"},
         "cannot be used as a series"),
        ({"kind": "aggregate", "dimension": "phase", "series_by": "phase"}, "must differ"),
        ({"kind": "aggregate", "dimension": "phase", "series_by": "cohort"},
         "requires at least two cohorts"),
        ({"kind": "aggregate", "dimension": "phase", "sort": {"by": "chronological"}},
         "requires a time axis"),
        ({"kind": "aggregate", "time": {"from_year": 2020, "to_year": 2015}}, "after"),
        ({"kind": "aggregate", "time": {"from_year": 1800}}, "outside supported range"),
        ({"kind": "aggregate", "dimension": "phase", "top_k": 500}, "top_k"),
        ({"kind": "cooccurrence"}, "requires `pair`"),
        ({"kind": "cooccurrence", "pair": {"left": "country", "right": "intervention"}},
         "cannot be used in a network"),
        ({"kind": "cooccurrence", "pair": {"left": "sponsor", "right": "intervention",
                                           "scope": "arm"}}, "arm scope"),
        ({"kind": "numeric_pair", "x_measure": "enrollment"}, "requires x_measure and y_measure"),
        ({"kind": "numeric_pair", "x_measure": "enrollment", "y_measure": "enrollment"},
         "must differ"),
    ],
)
def test_validator_rejects_illegal_plans(analysis: dict[str, Any], message: str) -> None:
    errors = validate_plan(plan(**analysis))
    assert any(message in e for e in errors), errors


def test_validator_rejects_unbounded_and_duplicate_cohorts() -> None:
    p = plan(kind="aggregate", dimension="phase", series_by="cohort",
             cohorts=[{"label": "A"}, {"label": "A", "intervention": "x"}])
    errors = validate_plan(p)
    assert any("unique" in e for e in errors)
    assert any("entire registry" in e for e in errors)


def test_comparison_requires_cohort_series() -> None:
    p = plan(kind="aggregate", dimension="phase",
             cohorts=[{"label": "A", "intervention": "a"}, {"label": "B", "intervention": "b"}])
    assert any("series_by='cohort'" in e for e in validate_plan(p))


def test_all_errors_reported_together() -> None:
    p = plan(kind="numeric_pair", dimension="phase", top_k=0)
    with pytest.raises(PlanValidationError) as exc:
        ensure_valid(p)
    assert len(exc.value.errors) >= 3


@pytest.mark.parametrize(
    "analysis",
    [
        {"kind": "aggregate", "time": {"from_year": 2015}, "sort": {"by": "chronological"}},
        {"kind": "aggregate", "dimension": "phase"},
        {"kind": "aggregate", "dimension": "country", "top_k": 15, "sort": {"by": "count_desc"}},
        {"kind": "aggregate", "dimension": "phase", "series_by": "sponsor_class"},
        {"kind": "cooccurrence", "pair": {"left": "intervention", "right": "intervention"}},
        {"kind": "cooccurrence", "pair": {"left": "intervention", "right": "intervention",
                                          "scope": "arm", "drugs_only": True}},
        {"kind": "cooccurrence", "pair": {"left": "sponsor", "right": "intervention"}},
        {"kind": "numeric_pair", "x_measure": "duration_months", "y_measure": "enrollment"},
    ],
)
def test_validator_accepts_supported_plans(analysis: dict[str, Any]) -> None:
    assert validate_plan(plan(**analysis)) == []
