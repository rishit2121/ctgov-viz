"""Pure, reusable analysis primitives. No I/O, no plan knowledge.

Every grouping primitive keys contributors by NCT ID, so "count distinct studies" holds by
construction, and each contributor carries the source values that placed it in the group.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Hashable, Iterable, Sequence
from itertools import combinations, product
from typing import Any

from app.contracts.analysis import Contributor
from app.contracts.trial import Trial


def contributor(trial: Trial, paths: Iterable[str], **extra: Any) -> Contributor:
    fields = {p: trial.source.get(p) for p in paths}
    fields.update(extra)
    return Contributor(nct_id=trial.nct_id, fields=fields)


def dedupe_keys(pairs: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """Keep the first (key, label) per key, preserving order."""
    seen: dict[str, str] = {}
    for key, lab in pairs:
        seen.setdefault(key, lab)
    return list(seen.items())


class LabelResolver:
    """Chooses one display label per key: most frequent spelling, ties broken alphabetically."""

    def __init__(self) -> None:
        self._counts: dict[str, Counter[str]] = {}

    def add(self, key: str, lab: str) -> None:
        self._counts.setdefault(key, Counter())[lab] += 1

    def __getitem__(self, key: str) -> str:
        counts = self._counts.get(key)
        if not counts:
            return key
        return min(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0]


def order_categories[K: Hashable](
    totals: dict[K, int],
    labels: dict[K, str],
    how: str,
    domain_order: Sequence[str] = (),
) -> list[K]:
    """Deterministic category order.

    domain: registry order, unknown values after it alphabetically.
    count_desc: descending count, ties broken by label then key.
    label: alphabetical.
    """
    keys = list(totals)
    if how == "domain" and domain_order:
        rank = {v: i for i, v in enumerate(domain_order)}
        return sorted(keys, key=lambda k: (rank.get(labels[k], len(rank)), labels[k].casefold()))
    if how == "label":
        return sorted(keys, key=lambda k: (labels[k].casefold(), str(k)))
    return sorted(keys, key=lambda k: (-totals[k], labels[k].casefold(), str(k)))


def unordered_pairs(keys: Iterable[str]) -> set[tuple[str, str]]:
    """All unordered pairs of distinct keys, each represented once as (a, b) with a < b."""
    return set(combinations(sorted(set(keys)), 2))


def bipartite_pairs(left: Iterable[str], right: Iterable[str]) -> set[tuple[str, str]]:
    return set(product(set(left), set(right)))


def arm_pairs(arm_sets: Iterable[frozenset[str]], eligible: set[str]) -> set[tuple[str, str]]:
    """Unordered pairs of interventions assigned to the *same* arm group."""
    pairs: set[tuple[str, str]] = set()
    for arm in arm_sets:
        pairs |= unordered_pairs(k for k in arm if k in eligible)
    return pairs


def year_range(years: Iterable[int], lo: int | None, hi: int | None) -> list[int]:
    ys = list(years)
    start = lo if lo is not None else (min(ys) if ys else None)
    end = hi if hi is not None else (max(ys) if ys else None)
    if start is None or end is None or start > end:
        return []
    return list(range(start, end + 1))
