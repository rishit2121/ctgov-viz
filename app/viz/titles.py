"""Deterministic chart titles and subtitles, built from the validated plan (never LLM text)."""

from __future__ import annotations

from app.contracts.plan import Analysis, Cohort, Dimension, Measure, QueryPlan
from app.normalize.labels import PHASE_LABELS, STATUS_LABELS, STUDY_TYPE_LABELS
from app.registry.fields import REGISTRY

MEASURE_TITLES = {
    Measure.enrollment: "Enrollment (participants)",
    Measure.duration_months: "Duration (months, start to primary completion)",
}


def dimension_title(dim: Dimension) -> str:
    return "Cohort" if dim == Dimension.cohort else REGISTRY[dim].title


def _join(labels: list[str]) -> str:
    if len(labels) <= 2:
        return " vs ".join(labels)
    return ", ".join(labels[:-1]) + " and " + labels[-1]


def subject(plan: QueryPlan) -> str:
    return f"{_join([c.label for c in plan.cohorts])} studies"


def title(plan: QueryPlan) -> str:
    a = plan.analysis
    if a.kind == "aggregate":
        if a.time is not None:
            return f"{subject(plan)} by start year"
        assert a.dimension is not None
        by = dimension_title(a.dimension).lower()
        if a.series_by not in (None, Dimension.cohort):
            by += f" and {dimension_title(a.series_by).lower()}"
        return f"{subject(plan)} by {by}"
    if a.kind == "cooccurrence":
        assert a.pair is not None
        if a.pair.left == a.pair.right:
            what = "Drug combinations" if a.pair.scope == "arm" else (
                f"{dimension_title(a.pair.left)} co-occurrence")
            return f"{what} in {subject(plan)}"
        return (f"{dimension_title(a.pair.left)} ↔ {dimension_title(a.pair.right).lower()} "
                f"network for {subject(plan)}")
    assert a.x_measure and a.y_measure
    return (f"{MEASURE_TITLES[a.y_measure].split(' (')[0]} vs "
            f"{MEASURE_TITLES[a.x_measure].split(' (')[0].lower()}: {subject(plan)}")


def _filters(c: Cohort, a: Analysis) -> list[str]:
    f = c.filters
    parts: list[str] = []
    if f.overall_status:
        parts.append(" / ".join(STATUS_LABELS[s.value] for s in f.overall_status))
    if f.phase:
        parts.append(" / ".join(PHASE_LABELS[p.value] for p in f.phase)
                     + " (incl. multi-phase)")
    if f.study_type:
        parts.append(STUDY_TYPE_LABELS[f.study_type.value])
    if f.country:
        parts.append(f"sites in {f.country}")
    if f.start_date_from or f.start_date_to:
        lo = f.start_date_from.isoformat() if f.start_date_from else "any"
        hi = f.start_date_to.isoformat() if f.start_date_to else "any"
        parts.append(f"start {lo} to {hi}")
    return parts


def subtitle(plan: QueryPlan) -> str:
    a = plan.analysis
    parts: list[str] = []
    filter_sets = {c.label: _filters(c, a) for c in plan.cohorts}
    distinct = {tuple(v) for v in filter_sets.values()}
    if len(distinct) == 1:
        parts += next(iter(filter_sets.values()))
    else:
        parts += [f"{lab}: {', '.join(v) or 'no filters'}" for lab, v in filter_sets.items()]
    if a.time is not None:
        lo, hi = a.time.from_year, a.time.to_year
        if lo and hi:
            parts.append(f"started {lo}–{hi}")
        elif lo:
            parts.append(f"started {lo} or later")
        elif hi:
            parts.append(f"started {hi} or earlier")
    if a.kind == "numeric_pair":
        parts.append("each point is one study")
    else:
        parts.append("distinct studies (NCT IDs)")
    return " · ".join(parts)
