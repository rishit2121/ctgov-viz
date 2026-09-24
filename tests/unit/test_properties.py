"""Property tests: invariants that must hold for *any* cohort, not just hand-picked ones."""

from __future__ import annotations

import random

from hypothesis import given, settings
from hypothesis import strategies as st

from app.analysis.engine import run
from app.contracts.plan import QueryPlan
from app.contracts.trial import Trial
from tests.conftest import trial

PHASES = ["EARLY_PHASE1", "PHASE1", "PHASE2", "PHASE3", "PHASE4", "NA"]
COUNTRIES = ["United States", "France", "Japan", "Brazil"]
DRUGS = ["Nivolumab", "Opdivo", "Ipilimumab", "Pembrolizumab", "Placebo", "Surgery"]

trial_specs = st.lists(
    st.fixed_dictionaries({
        "phases": st.lists(st.sampled_from(PHASES), max_size=2),
        "countries": st.lists(st.sampled_from(COUNTRIES), max_size=6),
        "drugs": st.lists(st.sampled_from(DRUGS), max_size=4),
        "year": st.one_of(st.none(), st.integers(2010, 2026)),
    }),
    max_size=25,
)


def build(specs: list[dict]) -> list[Trial]:
    return [
        trial(f"NCT{i:08}", phases=s["phases"], countries=s["countries"],
              interventions=[(d, "DRUG") for d in s["drugs"]],
              start=str(s["year"]) if s["year"] else None)
        for i, s in enumerate(specs)
    ]


def plan(analysis: dict) -> QueryPlan:
    return QueryPlan.model_validate({"cohorts": [{"label": "C", "condition": "c"}],
                                     "analysis": analysis})


@given(trial_specs)
@settings(max_examples=150, deadline=None)
def test_phase_buckets_partition_the_cohort(specs: list[dict]) -> None:
    trials = build(specs)
    r = run(plan({"kind": "aggregate", "dimension": "phase"}), {"C": trials})
    assert sum(row.count for row in r.rows) == len(trials)
    members = [n for row in r.rows for n in row.contributors]
    assert len(members) == len(set(members))


@given(trial_specs)
@settings(max_examples=150, deadline=None)
def test_country_counts_are_distinct_studies(specs: list[dict]) -> None:
    trials = build(specs)
    r = run(plan({"kind": "aggregate", "dimension": "country"}), {"C": trials})
    by_id = {t.nct_id: t for t in trials}
    for row in r.rows:
        country = row.values["country"]
        expected = {t.nct_id for t in trials if country in t.countries}
        assert set(row.contributors) == expected
        assert all(country in by_id[n].countries for n in row.contributors)
    no_country = sum(1 for t in trials if not t.countries)
    assert r.excluded.get("no_country_reported", 0) == no_country


@given(trial_specs)
@settings(max_examples=150, deadline=None)
def test_network_edges_are_bounded_by_their_nodes(specs: list[dict]) -> None:
    trials = build(specs)
    r = run(plan({"kind": "cooccurrence",
                  "pair": {"left": "intervention", "right": "intervention", "max_edges": 150}}),
            {"C": trials})
    nodes = {n.id: n for n in r.nodes}
    seen = set()
    for e in r.edges:
        assert e.source != e.target
        assert (e.source, e.target) not in seen
        seen.add((e.source, e.target))
        assert e.count <= min(nodes[e.source].count, nodes[e.target].count)
        assert set(e.contributors) <= set(nodes[e.source].contributors)
    assert not any("placebo" in n.id for n in r.nodes)


@given(trial_specs, st.randoms(use_true_random=False))
@settings(max_examples=100, deadline=None)
def test_output_is_independent_of_input_order(specs: list[dict], rnd: random.Random) -> None:
    trials = build(specs)
    shuffled = trials[:]
    rnd.shuffle(shuffled)
    for analysis in (
        {"kind": "aggregate", "dimension": "country"},
        {"kind": "aggregate", "time": {}},
        {"kind": "cooccurrence", "pair": {"left": "intervention", "right": "intervention"}},
    ):
        a, b = run(plan(analysis), {"C": trials}), run(plan(analysis), {"C": shuffled})
        assert [(r.values, r.count) for r in a.rows] == [(r.values, r.count) for r in b.rows]
        assert [(e.source, e.target, e.count) for e in a.edges] == \
               [(e.source, e.target, e.count) for e in b.edges]
        assert [n.id for n in a.nodes] == [n.id for n in b.nodes]
