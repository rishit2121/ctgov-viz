"""Deep citations: for one study and one chart datum, the exact record text that supports it.

A datum *claims* things about each contributing study: "its phase bucket is Phase 2/3", "it has a
site in South Korea", "it lists both nivolumab and ipilimumab", "it is in the Pembrolizumab
cohort". For each claim this module finds the supporting values in the study's raw API record
(``Trial.record``, the ``protocolSection`` exactly as ClinicalTrials.gov returned it) and cites
them by exact path, list index included:

    {"field": "protocolSection.contactsLocationsModule.locations[3].country",
     "text": "Korea, Republic of", "supports": "country: South Korea"}

``text`` is always copied verbatim from the record (``resolve(record, field)`` returns it), so an
excerpt can be checked mechanically, and the live tests re-fetch records to do exactly that. When
a claim rests on an *absent* value (e.g. "Not Reported" phase) or on ClinicalTrials.gov's search
expansion rather than a verbatim match, the excerpt says so with ``text: null`` instead of
pretending.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from app.contracts.plan import Cohort, Dimension, Measure
from app.contracts.trial import Trial
from app.contracts.viz import Citation, Excerpt
from app.normalize.countries import canonical_country
from app.normalize.interventions import resolve as resolve_intervention
from app.normalize.labels import (
    INTERVENTION_TYPE_LABELS,
    SPONSOR_CLASS_LABELS,
    STATUS_LABELS,
    STUDY_TYPE_LABELS,
    label,
)
from app.registry.fields import REGISTRY, duration_months

ROOT = "protocolSection"
STUDY_URL = "https://clinicaltrials.gov/study/{}"
RECORD_URL = "https://clinicaltrials.gov/api/v2/studies/{}"
_PART_RE = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


# --------------------------------------------------------------------------- claims


@dataclass(frozen=True)
class DimClaim:
    dimension: Dimension
    key: str


@dataclass(frozen=True)
class YearClaim:
    year: int


@dataclass(frozen=True)
class MeasureClaim:
    measure: Measure


Claim = DimClaim | YearClaim | MeasureClaim


# --------------------------------------------------------------------------- paths


def resolve(record: dict[str, Any], field: str) -> Any:
    """Value at ``field`` (e.g. 'protocolSection.designModule.phases[1]') or None."""
    node: Any = {ROOT: record}
    for name, index in _PART_RE.findall(field):
        if name:
            node = node.get(name) if isinstance(node, dict) else None
        else:
            i = int(index)
            node = node[i] if isinstance(node, list) and i < len(node) else None
        if node is None:
            return None
    return node


def _get(record: dict[str, Any], rel: str) -> Any:
    return resolve(record, f"{ROOT}.{rel}")


def _items(record: dict[str, Any], rel: str) -> Iterator[tuple[int, Any]]:
    value = _get(record, rel)
    if isinstance(value, list):
        yield from enumerate(value)


def _ex(rel: str, value: Any, supports: str) -> Excerpt:
    return Excerpt(field=f"{ROOT}.{rel}", text=None if value is None else str(value),
                   supports=supports)


def _absent(rel: str, supports: str) -> Excerpt:
    return Excerpt(field=f"{ROOT}.{rel}", text=None, supports=supports)


# --------------------------------------------------------------------------- dimensions


def _phase(r: dict[str, Any], key: str) -> list[Excerpt]:
    phases = [(i, v) for i, v in _items(r, "designModule.phases") if isinstance(v, str)]
    if not phases:
        return [_absent("designModule.phases", f"no phase registered, so phase: {key}")]
    return [_ex(f"designModule.phases[{i}]", v, f"phase: {key}") for i, v in phases]


def _scalar(rel: str, what: str, labels: dict[str, str]
            ) -> Callable[[dict[str, Any], str], list[Excerpt]]:
    def cite(r: dict[str, Any], key: str) -> list[Excerpt]:
        value = _get(r, rel)
        if value is None:
            return [_absent(rel, f"not registered, so {what}: {key}")]
        return [_ex(rel, value, f"{what}: {label(labels, value) if labels else key}")]
    return cite


def _condition(r: dict[str, Any], key: str) -> list[Excerpt]:
    return [_ex(f"conditionsModule.conditions[{i}]", v, f"condition: {v.strip()}")
            for i, v in _items(r, "conditionsModule.conditions")
            if isinstance(v, str) and v.strip().casefold() == key][:1]


def _intervention(r: dict[str, Any], key: str) -> list[Excerpt]:
    out = []
    for i, item in _items(r, "armsInterventionsModule.interventions"):
        name = item.get("name") if isinstance(item, dict) else None
        if isinstance(name, str) and name.strip() and resolve_intervention(name)[0] == key:
            out.append(_ex(f"armsInterventionsModule.interventions[{i}].name", name,
                           f"intervention: {resolve_intervention(name)[1]}"))
    return out[:3]


def _intervention_type(r: dict[str, Any], key: str) -> list[Excerpt]:
    for i, item in _items(r, "armsInterventionsModule.interventions"):
        kind = item.get("type") if isinstance(item, dict) else None
        if isinstance(kind, str) and label(INTERVENTION_TYPE_LABELS, kind) == key:
            return [_ex(f"armsInterventionsModule.interventions[{i}].type", kind,
                        f"intervention type: {key}")]
    return []


def _country(r: dict[str, Any], key: str) -> list[Excerpt]:
    for i, loc in _items(r, "contactsLocationsModule.locations"):
        c = loc.get("country") if isinstance(loc, dict) else None
        if isinstance(c, str) and c.strip() and canonical_country(c)[0] == key:
            return [_ex(f"contactsLocationsModule.locations[{i}].country", c,
                        f"site country: {key}")]
    return []


def _enrollment(r: dict[str, Any], key: str) -> list[Excerpt]:
    return [_ex("designModule.enrollmentInfo.count", _get(r, "designModule.enrollmentInfo.count"),
                f"enrollment in bin {key}")]


def _dates(what: str) -> Callable[[dict[str, Any], str], list[Excerpt]]:
    def cite(r: dict[str, Any], key: str) -> list[Excerpt]:
        return [_ex("statusModule.startDateStruct.date",
                    _get(r, "statusModule.startDateStruct.date"), f"start date ({what} {key})"),
                _ex("statusModule.primaryCompletionDateStruct.date",
                    _get(r, "statusModule.primaryCompletionDateStruct.date"),
                    f"primary completion date ({what} {key})")]
    return cite


DIMENSION_CITERS: dict[Dimension, Callable[[dict[str, Any], str], list[Excerpt]]] = {
    Dimension.phase: _phase,
    Dimension.overall_status: _scalar("statusModule.overallStatus", "status", STATUS_LABELS),
    Dimension.study_type: _scalar("designModule.studyType", "study type", STUDY_TYPE_LABELS),
    Dimension.sponsor: _scalar("sponsorCollaboratorsModule.leadSponsor.name", "lead sponsor", {}),
    Dimension.sponsor_class: _scalar("sponsorCollaboratorsModule.leadSponsor.class",
                                     "lead sponsor class", SPONSOR_CLASS_LABELS),
    Dimension.condition: _condition,
    Dimension.intervention: _intervention,
    Dimension.intervention_type: _intervention_type,
    Dimension.country: _country,
    Dimension.enrollment_size: _enrollment,
    Dimension.duration: _dates("duration"),
}


def _claim_excerpts(t: Trial, claim: Claim) -> list[Excerpt]:
    r = t.record
    if isinstance(claim, YearClaim):
        return [_ex("statusModule.startDateStruct.date",
                    _get(r, "statusModule.startDateStruct.date"), f"start year: {claim.year}")]
    if isinstance(claim, MeasureClaim):
        if claim.measure == Measure.enrollment:
            return [_ex("designModule.enrollmentInfo.count",
                        _get(r, "designModule.enrollmentInfo.count"),
                        f"enrollment: {t.enrollment}")]
        return _dates("duration")(r, f"{duration_months(t)} months")
    citer = DIMENSION_CITERS.get(claim.dimension)
    return citer(r, claim.key) if citer else []


# --------------------------------------------------------------------------- cohort membership


def _contains(r: dict[str, Any], rel: str, term: str, attr: str | None = None
              ) -> Excerpt | None:
    needle = term.casefold()
    for i, item in _items(r, rel):
        value = item.get(attr) if attr and isinstance(item, dict) else item
        if isinstance(value, str) and needle in value.casefold():
            return _ex(f"{rel}[{i}]{'.' + attr if attr else ''}", value, "")
    return None


def _membership(t: Trial, cohort: Cohort) -> list[Excerpt]:
    """Why this study is in the cohort: its search matches and each structured filter."""
    r, f, who = t.record, cohort.filters, f"cohort '{cohort.label}'"
    out: list[Excerpt] = []
    title = t.title or ""
    searches = (
        ("condition", cohort.condition, "conditionsModule.conditions", None),
        ("intervention", cohort.intervention, "armsInterventionsModule.interventions", "name"),
    )
    for what, term, rel, attr in searches:
        if not term:
            continue
        hit = _contains(r, rel, term, attr)
        if hit is None and what == "intervention":  # "Keytruda" in record, "pembrolizumab" asked
            key = resolve_intervention(term)[0]
            hit = next((_ex(f"{rel}[{i}].name", it["name"], "") for i, it in _items(r, rel)
                        if isinstance(it, dict) and isinstance(it.get("name"), str)
                        and resolve_intervention(it["name"])[0] == key), None)
        if hit is None and term.casefold() in title.casefold():
            hit = _ex("identificationModule.briefTitle", title, "")
        if hit is None:
            out.append(_absent(rel, f"{who}: matched by ClinicalTrials.gov's {what} search "
                                    f"(synonym expansion); '{term}' is not verbatim here"))
        else:
            out.append(hit.model_copy(update={"supports": f"{who} ({what} search '{term}')"}))
    if cohort.sponsor:
        name = _get(r, "sponsorCollaboratorsModule.leadSponsor.name")
        if isinstance(name, str) and cohort.sponsor.casefold() in name.casefold():
            out.append(_ex("sponsorCollaboratorsModule.leadSponsor.name", name,
                           f"{who} (sponsor search '{cohort.sponsor}')"))
        else:
            out.append(_absent("sponsorCollaboratorsModule",
                               f"{who}: matched by ClinicalTrials.gov's sponsor/collaborator "
                               f"search for '{cohort.sponsor}'"))
    if f.overall_status:
        out.append(_ex("statusModule.overallStatus", _get(r, "statusModule.overallStatus"),
                       f"{who} (status filter)"))
    if f.phase:
        wanted = {p.value for p in f.phase}
        out += [_ex(f"designModule.phases[{i}]", v, f"{who} (phase filter)")
                for i, v in _items(r, "designModule.phases") if v in wanted][:1]
    if f.study_type:
        out.append(_ex("designModule.studyType", _get(r, "designModule.studyType"),
                       f"{who} (study type filter)"))
    if f.country:
        want_name, want_iso = canonical_country(f.country)
        for i, loc in _items(r, "contactsLocationsModule.locations"):
            c = loc.get("country") if isinstance(loc, dict) else None
            if isinstance(c, str) and c.strip():
                name, iso = canonical_country(c)
                if (want_iso and iso == want_iso) or name.casefold() == want_name.casefold():
                    out.append(_ex(f"contactsLocationsModule.locations[{i}].country", c,
                                   f"{who} (country filter)"))
                    break
    if f.start_date_from or f.start_date_to:
        out.append(_ex("statusModule.startDateStruct.date",
                       _get(r, "statusModule.startDateStruct.date"),
                       f"{who} (start date filter)"))
    return out


# --------------------------------------------------------------------------- assembly


def cite(t: Trial, claims: list[Claim], cohort: Cohort | None) -> Citation:
    excerpts: list[Excerpt] = []
    for claim in claims:
        excerpts += _claim_excerpts(t, claim)
    if cohort is not None:
        excerpts += _membership(t, cohort)
    seen: set[tuple[str, str]] = set()
    unique: list[Excerpt] = []
    for e in excerpts:  # the same value can support two claims; cite it once, both reasons
        k = (e.field, e.text or "")
        if k in seen:
            for u in unique:
                if (u.field, u.text or "") == k and e.supports not in u.supports:
                    u.supports += f"; {e.supports}"
            continue
        seen.add(k)
        unique.append(e)
    primary = next((e.text for e in unique if e.text is not None), None)
    return Citation(nct_id=t.nct_id, title=t.title, url=STUDY_URL.format(t.nct_id),
                    record_url=RECORD_URL.format(t.nct_id), excerpt=primary, excerpts=unique)


def missing_citers() -> set[Dimension]:
    """Registry dimensions without a citer (must be empty; enforced by a test)."""
    return set(REGISTRY) - set(DIMENSION_CITERS)
