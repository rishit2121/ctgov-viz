"""Record real ClinicalTrials.gov studies that exercise normalizer/engine edge cases.

Writes ``tests/fixtures/studies.json``: ``{case_name: raw_study}``. Each case is chosen by a
predicate over live records, so the fixture set documents *why* each study is there. Re-run only
when intentionally refreshing fixtures (the tests pin the values these records contain).

    uv run python scripts/capture_fixtures.py
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from app.normalize.trial import API_FIELDS

BASE = "https://clinicaltrials.gov/api/v2/studies"
OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "studies.json"
SEARCHES = [
    {"query.cond": "melanoma"},
    {"query.intr": "pembrolizumab"},
    {"query.cond": "breast cancer", "filter.overallStatus": "WITHDRAWN"},
]

Study = dict[str, Any]


def ps(s: Study, *path: str) -> Any:
    obj: Any = s.get("protocolSection", {})
    for p in path:
        obj = obj.get(p) if isinstance(obj, dict) else None
    return obj


def countries(s: Study) -> list[str]:
    return [loc.get("country") for loc in ps(s, "contactsLocationsModule", "locations") or []]


def arms(s: Study) -> list[list[str]]:
    return [a.get("interventionNames", []) for a in ps(s, "armsInterventionsModule",
                                                        "armGroups") or []]


def names(s: Study) -> list[str]:
    return [i.get("name", "") for i in ps(s, "armsInterventionsModule", "interventions") or []]


CASES: dict[str, Callable[[Study], bool]] = {
    "multi_phase": lambda s: len(ps(s, "designModule", "phases") or []) == 2,
    "no_phase": lambda s: not ps(s, "designModule", "phases"),
    "repeated_site_country": lambda s: len(countries(s)) >= 3 and len(set(countries(s))) == 1,
    "multinational": lambda s: len(set(countries(s))) >= 4,
    "no_locations": lambda s: not countries(s),
    "month_precision_start": lambda s: len(ps(s, "statusModule", "startDateStruct", "date")
                                           or "") == 7,
    "missing_start": lambda s: not ps(s, "statusModule", "startDateStruct"),
    "estimated_start": lambda s: (ps(s, "statusModule", "startDateStruct", "type")
                                  == "ESTIMATED"),
    "placebo_arm": lambda s: any("placebo" in n.lower() for n in names(s)),
    "combination_arm": lambda s: any(len(a) >= 2 for a in arms(s))
                                 and len(arms(s)) >= 2,
    "trademark_name": lambda s: any(c in n for n in names(s) for c in "®™"),
    "dose_in_name": lambda s: any(" mg" in n.lower() for n in names(s)),
    "observational": lambda s: ps(s, "designModule", "studyType") == "OBSERVATIONAL",
    "zero_enrollment": lambda s: ps(s, "designModule", "enrollmentInfo", "count") == 0,
    "no_interventions": lambda s: not names(s),
}


def main() -> None:
    pool: list[Study] = []
    with httpx.Client(timeout=30) as http:
        for search in SEARCHES:
            params = {**search, "pageSize": "1000", "fields": ",".join(API_FIELDS)}
            pool += http.get(BASE, params=params).raise_for_status().json()["studies"]
    pool.sort(key=lambda s: ps(s, "identificationModule", "nctId"))

    chosen: dict[str, Study] = {}
    for case, pred in CASES.items():
        match = next((s for s in pool if pred(s)), None)
        if match is None:
            print(f"!! no study found for {case}")
            continue
        chosen[case] = match
        print(f"{case:24s} {ps(match, 'identificationModule', 'nctId')}")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(chosen, indent=1, ensure_ascii=False, sort_keys=True) + "\n")
    print(f"wrote {len(chosen)} cases to {OUT}")


if __name__ == "__main__":
    main()
