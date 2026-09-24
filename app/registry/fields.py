"""Field registry: the single source of truth for every analyzable dimension.

The engine, plan validator, visualization builder and LLM prompt all read from here. Adding a
new dimension means adding one ``FieldDef`` (plus a test) — no changes to the engine.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from app.contracts.plan import Dimension
from app.contracts.trial import Trial
from app.normalize import trial as paths
from app.normalize.labels import (
    INTERVENTION_TYPE_LABELS,
    PHASE_ORDER,
    SPONSOR_CLASS_LABELS,
    SPONSOR_CLASS_ORDER,
    STATUS_LABELS,
    STATUS_ORDER,
    STUDY_TYPE_LABELS,
    STUDY_TYPE_ORDER,
    label,
    phase_bucket,
)

Op = Literal["group", "series", "pair"]
Order = Literal["domain", "count"]
# (grouping key, display label) pairs. Keys are deduplicated per study by the engine.
Extractor = Callable[[Trial], list[tuple[str, str]]]


@dataclass(frozen=True)
class FieldDef:
    name: Dimension
    title: str  # axis / legend title
    ops: frozenset[Op]
    extractor: Extractor
    source_paths: tuple[str, ...]  # protocolSection paths cited as evidence
    order: Order
    domain_order: tuple[str, ...] = ()
    default_top_k: int | None = None
    multi_valued: bool = False  # a study can contribute to several categories
    description: str = ""


def _one(value: str) -> list[tuple[str, str]]:
    return [(value, value)]


def _interventions(t: Trial) -> list[tuple[str, str]]:
    return [(i.key, i.label) for i in t.interventions]


REGISTRY: dict[Dimension, FieldDef] = {
    f.name: f
    for f in [
        FieldDef(
            Dimension.phase, "Phase", frozenset({"group", "series"}),
            lambda t: _one(phase_bucket(t.phases)), (paths.P_PHASES,), "domain",
            tuple(PHASE_ORDER),
            description="One bucket per study; multi-phase studies form combined buckets "
                        "such as 'Phase 2/3'. 'Not Applicable' is registered for studies "
                        "without drug phases (e.g. device, behavioral); 'Not Reported' means no "
                        "phase is registered (typical for observational studies).",
        ),
        FieldDef(
            Dimension.overall_status, "Overall status", frozenset({"group", "series"}),
            lambda t: _one(label(STATUS_LABELS, t.overall_status)), (paths.P_STATUS,), "domain",
            tuple(STATUS_ORDER),
        ),
        FieldDef(
            Dimension.study_type, "Study type", frozenset({"group", "series"}),
            lambda t: _one(label(STUDY_TYPE_LABELS, t.study_type)), (paths.P_STUDY_TYPE,),
            "domain", tuple(STUDY_TYPE_ORDER),
        ),
        FieldDef(
            Dimension.sponsor_class, "Lead sponsor class", frozenset({"group", "series"}),
            lambda t: _one(label(SPONSOR_CLASS_LABELS, t.sponsor_class)), (paths.P_SPONSOR,),
            "domain", tuple(SPONSOR_CLASS_ORDER),
        ),
        FieldDef(
            Dimension.sponsor, "Lead sponsor", frozenset({"group", "pair"}),
            lambda t: _one(t.sponsor_name) if t.sponsor_name else [], (paths.P_SPONSOR,),
            "count", default_top_k=20,
        ),
        FieldDef(
            Dimension.intervention, "Intervention", frozenset({"group", "pair"}),
            _interventions, (paths.P_INTERVENTIONS,), "count", default_top_k=20,
            multi_valued=True,
        ),
        FieldDef(
            Dimension.intervention_type, "Intervention type", frozenset({"group", "series"}),
            lambda t: [(x, x) for x in sorted({label(INTERVENTION_TYPE_LABELS, i.type)
                                               for i in t.interventions})],
            (paths.P_INTERVENTIONS,), "count", multi_valued=True,
        ),
        FieldDef(
            Dimension.condition, "Condition", frozenset({"group", "pair"}),
            lambda t: [(c.casefold(), c) for c in t.conditions], (paths.P_CONDITIONS,), "count",
            default_top_k=20, multi_valued=True,
        ),
        FieldDef(
            Dimension.country, "Country", frozenset({"group"}),
            lambda t: [(c, c) for c in t.countries], (paths.P_COUNTRIES,), "count",
            default_top_k=20, multi_valued=True,
            description="Distinct site countries per study; a multinational study counts once "
                        "in each of its countries.",
        ),
    ]
}


def get_field(dim: Dimension) -> FieldDef:
    return REGISTRY[dim]


def allows(dim: Dimension, op: Op) -> bool:
    return dim in REGISTRY and op in REGISTRY[dim].ops
