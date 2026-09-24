"""Analysis primitives: pairing, ordering, year ranges, label resolution."""

from __future__ import annotations

from app.analysis.ops import (
    LabelResolver,
    arm_pairs,
    bipartite_pairs,
    dedupe_keys,
    order_categories,
    unordered_pairs,
    year_range,
)


def test_unordered_pairs_are_canonical_and_deduped() -> None:
    assert unordered_pairs(["b", "a", "b", "c"]) == {("a", "b"), ("a", "c"), ("b", "c")}
    assert unordered_pairs(["a", "a"]) == set()  # no self-pairs
    assert unordered_pairs([]) == set()


def test_bipartite_pairs() -> None:
    assert bipartite_pairs(["s1", "s1"], ["d1", "d2"]) == {("s1", "d1"), ("s1", "d2")}


def test_arm_pairs_only_pair_within_an_arm() -> None:
    arms = [frozenset({"a", "b"}), frozenset({"c"}), frozenset({"a", "b", "placebo"})]
    assert arm_pairs(arms, eligible={"a", "b", "c"}) == {("a", "b")}


def test_count_desc_breaks_ties_by_label() -> None:
    totals = {"k1": 3, "k2": 5, "k3": 3}
    labels = {"k1": "Beta", "k2": "Alpha", "k3": "alpha"}
    assert order_categories(totals, labels, "count_desc") == ["k2", "k3", "k1"]


def test_domain_order_puts_unknown_values_last() -> None:
    totals = {"a": 1, "b": 9, "c": 2}
    labels = {"a": "Phase 3", "b": "Weird", "c": "Phase 1"}
    assert order_categories(totals, labels, "domain", ["Phase 1", "Phase 3"]) == ["c", "a", "b"]


def test_year_range_zero_fills_and_respects_bounds() -> None:
    assert year_range({2017, 2019}, None, None) == [2017, 2018, 2019]
    assert year_range({2017}, 2015, 2018) == [2015, 2016, 2017, 2018]
    assert year_range(set(), None, None) == []


def test_label_resolver_prefers_most_common_spelling() -> None:
    r = LabelResolver()
    for lab in ["pembrolizumab", "Pembrolizumab", "Pembrolizumab"]:
        r.add("pembrolizumab", lab)
    assert r["pembrolizumab"] == "Pembrolizumab"
    assert r["unknown"] == "unknown"


def test_dedupe_keys_keeps_first_label() -> None:
    assert dedupe_keys([("a", "A"), ("a", "a"), ("b", "B")]) == [("a", "A"), ("b", "B")]
