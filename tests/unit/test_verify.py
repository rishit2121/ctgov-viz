"""Verifier: a correct response passes; each injected fault is caught."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from app.analysis.engine import run
from app.contracts.analysis import AnalysisResult, Contributor
from app.contracts.plan import QueryPlan
from app.contracts.response import ApiQuery, Completeness, Meta, QueryResponse
from app.contracts.trial import Trial
from app.evidence.store import EvidenceBundle
from app.verify.checks import CohortCheck, verify
from app.viz.builder import build
from tests.conftest import trial

Built = tuple[QueryResponse, AnalysisResult, EvidenceBundle, dict[str, CohortCheck]]


def respond(analysis: dict[str, Any], cohorts: dict[str, list[Trial]],
            predicate: Callable[[Trial], bool] = lambda t: True) -> Built:
    plan = QueryPlan.model_validate({
        "cohorts": [{"label": lab, "condition": lab.lower()} for lab in cohorts],
        "analysis": analysis})
    result = run(plan, cohorts)
    trials = {t.nct_id: t for ts in cohorts.values() for t in ts}
    bundle = EvidenceBundle(query_id="q_t", trials=trials, sample_size=2)
    spec = build(plan, result, bundle)
    meta = Meta(api_queries=[ApiQuery(cohort=lab, url="u", total=len(ts), fetched=len(ts),
                                      pages=1, complete=True) for lab, ts in cohorts.items()])
    response = QueryResponse(status="ok", plan=plan, visualization=spec, meta=meta)
    checks = {lab: CohortCheck(ts, predicate) for lab, ts in cohorts.items()}
    return response, result, bundle, checks


PHASED = [trial("NCT00000001", phases=["PHASE1"]), trial("NCT00000002", phases=["PHASE3"]),
          trial("NCT00000003", phases=["PHASE3"]), trial("NCT00000004", phases=[])]
NETWORK = [trial("NCT00000011", interventions=[("A", "DRUG"), ("B", "DRUG"), ("C", "DRUG")]),
           trial("NCT00000012", interventions=[("A", "DRUG"), ("B", "DRUG")])]


def bar() -> Built:
    return respond({"kind": "aggregate", "dimension": "phase"}, {"X": PHASED})


def network() -> Built:
    return respond({"kind": "cooccurrence",
                    "pair": {"left": "intervention", "right": "intervention"}}, {"X": NETWORK})


@pytest.mark.parametrize("make", [
    bar, network,
    lambda: respond({"kind": "aggregate", "time": {}}, {"X": [trial("NCT00000021", start="2019"),
                                                              trial("NCT00000022", start="2021")]}),
    lambda: respond({"kind": "aggregate", "dimension": "phase", "series_by": "cohort"},
                    {"A": PHASED[:2], "B": PHASED[1:]}),
    lambda: respond({"kind": "aggregate", "dimension": "country"},
                    {"X": [trial("NCT00000031", countries=["France", "Spain"])]}),
    lambda: respond({"kind": "numeric_pair", "x_measure": "duration_months",
                     "y_measure": "enrollment"},
                    {"X": [trial("NCT00000041", enrollment=5, start="2019-01",
                                 completion="2020-01")]}),
])
def test_correct_responses_pass(make: Callable[[], Built]) -> None:
    assert verify(*make()) == []


def test_count_that_disagrees_with_evidence() -> None:
    r, res, b, c = bar()
    assert r.visualization is not None
    r.visualization.data[2]["study_count"] += 1
    assert any("must be equal" in v for v in verify(r, res, b, c))


def test_sample_with_non_contributor() -> None:
    r, res, b, c = bar()
    assert r.visualization is not None
    r.visualization.data[0]["evidence"]["sample"] = ["NCT00000003"]
    assert any("not contributors" in v for v in verify(r, res, b, c))


def test_cited_study_that_was_never_fetched() -> None:
    r, res, b, c = bar()
    b.items["r0"].append(Contributor(nct_id="NCT99999999", fields={}))
    violations = verify(r, res, b, c)
    assert any("not among the fetched studies" in v for v in violations)


def test_malformed_nct_id() -> None:
    r, res, b, c = bar()
    b.items["r0"][0] = Contributor(nct_id="NCT_EXAMPLE", fields={})
    assert any("malformed NCT ID" in v for v in verify(r, res, b, c))


def test_contributor_failing_cohort_filters() -> None:
    r, res, b, c = respond({"kind": "aggregate", "dimension": "phase"}, {"X": PHASED},
                           predicate=lambda t: t.nct_id != "NCT00000002")
    assert any("does not satisfy" in v for v in verify(r, res, b, c))


def test_rows_out_of_order() -> None:
    r, res, b, c = bar()
    assert r.visualization is not None
    r.visualization.data.reverse()
    assert any("not in (series order" in v for v in verify(r, res, b, c))


def test_missing_zero_row_in_aligned_series() -> None:
    r, res, b, c = respond({"kind": "aggregate", "dimension": "phase", "series_by": "cohort"},
                           {"A": PHASED[:1], "B": PHASED[1:2]})
    assert r.visualization is not None
    r.visualization.data = [d for d in r.visualization.data if d["study_count"] > 0]
    assert any("aligned with explicit zeros" in v for v in verify(r, res, b, c))


def test_partition_must_account_for_every_study() -> None:
    r, res, b, c = bar()
    c["X"] = CohortCheck([*PHASED, trial("NCT00000099", phases=["PHASE2"])], lambda t: True)
    assert any("does not account" in v for v in verify(r, res, b, c))


def test_network_faults() -> None:
    r, res, b, c = network()
    spec = r.visualization
    assert spec is not None and spec.edges and spec.nodes
    spec.edges[0].target = spec.edges[0].source
    spec.edges[1].target = "intervention:ghost"
    spec.nodes[-1].study_count = 0
    b.items[spec.nodes[-1].evidence.ref.rsplit("/", 1)[-1]] = []
    spec.nodes[-1].evidence.total = 0
    violations = verify(r, res, b, c)
    assert any("self-loop" in v for v in violations)
    assert any("missing node" in v for v in violations)
    assert any("outweighs" in v for v in violations)


def test_status_must_match_completeness() -> None:
    r, res, b, c = bar()
    r.meta.api_queries[0].complete = False
    r.meta.completeness = Completeness(complete=False, reason="page 2 failed")
    assert any("'partial' exactly when" in v for v in verify(r, res, b, c))
    r.status = "partial"
    assert verify(r, res, b, c) == []
    r.meta.completeness = Completeness(complete=True)
    assert any("disagrees" in v for v in verify(r, res, b, c))


def test_empty_status_rules() -> None:
    r, res, b, c = bar()
    r.status = "empty"
    violations = verify(r, res, b, c)
    assert any("nonzero data" in v for v in violations)
    assert any("explain why" in v for v in violations)


def test_tampered_citation_excerpt_is_caught() -> None:
    r, res, b, c = bar()
    assert r.visualization is not None
    r.visualization.data[0]["citations"][0]["excerpts"][0]["text"] = "PHASE4"
    assert any("not verbatim" in v for v in verify(r, res, b, c))


def test_citation_of_a_non_contributor_is_caught() -> None:
    r, res, b, c = network()
    assert r.visualization is not None and r.visualization.edges
    r.visualization.edges[0].citations[0].nct_id = "NCT00000099"
    assert any("not one of its contributors" in v for v in verify(r, res, b, c))


def breakdown() -> Built:
    trials = [trial("NCT00000051", countries=["France", "Spain"], phases=["PHASE2"]),
              trial("NCT00000052", countries=["France"], phases=["PHASE3"])]
    return respond({"kind": "aggregate", "dimension": "country", "series_by": "phase"},
                   {"X": trials})


def test_breakdown_with_totals_passes() -> None:
    assert verify(*breakdown()) == []


def test_stacking_series_that_do_not_add_up_is_caught() -> None:
    r, res, b, c = breakdown()
    assert r.visualization is not None and r.visualization.totals is not None
    r.visualization.data[0]["study_count"] += 1  # series now exceed the total
    violations = verify(r, res, b, c)
    assert any("overlapping series must not be stacked" in v for v in violations)


def test_missing_or_tampered_totals_are_caught() -> None:
    r, res, b, c = breakdown()
    assert r.visualization is not None and r.visualization.totals is not None
    r.visualization.totals[0]["study_count"] = 99
    assert any("must be equal" in v for v in verify(r, res, b, c))
    r.visualization.totals = None
    assert any("must carry per-category totals" in v for v in verify(r, res, b, c))
