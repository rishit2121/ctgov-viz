"""QueryPlan: the only artifact the LLM produces.

A plan is a list of *cohorts* (named retrieval specs against ClinicalTrials.gov) plus a single
*analysis* describing how to aggregate them. Comparisons ("drug A vs drug B", "condition X vs Y")
are simply multiple cohorts with ``series_by="cohort"`` — no intent-specific code paths.

Shape-level constraints live here; cross-field semantic rules live in
``app.registry.validator`` so they can be reported back to the LLM as a list of messages.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Phase(StrEnum):
    EARLY_PHASE1 = "EARLY_PHASE1"
    PHASE1 = "PHASE1"
    PHASE2 = "PHASE2"
    PHASE3 = "PHASE3"
    PHASE4 = "PHASE4"
    NA = "NA"


class OverallStatus(StrEnum):
    NOT_YET_RECRUITING = "NOT_YET_RECRUITING"
    RECRUITING = "RECRUITING"
    ENROLLING_BY_INVITATION = "ENROLLING_BY_INVITATION"
    ACTIVE_NOT_RECRUITING = "ACTIVE_NOT_RECRUITING"
    SUSPENDED = "SUSPENDED"
    TERMINATED = "TERMINATED"
    COMPLETED = "COMPLETED"
    WITHDRAWN = "WITHDRAWN"
    UNKNOWN = "UNKNOWN"
    AVAILABLE = "AVAILABLE"
    NO_LONGER_AVAILABLE = "NO_LONGER_AVAILABLE"
    TEMPORARILY_NOT_AVAILABLE = "TEMPORARILY_NOT_AVAILABLE"
    APPROVED_FOR_MARKETING = "APPROVED_FOR_MARKETING"
    WITHHELD = "WITHHELD"


class StudyType(StrEnum):
    INTERVENTIONAL = "INTERVENTIONAL"
    OBSERVATIONAL = "OBSERVATIONAL"
    EXPANDED_ACCESS = "EXPANDED_ACCESS"


class Dimension(StrEnum):
    """Categorical fields that can be grouped on, paired, or used as a series."""

    phase = "phase"
    overall_status = "overall_status"
    intervention = "intervention"
    intervention_type = "intervention_type"
    condition = "condition"
    sponsor = "sponsor"
    sponsor_class = "sponsor_class"
    country = "country"
    study_type = "study_type"
    enrollment_size = "enrollment_size"  # binned numeric -> histogram
    duration = "duration"  # binned numeric (start -> primary completion) -> histogram
    cohort = "cohort"  # virtual: the plan's cohort label


class Measure(StrEnum):
    enrollment = "enrollment"
    duration_months = "duration_months"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CohortFilters(_Strict):
    """Structured filters. Pushed down to the API where possible and always re-verified locally."""

    overall_status: list[OverallStatus] = Field(
        default_factory=list, description="Match any of these overall statuses (empty = any)."
    )
    phase: list[Phase] = Field(
        default_factory=list,
        description="Match studies whose phase list contains any of these (empty = any).",
    )
    study_type: StudyType | None = None
    start_date_from: date | None = Field(None, description="Inclusive lower bound on start date.")
    start_date_to: date | None = Field(None, description="Inclusive upper bound on start date.")
    country: str | None = Field(
        None, description="Restrict to studies with a site in this country."
    )


class Cohort(_Strict):
    label: str = Field(
        description="Short display name used as the series label, e.g. 'Nivolumab'."
    )
    condition: str | None = Field(None, description="Condition/disease search text (query.cond).")
    intervention: str | None = Field(
        None, description="Drug/intervention search text (query.intr)."
    )
    term: str | None = Field(None, description="Free-text search (query.term); use sparingly.")
    sponsor: str | None = Field(None, description="Sponsor/collaborator search text (query.spons).")
    filters: CohortFilters = Field(default_factory=CohortFilters)


class TimeSpec(_Strict):
    field: Literal["start_date"] = "start_date"
    granularity: Literal["year"] = "year"
    from_year: int | None = None
    to_year: int | None = None


class PairSpec(_Strict):
    left: Dimension
    right: Dimension
    scope: Literal["study", "arm"] = Field(
        "study",
        description="'study': both listed in the same study. 'arm': both assigned to the same arm "
        "group (i.e. given together — use for 'combination' questions).",
    )
    exclude_placebo: bool = Field(
        True, description="Drop placebo/sham/standard-of-care interventions from the network."
    )
    exclude_ancillary: bool = Field(
        True, description="Drop assessment/data-collection entries (imaging, biospecimen "
        "collection, questionnaires) that are registered as interventions but are not treatments."
    )
    drugs_only: bool = Field(
        False, description="Keep only DRUG/BIOLOGICAL interventions (for 'drug' networks)."
    )
    max_edges: int = 40


class SortSpec(_Strict):
    by: Literal["domain", "count_desc", "chronological", "label"] = "domain"


class Analysis(_Strict):
    kind: Literal["aggregate", "cooccurrence", "numeric_pair"]
    dimension: Dimension | None = Field(
        None, description="Category axis for kind=aggregate (omit when `time` is set)."
    )
    time: TimeSpec | None = Field(None, description="Temporal axis for kind=aggregate trends.")
    series_by: Dimension | None = Field(
        None, description="Split into series. Use 'cohort' when comparing 2+ cohorts."
    )
    pair: PairSpec | None = None
    x_measure: Measure | None = None
    y_measure: Measure | None = None
    top_k: int | None = Field(None, description="Keep only the k largest categories / nodes.")
    sort: SortSpec = Field(default_factory=SortSpec)


class QueryPlan(_Strict):
    version: Literal["1"] = "1"
    cohorts: list[Cohort] = Field(min_length=1, max_length=4)
    analysis: Analysis
    assumptions: list[str] = Field(
        default_factory=list,
        description="Interpretation choices made for ambiguous wording, in plain English.",
    )
