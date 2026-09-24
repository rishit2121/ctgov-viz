"""Engine results on small hand-built cohorts with hand-computed answers."""

from __future__ import annotations

from datetime import date
from typing import Any

from app.analysis.engine import run
from app.contracts.analysis import AnalysisResult
from app.contracts.plan import QueryPlan
from app.contracts.trial import Trial
from tests.conftest import trial

TODAY = date(2026, 9, 23)


def plan(analysis: dict[str, Any], labels: tuple[str, ...] = ("Cohort",)) -> QueryPlan:
    cohorts = [{"label": lab, "condition": lab.lower()} for lab in labels]
    return QueryPlan.model_validate({"cohorts": cohorts, "analysis": analysis})


def counts(result: AnalysisResult, *fields: str) -> dict[Any, int]:
    def key(values: dict[str, Any]) -> Any:
        return values[fields[0]] if len(fields) == 1 else tuple(values[f] for f in fields)
    return {key(r.values): r.count for r in result.rows}


# ------------------------------------------------------------------ geography


def test_country_ranking_counts_studies_not_sites() -> None:
    trials = [
        trial("NCT01", countries=["United States"] * 7 + ["Canada"]),
        trial("NCT02", countries=["United States", "Germany"]),
        trial("NCT03", countries=["Germany"]),
        trial("NCT04", countries=["Canada"]),
        trial("NCT05"),  # no locations
    ]
    r = run(plan({"kind": "aggregate", "dimension": "country", "sort": {"by": "count_desc"}}),
            {"Cohort": trials})
    assert r.shape == "geo"
    assert counts(r, "country") == {"Canada": 2, "Germany": 2, "United States": 2}
    assert r.category_order == ["Canada", "Germany", "United States"]  # ties -> alphabetical
    assert r.rows[0].extra["iso3"] == "CAN"
    assert r.excluded == {"no_country_reported": 1}
    assert set(r.rows[2].contributors) == {"NCT01", "NCT02"}


def test_top_k_is_disclosed() -> None:
    trials = [trial(f"NCT{i:02}", countries=[c]) for i, c in
              enumerate(["France", "France", "Spain", "Italy", "Italy", "Italy"])]
    r = run(plan({"kind": "aggregate", "dimension": "country", "top_k": 2}), {"Cohort": trials})
    assert r.category_order == ["Italy", "France"]
    assert any("top 2 of 3" in w for w in r.warnings)


# ------------------------------------------------------------------ distribution


def test_phase_distribution_single_bucket_per_study() -> None:
    trials = [
        trial("NCT01", phases=["PHASE2", "PHASE3"]),
        trial("NCT02", phases=["PHASE3"]),
        trial("NCT03", phases=["PHASE1"]),
        trial("NCT04", phases=[]),
        trial("NCT05", phases=["PHASE3"]),
    ]
    r = run(plan({"kind": "aggregate", "dimension": "phase"}), {"Cohort": trials})
    assert r.shape == "category"
    assert r.category_order == ["Phase 1", "Phase 2/3", "Phase 3", "Not Reported"]
    assert counts(r, "phase") == {"Phase 1": 1, "Phase 2/3": 1, "Phase 3": 2, "Not Reported": 1}
    assert sum(row.count for row in r.rows) == len(trials)  # buckets partition the cohort


def test_intervention_type_is_multi_valued_but_distinct_per_study() -> None:
    trials = [
        trial("NCT01", interventions=[("A", "DRUG"), ("B", "DRUG"), ("R", "RADIATION")]),
        trial("NCT02", interventions=[("C", "DRUG")]),
    ]
    r = run(plan({"kind": "aggregate", "dimension": "intervention_type"}), {"Cohort": trials})
    assert counts(r, "intervention_type") == {"Drug": 2, "Radiation": 1}
    assert "multi_valued" in r.definitions


# ------------------------------------------------------------------ comparison


def test_comparison_counts_overlapping_study_in_both_series() -> None:
    shared = trial("NCT03", phases=["PHASE3"])
    cohorts = {
        "Pembrolizumab": [trial("NCT01", phases=["PHASE1"]), trial("NCT02", phases=["PHASE3"]),
                          shared],
        "Nivolumab": [trial("NCT04", phases=["PHASE2"]), shared],
    }
    r = run(plan({"kind": "aggregate", "dimension": "phase", "series_by": "cohort"},
                 ("Pembrolizumab", "Nivolumab")), cohorts)
    assert r.shape == "category_series"
    assert r.series_order == ["Pembrolizumab", "Nivolumab"]
    assert r.category_order == ["Phase 1", "Phase 2", "Phase 3"]
    assert counts(r, "cohort", "phase") == {
        ("Pembrolizumab", "Phase 1"): 1, ("Pembrolizumab", "Phase 2"): 0,
        ("Pembrolizumab", "Phase 3"): 2,
        ("Nivolumab", "Phase 1"): 0, ("Nivolumab", "Phase 2"): 1, ("Nivolumab", "Phase 3"): 1,
    }
    nivo_p3 = next(row for row in r.rows if row.values == {"phase": "Phase 3",
                                                           "cohort": "Nivolumab"})
    assert nivo_p3.contributors["NCT03"].fields["cohort_query"] == "Nivolumab"


def test_dimension_series_cross_tab() -> None:
    trials = [
        trial("NCT01", phases=["PHASE3"], sponsor=("Merck", "INDUSTRY")),
        trial("NCT02", phases=["PHASE3"], sponsor=("NCI", "NIH")),
        trial("NCT03", phases=["PHASE2"], sponsor=("Pfizer", "INDUSTRY")),
    ]
    r = run(plan({"kind": "aggregate", "dimension": "phase", "series_by": "sponsor_class"}),
            {"Cohort": trials})
    assert r.series_order == ["Industry", "NIH"]
    assert counts(r, "sponsor_class", "phase") == {
        ("Industry", "Phase 2"): 1, ("Industry", "Phase 3"): 1,
        ("NIH", "Phase 2"): 0, ("NIH", "Phase 3"): 1,
    }


# ------------------------------------------------------------------ trend


def test_trend_is_chronological_zero_filled_and_flags_partial_year() -> None:
    trials = [
        trial("NCT01", start="2015-03"),
        trial("NCT02", start="2015"),
        trial("NCT03", start="2017-06-01"),
        trial("NCT04", start="2014-12-31"),  # before window
        trial("NCT05", start=None),  # no start date
        trial("NCT06", start="2027-01", start_type="ESTIMATED"),  # anticipated
    ]
    r = run(plan({"kind": "aggregate", "time": {"from_year": 2015}}), {"Cohort": trials},
            today=TODAY)
    assert r.shape == "temporal"
    years = counts(r, "start_year")
    assert list(years) == list(range(2015, 2028))
    assert years[2015] == 2 and years[2016] == 0 and years[2017] == 1 and years[2027] == 1
    assert r.excluded == {"missing_start_date": 1, "start_year_outside_range": 1}
    assert any("2026 is still in progress" in w for w in r.warnings)
    assert any("anticipated start dates after 2026" in w for w in r.warnings)
    assert "not the same as being active" in r.definitions["start_year"]


def test_trend_comparison_aligns_series() -> None:
    cohorts = {"A": [trial("NCT01", start="2019")], "B": [trial("NCT02", start="2021")]}
    r = run(plan({"kind": "aggregate", "time": {}, "series_by": "cohort"}, ("A", "B")),
            cohorts, today=TODAY)
    assert r.shape == "temporal_series"
    assert counts(r, "cohort", "start_year") == {
        ("A", 2019): 1, ("A", 2020): 0, ("A", 2021): 0,
        ("B", 2019): 0, ("B", 2020): 0, ("B", 2021): 1,
    }


# ------------------------------------------------------------------ networks


def _melanoma() -> list[Trial]:
    return [
        trial("NCT01", interventions=[("Nivolumab", "DRUG"), ("Ipilimumab", "DRUG"),
                                      ("Placebo", "DRUG")],
              arms=[["Drug: Nivolumab", "Drug: Ipilimumab"], ["Drug: Placebo"]]),
        # same drug registered twice under different spellings -> one node, no self-pair
        trial("NCT02", interventions=[("Opdivo", "DRUG"), ("Nivolumab 3 mg/kg", "DRUG"),
                                      ("Yervoy", "DRUG")],
              arms=[["Drug: Opdivo"], ["Drug: Yervoy"]]),
        trial("NCT03", interventions=[("Nivolumab", "DRUG"), ("Relatlimab", "DRUG"),
                                      ("Surgery", "PROCEDURE")],
              arms=[["Drug: Nivolumab", "Drug: Relatlimab", "Procedure: Surgery"]]),
        trial("NCT04", interventions=[("Pembrolizumab", "DRUG")]),
    ]


def _edges(r: AnalysisResult) -> dict[tuple[str, str], int]:
    return {(e.source.split(":", 1)[1], e.target.split(":", 1)[1]): e.count for e in r.edges}


def test_study_level_cooccurrence() -> None:
    r = run(plan({"kind": "cooccurrence",
                  "pair": {"left": "intervention", "right": "intervention"}}),
            {"Cohort": _melanoma()})
    assert r.shape == "network"
    assert _edges(r) == {("ipilimumab", "nivolumab"): 2, ("nivolumab", "relatlimab"): 1,
                         ("nivolumab", "surgery"): 1, ("relatlimab", "surgery"): 1}
    nivo = next(n for n in r.nodes if n.id == "intervention:nivolumab")
    assert nivo.count == 3 and nivo.label == "Nivolumab" and nivo.group == "Drug"
    assert r.excluded == {"studies_without_a_pair": 1}
    assert "does not imply they were given together" in r.definitions["edge_weight"]


def test_arm_level_cooccurrence_drugs_only() -> None:
    r = run(plan({"kind": "cooccurrence",
                  "pair": {"left": "intervention", "right": "intervention", "scope": "arm",
                           "drugs_only": True}}),
            {"Cohort": _melanoma()})
    # NCT02 lists both drugs but in separate arms -> not a combination
    assert _edges(r) == {("ipilimumab", "nivolumab"): 1, ("nivolumab", "relatlimab"): 1}
    assert "same arm group" in r.definitions["edge_weight"]


def test_bipartite_sponsor_drug_network() -> None:
    trials = [
        trial("NCT01", sponsor=("Merck", "INDUSTRY"), interventions=[("Keytruda", "DRUG")]),
        trial("NCT02", sponsor=("Merck", "INDUSTRY"),
              interventions=[("Pembrolizumab", "DRUG"), ("Lenvatinib", "DRUG")]),
        trial("NCT03", sponsor=("BMS", "INDUSTRY"), interventions=[("Nivolumab", "DRUG")]),
    ]
    r = run(plan({"kind": "cooccurrence", "pair": {"left": "sponsor", "right": "intervention"}}),
            {"Cohort": trials})
    assert _edges(r) == {("Merck", "pembrolizumab"): 2, ("Merck", "lenvatinib"): 1,
                         ("BMS", "nivolumab"): 1}
    assert {n.group for n in r.nodes} == {"sponsor", "intervention"}  # the two sides


def test_network_edge_cap_is_disclosed() -> None:
    trials = [trial(f"NCT{i:02}", interventions=[(f"D{i}", "DRUG"), (f"E{i}", "DRUG")])
              for i in range(5)]
    r = run(plan({"kind": "cooccurrence", "pair": {"left": "intervention",
                                                   "right": "intervention", "max_edges": 3}}),
            {"Cohort": trials})
    assert len(r.edges) == 3
    assert any("3 strongest of 5" in w for w in r.warnings)
    node_ids = {n.id for n in r.nodes}
    assert all(e.source in node_ids and e.target in node_ids for e in r.edges)


# ------------------------------------------------------------------ scatter


def test_scatter_drops_studies_missing_a_measure() -> None:
    trials = [
        trial("NCT02", enrollment=100, start="2019-01", completion="2021-01"),
        trial("NCT01", enrollment=40, start="2020-01", completion="2020-07"),
        trial("NCT03", enrollment=50, start="2020", completion="2021-01"),  # year precision
        trial("NCT04", start="2020-01", completion="2021-01"),  # no enrollment
    ]
    r = run(plan({"kind": "numeric_pair", "x_measure": "duration_months",
                  "y_measure": "enrollment"}), {"Cohort": trials})
    assert [(p.nct_id, p.x, p.y) for p in r.points] == [("NCT01", 6, 40), ("NCT02", 24, 100)]
    assert r.excluded == {"missing_or_invalid_measure": 2}


def test_series_beyond_color_slots_fold_into_other() -> None:
    statuses = ["RECRUITING", "COMPLETED", "TERMINATED", "WITHDRAWN", "SUSPENDED", "UNKNOWN",
                "NOT_YET_RECRUITING", "ACTIVE_NOT_RECRUITING", "ENROLLING_BY_INVITATION"]
    trials = [trial(f"NCT{i:02}{j}", status=s, phases=["PHASE2"])
              for i, s in enumerate(statuses) for j in range(len(statuses) - i)]
    r = run(plan({"kind": "aggregate", "dimension": "phase", "series_by": "overall_status"}),
            {"Cohort": trials})
    assert len(r.series_order) == 8 and r.series_order[-1] == "Other"
    other = next(row for row in r.rows if row.values["overall_status"] == "Other")
    assert other.count == 2 + 1  # the two smallest statuses, distinct studies
    assert any("grouped into 'Other'" in w for w in r.warnings)


def test_networks_drop_ancillary_assessments_by_default() -> None:
    trials = [trial("NCT01", interventions=[("Nivolumab", "DRUG"),
                                            ("Biospecimen Collection", "PROCEDURE"),
                                            ("Computed Tomography", "PROCEDURE"),
                                            ("Usual Care", "OTHER")])] * 1 + [
              trial("NCT02", interventions=[("Nivolumab", "DRUG"), ("Surgery", "PROCEDURE")])]
    pair = {"left": "intervention", "right": "intervention"}
    r = run(plan({"kind": "cooccurrence", "pair": pair}), {"Cohort": trials})
    assert _edges(r) == {("nivolumab", "surgery"): 1}
    r = run(plan({"kind": "cooccurrence", "pair": {**pair, "exclude_ancillary": False}}),
            {"Cohort": trials})
    assert ("biospecimen collection", "nivolumab") in _edges(r)


def test_breakdown_totals_are_distinct_and_partition_is_detected() -> None:
    trials = [trial("NCT01", countries=["France", "Spain"], phases=["PHASE2"]),
              trial("NCT02", countries=["France"], phases=["PHASE3"]),
              trial("NCT03", countries=["France"], phases=["PHASE2", "PHASE3"])]
    r = run(plan({"kind": "aggregate", "dimension": "country", "series_by": "phase"}),
            {"Cohort": trials})
    assert [(t.values["country"], t.count) for t in r.totals] == [("France", 3), ("Spain", 1)]
    assert r.series_partition  # one phase bucket per study
    assert "add up" in r.definitions["total"]


def test_overlapping_series_are_not_a_partition() -> None:
    trials = [trial("NCT01", phases=["PHASE2"],
                    interventions=[("A", "DRUG"), ("R", "RADIATION")]),
              trial("NCT02", phases=["PHASE2"], interventions=[("B", "DRUG")])]
    r = run(plan({"kind": "aggregate", "dimension": "phase", "series_by": "intervention_type"}),
            {"Cohort": trials})
    (total,) = r.totals
    assert total.count == 2 and sum(row.count for row in r.rows) == 3
    assert not r.series_partition
    assert "do not add up" in r.definitions["total"]
