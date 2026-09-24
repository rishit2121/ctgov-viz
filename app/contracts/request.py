"""Public request contract for ``POST /query``.

Only ``query`` is required. The optional structured fields are *hard constraints*: the planner is
told about them, and after planning they are applied to every cohort in code (see
``app.registry.request_fields``), so an explicit field always wins over the model's reading of the
question.

Phase and status values are parsed leniently ("Phase 3", "phase 2/3", "PHASE3", "3";
"recruiting", "Active, not recruiting") into the same closed enums the plan uses.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.contracts.plan import OverallStatus, Phase, QueryPlan

MIN_YEAR, MAX_YEAR = 1900, 2100

_PHASE_WORDS = {"1": Phase.PHASE1, "2": Phase.PHASE2, "3": Phase.PHASE3, "4": Phase.PHASE4,
                "EARLY1": Phase.EARLY_PHASE1, "0": Phase.EARLY_PHASE1, "NA": Phase.NA}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def parse_phases(value: Any) -> list[Phase]:
    """'Phase 2/3' -> [PHASE2, PHASE3]; 'early phase 1' -> [EARLY_PHASE1]; 'N/A' -> [NA]."""
    phases: list[Phase] = []
    for item in _as_list(value):
        if isinstance(item, Phase):
            phases.append(item)
            continue
        text = re.sub(r"[\s_\-]", "", str(item).upper()).replace("N/A", "NA")
        early = text.startswith("EARLY")
        text = text.removeprefix("EARLY").removeprefix("PHASE")
        if text in ("NA", "NOTAPPLICABLE"):
            phases.append(Phase.NA)
            continue
        parts = text.split("/") if text else []
        if not parts or not all(p in _PHASE_WORDS for p in parts):
            raise ValueError(f"unknown trial phase {item!r}; use e.g. 'Phase 3', 'Phase 2/3', "
                             "'Early Phase 1' or 'NA'")
        for p in parts:
            phases.append(Phase.EARLY_PHASE1 if early and p == "1" else _PHASE_WORDS[p])
    return list(dict.fromkeys(phases))


def parse_statuses(value: Any) -> list[OverallStatus]:
    """'recruiting' / 'Active, not recruiting' / 'NOT_YET_RECRUITING' -> enum values."""
    statuses: list[OverallStatus] = []
    for item in _as_list(value):
        key = re.sub(r"[^A-Z]+", "_", str(item).upper()).strip("_")
        try:
            statuses.append(OverallStatus(key))
        except ValueError as e:
            allowed = ", ".join(s.value.lower() for s in OverallStatus)
            raise ValueError(f"unknown status {item!r}; allowed: {allowed}") from e
    return list(dict.fromkeys(statuses))


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [
        {"query": "How has the number of trials for this drug changed over time?",
         "drug_name": "Pembrolizumab"},
        {"query": "Which countries have the most trials?", "condition": "lung cancer",
         "trial_phase": ["Phase 3"], "status": ["recruiting"]},
    ]})

    query: str = Field(min_length=1, max_length=1000,
                       description="Natural-language question about clinical trials.")
    drug_name: str | None = Field(None, max_length=200,
                                  description="Restrict to studies of this drug/intervention.")
    condition: str | None = Field(None, max_length=200,
                                  description="Restrict to studies of this condition/disease.")
    sponsor: str | None = Field(None, max_length=200,
                                description="Restrict to studies with this sponsor/collaborator.")
    country: str | None = Field(None, max_length=100,
                                description="Restrict to studies with a site in this country.")
    trial_phase: list[Phase] | None = Field(
        None, description="Restrict to studies that include any of these phases. Accepts "
        "'Phase 3', 'Phase 2/3', 'PHASE3', '3', 'Early Phase 1', 'NA'.")
    status: list[OverallStatus] | None = Field(
        None, description="Restrict to these overall statuses, e.g. 'recruiting', 'completed'.")
    start_year: int | None = Field(None, ge=MIN_YEAR, le=MAX_YEAR,
                                   description="Studies starting in or after this year.")
    end_year: int | None = Field(None, ge=MIN_YEAR, le=MAX_YEAR,
                                 description="Studies starting in or before this year.")
    plan: QueryPlan | None = Field(
        None, description="Advanced: execute this plan instead of interpreting `query` (e.g. a "
        "clarification option). Structured fields above still apply.")

    @field_validator("drug_name", "condition", "sponsor", "country", mode="before")
    @classmethod
    def _blank_is_none(cls, v: Any) -> Any:
        return v.strip() or None if isinstance(v, str) else v

    @field_validator("trial_phase", mode="before")
    @classmethod
    def _phases(cls, v: Any) -> list[Phase] | None:
        return parse_phases(v) or None

    @field_validator("status", mode="before")
    @classmethod
    def _statuses(cls, v: Any) -> list[OverallStatus] | None:
        return parse_statuses(v) or None

    @model_validator(mode="after")
    def _years(self) -> QueryRequest:
        if self.start_year and self.end_year and self.start_year > self.end_year:
            raise ValueError("start_year must not be after end_year")
        return self

    def structured_fields(self) -> dict[str, Any]:
        """The optional constraint fields that were actually supplied."""
        return self.model_dump(mode="json", exclude={"query", "plan"}, exclude_none=True)
