"""Engine output: aggregated rows / graph / points, each carrying its contributing studies.

Counts are never stored: ``count`` is always ``len(contributors)``, so a row's number and its
evidence cannot drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Shape = Literal["category", "category_series", "temporal", "temporal_series", "geo", "network",
                "scatter"]


@dataclass(slots=True)
class Contributor:
    nct_id: str
    fields: dict[str, Any]  # API field path -> source value that justified membership


@dataclass(slots=True)
class Row:
    values: dict[str, str | int]  # e.g. {"phase": "Phase 2"} or {"cohort": "A", "start_year": 2019}
    contributors: dict[str, Contributor] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)  # non-key attributes, e.g. iso3

    @property
    def count(self) -> int:
        return len(self.contributors)


@dataclass(slots=True)
class Node:
    id: str
    label: str
    group: str  # dimension name; differs across the two sides of a bipartite network
    contributors: dict[str, Contributor] = field(default_factory=dict)

    @property
    def count(self) -> int:
        return len(self.contributors)


@dataclass(slots=True)
class Edge:
    source: str
    target: str
    contributors: dict[str, Contributor] = field(default_factory=dict)

    @property
    def count(self) -> int:
        return len(self.contributors)


@dataclass(slots=True)
class Point:
    nct_id: str
    x: float
    y: float
    attrs: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AnalysisResult:
    shape: Shape
    x_field: str | None = None
    x_label: str | None = None
    series_field: str | None = None
    rows: list[Row] = field(default_factory=list)
    category_order: list[str | int] = field(default_factory=list)
    series_order: list[str] = field(default_factory=list)
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    points: list[Point] = field(default_factory=list)
    x_measure: str | None = None
    y_measure: str | None = None
    excluded: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    definitions: dict[str, str] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not (self.rows and any(r.count for r in self.rows)) and not self.edges \
            and not self.nodes and not self.points
