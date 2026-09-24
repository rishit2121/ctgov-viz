"""Field projection must never change an analysis: projected records give identical results."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from app.analysis.engine import run
from app.contracts.analysis import AnalysisResult
from app.contracts.plan import QueryPlan
from app.ctgov.compiler import compile_cohort, fields_for
from app.normalize.trial import API_FIELDS, normalize_study
from tests.conftest import study

# API `fields=` name -> (module, path inside it); lists are marked with []
PATHS = {
    "NCTId": "identificationModule.nctId",
    "BriefTitle": "identificationModule.briefTitle",
    "OverallStatus": "statusModule.overallStatus",
    "StudyType": "designModule.studyType",
    "Phase": "designModule.phases",
    "StartDate": "statusModule.startDateStruct.date",
    "StartDateType": "statusModule.startDateStruct.type",
    "PrimaryCompletionDate": "statusModule.primaryCompletionDateStruct.date",
    "PrimaryCompletionDateType": "statusModule.primaryCompletionDateStruct.type",
    "LeadSponsorName": "sponsorCollaboratorsModule.leadSponsor.name",
    "LeadSponsorClass": "sponsorCollaboratorsModule.leadSponsor.class",
    "Condition": "conditionsModule.conditions",
    "InterventionName": "armsInterventionsModule.interventions[].name",
    "InterventionType": "armsInterventionsModule.interventions[].type",
    "ArmGroupInterventionName": "armsInterventionsModule.armGroups[].interventionNames",
    "LocationCountry": "contactsLocationsModule.locations[].country",
    "EnrollmentCount": "designModule.enrollmentInfo.count",
    "EnrollmentType": "designModule.enrollmentInfo.type",
}
PLANS = sorted((Path(__file__).parents[2] / "examples" / "plans").glob("*.json"))


def _copy_path(src: Any, dst: Any, parts: list[str]) -> None:
    head, rest = parts[0], parts[1:]
    is_list = head.endswith("[]")
    key = head.removesuffix("[]")
    if not isinstance(src, dict) or key not in src:
        return
    if not rest:
        dst[key] = copy.deepcopy(src[key])
    elif is_list:
        items = dst.setdefault(key, [{} for _ in src[key]])
        for s_item, d_item in zip(src[key], items, strict=True):
            _copy_path(s_item, d_item, rest)
    else:
        _copy_path(src[key], dst.setdefault(key, {}), rest)


def project(raw: dict[str, Any], fields: list[str]) -> dict[str, Any]:
    """What the API returns for ``fields=...``: only those paths, same nesting."""
    out: dict[str, Any] = {"protocolSection": {}}
    for f in fields:
        _copy_path(raw["protocolSection"], out["protocolSection"], PATHS[f].split("."))
    return out


def summary(r: AnalysisResult) -> Any:
    return (
        [(row.values, sorted((k, json.dumps(c.fields, sort_keys=True, default=str))
                             for k, c in row.contributors.items())) for row in r.rows],
        [(n.id, n.label, n.group, sorted(n.contributors)) for n in r.nodes],
        [(e.source, e.target, sorted(e.contributors)) for e in r.edges],
        [(p.nct_id, p.x, p.y, p.attrs) for p in r.points],
        r.excluded, r.warnings,
    )


def test_every_api_field_is_mapped() -> None:
    assert set(PATHS) == set(API_FIELDS)


@pytest.mark.parametrize("path", PLANS, ids=[p.stem for p in PLANS])
def test_projection_preserves_results(path: Path, real_studies: dict[str, Any]) -> None:
    plan = QueryPlan.model_validate(json.loads(path.read_text())["plan"])
    raws = [*real_studies.values(), *[
        study(f"NCT9{i:07}", phases=["PHASE3"], status="RECRUITING", countries=["France"],
              conditions=["Lung Cancer", "Melanoma", "Breast Cancer"],
              interventions=[("Nivolumab", "DRUG"), ("Ipilimumab", "DRUG")],
              arms=[["Drug: Nivolumab", "Drug: Ipilimumab"]], start=f"20{15 + i}-03",
              completion=f"20{18 + i}-06", enrollment=100 + i, study_type="INTERVENTIONAL")
        for i in range(8)]]
    fields = fields_for(plan)
    assert len(fields) < len(API_FIELDS) or plan.analysis.kind == "numeric_pair"
    preds = [compile_cohort(c).predicate for c in plan.cohorts]

    def analyze(records: list[dict[str, Any]]) -> AnalysisResult:
        trials = [normalize_study(r) for r in records]
        return run(plan, {c.label: [t for t in trials if pred(t)]
                          for c, pred in zip(plan.cohorts, preds, strict=True)})

    full = analyze(raws)
    projected = analyze([project(r, fields) for r in raws])
    assert summary(projected) == summary(full)
