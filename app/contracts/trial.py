"""Canonical, API-independent representation of one ClinicalTrials.gov study.

Internal type (plain frozen dataclasses: fast to build for tens of thousands of records).
Missing values stay explicit (``None`` / empty tuple) — nothing is imputed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

DatePrecision = Literal["day", "month", "year"]


@dataclass(frozen=True, slots=True)
class DateValue:
    raw: str | None = None
    year: int | None = None
    month: int | None = None
    day: int | None = None
    precision: DatePrecision | None = None
    kind: str | None = None  # "ACTUAL" | "ESTIMATED" | None


@dataclass(frozen=True, slots=True)
class Intervention:
    key: str  # normalized grouping key, e.g. "pembrolizumab"
    label: str  # cleaned display name, e.g. "Pembrolizumab"
    raw_name: str  # exactly as registered, e.g. "Pembrolizumab (KEYTRUDA®)"
    type: str | None  # CT.gov enum: DRUG, BIOLOGICAL, DEVICE, ...
    is_placebo: bool = False  # placebo / sham / usual-care comparator
    is_ancillary: bool = False  # assessment or data collection (imaging, biospecimens, surveys)


@dataclass(frozen=True, slots=True)
class Trial:
    nct_id: str
    title: str | None
    overall_status: str | None
    study_type: str | None
    phases: tuple[str, ...]  # sorted, deduped source enums, e.g. ("PHASE2", "PHASE3")
    conditions: tuple[str, ...]
    interventions: tuple[Intervention, ...]  # deduped by key
    arm_intervention_keys: tuple[frozenset[str], ...]  # intervention keys per arm group
    sponsor_name: str | None
    sponsor_class: str | None
    countries: tuple[str, ...]  # canonical, deduped, sorted
    country_iso3: dict[str, str | None]
    start: DateValue
    primary_completion: DateValue
    enrollment: int | None
    enrollment_type: str | None
    # API field path -> raw source value, for field-level evidence.
    source: dict[str, Any] = field(default_factory=dict)
    # The record's protocolSection exactly as the API returned it (already field-projected).
    # Citation excerpts are read from here by exact path, e.g. "designModule.phases[0]".
    record: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def url(self) -> str:
        return f"https://clinicaltrials.gov/study/{self.nct_id}"
