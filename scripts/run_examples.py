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
    "01_trend_two_lines_by_status": {
        "query": "For interventional breast cancer studies, how many distinct trials started in "
                 "each year from 2015 through 2024, split into recruiting and completed studies "
                 "based on their current status?"},
    "02_comparison_drug_phases": {
        "query": "Compare pembrolizumab and nivolumab trials across phases."},
    "03_geography_country_by_phase": {
        "query": "For interventional breast cancer studies that started from 2020 through 2024 "
                 "and are currently recruiting, which 10 countries have the most Phase 2 and "
                 "Phase 3 trials? Show the counts by phase for each country."},
    "04_network_drug_pairs": {
        "query": "Among interventional melanoma studies that started from 2020 through 2024, "
                 "which pairs of drug interventions appear together in the same study most "
                 "often? Show the top 15 pairs as a network, with drugs as nodes and the number "
                 "of distinct studies as each edge’s weight."},
    "05_histogram_with_fields": {
        "query": "What is the distribution of enrollment sizes?",
        "condition": "breast cancer", "trial_phase": "Phase 3", "status": "completed"},
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
