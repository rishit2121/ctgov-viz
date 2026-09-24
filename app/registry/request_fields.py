"""Apply the request's optional structured fields to a plan, deterministically.

Each field is a hard constraint on *every* cohort and wins over the LLM's reading of the question.
Every change is recorded as a plain-English assumption. If the question compares cohorts on the
very field a request field fixes (e.g. "A vs B" with ``drug_name`` set), applying it would merge
the comparison, so that is rejected instead of guessed at.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.contracts.plan import Cohort, QueryPlan
from app.contracts.request import QueryRequest

# request field -> (where it lands on a cohort, display name)
_SEARCH = {"drug_name": "intervention", "condition": "condition", "sponsor": "sponsor"}
_FILTERS = {"country": "country", "trial_phase": "phase", "status": "overall_status"}


class RequestFieldConflict(ValueError):
    pass


def constraints_text(req: QueryRequest) -> str | None:
    """How the planner is told about the fields (so its plan is consistent with them)."""
    fields = req.structured_fields()
    if not fields:
        return None
    listed = ", ".join(f"{k}={v}" for k, v in fields.items())
    return ("Structured fields supplied with this request (hard constraints that will be applied "
            f"to every cohort; plan consistently with them): {listed}")


def _norm(value: Any) -> Any:
    if isinstance(value, str):
        return value.casefold().strip()
    if isinstance(value, list):
        return tuple(sorted(str(getattr(v, "value", v)) for v in value))
    return value


def apply_request_fields(plan: QueryPlan, req: QueryRequest) -> QueryPlan:
    """Return a copy of ``plan`` with every supplied field enforced on every cohort."""
    fields = req.structured_fields()
    if not fields:
        return plan
    cohorts = [c.model_copy(deep=True) for c in plan.cohorts]
    notes: list[str] = []

    def set_all(field: str, value: Any, get: Any, put: Any, what: str) -> None:
        previous = {_norm(get(c)) for c in cohorts if get(c)}
        if len(cohorts) > 1 and len(previous) > 1:
            raise RequestFieldConflict(
                f"`{field}` conflicts with the question, which compares cohorts by {what} "
                f"({', '.join(c.label for c in cohorts)}). Remove `{field}` or rephrase.")
        replaced = [c.label for c in cohorts if get(c) and _norm(get(c)) != _norm(value)]
        for c in cohorts:
            put(c, value)
        note = f"Request field {field}={_show(value)} applied to every cohort"
        notes.append(note + (f" (replacing the interpreted {what} in {', '.join(replaced)})."
                             if replaced else "."))

    for field, attr in _SEARCH.items():
        if field in fields:
            set_all(field, getattr(req, field), lambda c, a=attr: getattr(c, a),
                    lambda c, v, a=attr: setattr(c, a, v), attr)
    for field, attr in _FILTERS.items():
        if field in fields:
            set_all(field, getattr(req, field), lambda c, a=attr: getattr(c.filters, a),
                    lambda c, v, a=attr: setattr(c.filters, a, v), attr.replace("_", " "))
    if req.start_year is not None:
        set_all("start_year", date(req.start_year, 1, 1),
                lambda c: c.filters.start_date_from,
                lambda c, v: setattr(c.filters, "start_date_from", v), "start date")
    if req.end_year is not None:
        set_all("end_year", date(req.end_year, 12, 31),
                lambda c: c.filters.start_date_to,
                lambda c, v: setattr(c.filters, "start_date_to", v), "start date")

    analysis = plan.analysis
    if analysis.time is not None and (req.start_year or req.end_year):
        # keep a trend's x-axis consistent with the enforced start-date bounds
        time = analysis.time.model_copy(update={
            k: v for k, v in (("from_year", req.start_year), ("to_year", req.end_year)) if v})
        analysis = analysis.model_copy(update={"time": time})

    return plan.model_copy(update={
        "cohorts": [Cohort.model_validate(c.model_dump()) for c in cohorts],
        "analysis": analysis,
        "assumptions": [*plan.assumptions, *notes],
    })


def _show(value: Any) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(getattr(v, "value", str(v)) for v in value) + "]"
    return repr(value.isoformat() if isinstance(value, date) else value)
