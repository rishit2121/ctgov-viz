"""Versioned, frontend-facing visualization contract (schema_version 1.0).

Every chart ships already-aggregated, already-ordered data. Frontends never recompute anything:
they map ``encoding`` fields onto marks and may optionally render the embedded Vega-Lite spec.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

ChartType = Literal["bar", "grouped_bar", "line", "choropleth_bar", "network", "scatter"]
FieldType = Literal["nominal", "ordinal", "quantitative", "temporal"]


class EvidenceRef(BaseModel):
    total: int = Field(description="Number of distinct contributing studies (== the count shown).")
    sample: list[str] = Field(description="Up to N contributing NCT IDs, sorted.")
    ref: str = Field(description="Relative URL returning the complete, paginated contributor list.")


class Channel(BaseModel):
    field: str
    type: FieldType
    title: str
    sort: list[str | int] | None = Field(
        None, description="Explicit category order; render in exactly this order."
    )


class Encoding(BaseModel):
    x: Channel | None = None
    y: Channel | None = None
    color: Channel | None = None
    size: Channel | None = None


class NetworkNode(BaseModel):
    id: str
    label: str
    group: str
    study_count: int
    evidence: EvidenceRef


class NetworkEdge(BaseModel):
    source: str
    target: str
    weight: int = Field(description="Distinct studies containing both endpoints.")
    evidence: EvidenceRef


class GeoHint(BaseModel):
    iso3_field: str = "iso3"
    name_field: str = "country"
    value_field: str = "study_count"


class VisualizationSpec(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    type: ChartType
    title: str
    subtitle: str
    encoding: Encoding = Field(default_factory=Encoding)
    data: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Rows (bar/line/scatter). Each aggregated row has study_count + evidence.",
    )
    nodes: list[NetworkNode] | None = None
    edges: list[NetworkEdge] | None = None
    geo: GeoHint | None = None
    vega_lite: dict[str, Any] | None = Field(
        None, description="Optional ready-to-render Vega-Lite v5 spec of the same data."
    )
