# Example runs

Real requests sent to `POST /query` and the **complete, unedited JSON responses**. The planner was
Claude (`claude-opus-5`) and the data came live from ClinicalTrials.gov (API 2.0.5, data snapshot
2026-09-23). Each run has two files: `NN_name.request.json` and `NN_name.response.json`.

Regenerate them with `uv run python scripts/run_examples.py` (optionally passing name
substrings, e.g. `… 08 13`).

## Suggested starting points

If you only open five, open these. Together they cover a trend, a comparison, a geographic
ranking, a network, and a breakdown with totals:

| Run | Question | Chart |
|---|---|---|
| [`01_trend_drug_field`](01_trend_drug_field.response.json) | "How has the number of trials for this drug changed over time?" + `drug_name: Pembrolizumab` | line |
| [`02_compare_phases`](02_compare_phases.response.json) | "Compare pembrolizumab and nivolumab trials across phases." | grouped bar |
| [`03_geography_fields`](03_geography_fields.response.json) | "Which countries have the most trials?" + `condition`, `trial_phase`, `status` fields | ranked bar with ISO3 codes |
| [`13_drug_pair_network`](13_drug_pair_network.response.json) | "Among interventional melanoma studies that started from 2020 through 2024, which pairs of drug interventions appear together in the same study most often? …" | network |
| [`08_country_by_phase_stacked`](08_country_by_phase_stacked.response.json) | "For interventional breast cancer studies that started from 2020 through 2024 and are currently recruiting, which 10 countries have the most Phase 2 and Phase 3 trials? …" | stacked bar with totals |

## Coverage of the assignment's example query types

| Appendix category | Example in the assignment | Run | Chart | Headline result |
|---|---|---|---|---|
| Time trends | "How has the number of trials for [drug] changed per year since 2015?" | `01_trend_drug_field` | line | pembrolizumab peaks at 298 studies started in 2022 |
| Time trends | "How many trials started each year for [condition]?" | `12_condition_trend_by_status` | two lines | recruiting 3 → 386, completed 328 → 124 (2015 → 2024) |
| Distributions | "How are [condition] trials distributed across phases?" | `09_condition_phase_distribution` | bar | melanoma: Phase 2 is the largest bucket (1,039) |
| Distributions | "What are the most common intervention types for [drug/condition] trials?" | `10_intervention_types` | bar | lung cancer: Drug (8,396) |
| Comparisons | "Compare phases for trials involving Drug A vs Drug B." | `02_compare_phases` | grouped bar | pembrolizumab vs nivolumab; 293 studies are in both cohorts |
| Comparisons | "Compare sponsor categories across two conditions." | `11_sponsor_categories_two_conditions` | grouped bar | breast vs prostate cancer, by lead sponsor class |
| Geographic patterns | "Which countries have the most recruiting trials for [condition]?" | `03_geography_fields` | ranked bar | recruiting Phase 3 lung cancer: China 136, US 89 |
| Relationships / networks | "Show a network of sponsors ↔ drugs for [condition] trials." | `05_sponsor_drug_network` | two-sided network | NSCLC since 2020: Merck ↔ pembrolizumab (21) |
| Relationships / networks | "Which drugs frequently co-occur in combination studies (drug ↔ drug network)?" | `04_drug_combination_network` | network | melanoma, same arm: ipilimumab + nivolumab (111) |

## Beyond the appendix

| Run | What it shows | Chart / status |
|---|---|---|
| `08_country_by_phase_stacked` | Breakdown with distinct per-country totals (US 191, China 184; both matched direct API counts) | stacked bar |
| `13_drug_pair_network` | Drug pairs in the same study, 2020–2024, top 15 | network |
| `14_country_collaboration_network` | Countries that run melanoma trials together (Australia–US, 221) | network |
| `06_enrollment_histogram` | Enrollment-size distribution, Phase 3 breast cancer | histogram |
| `15_duration_histogram` | How long pembrolizumab trials run (3–5 years most common) | histogram |
| `07_enrollment_vs_duration_scatter` | One point per study (1,424 studies) | scatter |
| `16_clarification` | Ambiguous question ("Show me the immunotherapy landscape") gets three complete alternative plans | `needs_clarification` |
| `17_unsupported` | Efficacy question declined, with answerable alternatives suggested | `unsupported` |
| `18_broad_question_auto_narrowed` | 123k-study question: the planner probed the size and narrowed to recruiting interventional trials, disclosed in `meta.assumptions` | ranked bar |
| `19_too_broad_plan_refused` | The same question as an unrestricted `plan`: refused, with narrowing options whose counts were checked (18,852 / 11,020 studies) | `needs_clarification` |

## Reading a response

- `visualization.data`: the chart rows, already counted and ordered, each with `evidence`
  (total, a few NCT IDs, and a link to all of them) and `citations`.
- `visualization.totals` (breakdowns): distinct studies per category across all series.
- `visualization.vega_lite`: a ready-to-render Vega-Lite spec of the same data.
- `plan`: what actually ran. `meta.filters`: the effective filters per cohort.
  `meta.api_queries`: the exact ClinicalTrials.gov URLs.
- `meta.assumptions`, `meta.definitions`, `meta.warnings`: how to read the numbers.

Evidence links (`/query/<id>/evidence/…`) point at the server that produced the response. They
work while that server holds the result in its cache; re-running the request reproduces them.
