# ctgov-viz

Natural-language questions about clinical trials → verified, visualization-ready JSON built from
real ClinicalTrials.gov records.

> Work in progress. See [PLAN.md](PLAN.md) for the design and implementation plan.

## Setup

```bash
uv sync
uv run pytest            # unit + integration tests (no network)
uv run pytest -m live    # tests against the real ClinicalTrials.gov / LLM APIs
```
