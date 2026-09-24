"""Example runs: real requests through the HTTP API (Claude planner + live ClinicalTrials.gov).

Writes ``examples/runs/<name>.request.json`` and ``<name>.response.json`` for each request below
and prints a summary. Needs ANTHROPIC_API_KEY (or LLM_MODE=openai + OPENAI_API_KEY).

    uv run python scripts/run_examples.py [name-substring ...]
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.api.main import create_app

OUT = Path(__file__).resolve().parents[1] / "examples" / "runs"
REQUESTS: dict[str, dict[str, Any]] = {
    "01_trend_drug_field": {"query": "How has the number of trials for this drug changed over "
                                     "time?", "drug_name": "Pembrolizumab"},
    "02_compare_phases": {"query": "Compare pembrolizumab and nivolumab trials across phases."},
    "03_geography_fields": {"query": "Which countries have the most trials?",
                            "condition": "lung cancer", "trial_phase": "Phase 3",
                            "status": "recruiting"},
    "04_drug_combination_network": {"query": "Which drugs are combined in the same arm in "
                                             "melanoma trials?"},
    "05_sponsor_drug_network": {"query": "Show a network of sponsors and drugs for non-small "
                                         "cell lung cancer trials", "start_year": 2020},
    "06_enrollment_histogram": {"query": "What is the distribution of enrollment sizes for "
                                         "Phase 3 breast cancer trials?"},
    "07_enrollment_vs_duration_scatter": {"query": "How does enrollment relate to study duration "
                                                   "for Phase 3 breast cancer trials?"},
    "08_country_by_phase_stacked": {
        "query": "For interventional breast cancer studies that started from 2020 through 2024 "
                 "and are currently recruiting, which 10 countries have the most Phase 2 and "
                 "Phase 3 trials? Show the counts by phase for each country."},
    "09_condition_phase_distribution": {
        "query": "How are melanoma trials distributed across phases?"},
    "10_intervention_types": {
        "query": "What are the most common intervention types for lung cancer trials?"},
    "11_sponsor_categories_two_conditions": {
        "query": "Compare sponsor categories across breast cancer and prostate cancer trials."},
    "12_condition_trend_by_status": {
        "query": "For interventional breast cancer studies, how many distinct trials started in "
                 "each year from 2015 through 2024, split into recruiting and completed studies "
                 "based on their current status?"},
    "13_drug_pair_network": {
        "query": "Among interventional melanoma studies that started from 2020 through 2024, "
                 "which pairs of drug interventions appear together in the same study most "
                 "often? Show the top 15 pairs as a network, with drugs as nodes and the number "
                 "of distinct studies as each edge’s weight."},
    "14_country_collaboration_network": {
        "query": "Which countries most often run melanoma trials together?"},
    "15_duration_histogram": {"query": "How long do pembrolizumab trials typically run?"},
    "16_clarification": {"query": "Show me the immunotherapy landscape"},
    "17_unsupported": {"query": "Which melanoma drug has the best overall survival?"},
    "18_broad_question_auto_narrowed": {
        "query": "How are cancer trials distributed by country?"},
    # the same question as an explicit, unrestricted plan: shows the refusal path with counted
    # narrowing options (no LLM call)
    "19_too_broad_plan_refused": {
        "query": "How are cancer trials distributed by country?",
        "plan": {"cohorts": [{"label": "Cancer", "condition": "cancer"}],
                 "analysis": {"kind": "aggregate", "dimension": "country", "top_k": 20,
                              "sort": {"by": "count_desc"}}}},
}


def main(filters: list[str]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with TestClient(create_app()) as api:
        for name, request in REQUESTS.items():
            if filters and not any(f in name for f in filters):
                continue
            started = time.perf_counter()
            r = api.post("/query", json=request)
            body = r.json()
            (OUT / f"{name}.request.json").write_text(json.dumps(request, indent=2) + "\n")
            (OUT / f"{name}.response.json").write_text(json.dumps(body, indent=1,
                                                                  ensure_ascii=False) + "\n")
            viz = body.get("visualization") or {}
            items = len(viz.get("data") or viz.get("edges") or [])
            print(f"{name:38s} HTTP {r.status_code} {body.get('status', body.get('code')):20s} "
                  f"{viz.get('type', '-'):14s} items={items:<5} "
                  f"{time.perf_counter() - started:5.1f}s  {viz.get('title', '')}")


if __name__ == "__main__":
    main(sys.argv[1:])
