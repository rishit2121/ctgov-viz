"""Evidence registry: every chart datum links to its complete list of contributing studies.

A chart datum carries a bounded ``EvidenceRef`` (total, a few NCT IDs, a ref URL) plus inline deep
citations for those sample studies. The full contributor list is kept here and served page by
page from ``GET /query/{query_id}/evidence/{item_id}``, each entry with the same deep citation.

Storage is an in-process LRU. Eviction is visible (the endpoint returns 404 "expired"), and the
response's ``meta.api_queries`` URLs plus the plan let anyone reproduce the result.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.contracts.analysis import Contributor
from app.contracts.plan import Cohort
from app.contracts.response import EvidenceItem, EvidencePage
from app.contracts.trial import Trial
from app.contracts.viz import Citation, EvidenceRef
from app.evidence.citations import Claim, cite


@dataclass
class _Item:
    claims: list[Claim]
    cohort: Cohort | None


@dataclass
class EvidenceBundle:
    """Contributors, claims and source trials for every datum of one response."""

    query_id: str
    trials: Mapping[str, Trial]
    sample_size: int = 5
    items: dict[str, list[Contributor]] = field(default_factory=dict)
    _claims: dict[str, _Item] = field(default_factory=dict, repr=False)

    def register(self, item_id: str, contributors: Mapping[str, Contributor],
                 claims: list[Claim] | None = None,
                 cohort: Cohort | None = None) -> tuple[EvidenceRef, list[Citation]]:
        """Store a datum's contributors; return its ref and inline citations for the sample."""
        ordered = [contributors[k] for k in sorted(contributors)]
        self.items[item_id] = ordered
        self._claims[item_id] = _Item(claims or [], cohort)
        sample = [c.nct_id for c in ordered[: self.sample_size]]
        ref = EvidenceRef(
            total=len(ordered), sample=sample, complete_inline=len(sample) == len(ordered),
            ref=f"/query/{self.query_id}/evidence/{item_id}",
        )
        return ref, [self.cite(item_id, n) for n in sample]

    def cite(self, item_id: str, nct_id: str, membership: bool = True) -> Citation:
        """Deep citation of one contributing study for one registered datum.

        ``membership=False`` cites only the datum's own claims (used for compact per-point
        scatter citations; the evidence endpoint always returns the full citation).
        """
        item = self._claims[item_id]
        return cite(self.trials[nct_id], item.claims, item.cohort if membership else None)

    def page(self, item_id: str, page: int, page_size: int) -> EvidencePage | None:
        contributors = self.items.get(item_id)
        if contributors is None:
            return None
        start = (page - 1) * page_size
        items = [
            EvidenceItem(**self.cite(item_id, c.nct_id).model_dump(), fields_used=c.fields)
            for c in contributors[start:start + page_size]
        ]
        return EvidencePage(query_id=self.query_id, item=item_id, total=len(contributors),
                            page=page, page_size=page_size, items=items)


class ResultCache:
    """Thread-safe LRU of ``query_id -> (response JSON, evidence bundle)``."""

    def __init__(self, max_entries: int = 128):
        self.max_entries = max_entries
        self._entries: OrderedDict[str, tuple[dict[str, Any], EvidenceBundle]] = OrderedDict()
        self._lock = threading.Lock()

    def put(self, query_id: str, response: dict[str, Any], bundle: EvidenceBundle) -> None:
        with self._lock:
            self._entries[query_id] = (response, bundle)
            self._entries.move_to_end(query_id)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)

    def get(self, query_id: str) -> tuple[dict[str, Any], EvidenceBundle] | None:
        with self._lock:
            entry = self._entries.get(query_id)
            if entry is not None:
                self._entries.move_to_end(query_id)
            return entry
