"""Cohort -> ClinicalTrials.gov v2 query parameters, plus a local re-verification predicate.

Structured filters are pushed down to the API (smaller downloads) *and* re-checked on the
normalized record, so every study cited as evidence provably satisfies the plan's filters.
Free-text search (condition / intervention / term / sponsor) is deliberately left to CT.gov,
whose search expands synonyms (e.g. "Keytruda" / "MK-3475" -> pembrolizumab).

A trend's year window (``TimeSpec``) is also pushed down as a start-date range; studies without a
start date are then not downloaded, so ``missing_start_params`` lets the caller count them.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from urllib.parse import urlencode

from app.contracts.plan import Cohort, CohortFilters, TimeSpec
from app.contracts.trial import DateValue, Trial
from app.normalize.countries import canonical_country
from app.normalize.trial import API_FIELDS


@dataclass(frozen=True)
class CTGovQuery:
    cohort_label: str
    params: dict[str, str]
    predicate: Callable[[Trial], bool]

    def url(self, base_url: str) -> str:
        return f"{base_url}/studies?{urlencode(self.params)}"


# Search text is passed as plain terms: strip Essie operators and control characters so user
# wording cannot smuggle in AREA[...] / RANGE[...] clauses.
_ESSIE_RE = re.compile(r"\b(?:AREA|RANGE|SEARCH|EXPANSION|COVERAGE)\s*\[[^\]]*\]?", re.IGNORECASE)
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


def sanitize_text(value: str) -> str:
    return " ".join(_CTRL_RE.sub(" ", _ESSIE_RE.sub(" ", value)).split())


def _date_bounds(f: CohortFilters, time: TimeSpec | None) -> tuple[date | None, date | None]:
    lo, hi = f.start_date_from, f.start_date_to
    if time is not None and time.from_year is not None:
        y = date(time.from_year, 1, 1)
        lo = max(lo, y) if lo else y
    if time is not None and time.to_year is not None:
        y = date(time.to_year, 12, 31)
        hi = min(hi, y) if hi else y
    return lo, hi


def _advanced(f: CohortFilters, time: TimeSpec | None = None) -> str | None:
    clauses: list[str] = []
    if f.phase:
        phases = " OR ".join(p.value for p in f.phase)
        clauses.append(f"AREA[Phase]({phases})" if len(f.phase) > 1 else f"AREA[Phase]{phases}")
    if f.study_type:
        clauses.append(f"AREA[StudyType]{f.study_type.value}")
    lo, hi = _date_bounds(f, time)
    if lo or hi:
        clauses.append(f"AREA[StartDate]RANGE[{lo.isoformat() if lo else 'MIN'},"
                       f"{hi.isoformat() if hi else 'MAX'}]")
    return " AND ".join(clauses) or None


def _search_params(cohort: Cohort) -> dict[str, str]:
    params: dict[str, str] = {}
    for key, value in (
        ("query.cond", cohort.condition),
        ("query.intr", cohort.intervention),
        ("query.term", cohort.term),
        ("query.spons", cohort.sponsor),
        ("query.locn", cohort.filters.country),
    ):
        if value and (clean := sanitize_text(value)):
            params[key] = clean
    if cohort.filters.overall_status:
        params["filter.overallStatus"] = ",".join(s.value for s in cohort.filters.overall_status)
    return params


def compile_cohort(cohort: Cohort, time: TimeSpec | None = None) -> CTGovQuery:
    params = _search_params(cohort)
    if adv := _advanced(cohort.filters, time):
        params["filter.advanced"] = adv
    params["fields"] = ",".join(API_FIELDS)
    return CTGovQuery(cohort.label, params, _predicate(cohort.filters))


def missing_start_params(cohort: Cohort) -> dict[str, str]:
    """Count query for cohort studies with no registered start date (excluded from trends)."""
    params = _search_params(cohort)
    adv = _advanced(cohort.filters.model_copy(update={"start_date_from": None,
                                                      "start_date_to": None}))
    params["filter.advanced"] = " AND ".join(filter(None, [adv, "AREA[StartDate]MISSING"]))
    return params


def _date_key(d: DateValue) -> tuple[int, int, int] | None:
    if d.year is None:
        return None
    return (d.year, d.month or 1, d.day or 1)


def _predicate(f: CohortFilters) -> Callable[[Trial], bool]:
    statuses = {s.value for s in f.overall_status}
    phases = {p.value for p in f.phase}
    country, country_iso3 = canonical_country(f.country) if f.country else (None, None)
    lo = _as_key(f.start_date_from)
    hi = _as_key(f.start_date_to)

    def check(t: Trial) -> bool:
        if statuses and t.overall_status not in statuses:
            return False
        if phases and not phases.intersection(t.phases):
            return False
        if f.study_type and t.study_type != f.study_type.value:
            return False
        if country and not _has_country(t, country, country_iso3):
            return False
        if lo or hi:
            k = _date_key(t.start)
            if k is None or (lo and k < lo) or (hi and k > hi):
                return False
        return True

    return check


def _has_country(t: Trial, name: str, iso3: str | None) -> bool:
    if iso3 is not None:
        return iso3 in t.country_iso3.values()
    return name.casefold() in {c.casefold() for c in t.countries}


def _as_key(d: date | None) -> tuple[int, int, int] | None:
    return (d.year, d.month, d.day) if d else None
