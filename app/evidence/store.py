"""Evidence registry: every chart datum links to its complete list of contributing studies.

A chart row carries only a bounded ``EvidenceRef`` (total, a few NCT IDs, and a ref URL); the
full contributor list — with the exact source field values that placed each study in the group —
is kept here and served page by page from ``GET /query/{query_id}/evidence/{item_id}``.

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
from app.contracts.response import EvidenceItem, EvidencePage
from app.contracts.viz import EvidenceRef

STUDY_URL = "https://clinicaltrials.gov/study/{}"


@dataclass
class EvidenceBundle:
    query_id: str
    titles: Mapping[str, str | None]
    sample_size: int = 5
    items: dict[str, list[Contributor]] = field(default_factory=dict)

    def register(self, item_id: str, contributors: Mapping[str, Contributor]) -> EvidenceRef:
        ordered = [contributors[k] for k in sorted(contributors)]
        self.items[item_id] = ordered
        sample = [c.nct_id for c in ordered[: self.sample_size]]
        return EvidenceRef(
            total=len(ordered), sample=sample, complete_inline=len(sample) == len(ordered),
            ref=f"/query/{self.query_id}/evidence/{item_id}",
        )

    def page(self, item_id: str, page: int, page_size: int) -> EvidencePage | None:
        contributors = self.items.get(item_id)
        if contributors is None:
            return None
        start = (page - 1) * page_size
        items = [
            EvidenceItem(nct_id=c.nct_id, url=STUDY_URL.format(c.nct_id),
                         title=self.titles.get(c.nct_id), fields_used=c.fields)
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
