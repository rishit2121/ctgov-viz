"""Raw CT.gov v2 study JSON -> canonical ``Trial``.

Every module/field is optional in real records, so all access goes through ``_get``. The
``source`` map keeps the raw value of each field path we read, so evidence can show exactly
which registered values justified a study's membership in a group.
"""

from __future__ import annotations

from typing import Any

from app.contracts.trial import Intervention, Trial
from app.normalize.countries import canonical_country
from app.normalize.dates import parse_date_struct
from app.normalize.interventions import (
    is_ancillary,
    is_placebo,
    resolve,
    split_arm_intervention,
)
from app.normalize.labels import sort_phases

# Field paths (relative to protocolSection) used in evidence.
P_NCT = "identificationModule.nctId"
P_TITLE = "identificationModule.briefTitle"
P_STATUS = "statusModule.overallStatus"
P_START = "statusModule.startDateStruct"
P_PCD = "statusModule.primaryCompletionDateStruct"
P_SPONSOR = "sponsorCollaboratorsModule.leadSponsor"
P_CONDITIONS = "conditionsModule.conditions"
P_STUDY_TYPE = "designModule.studyType"
P_PHASES = "designModule.phases"
P_ENROLLMENT = "designModule.enrollmentInfo"
P_INTERVENTIONS = "armsInterventionsModule.interventions"
P_ARMS = "armsInterventionsModule.armGroups"
P_COUNTRIES = "contactsLocationsModule.locations[].country"

# `fields=` pieces requested from the API (keeps payloads small).
API_FIELDS = [
    "NCTId", "BriefTitle", "OverallStatus", "StudyType", "Phase", "StartDate", "StartDateType",
    "PrimaryCompletionDate", "PrimaryCompletionDateType", "LeadSponsorName", "LeadSponsorClass",
    "Condition", "InterventionName", "InterventionType", "ArmGroupInterventionName",
    "LocationCountry", "EnrollmentCount", "EnrollmentType",
]


def _get(obj: Any, path: str) -> Any:
    for part in path.split("."):
        if not isinstance(obj, dict):
            return None
        obj = obj.get(part)
    return obj


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [v.strip() for v in value if isinstance(v, str) and v.strip()]


class NormalizationError(ValueError):
    pass


def normalize_study(raw: dict[str, Any]) -> Trial:
    ps = raw.get("protocolSection") or {}
    nct_id = _get(ps, P_NCT)
    if not isinstance(nct_id, str) or not nct_id.startswith("NCT"):
        raise NormalizationError(f"record without a valid NCT ID: {nct_id!r}")

    phases = sort_phases(_str_list(_get(ps, P_PHASES)))
    conditions = tuple(dict.fromkeys(_str_list(_get(ps, P_CONDITIONS))))

    interventions: dict[str, Intervention] = {}
    registered: list[dict[str, Any]] = []  # every registered entry, for evidence
    raw_interventions = _get(ps, P_INTERVENTIONS) or []
    for item in raw_interventions if isinstance(raw_interventions, list) else []:
        name = item.get("name") if isinstance(item, dict) else None
        if not isinstance(name, str) or not name.strip():
            continue
        key, label = resolve(name)
        registered.append({"name": name.strip(), "type": item.get("type"), "resolved_as": key})
        if key not in interventions:
            interventions[key] = Intervention(
                key=key, label=label, raw_name=name.strip(),
                type=item.get("type"), is_placebo=is_placebo(name),
                is_ancillary=is_ancillary(key),
            )

    arm_sets: list[frozenset[str]] = []
    raw_arms = _get(ps, P_ARMS) or []
    for arm in raw_arms if isinstance(raw_arms, list) else []:
        names = _str_list(arm.get("interventionNames") if isinstance(arm, dict) else None)
        keys = frozenset(resolve(split_arm_intervention(n)[1])[0] for n in names)
        if keys:
            arm_sets.append(keys)

    countries: dict[str, str | None] = {}
    raw_locations = _get(ps, "contactsLocationsModule.locations") or []
    raw_countries: list[str] = []
    for loc in raw_locations if isinstance(raw_locations, list) else []:
        c = loc.get("country") if isinstance(loc, dict) else None
        if isinstance(c, str) and c.strip():
            raw_countries.append(c)
            name, iso3 = canonical_country(c)
            countries[name] = iso3

    sponsor = _get(ps, P_SPONSOR) or {}
    enrollment = _get(ps, P_ENROLLMENT) or {}
    count = enrollment.get("count") if isinstance(enrollment, dict) else None

    source: dict[str, Any] = {
        P_STATUS: _get(ps, P_STATUS),
        P_STUDY_TYPE: _get(ps, P_STUDY_TYPE),
        P_PHASES: list(phases),
        P_START: _get(ps, P_START),
        P_PCD: _get(ps, P_PCD),
        P_SPONSOR: sponsor or None,
        P_CONDITIONS: list(conditions),
        P_INTERVENTIONS: registered,
        P_ARMS: [sorted(s) for s in arm_sets],
        P_COUNTRIES: sorted(set(raw_countries)),
        P_ENROLLMENT: enrollment or None,
    }

    return Trial(
        nct_id=nct_id,
        title=_get(ps, P_TITLE),
        overall_status=_get(ps, P_STATUS),
        study_type=_get(ps, P_STUDY_TYPE),
        phases=phases,
        conditions=conditions,
        interventions=tuple(interventions.values()),
        arm_intervention_keys=tuple(arm_sets),
        sponsor_name=(sponsor.get("name") or None) if isinstance(sponsor, dict) else None,
        sponsor_class=(sponsor.get("class") or None) if isinstance(sponsor, dict) else None,
        countries=tuple(sorted(countries)),
        country_iso3=countries,
        start=parse_date_struct(_get(ps, P_START)),
        primary_completion=parse_date_struct(_get(ps, P_PCD)),
        enrollment=count if isinstance(count, int) and count >= 0 else None,
        enrollment_type=enrollment.get("type") if isinstance(enrollment, dict) else None,
        source=source,
        record=ps,
    )
