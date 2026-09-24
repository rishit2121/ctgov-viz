"""Display labels and domain orderings for CT.gov enumerations.

Phase handling: a study registered as ``[PHASE2, PHASE3]`` is a single "Phase 2/3" study, so
phase distributions assign each study to exactly one bucket and bars sum to the study total.
"""

from __future__ import annotations

PHASE_LABELS = {
    "EARLY_PHASE1": "Early Phase 1",
    "PHASE1": "Phase 1",
    "PHASE2": "Phase 2",
    "PHASE3": "Phase 3",
    "PHASE4": "Phase 4",
    "NA": "Not Applicable",
}
PHASE_NOT_REPORTED = "Not Reported"
PHASE_ORDER = [
    "Early Phase 1", "Phase 1", "Phase 1/2", "Phase 2", "Phase 2/3", "Phase 3", "Phase 4",
    "Not Applicable", PHASE_NOT_REPORTED,
]
_PHASE_RANK = {p: i for i, p in enumerate(["EARLY_PHASE1", "PHASE1", "PHASE2", "PHASE3", "PHASE4",
                                           "NA"])}


def sort_phases(phases: list[str]) -> tuple[str, ...]:
    return tuple(sorted(set(phases), key=lambda p: _PHASE_RANK.get(p, 99)))


def phase_bucket(phases: tuple[str, ...]) -> str:
    """Single display bucket for a study's phase list."""
    if not phases:
        return PHASE_NOT_REPORTED
    if len(phases) == 1:
        return PHASE_LABELS.get(phases[0], phases[0])
    numbers = [PHASE_LABELS.get(p, p).removeprefix("Phase ") for p in phases]
    return "Phase " + "/".join(numbers)


STATUS_LABELS = {
    "NOT_YET_RECRUITING": "Not yet recruiting",
    "RECRUITING": "Recruiting",
    "ENROLLING_BY_INVITATION": "Enrolling by invitation",
    "ACTIVE_NOT_RECRUITING": "Active, not recruiting",
    "SUSPENDED": "Suspended",
    "TERMINATED": "Terminated",
    "COMPLETED": "Completed",
    "WITHDRAWN": "Withdrawn",
    "UNKNOWN": "Unknown status",
    "AVAILABLE": "Available (expanded access)",
    "NO_LONGER_AVAILABLE": "No longer available",
    "TEMPORARILY_NOT_AVAILABLE": "Temporarily not available",
    "APPROVED_FOR_MARKETING": "Approved for marketing",
    "WITHHELD": "Withheld",
}
STATUS_ORDER = [*STATUS_LABELS.values(), "Not Reported"]

SPONSOR_CLASS_LABELS = {
    "INDUSTRY": "Industry",
    "NIH": "NIH",
    "FED": "U.S. Federal (non-NIH)",
    "OTHER_GOV": "Other government",
    "NETWORK": "Network",
    "INDIV": "Individual",
    "OTHER": "Other (academic, hospital, non-profit)",
    "AMBIG": "Ambiguous",
    "UNKNOWN": "Unknown",
}
SPONSOR_CLASS_ORDER = [*SPONSOR_CLASS_LABELS.values(), "Not Reported"]

STUDY_TYPE_LABELS = {
    "INTERVENTIONAL": "Interventional",
    "OBSERVATIONAL": "Observational",
    "EXPANDED_ACCESS": "Expanded access",
}
STUDY_TYPE_ORDER = [*STUDY_TYPE_LABELS.values(), "Not Reported"]

INTERVENTION_TYPE_LABELS = {
    "DRUG": "Drug",
    "BIOLOGICAL": "Biological",
    "DEVICE": "Device",
    "PROCEDURE": "Procedure",
    "RADIATION": "Radiation",
    "BEHAVIORAL": "Behavioral",
    "GENETIC": "Genetic",
    "DIETARY_SUPPLEMENT": "Dietary supplement",
    "COMBINATION_PRODUCT": "Combination product",
    "DIAGNOSTIC_TEST": "Diagnostic test",
    "OTHER": "Other",
}


def label(mapping: dict[str, str], value: str | None) -> str:
    if value is None:
        return "Not Reported"
    return mapping.get(value, value.replace("_", " ").title())
