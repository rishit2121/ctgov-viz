"""Shared test helpers.

``study(...)`` builds a raw CT.gov v2 record (same nesting the API returns) so tests exercise the
real normalizer rather than hand-constructing ``Trial`` objects.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.contracts.trial import Trial
from app.normalize.trial import normalize_study

FIXTURES = Path(__file__).parent / "fixtures"


def study(
    nct: str,
    *,
    title: str | None = None,
    status: str = "RECRUITING",
    study_type: str = "INTERVENTIONAL",
    phases: list[str] | None = None,
    conditions: list[str] | None = None,
    interventions: list[tuple[str, str]] | None = None,
    arms: list[list[str]] | None = None,
    countries: list[str] | None = None,
    start: str | None = "2020-01-15",
    start_type: str = "ACTUAL",
    completion: str | None = None,
    sponsor: tuple[str, str] | None = ("Acme Pharma", "INDUSTRY"),
    enrollment: int | None = None,
) -> dict[str, Any]:
    ps: dict[str, Any] = {
        "identificationModule": {"nctId": nct, "briefTitle": title or f"Study {nct}"},
        "statusModule": {"overallStatus": status},
        "designModule": {"studyType": study_type},
    }
    if start is not None:
        ps["statusModule"]["startDateStruct"] = {"date": start, "type": start_type}
    if completion is not None:
        ps["statusModule"]["primaryCompletionDateStruct"] = {"date": completion,
                                                             "type": "ESTIMATED"}
    if phases is not None:
        ps["designModule"]["phases"] = phases
    if enrollment is not None:
        ps["designModule"]["enrollmentInfo"] = {"count": enrollment, "type": "ACTUAL"}
    if conditions is not None:
        ps["conditionsModule"] = {"conditions": conditions}
    if interventions is not None or arms is not None:
        ps["armsInterventionsModule"] = {
            "interventions": [{"name": n, "type": t} for n, t in interventions or []],
            "armGroups": [{"interventionNames": a} for a in arms or []],
        }
    if countries is not None:
        ps["contactsLocationsModule"] = {"locations": [{"country": c} for c in countries]}
    if sponsor is not None:
        ps["sponsorCollaboratorsModule"] = {"leadSponsor": {"name": sponsor[0],
                                                            "class": sponsor[1]}}
    return {"protocolSection": ps}


def trial(nct: str, **kwargs: Any) -> Trial:
    return normalize_study(study(nct, **kwargs))


@pytest.fixture(scope="session")
def real_studies() -> dict[str, dict[str, Any]]:
    """Real CT.gov records keyed by the edge case they exercise (see capture_fixtures.py)."""
    return json.loads((FIXTURES / "studies.json").read_text())  # type: ignore[no-any-return]
