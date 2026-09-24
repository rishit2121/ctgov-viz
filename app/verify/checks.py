"""Response verifier: invariants every outgoing response must satisfy.

The verifier re-derives facts from the fetched studies and the evidence registry and compares
them with what the response claims. It never repairs anything — any violation is a bug, and the
pipeline turns it into a ``verification_failed`` error instead of shipping a wrong chart.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.analysis.engine import COHORT_PATH
from app.contracts.analysis import AnalysisResult
from app.contracts.plan import Dimension
from app.contracts.response import QueryResponse
from app.contracts.trial import Trial
from app.contracts.viz import Citation, EvidenceRef, VisualizationSpec
from app.evidence.citations import resolve
from app.evidence.store import EvidenceBundle
from app.registry.fields import REGISTRY

NCT_RE = re.compile(r"^NCT\d{8}$")
MAX_ROWS, MAX_NODES, MAX_EDGES, MAX_POINTS = 1000, 300, 150, 5000
MAX_PAYLOAD_BYTES = 3_000_000


@dataclass(frozen=True)
class CohortCheck:
    trials: Sequence[Trial]
    predicate: Callable[[Trial], bool]


class VerificationError(RuntimeError):
    def __init__(self, violations: list[str]):
        super().__init__("; ".join(violations[:5]))
        self.violations = violations


def verify(
    response: QueryResponse,
    result: AnalysisResult | None,
    bundle: EvidenceBundle | None,
    cohorts: Mapping[str, CohortCheck],
) -> list[str]:
    v: list[str] = []
    v += _check_status(response)
    spec = response.visualization
    if spec is None or result is None or bundle is None:
        return v
    trials = {t.nct_id: t for c in cohorts.values() for t in c.trials}
    v += _check_evidence(spec, bundle, trials, cohorts)
    if spec.type == "network":
        v += _check_network(spec)
    elif spec.type != "scatter":
        v += _check_rows(spec, result)
        v += _check_partition(response, result, cohorts)
    v += _check_bounds(spec)
    return v


def ensure_verified(response: QueryResponse, result: AnalysisResult | None,
                    bundle: EvidenceBundle | None, cohorts: Mapping[str, CohortCheck]) -> None:
    if violations := verify(response, result, bundle, cohorts):
        raise VerificationError(violations)


# --------------------------------------------------------------------------- status / meta


def _nonzero(spec: VisualizationSpec) -> bool:
    if spec.type == "network":
        return bool(spec.edges)
    if spec.type == "scatter":
        return bool(spec.data)
    return any(d.get("study_count", 0) > 0 for d in spec.data)


def _check_status(r: QueryResponse) -> list[str]:
    v: list[str] = []
    m = r.meta
    all_complete = all(q.complete for q in m.api_queries)
    if m.completeness.complete != all_complete:
        v.append("meta.completeness disagrees with api_queries[].complete")
    if r.status in ("ok", "partial") and r.visualization is None:
        v.append(f"status '{r.status}' requires a visualization")
    elif r.status in ("ok", "partial"):
        assert r.visualization is not None
        if not _nonzero(r.visualization):
            v.append(f"status '{r.status}' with no nonzero datum should be 'empty'")
        if (r.status == "partial") == m.completeness.complete:
            v.append("status must be 'partial' exactly when retrieval was incomplete")
    elif r.status == "empty" and r.visualization is not None and _nonzero(r.visualization):
        v.append("status 'empty' but the chart has nonzero data")
    if r.status == "empty" and not r.message:
        v.append("empty responses must explain why in `message`")
    return v


# --------------------------------------------------------------------------- evidence


def _item_id(ref: EvidenceRef) -> str:
    return ref.ref.rsplit("/", 1)[-1]


def _check_ref(where: str, count: int, ref: EvidenceRef, bundle: EvidenceBundle) -> list[str]:
    v: list[str] = []
    items = bundle.items.get(_item_id(ref))
    if items is None:
        return [f"{where}: evidence ref {ref.ref} is not registered"]
    ids = [c.nct_id for c in items]
    if not count == ref.total == len(set(ids)) == len(ids):
        v.append(f"{where}: count {count}, evidence total {ref.total} and {len(ids)} "
                 "registered contributors must be equal and distinct")
    if count < 0:
        v.append(f"{where}: negative count")
    if not set(ref.sample) <= set(ids):
        v.append(f"{where}: evidence sample contains studies that are not contributors")
    if ref.complete_inline != (len(ref.sample) == len(ids)):
        v.append(f"{where}: complete_inline is wrong")
    return v


def _check_evidence(spec: VisualizationSpec, bundle: EvidenceBundle, trials: Mapping[str, Trial],
                    cohorts: Mapping[str, CohortCheck]) -> list[str]:
    v: list[str] = []
    if spec.type == "network":
        for n in spec.nodes or []:
            v += _check_ref(f"node {n.id}", n.study_count, n.evidence, bundle)
        for e in spec.edges or []:
            v += _check_ref(f"edge {e.source}--{e.target}", e.weight, e.evidence, bundle)
    elif spec.type == "scatter":
        points = {c.nct_id for c in bundle.items.get("points", [])}
        if points != {d["nct_id"] for d in spec.data}:
            v.append("scatter points and their registered evidence differ")
    else:
        for i, d in enumerate(spec.data):
            v += _check_ref(f"row {i}", d["study_count"], EvidenceRef(**d["evidence"]), bundle)

    v += _check_citations(spec, bundle, trials)

    # Every cited study is a real, fetched record that satisfies its cohort's filters.
    valid = {label: {t.nct_id for t in ch.trials if ch.predicate(t)}
             for label, ch in cohorts.items()}
    valid_any = set().union(*valid.values())
    for item_id, contributors in bundle.items.items():
        for c in contributors:
            if not NCT_RE.match(c.nct_id):
                v.append(f"{item_id}: malformed NCT ID {c.nct_id!r}")
            elif c.nct_id not in trials:
                v.append(f"{item_id}: {c.nct_id} was not among the fetched studies")
            elif c.nct_id not in (valid[label] if (label := c.fields.get(COHORT_PATH)) in valid
                                  else valid_any):
                v.append(f"{item_id}: {c.nct_id} does not satisfy its cohort's filters")
    return v


def _check_citations(spec: VisualizationSpec, bundle: EvidenceBundle,
                     trials: Mapping[str, Trial]) -> list[str]:
    """Inline citations cite contributors of that datum, and every excerpt is verbatim."""
    groups: list[tuple[str, str, list[Citation]]] = []  # (where, item id, citations)
    if spec.type == "network":
        groups += [(f"node {n.id}", _item_id(n.evidence), n.citations) for n in spec.nodes or []]
        groups += [(f"edge {e.source}--{e.target}", _item_id(e.evidence), e.citations)
                   for e in spec.edges or []]
    elif spec.type == "scatter":
        groups += [(f"point {d['nct_id']}", "points", [Citation(**c) for c in d["citations"]])
                   for d in spec.data]
    else:
        groups += [(f"row {i}", _item_id(EvidenceRef(**d["evidence"])),
                    [Citation(**c) for c in d.get("citations", [])])
                   for i, d in enumerate(spec.data)]
    v: list[str] = []
    for where, item_id, citations in groups:
        members = {c.nct_id for c in bundle.items.get(item_id, [])}
        for c in citations:
            if c.nct_id not in members:
                v.append(f"{where}: cites {c.nct_id}, which is not one of its contributors")
                continue
            record = trials[c.nct_id].record if c.nct_id in trials else {}
            for e in c.excerpts:
                if e.text is not None and str(resolve(record, e.field)) != e.text:
                    v.append(f"{where}: excerpt {e.field} of {c.nct_id} is not verbatim")
    return v


# --------------------------------------------------------------------------- shapes


def _check_rows(spec: VisualizationSpec, result: AnalysisResult) -> list[str]:
    v: list[str] = []
    enc = spec.encoding
    horizontal = spec.hints.orientation == "horizontal"
    category = enc.y if horizontal else enc.x
    if category is None or category.sort is None:
        return ["category channel must carry an explicit sort order"]
    series = enc.color.field if enc.color else None
    series_order = (enc.color.sort or []) if enc.color else [None]
    expected = [(s, c) for s in series_order for c in category.sort]
    actual = [(d.get(series) if series else None, d[category.field]) for d in spec.data]
    if actual != expected:
        v.append("data rows are not in (series order x category order), or series are not "
                 "aligned with explicit zeros")
    years = [int(y) for y in category.sort] if category.field == "start_year" else []
    if years and years != list(range(years[0], years[-1] + 1)):
        v.append("years must be contiguous and ascending")
    if list(category.sort) != list(result.category_order):
        v.append("encoding sort differs from the analysis category order")
    return v


def _check_network(spec: VisualizationSpec) -> list[str]:
    v: list[str] = []
    nodes = {n.id: n for n in spec.nodes or []}
    seen: set[frozenset[str]] = set()
    for e in spec.edges or []:
        if e.source not in nodes or e.target not in nodes:
            v.append(f"edge {e.source}--{e.target} references a missing node")
            continue
        if e.source == e.target:
            v.append(f"self-loop on {e.source}")
        pair = frozenset((e.source, e.target))
        if pair in seen:
            v.append(f"duplicate edge {e.source}--{e.target}")
        seen.add(pair)
        if e.weight > min(nodes[e.source].study_count, nodes[e.target].study_count):
            v.append(f"edge {e.source}--{e.target} outweighs one of its endpoints")
    return v


def _check_partition(response: QueryResponse, result: AnalysisResult,
                     cohorts: Mapping[str, CohortCheck]) -> list[str]:
    """Single-valued, untruncated breakdowns must account for every fetched study exactly once."""
    plan = response.plan
    if plan is None:
        return []
    a = plan.analysis
    if a.series_by not in (None, Dimension.cohort):
        return []
    if a.dimension is not None and REGISTRY[a.dimension].multi_valued:
        return []
    if any(w.startswith("Showing the top") for w in result.warnings):
        return []
    excluded = sum(result.excluded.values())
    per_series: dict[Any, int] = {}
    field = result.series_field
    for row in result.rows:
        key = row.values.get(field) if field else None
        per_series[key] = per_series.get(key, 0) + row.count
    v = []
    for key, total in per_series.items():
        cohort = cohorts.get(key) if key is not None else next(iter(cohorts.values()), None)
        if cohort is None:
            continue
        gap = len(cohort.trials) - total
        # One cohort: exact. Several: excluded counts are distinct across cohorts, so they
        # bound each cohort's gap rather than equal it.
        ok = gap == excluded if len(cohorts) == 1 else 0 <= gap <= excluded
        if not ok:
            v.append(f"series {key!r}: {total} counted + {excluded} excluded does not account "
                     f"for {len(cohort.trials)} fetched studies")
    return v


def _check_bounds(spec: VisualizationSpec) -> list[str]:
    v: list[str] = []
    if len(spec.data) > (MAX_POINTS if spec.type == "scatter" else MAX_ROWS):
        v.append(f"too many data rows ({len(spec.data)})")
    if len(spec.nodes or []) > MAX_NODES or len(spec.edges or []) > MAX_EDGES:
        v.append("network exceeds node/edge bounds")
    size = len(spec.model_dump_json())
    if size > MAX_PAYLOAD_BYTES:
        v.append(f"visualization payload is {size:,} bytes (> {MAX_PAYLOAD_BYTES:,})")
    return v
