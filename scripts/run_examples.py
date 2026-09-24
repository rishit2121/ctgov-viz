"""Run the example plans in ``examples/plans`` against live ClinicalTrials.gov.

Writes each verified response to ``examples/responses/<name>.json`` and prints a summary.

    uv run python scripts/run_examples.py [name-substring ...]
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from app.contracts.plan import QueryPlan
from app.ctgov.client import CTGovClient
from app.evidence.store import ResultCache
from app.pipeline import Pipeline
from app.settings import get_settings

ROOT = Path(__file__).resolve().parents[1] / "examples"


async def main(filters: list[str]) -> None:
    settings = get_settings()
    client = CTGovClient(settings.ctgov_base_url, timeout_s=settings.ctgov_timeout_s,
                         max_retries=settings.ctgov_max_retries)
    pipeline = Pipeline(client, settings, ResultCache())
    out_dir = ROOT / "responses"
    out_dir.mkdir(exist_ok=True)
    try:
        for path in sorted((ROOT / "plans").glob("*.json")):
            if filters and not any(f in path.stem for f in filters):
                continue
            spec = json.loads(path.read_text())
            response = await pipeline.run_plan(QueryPlan.model_validate(spec["plan"]),
                                               question=spec["question"])
            (out_dir / path.name).write_text(response.model_dump_json(indent=1) + "\n")
            m, viz = response.meta, response.visualization
            size = len(viz.data) if viz and viz.data else len(viz.edges or []) if viz else 0
            print(f"{path.stem:40s} {response.status:8s} studies={m.studies_analyzed:<6} "
                  f"items={size:<4} {m.timings_ms}")
            for w in m.warnings:
                print(f"    warning: {w}")
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
