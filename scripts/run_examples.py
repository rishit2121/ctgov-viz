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
            print(f"{name:36s} HTTP {r.status_code} {body.get('status', body.get('code')):8s} "
                  f"{viz.get('type', '-'):14s} items={items:<5} "
                  f"{time.perf_counter() - started:5.1f}s  {viz.get('title', '')}")


if __name__ == "__main__":
    main(sys.argv[1:])
