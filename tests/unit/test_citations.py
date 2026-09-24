"""Deep citations: every excerpt is verbatim record text at an exact path, for every chart type."""

from __future__ import annotations

from typing import Any

import pytest

from app.analysis.engine import run
from app.contracts.plan import QueryPlan
from app.contracts.trial import Trial
from app.contracts.viz import Citation, VisualizationSpec
from app.evidence.citations import missing_citers, resolve
from app.evidence.store import EvidenceBundle
from app.normalize.trial import normalize_study
from app.viz.builder import build
from tests.conftest import trial


def make(analysis: dict[str, Any], cohorts: dict[str, list[Trial]],
         cohort_fields: dict[str, Any] | None = None) -> tuple[VisualizationSpec, EvidenceBundle]:
    plan = QueryPlan.model_validate({"cohorts": [
        {"label": lab, **(cohort_fields or {"condition": lab.lower()})} for lab in cohorts],
        "analysis": analysis})
    trials = {t.nct_id: t for ts in cohorts.values() for t in ts}
    bundle = EvidenceBundle("q", trials, sample_size=5)
    return build(plan, run(plan, cohorts), bundle), bundle


def all_citations(spec: VisualizationSpec) -> list[Citation]:
    out = [Citation(**c) for d in spec.data for c in d.get("citations", [])]
    for item in [*(spec.nodes or []), *(spec.edges or [])]:
        out += item.citations
    return out


def assert_verbatim(spec: VisualizationSpec, trials: dict[str, Trial]) -> None:
    citations = all_citations(spec)
    assert citations
    for c in citations:
        assert c.record_url.endswith(c.nct_id) and c.title == trials[c.nct_id].title
        for e in c.excerpts:
            if e.text is not None:
                assert str(resolve(trials[c.nct_id].record, e.field)) == e.text, e


def test_every_dimension_has_a_citer() -> None:
    assert missing_citers() == set()


REAL_PLANS = [
    {"kind": "aggregate", "dimension": d} for d in
    ["phase", "overall_status", "study_type", "sponsor", "sponsor_class", "condition",
     "intervention", "intervention_type", "country", "enrollment_size", "duration"]
] + [
    {"kind": "aggregate", "time": {}},
    {"kind": "cooccurrence", "pair": {"left": "intervention", "right": "intervention"}},
    {"kind": "cooccurrence", "pair": {"left": "sponsor", "right": "intervention"}},
    {"kind": "cooccurrence", "pair": {"left": "country", "right": "country"}},
    {"kind": "numeric_pair", "x_measure": "duration_months", "y_measure": "enrollment"},
]


@pytest.mark.parametrize("analysis", REAL_PLANS, ids=lambda a: a.get("dimension") or a["kind"])
def test_excerpts_resolve_verbatim_on_real_records(analysis: dict[str, Any],
                                                   real_studies: dict[str, Any]) -> None:
    trials = [normalize_study(s) for s in real_studies.values()]
    unique = list({t.nct_id: t for t in trials}.values())
    spec, _ = make(analysis, {"Melanoma": unique}, {"condition": "melanoma"})
    assert_verbatim(spec, {t.nct_id: t for t in unique})


def test_country_cites_the_raw_spelling_at_its_index() -> None:
    t = trial("NCT00000001",
              countries=["United States", "Korea, Republic of", "Korea, Republic of"])
    spec, _ = make({"kind": "aggregate", "dimension": "country"}, {"X": [t]})
    korea = next(d for d in spec.data if d["country"] == "South Korea")
    (excerpt, *_) = korea["citations"][0]["excerpts"]
    assert excerpt == {"field": "protocolSection.contactsLocationsModule.locations[1].country",
                       "text": "Korea, Republic of", "supports": "site country: South Korea"}


def test_brand_spelling_supports_generic_cohort_and_group() -> None:
    t = trial("NCT00000001", phases=["PHASE3"],
              interventions=[("Placebo", "DRUG"), ("KEYTRUDA® (pembrolizumab)", "BIOLOGICAL")])
    spec, _ = make({"kind": "aggregate", "dimension": "phase"}, {"Pembrolizumab": [t]},
                   {"intervention": "pembrolizumab"})
    c = spec.data[0]["citations"][0]
    assert c["excerpt"] == "PHASE3"
    assert c["excerpts"][1] == {
        "field": "protocolSection.armsInterventionsModule.interventions[1].name",
        "text": "KEYTRUDA® (pembrolizumab)",
        "supports": "cohort 'Pembrolizumab' (intervention search 'pembrolizumab')"}


def test_absent_values_and_search_expansion_are_not_faked() -> None:
    t = trial("NCT00000001", phases=[], conditions=["Cutaneous Melanoma Stage IV"])
    spec, _ = make({"kind": "aggregate", "dimension": "phase"}, {"Skin cancer": [t]},
                   {"condition": "skin cancer"})
    excerpts = spec.data[0]["citations"][0]["excerpts"]
    assert excerpts[0]["text"] is None and "no phase registered" in excerpts[0]["supports"]
    assert excerpts[1]["text"] is None and "synonym expansion" in excerpts[1]["supports"]


def test_edge_cites_both_endpoints() -> None:
    t = trial("NCT00000001", interventions=[("Opdivo", "DRUG"), ("Ipilimumab 3 mg/kg", "DRUG")])
    spec, _ = make({"kind": "cooccurrence",
                    "pair": {"left": "intervention", "right": "intervention"}}, {"Melanoma": [t]})
    assert spec.edges is not None
    texts = [e.text for e in spec.edges[0].citations[0].excerpts]
    assert "Opdivo" in texts and "Ipilimumab 3 mg/kg" in texts


def test_comparison_rows_cite_their_own_cohort() -> None:
    a = trial("NCT00000001", phases=["PHASE2"], interventions=[("Nivolumab", "DRUG")])
    spec, _ = make({"kind": "aggregate", "dimension": "phase", "series_by": "cohort"},
                   {"Nivolumab": [a], "Pembrolizumab": [a]}, {"intervention": "nivolumab"})
    rows = [d for d in spec.data if d["study_count"]]
    supports = {d["cohort"]: d["citations"][0]["excerpts"][-1]["supports"] for d in rows}
    assert supports["Nivolumab"].startswith("cohort 'Nivolumab'")
    assert supports["Pembrolizumab"].startswith("cohort 'Pembrolizumab'")


def test_filters_are_cited_too() -> None:
    t = trial("NCT00000001", status="RECRUITING", phases=["PHASE2", "PHASE3"],
              conditions=["Lung Cancer"], countries=["France", "Germany"])
    plan_cohort = {"condition": "lung cancer",
                   "filters": {"overall_status": ["RECRUITING"], "phase": ["PHASE3"],
                               "country": "Germany"}}
    spec, _ = make({"kind": "aggregate", "dimension": "country"}, {"Lung": [t]}, plan_cohort)
    germany = next(d for d in spec.data if d["country"] == "Germany")
    supports = " | ".join(e["supports"] for e in germany["citations"][0]["excerpts"])
    for reason in ("site country: Germany", "condition search", "status filter", "phase filter",
                   "country filter"):
        assert reason in supports


def test_evidence_pages_carry_the_same_citations() -> None:
    ts = [trial(f"NCT0000000{i}", phases=["PHASE1"]) for i in range(1, 8)]
    spec, bundle = make({"kind": "aggregate", "dimension": "phase"}, {"X": ts})
    page = bundle.page("r0", page=2, page_size=5)
    assert page is not None and [i.nct_id for i in page.items] == ["NCT00000006", "NCT00000007"]
    assert page.items[0].excerpts[0].text == "PHASE1"
    assert spec.data[0]["evidence"]["complete_inline"] is False
