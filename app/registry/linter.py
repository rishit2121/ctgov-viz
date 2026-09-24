"""Code-generated disclosures: the assumptions a reader must know, derived from the plan itself.

The LLM may add plain-English notes about how it read ambiguous wording, but the disclosures that
determine what a number *means* are produced here, deterministically, so they are always present.
"""

from __future__ import annotations

from app.contracts.plan import OverallStatus, QueryPlan


def disclosures(plan: QueryPlan) -> tuple[list[str], dict[str, str]]:
    assumptions: list[str] = []
    definitions: dict[str, str] = {}
    cohorts = plan.cohorts
    a = plan.analysis

    if any(c.condition for c in cohorts):
        definitions["condition_match"] = (
            "Studies are matched with ClinicalTrials.gov's condition search, which includes its "
            "own synonym expansion (e.g. 'lung cancer' also matches 'NSCLC').")
    if any(c.intervention for c in cohorts):
        definitions["intervention_match"] = (
            "Studies are matched with ClinicalTrials.gov's intervention search, which includes "
            "brand names and development codes (e.g. 'Keytruda', 'MK-3475').")
    if any(c.filters.phase for c in cohorts):
        assumptions.append(
            "A phase filter keeps studies whose registered phases include that phase, so "
            "multi-phase studies (e.g. Phase 2/3) are included in a Phase 3 filter.")
    if any(c.filters.overall_status == [OverallStatus.RECRUITING] for c in cohorts):
        assumptions.append(
            "'Recruiting' means overall status RECRUITING only; 'not yet recruiting' and "
            "'enrolling by invitation' studies are not included.")
    for c in cohorts:
        if c.filters.country:
            assumptions.append(f"'{c.label}' keeps studies with at least one site in "
                               f"{c.filters.country}.")
        if c.filters.start_date_from or c.filters.start_date_to:
            definitions["start_date_filter"] = (
                "Start-date bounds are inclusive and use the registered start date (actual or "
                "anticipated).")
    if len(cohorts) > 1:
        assumptions.append(
            "Each cohort is retrieved independently; a study matching several cohorts counts "
            "in each of their series (see meta.cohort_overlap).")
    if a.time is not None:
        assumptions.append("Studies without a registered start date are excluded from the trend.")
    if a.kind == "numeric_pair":
        assumptions.append("Studies missing either measure (or with 0 enrollment) are excluded.")
    if a.kind == "cooccurrence" and a.pair is not None and a.pair.left == a.pair.right:
        assumptions.append(
            "Intervention names are merged only by deterministic rules (case, trademark "
            "symbols, doses, formulation words, a fixed brand/code alias table); combination "
            "products registered under one name stay one node.")
    return assumptions, definitions
