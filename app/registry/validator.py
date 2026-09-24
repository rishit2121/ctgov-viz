"""Semantic plan validation (cross-field rules the JSON schema cannot express).

Returns human-readable messages rather than raising on the first problem, so the planner can
feed the complete list back to the LLM for a single repair attempt.
"""

from __future__ import annotations

from datetime import date

from app.contracts.plan import Dimension, QueryPlan
from app.registry.fields import REGISTRY, allows

MAX_TOP_K = 50
MIN_YEAR = 1990


class PlanValidationError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def validate_plan(plan: QueryPlan) -> list[str]:
    errors: list[str] = []
    a = plan.analysis
    labels = [c.label for c in plan.cohorts]
    if len(set(labels)) != len(labels):
        errors.append("cohort labels must be unique")

    for c in plan.cohorts:
        has_search = any([c.condition, c.intervention, c.term, c.sponsor])
        f = c.filters
        has_filter = bool(f.overall_status or f.phase or f.study_type or f.country
                          or f.start_date_from or f.start_date_to)
        if not has_search and not has_filter:
            errors.append(f"cohort '{c.label}' has no search terms or filters (would match "
                          "the entire registry)")
        if f.start_date_from and f.start_date_to and f.start_date_from > f.start_date_to:
            errors.append(f"cohort '{c.label}': start_date_from is after start_date_to")

    multi = len(plan.cohorts) > 1
    if multi and a.series_by != Dimension.cohort:
        errors.append("multiple cohorts require analysis.series_by='cohort'")
    if a.series_by == Dimension.cohort and not multi:
        errors.append("series_by='cohort' requires at least two cohorts")

    if a.kind == "aggregate":
        if (a.dimension is None) == (a.time is None):
            errors.append("aggregate analysis needs exactly one of `dimension` or `time`")
        if a.dimension is not None and not allows(a.dimension, "group"):
            errors.append(f"dimension '{a.dimension}' cannot be grouped on")
        if a.series_by not in (None, Dimension.cohort):
            if not allows(a.series_by, "series"):
                errors.append(f"'{a.series_by}' cannot be used as a series (too many values); "
                              f"allowed: {_names('series')}")
            if a.series_by == a.dimension:
                errors.append("series_by must differ from dimension")
        if a.pair is not None or a.x_measure or a.y_measure:
            errors.append("aggregate analysis must not set pair or measures")
        if a.time is not None:
            lo, hi = a.time.from_year, a.time.to_year
            max_year = date.today().year + 10
            for y in (lo, hi):
                if y is not None and not MIN_YEAR <= y <= max_year:
                    errors.append(f"year {y} outside supported range {MIN_YEAR}-{max_year}")
            if lo is not None and hi is not None and lo > hi:
                errors.append("time.from_year is after time.to_year")
            if a.sort.by not in ("chronological", "domain"):
                errors.append("time series must be sorted chronologically")
        elif a.sort.by == "chronological":
            errors.append("chronological sort requires a time axis")

    elif a.kind == "cooccurrence":
        if a.pair is None:
            errors.append("cooccurrence analysis requires `pair`")
        else:
            for side in (a.pair.left, a.pair.right):
                if not allows(side, "pair"):
                    errors.append(f"'{side}' cannot be used in a network; allowed: "
                                  f"{_names('pair')}")
            if a.pair.scope == "arm" and {a.pair.left, a.pair.right} != {Dimension.intervention}:
                errors.append("arm scope only applies to intervention-intervention networks")
            if not 2 <= a.pair.max_edges <= 150:
                errors.append("pair.max_edges must be between 2 and 150")
        if a.dimension or a.time:
            errors.append("cooccurrence analysis must not set dimension or time")
        if multi:
            errors.append("networks support a single cohort")

    elif a.kind == "numeric_pair":
        if not a.x_measure or not a.y_measure:
            errors.append("numeric_pair requires x_measure and y_measure")
        elif a.x_measure == a.y_measure:
            errors.append("x_measure and y_measure must differ")
        if a.dimension or a.time or a.pair:
            errors.append("numeric_pair must not set dimension, time or pair")
        if multi:
            errors.append("scatter plots support a single cohort")

    if a.top_k is not None and not 1 <= a.top_k <= MAX_TOP_K:
        errors.append(f"top_k must be between 1 and {MAX_TOP_K}")
    return errors


def ensure_valid(plan: QueryPlan) -> QueryPlan:
    errors = validate_plan(plan)
    if errors:
        raise PlanValidationError(errors)
    return plan


def _names(op: str) -> str:
    return ", ".join(sorted(d.value for d, f in REGISTRY.items() if op in f.ops))
