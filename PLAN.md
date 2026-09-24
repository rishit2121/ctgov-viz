# ctgov-viz: design details

This goes one step deeper than the System design section of the README. It covers how the pieces fit together, the rules behind the numbers, and the tradeoffs I made. The full report, with measurements and test results, is in [`docs/SYSTEM_DESIGN.md`](docs/SYSTEM_DESIGN.md).

The rule behind everything: **Claude only interprets the question.** It writes a constrained plan. Code retrieves the records, computes every number, picks the chart, and checks the result. No count, NCT ID, or chart value in a response comes from the model.

## Architecture

A request moves through four stages, always in the same direction:

```text
  question + optional fields
     │
     ▼
  1. PLAN ··········· Claude writes a QueryPlan; code validates it      (only LLM step)
     │ validated plan
     ▼
  2. RETRIEVE ······· compile → preflight count → fetch every page     (only network step)
     │ raw study records
     ▼
  3. CALCULATE ······ normalize → re-check filters → count by NCT ID   (pure code)
     │ results that keep their contributing studies
     ▼
  4. BUILD & CHECK ·· chart spec → citations → verifier                (pure code)
     │
     ▼
  QueryResponse, cached by query_id
```

What each stage contains:

| Stage | Component | Code | Job | Never does |
|---|---|---|---|---|
| 1. Plan | Planner | `app/planner/` | Question → plan, clarification, or refusal | Write counts, NCT IDs, or chart data |
| | Request fields | `app/registry/request_fields.py` | Apply the optional fields to every cohort as hard constraints | Silently drop one side of a comparison |
| | Validator and linter | `app/registry/validator.py`, `linter.py` | Reject illegal plans; write the definitions that explain the numbers | Rely on the model's wording for disclosures |
| 2. Retrieve | Compiler | `app/ctgov/compiler.py` | Plan → API parameters, a local filter check, and the fields to request | Decide anything the plan didn't say |
| | Client | `app/ctgov/client.py` | Fetch every page with retries and report completeness | Call a result complete after a page failed |
| 3. Calculate | Normalizer | `app/normalize/` | Raw record → `Trial`, keeping the raw record for citations | Fill in missing values or fuzzy-merge names |
| | Analysis engine | `app/analysis/` | Group, count distinct studies, order, build pairs and totals | Know about HTTP, the LLM, or chart types |
| 4. Build & check | Chart builder | `app/viz/` | Pick the chart type and encodings from the result's shape | Recompute or change counts |
| | Evidence | `app/evidence/` | Store contributors; build verbatim citations | Cite text that isn't in the record |
| | Verifier | `app/verify/checks.py` | Check counts, order, evidence, and bounds | Fix anything. It only rejects |

`app/contracts/` holds the typed models passed between stages and depends on nothing. `app/pipeline.py` runs the stages in order, and `app/api/` is a thin FastAPI layer on top.

Supported fields live in one place, the field registry (`app/registry/fields.py`). Each of the 11 dimensions (phase, status, study type, sponsor, sponsor class, intervention, intervention type, condition, country, enrollment size, duration) is one `FieldDef`. The prompt, validator, engine, chart builder, field projection, citations, and `GET /capabilities` all read from it. Adding a dimension means adding one entry and its citer, not a new handler. For example, lead sponsor:

```python
# app/registry/fields.py
FieldDef(
    Dimension.sponsor, "Lead sponsor", frozenset({"group", "pair"}),  # title, legal operations
    lambda t: _one(t.sponsor_name) if t.sponsor_name else [],          # extractor
    (paths.P_SPONSOR,),                                                # path cited as evidence
    "count", default_top_k=20,                                         # rank by count, top 20
),
```

## One request, step by step

Take `{"query": "Which countries have the most trials?", "condition": "lung cancer", "trial_phase": "Phase 3", "status": "recruiting"}`.

1. **Parse.** `"Phase 3"` and `"recruiting"` are parsed into fixed values (`PHASE3`, `RECRUITING`). Unknown fields are rejected.
2. **Plan.** Claude can check how many studies match a search with `probe_cohort`. It then calls `submit_plan` with one cohort and an `aggregate` analysis by `country`, sorted by count.
3. **Apply the fields.** The request's condition, phase, and status override those parts of every cohort. Each override is recorded as an assumption.
4. **Validate and disclose.** The validator checks the plan. The linter adds definitions such as "a Phase 3 filter includes Phase 2/3 studies".
5. **Compile.** The plan becomes API parameters (`query.cond`, `filter.overallStatus`, `filter.advanced=AREA[Phase]PHASE3`). It also produces a local check for each structured filter and the short list of fields this plan reads.
6. **Preflight.** One count request per cohort. Zero matches returns `empty`. Over 20,000 matches returns `needs_clarification` with narrower options.
7. **Retrieve.** Every page, 1,000 studies at a time, with retries.
8. **Normalize and re-check.** Each record becomes a `Trial`. Studies that fail the local filter check are dropped and counted.
9. **Analyze.** Each country maps to a set of NCT IDs, and the count is the size of that set. The top 20 are kept, and the response says so.
10. **Build.** A country result becomes a `choropleth_bar`: ranked bars plus ISO3 codes, so a frontend can also draw a map. Every row gets inline citations.
11. **Verify.** If any check fails, the response becomes a 500 `verification_failed` instead of a wrong chart.
12. **Respond.** `query_id` is a hash of the plan and the data snapshot. It keys a cache that keeps the response and its evidence links working.

## The planner (agent design)

**What a plan can say.** A `QueryPlan` has 1–4 cohorts and one analysis. A cohort is a ClinicalTrials.gov search (condition, intervention, sponsor, or free text) plus structured filters (status, phase, study type, start dates, country). The analysis is one of three kinds:

| Kind | Used for | Charts |
|---|---|---|
| `aggregate` | Counts by a category or by start year, optionally split into series | bar, grouped bar, stacked bar, histogram, line, country ranking |
| `cooccurrence` | Pairs that appear in the same study or the same arm | network |
| `numeric_pair` | Two numbers per study | scatter |

A comparison isn't a special case. It is just two or more cohorts with `series_by="cohort"`. No field in the plan can hold a count, an NCT ID, or chart data.

**Tools.** Claude gets two read-only research tools and three answer tools:

| Tool | Type | What it does |
|---|---|---|
| `probe_cohort` | research | Returns a search's match count and three titles. It catches typos, zero-hit terms, and searches that are too broad |
| `validate_plan` | research | Runs the validator on a draft plan |
| `submit_plan` | answer | Final plan |
| `ask_clarification` | answer | 2–3 options, each a complete plan the UI can re-submit |
| `declare_unsupported` | answer | For outcome, efficacy, or off-topic questions, with a nearby question that is supported |

Research tools are capped at 4 calls per question. A reply in plain text instead of an answer tool is rejected.

**Checking the answer.** The plan arrives as tool arguments. It is checked in code: first the Pydantic schema, then the validator. I tried making Claude decode against the plan schema directly, but it was larger than the decoding grammar limit, and it would only grow as dimensions are added. An invalid plan gets one repair turn with the full list of problems. A second failure returns `plan_invalid`; the service doesn't guess.

```python
# app/planner/planner.py (inside the tool loop)
answer = next((c for c in reply.tool_calls if c.name in ANSWER_TOOLS), None)
if answer is not None:
    errors, result = self._interpret(answer, info, tools.probe_totals)  # schema + validator
    if result is not None:
        return result
...
if errors:
    if repaired:
        raise PlannerError("plan_invalid", "Could not turn the question into a "
                           "valid query plan.", errors)
    repaired = True  # the errors go back to Claude as the tool result
```

**Keeping model text out of the numbers.** The prompt is generated from the field registry, so it can't drift from what the validator accepts. Definitions come from the linter, not from the model. If a model-written assumption repeats a study count it saw while probing, it is dropped, because counts may only come from the pipeline.

**Providers.** The planner uses a provider-neutral conversation format with adapters for Anthropic (default, `claude-opus-5`) and OpenAI. `LLM_MODE=fake` answers the saved example questions without an API key, and a scripted LLM drives the tests.

**Public demo protections.** Identical questions reuse a cached plan, so only new questions reach Claude. `LLM_REQUESTS_PER_CLIENT_PER_HOUR` and `LLM_REQUESTS_PER_DAY` limit the rest; going over returns 429 `rate_limited`.

## Retrieval

- **Filters are sent to the API and checked again.** Structured filters go to ClinicalTrials.gov to keep downloads small. They are then checked on every normalized study, so every cited study meets them. Free-text matching is left to ClinicalTrials.gov, whose synonym expansion (Keytruda, MK-3475 → pembrolizumab) is better than anything I would write. The local check is plain code over the normalized study:

  ```python
  # app/ctgov/compiler.py
  def check(t: Trial) -> bool:
      if statuses and t.overall_status not in statuses:
          return False
      if phases and not phases.intersection(t.phases):  # Phase 3 filter includes Phase 2/3
          return False
      if country and not _has_country(t, country, country_iso3):
          return False
      ...
      return True
  ```
- **Only the needed fields.** The compiler requests just the fields the plan reads. For a trend, that makes pages about 10× smaller. A test checks that projected and full records give the same analysis.
- **Year ranges are pushed down.** A trend's years become `AREA[StartDate]RANGE[...]`, so out-of-range studies are never downloaded.
- **Search text is sanitized.** API filter syntax (`AREA[`, `RANGE[`) and control characters are removed from user text.
- **Every page, or an honest label.** Pages come from a cursor, 1,000 studies each, with duplicates removed. Timeouts, 429s, 5xx errors, and non-JSON responses are retried with backoff. A 400 is not retried. If page 1 fails, the request fails; if a later page fails, the result is `partial`. It never returns fewer studies than the API reported without saying so.
- **No sampling.** A cohort over 20,000 studies is refused with narrowing options. A first-N sample would be biased by the API's sort order.

## Counting rules

These rules are also written into `meta.definitions` on every response.

| Topic | Rule |
|---|---|
| Unit | Distinct NCT IDs. Sites, arms, and conditions don't create extra trials |
| Phase breakdown | One bucket per study. `[PHASE2, PHASE3]` is "Phase 2/3", so the buckets add up to the cohort |
| Phase filter | Includes studies whose phases contain it, so a Phase 3 filter includes Phase 2/3 |
| Recruiting | Current overall status `RECRUITING` only |
| Year | Registered start year (actual or anticipated), not "active during". Future and in-progress years are flagged |
| Country | Distinct site countries per study; a multinational study counts once in each |
| Comparison | Cohorts are fetched separately; a study in both counts in both series, and the overlap is reported |
| Totals | Distinct studies per category across all series, computed from IDs and never summed |
| Network, same study | Both interventions are listed on the record; this doesn't prove they were given together |
| Network, same arm | Both are assigned to the same arm group, so they were given together |
| Interventions | Trademark symbols, doses, and salts removed, plus a fixed alias list. No fuzzy matching. Placebo and assessment-only entries are left out of networks by default |
| Duration | Start to primary completion, in months; both dates need month precision |

## Analysis engine

Every group is a dict keyed by NCT ID, and its count is the dict's length. Counts can't drift from their evidence, and a study can't be counted twice in one group.

```python
# app/contracts/analysis.py
@dataclass(slots=True)
class Row:
    values: dict[str, str | int]  # e.g. {"phase": "Phase 2"}
    contributors: dict[str, Contributor] = field(default_factory=dict)  # keyed by NCT ID

    @property
    def count(self) -> int:
        return len(self.contributors)  # never stored separately
```

| Result | Steps |
|---|---|
| Phase bars | filter → one phase bucket per study → group → distinct count → phase order |
| Yearly trend | filter → start year → year range, zero-filled → distinct count → chronological |
| Country ranking | filter → distinct countries per study → group → distinct count → count desc → top k |
| Drug comparison | fetch each cohort → tag with cohort → group by (cohort, phase) → distinct count → aligned series |
| Drug network | eligible interventions per study or arm → unordered pairs → group → distinct count → strongest edges across all pairs |
| Sponsor–drug network | sponsor × eligible interventions per study → pairs → group → distinct count |
| Histogram | measure per study → fixed bins → distinct count per bin |
| Scatter | two measures per study → drop missing → sort by NCT ID |

When a result is split into series, the engine also computes a distinct total for each category. It checks whether the series partition the category, meaning every study is in exactly one series. That decides between a stacked and a grouped bar.

For networks, "top 15 pairs" means `max_edges: 15`, ranked across every pair. `top_k` is a separate cap on nodes, used only when the question limits them ("among the 20 most common drugs"). I first applied both together, which could drop a strong pair whose drugs weren't among the most frequent. When a node cap is used, the warning now says so. Ordering is deterministic: domain order for phase and status, count descending with alphabetical tie-breaks, and ascending years. The palette has eight colors, so a ninth series isn't given a new one; the smaller series fold into "Other", counted as a distinct union.

## Charts

The chart type is chosen by code from the result's shape, not by the model:

| Result shape | Chart |
|---|---|
| One category | `bar` (horizontal when ranked or when labels are long) |
| Category × series, where the series partition each category | `stacked_bar`, with the total labeled at the end of each bar |
| Category × series, where the series overlap | `grouped_bar` with a total marker per category |
| Category × cohort (a comparison) | `grouped_bar` |
| Binned number | `histogram` |
| Start year, with or without series | `line` |
| Country | `choropleth_bar` (ranked bars plus ISO3 codes) |
| Pairs | `network` (nodes, edges, groups) |
| Two numbers | `scatter` (log scale for enrollment) |

The spec includes explicit sort orders, titles built from the plan, and colors from a fixed colorblind-safe order. Colors are assigned per entity, never by rank. There is also an optional Vega-Lite v5 spec of the same data, whose rows carry indices so a click maps back to its evidence.

## Evidence and citations

Each chart value makes a claim about its studies, for example "phase bucket = Phase 2/3", "has a site in South Korea", or "lists both nivolumab and ipilimumab". Each dimension has a citer that finds the supporting value in the raw record and cites it by exact path, including the list index, such as `protocolSection.contactsLocationsModule.locations[52].country`. Cohort membership is cited as well: the search match and each structured filter.

Excerpt text is always read from the record at the cited path, so it is verbatim. When a claim rests on a missing value, or on ClinicalTrials.gov's synonym expansion rather than a verbatim match, the excerpt has `text: null` and says why. Each value carries 5 citations inline, and `GET /query/{id}/evidence/{item}` pages through every contributing study in the same format.

## The verifier

The verifier works from the fetched studies and the evidence store, and rejects the response if any check fails:

- count = evidence total = number of distinct contributors;
- every cited NCT ID is well formed, was fetched, and meets its cohort's filters;
- every excerpt is verbatim at its path;
- rows follow the series × category order with explicit zeros, and years are contiguous;
- each total equals its evidence, lies between the largest series and the sum of the series, and stacked series add up exactly;
- network edges point to existing nodes, have no self-loops or duplicates, and weigh no more than either endpoint;
- single-valued breakdowns account for every fetched study;
- the status matches completeness;
- the payload stays within bounds (1,000 rows, 300 nodes, 150 edges, 5,000 points, 3 MB).

A failed check means there's a bug, so the service returns an error rather than a chart that might be wrong. Each check has a test that injects the fault it should catch. Here is the count check, run on every row, node, and edge:

```python
# app/verify/checks.py
ids = [c.nct_id for c in items]  # contributors registered in the evidence store
if not count == ref.total == len(set(ids)) == len(ids):
    v.append(f"{where}: count {count}, evidence total {ref.total} and {len(ids)} "
             "registered contributors must be equal and distinct")
if not set(ref.sample) <= set(ids):
    v.append(f"{where}: evidence sample contains studies that are not contributors")
```

## When things go wrong

| Situation | HTTP | `status` or error code |
|---|---|---|
| Chart produced | 200 | `ok` |
| A later page failed after retries | 200 | `partial`, with the reason |
| Valid plan, no matches | 200 | `empty`, with the API queries |
| Ambiguous question, or cohort over 20,000 | 200 | `needs_clarification`, with options |
| Outside registration data | 200 | `unsupported`, with a nearby supported question |
| Bad request or bad supplied plan | 422 | `invalid_request`, `plan_invalid` |
| Model couldn't produce a valid plan | 422 | `plan_invalid` |
| Request field would merge a comparison | 422 | `conflicting_fields` |
| Rate limit reached | 429 | `rate_limited` |
| ClinicalTrials.gov rejected the query | 502 | `upstream_rejected` |
| ClinicalTrials.gov down on page 1 | 502 | `upstream_unavailable` |
| LLM missing or unreachable | 503 | `llm_unavailable` |
| Retrieval took longer than 90 s | 504 | `upstream_timeout` |
| A verifier check failed | 500 | `verification_failed` |

## Question coverage

All of these use the same three analysis kinds, with no per-question code:

| Question type | Example | Plan | Chart |
|---|---|---|---|
| Trend | Breast cancer trials started each year since 2015 | 1 cohort; aggregate by start year | line |
| Trend by series | Recruiting vs completed breast cancer trials by start year | 1 cohort; start year, series by status | line (two lines) |
| Trend comparison | Pembrolizumab vs nivolumab trials per year | 2 cohorts; start year, series by cohort | line (two lines) |
| Distribution | Pembrolizumab trials by phase | intervention cohort; by phase | bar |
| Top k | Top sponsors of Alzheimer's trials | by sponsor, top k | horizontal bar |
| Comparison | Pembrolizumab vs nivolumab by phase | 2 cohorts; by phase, series by cohort | grouped bar |
| Breakdown | Lung cancer trials by phase and sponsor class | 1 cohort; by phase, series by sponsor class | stacked bar |
| Geography | Countries with the most recruiting Phase 2/3 breast cancer trials, by phase | by country, series by phase | stacked bar with totals |
| Geography filter | Phase distribution of diabetes trials in India | country filter; by phase | bar |
| Distribution of a number | Enrollment sizes of Phase 3 breast cancer trials | by enrollment size | histogram |
| Network | Drug pairs in melanoma studies | cooccurrence, intervention × intervention | network |
| Sponsor network | Sponsors and the drugs they study in NSCLC | cooccurrence, sponsor × intervention | network |
| Two numbers | Enrollment vs duration for Phase 3 breast cancer | numeric pair | scatter |
| Ambiguous | "Show immunotherapy trends" | `needs_clarification` with plan options | none |
| Unsupported | "Which trials had the best survival outcomes?" | `unsupported` | none |
| Too broad | "Cancer trials by country" (over 120,000 studies) | `needs_clarification` with narrower options | none |

## Design decisions

| Decision | Why | What I didn't do |
|---|---|---|
| The model writes a plan, never data | Numbers and IDs have no path from the model to the response | Let the model count or summarize fetched records |
| Cohorts + one analysis kind | Covers every question type with shared code | An intent classifier with a handler per intent |
| Filters sent to the API and re-checked | Small downloads, and every cited study provably matches | Trust the API's filtering alone |
| Refuse cohorts over 20,000 | A first-N sample is biased by sort order | Quietly analyze a sample |
| Plan checked in code, not by decoding | The plan schema exceeds the decoding grammar limit and grows with the registry | Shrink the schema to fit |
| One phase bucket per study | Phase bars add up to the cohort | Count a Phase 2/3 study in both Phase 2 and Phase 3 |
| Totals computed from IDs | Series can overlap, so summing them would be wrong | Leave totals to the frontend |
| Citations read from the raw record by path | Verbatim by construction and easy to check | Cite re-serialized, normalized values |
| Verifier rejects instead of repairing | A wrong chart is worse than none | Best-effort fixes |
| Fixed normalization rules and an alias list | Auditable, and raw names stay in the evidence | Fuzzy or embedding-based name merging |
| Plain Python, no agent framework | Provenance stays explicit and the tool loop is short | An agent framework |

## Limits

- This covers registration data only. Efficacy, outcome, and adverse-event questions get `unsupported`.
- Cohorts over 20,000 studies are refused rather than sampled.
- Search recall is ClinicalTrials.gov's. Citations say when a match came from its synonym expansion.
- The drug alias list is fixed, so some brand names can still appear as separate nodes.
- Conditions are grouped by their registered text, without a MeSH roll-up.
- There is no "active during year X" view yet; trends use start year.
- Cached responses and evidence links live in memory and are lost on restart.
