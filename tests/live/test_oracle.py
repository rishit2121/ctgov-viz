"""Live end-to-end checks against ClinicalTrials.gov with independent oracles (``-m live``).

Each demo plan runs through the real pipeline; its numbers are then re-derived with *separate*
API count queries (Essie filters) that share no code with the engine, and cited evidence is
re-fetched study by study.

    uv run pytest -m live tests/live -q
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.contracts.plan import QueryPlan
from app.contracts.response import QueryResponse
from app.ctgov.client import CTGovClient
from app.evidence.store import ResultCache
from app.pipeline import Pipeline
from app.settings import Settings

pytestmark = pytest.mark.live
BASE = "https://clinicaltrials.gov/api/v2"
PLANS = Path(__file__).parents[2] / "examples" / "plans"
PHASES = ["EARLY_PHASE1", "PHASE1", "PHASE2", "PHASE3", "PHASE4", "NA"]


async def respond(name: str) -> tuple[QueryResponse, Pipeline]:
    settings = Settings()
    client = CTGovClient(settings.ctgov_base_url)
    pipeline = Pipeline(client, settings, ResultCache())
    plan = QueryPlan.model_validate(json.loads((PLANS / f"{name}.json").read_text())["plan"])
    return await pipeline.run_plan(plan), pipeline


def api_count(**params: str) -> int:
    r = httpx.get(f"{BASE}/studies", params={**params, "countTotal": "true", "pageSize": "1",
                                              "fields": "NCTId"}, timeout=60)
    r.raise_for_status()
    return int(r.json()["totalCount"])


def rows(response: QueryResponse) -> list[dict[str, Any]]:
    assert response.status == "ok" and response.visualization is not None
    return response.visualization.data


async def test_phase_distribution_matches_api_counts() -> None:
    response, _ = await respond("02_pembrolizumab_phases")
    bars = {d["phase"]: d["study_count"] for d in rows(response)}
    assert sum(bars.values()) == api_count(**{"query.intr": "pembrolizumab"})
    others = " OR ".join(p for p in PHASES if p != "PHASE3")
    assert bars["Phase 3"] == api_count(**{
        "query.intr": "pembrolizumab",
        "filter.advanced": f"AREA[Phase]PHASE3 AND NOT AREA[Phase]({others})"})
    assert bars["Not Reported"] == api_count(**{"query.intr": "pembrolizumab",
                                                "filter.advanced": "AREA[Phase]MISSING"})


async def test_trend_matches_api_counts_per_year() -> None:
    response, _ = await respond("01_breast_cancer_trend")
    by_year = {d["start_year"]: d["study_count"] for d in rows(response)}
    for year in (2015, 2019, 2023):
        assert by_year[year] == api_count(**{
            "query.cond": "breast cancer",
            "filter.advanced": f"AREA[StartDate]RANGE[{year}-01-01,{year}-12-31]"}), year
    assert response.meta.excluded["missing_start_date_not_retrieved"] == api_count(**{
        "query.cond": "breast cancer", "filter.advanced": "AREA[StartDate]MISSING"})


async def test_country_ranking_matches_api_counts() -> None:
    response, _ = await respond("04_lung_cancer_countries")
    data = rows(response)
    counts = [d["study_count"] for d in data]
    assert counts == sorted(counts, reverse=True)
    for d in data[:3]:
        assert d["study_count"] == api_count(**{
            "query.cond": "lung cancer", "filter.overallStatus": "RECRUITING",
            "filter.advanced": f'AREA[Phase]PHASE3 AND AREA[LocationCountry]"{d["country"]}"'}), d


async def test_comparison_series_match_their_cohorts() -> None:
    response, _ = await respond("03_pembro_vs_nivo_phases")
    totals: dict[str, int] = {}
    for d in rows(response):
        totals[d["cohort"]] = totals.get(d["cohort"], 0) + d["study_count"]
    assert totals == {"Pembrolizumab": api_count(**{"query.intr": "pembrolizumab"}),
                      "Nivolumab": api_count(**{"query.intr": "nivolumab"})}
    assert response.meta.cohort_overlap["Pembrolizumab ∩ Nivolumab"] > 0


@pytest.mark.parametrize("name", ["04_lung_cancer_countries", "05_melanoma_cooccurrence"])
async def test_cited_evidence_matches_source_records(name: str) -> None:
    response, pipeline = await respond(name)
    assert response.query_id is not None
    bundle = pipeline.evidence(response.query_id)
    assert bundle is not None
    rng = random.Random(0)
    item_id = rng.choice(sorted(bundle.items))
    for contributor in rng.sample(bundle.items[item_id], k=min(3, len(bundle.items[item_id]))):
        record = httpx.get(f"{BASE}/studies/{contributor.nct_id}", timeout=60).json()
        ps = record["protocolSection"]
        for path, value in contributor.fields.items():
            if path == "contactsLocationsModule.locations[].country":
                assert set(value) == {loc["country"] for loc in
                                      ps.get("contactsLocationsModule", {}).get("locations", [])
                                      if loc.get("country")}
            elif path == "armsInterventionsModule.interventions":
                registered = {i["name"] for i in ps["armsInterventionsModule"]["interventions"]}
                assert {v["name"] for v in value} <= registered
            elif path == "designModule.phases":
                assert value == ps.get("designModule", {}).get("phases", [])
