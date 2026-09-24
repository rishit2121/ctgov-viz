"""Visualization builder: shape -> chart type, encodings, evidence registration, palette rules."""

from __future__ import annotations

from typing import Any

from app.analysis.engine import run
from app.contracts.plan import QueryPlan
from app.contracts.trial import Trial
from app.contracts.viz import VisualizationSpec
from app.evidence.store import EvidenceBundle
from app.viz import theme
from app.viz.builder import build
from tests.conftest import trial


def make(analysis: dict[str, Any], cohorts: dict[str, list[Trial]],
         filters: dict[str, Any] | None = None) -> tuple[VisualizationSpec, EvidenceBundle]:
    plan = QueryPlan.model_validate({
        "cohorts": [{"label": lab, "condition": lab.lower(), "filters": filters or {}}
                    for lab in cohorts],
        "analysis": analysis,
    })
    titles = {t.nct_id: t.title for ts in cohorts.values() for t in ts}
    bundle = EvidenceBundle(query_id="q_test", titles=titles, sample_size=2)
    return build(plan, run(plan, cohorts), bundle), bundle


PHASED = [trial("NCT01", phases=["PHASE1"]), trial("NCT02", phases=["PHASE3"]),
          trial("NCT03", phases=["PHASE3"]), trial("NCT04", phases=["PHASE2", "PHASE3"])]


def test_phase_distribution_is_vertical_ordinal_bar() -> None:
    spec, bundle = make({"kind": "aggregate", "dimension": "phase"}, {"Pembrolizumab": PHASED})
    assert spec.type == "bar"
    assert spec.title == "Pembrolizumab studies by phase"
    assert spec.hints.orientation == "vertical" and not spec.hints.legend
    x, y = spec.encoding.x, spec.encoding.y
    assert x is not None and y is not None
    assert (x.field, x.type, x.sort) == ("phase", "ordinal", ["Phase 1", "Phase 2/3", "Phase 3"])
    assert y.field == "study_count"
    assert [d["phase"] for d in spec.data] == x.sort
    p3 = spec.data[2]
    assert p3["study_count"] == p3["evidence"]["total"] == 2
    assert p3["evidence"]["sample"] == ["NCT02", "NCT03"] and p3["evidence"]["complete_inline"]
    assert p3["evidence"]["ref"] == "/query/q_test/evidence/r2"
    assert {c.nct_id for c in bundle.items["r2"]} == {"NCT02", "NCT03"}


def test_country_ranking_is_horizontal_choropleth_bar() -> None:
    trials = [trial("NCT01", countries=["United States", "Japan"]),
              trial("NCT02", countries=["Japan"])]
    spec, _ = make({"kind": "aggregate", "dimension": "country", "sort": {"by": "count_desc"}},
                   {"Lung cancer": trials},
                   filters={"overall_status": ["RECRUITING"], "phase": ["PHASE3"]})
    assert spec.type == "choropleth_bar"
    assert spec.hints.orientation == "horizontal"
    assert spec.encoding.y is not None and spec.encoding.y.field == "country"
    assert spec.encoding.x is not None and spec.encoding.x.field == "study_count"
    assert spec.data[0] | {"evidence": None} == {"country": "Japan", "iso3": "JPN",
                                                 "study_count": 2, "evidence": None}
    assert spec.geo is not None and spec.geo.color_ramp == theme.SEQUENTIAL_BLUE
    assert spec.subtitle == ("Recruiting · Phase 3 (incl. multi-phase) · "
                             "distinct studies (NCT IDs)")


def test_comparison_is_grouped_bar_with_legend_and_fixed_palette() -> None:
    spec, _ = make({"kind": "aggregate", "dimension": "phase", "series_by": "cohort"},
                   {"Pembrolizumab": PHASED[:2], "Nivolumab": PHASED[2:]})
    assert spec.type == "grouped_bar"
    assert spec.title == "Pembrolizumab vs Nivolumab studies by phase"
    assert spec.hints.legend
    assert spec.encoding.color is not None
    assert spec.encoding.color.sort == ["Pembrolizumab", "Nivolumab"]
    assert spec.palette == {"Pembrolizumab": theme.CATEGORICAL[0],
                            "Nivolumab": theme.CATEGORICAL[1]}
    assert len(spec.data) == 2 * 3  # aligned series with explicit zeros
    vl = spec.vega_lite
    assert vl is not None and "xOffset" in vl["layer"][0]["encoding"]


def test_trend_is_line_with_years_in_order() -> None:
    trials = [trial("NCT01", start="2016-02"), trial("NCT02", start="2018")]
    spec, _ = make({"kind": "aggregate", "time": {"from_year": 2015, "to_year": 2018}},
                   {"Breast cancer": trials})
    assert spec.type == "line"
    assert [d["start_year"] for d in spec.data] == [2015, 2016, 2017, 2018]
    assert [d["study_count"] for d in spec.data] == [0, 1, 0, 1]
    assert "started 2015–2018" in spec.subtitle
    assert spec.hints.orientation is None


def test_network_spec_registers_nodes_and_edges() -> None:
    trials = [trial("NCT01", interventions=[("Nivolumab", "DRUG"), ("Ipilimumab", "DRUG")]),
              trial("NCT02", interventions=[("Nivolumab", "DRUG"), ("Surgery", "PROCEDURE")])]
    spec, bundle = make({"kind": "cooccurrence",
                         "pair": {"left": "intervention", "right": "intervention"}},
                        {"Melanoma": trials})
    assert spec.type == "network" and spec.hints.layout == "force"
    assert spec.nodes is not None and spec.edges is not None
    ids = {n.id for n in spec.nodes}
    assert all(e.source in ids and e.target in ids for e in spec.edges)
    assert all(e.weight == e.evidence.total == len(bundle.items[e.evidence.ref.split("/")[-1]])
               for e in spec.edges)
    assert spec.palette == {"Drug": theme.CATEGORICAL[0], "Procedure": theme.CATEGORICAL[1]}
    assert spec.title == "Intervention co-occurrence in Melanoma studies"


def test_network_groups_beyond_three_share_muted_color() -> None:
    assert theme.group_palette(["A", "B", "C", "D", "E"]) == {
        "A": theme.CATEGORICAL[0], "B": theme.CATEGORICAL[1], "C": theme.CATEGORICAL[2],
        "D": theme.MUTED, "E": theme.MUTED}


def test_scatter_rows_are_single_studies_with_log_enrollment() -> None:
    trials = [trial("NCT01", enrollment=120, start="2019-01", completion="2020-01"),
              trial("NCT02", enrollment=40, start="2019-01", completion="2019-07")]
    spec, bundle = make({"kind": "numeric_pair", "x_measure": "duration_months",
                         "y_measure": "enrollment"}, {"Melanoma": trials})
    assert spec.type == "scatter"
    assert spec.encoding.y is not None and spec.encoding.y.scale == "log"
    assert spec.data[0]["nct_id"] == "NCT01" and spec.data[0]["duration_months"] == 12
    assert set(bundle.items) == {"points"} and len(bundle.items["points"]) == 2
    assert "statusModule.startDateStruct" in bundle.items["points"][0].fields


def test_vega_lite_spec_is_self_contained() -> None:
    spec, _ = make({"kind": "aggregate", "dimension": "phase"}, {"X": PHASED})
    vl = spec.vega_lite
    assert vl is not None
    assert vl["$schema"].endswith("vega-lite/v5.json")
    values = vl["data"]["values"]
    assert values == [{k: v for k, v in d.items() if k != "evidence"} for d in spec.data]
    bar = vl["layer"][0]["mark"]
    assert bar["size"] <= 24 and bar["cornerRadiusEnd"] == 4
    assert vl["layer"][1]["mark"]["type"] == "text"  # value labels on single-series bars


def test_evidence_pages() -> None:
    _, bundle = make({"kind": "aggregate", "dimension": "phase"}, {"X": PHASED})
    page = bundle.page("r2", page=1, page_size=1)
    assert page is not None and page.total == 2 and [i.nct_id for i in page.items] == ["NCT02"]
    assert page.items[0].url == "https://clinicaltrials.gov/study/NCT02"
    assert page.items[0].fields_used["designModule.phases"] == ["PHASE3"]
    assert bundle.page("r99", 1, 10) is None
