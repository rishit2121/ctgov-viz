"""LLM-facing schemas (all fields required, optionals nullable, no defaults) and their conversion
to QueryPlan.

The internal ``QueryPlan`` uses defaults for readability; the model instead fills in these explicit
*draft* models, and ``to_plan`` converts a draft into a validated ``QueryPlan``. None of them has
anywhere to put a count, an NCT ID, or chart data.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.contracts.plan import Dimension, Measure, OverallStatus, Phase, QueryPlan, StudyType


class _Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FiltersDraft(_Draft):
    overall_status: list[OverallStatus] = Field(description="Empty list = any status.")
    phase: list[Phase] = Field(description="Empty list = any phase.")
    study_type: StudyType | None
    start_date_from: str | None = Field(description="YYYY-MM-DD, inclusive.")
    start_date_to: str | None = Field(description="YYYY-MM-DD, inclusive.")
    country: str | None = Field(description="Full English country name, e.g. 'South Korea'.")


class CohortDraft(_Draft):
    label: str = Field(description="Short display name, e.g. 'Nivolumab' or 'Lung cancer'.")
    condition: str | None = Field(description="Disease/condition search text.")
    intervention: str | None = Field(description="Drug/device/procedure search text.")
    term: str | None = Field(description="Free-text search; only when no other field fits.")
    sponsor: str | None = Field(description="Sponsor/collaborator search text.")
    filters: FiltersDraft


class TimeDraft(_Draft):
    from_year: int | None
    to_year: int | None


class PairDraft(_Draft):
    left: Dimension
    right: Dimension
    scope: Literal["study", "arm"] = Field(
        description="'arm' = given together in the same arm (combination questions).")
    exclude_placebo: bool
    exclude_ancillary: bool = Field(description="Drop imaging/biospecimen/questionnaire entries.")
    drugs_only: bool
    max_edges: int | None


class AnalysisDraft(_Draft):
    kind: Literal["aggregate", "cooccurrence", "numeric_pair"]
    dimension: Dimension | None
    time: TimeDraft | None
    series_by: Dimension | None
    pair: PairDraft | None
    x_measure: Measure | None
    y_measure: Measure | None
    top_k: int | None
    sort: Literal["domain", "count_desc", "chronological", "label"] | None


class PlanDraft(_Draft):
    cohorts: list[CohortDraft]
    analysis: AnalysisDraft
    assumptions: list[str] = Field(
        description="How ambiguous wording was interpreted, in plain English. No counts.")


class OptionDraft(_Draft):
    label: str
    description: str
    plan: PlanDraft


class ClarificationDraft(_Draft):
    question: str
    options: list[OptionDraft] = Field(description="2-3 complete alternative plans.")


# Arguments of the three answer tools; the model finishes by calling exactly one of them.
class SubmitPlanArgs(_Draft):
    plan: PlanDraft


class DeclareUnsupportedArgs(_Draft):
    reason: str = Field(description="What cannot be answered, and a similar question that can.")


def drop_nulls(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: drop_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [drop_nulls(v) for v in value]
    return value


def to_plan(draft: PlanDraft) -> QueryPlan:
    """Draft -> QueryPlan. Raises ``PlanConversionError`` with readable messages."""
    data = drop_nulls(draft.model_dump(mode="json"))
    analysis = data["analysis"]
    if "sort" in analysis:
        analysis["sort"] = {"by": analysis["sort"]}
    try:
        return QueryPlan.model_validate(data)
    except ValidationError as e:
        raise PlanConversionError([
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()
        ]) from e


class PlanConversionError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors
