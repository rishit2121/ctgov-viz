"""Versioned, frontend-facing visualization contract (schema_version 1.0).

Every chart ships already-aggregated, already-ordered data. Frontends never recompute anything:
they map ``encoding`` fields onto marks (in the given ``sort`` order, with the given ``palette``)
or render the embedded Vega-Lite spec directly.

Encodings describe the chart *as it should be drawn*: a horizontal bar chart has the category on
``y`` and the count on ``x``, exactly as in the embedded Vega-Lite spec.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

ChartType = Literal["bar", "grouped_bar", "line", "choropleth_bar", "network", "scatter"]
FieldType = Literal["nominal", "ordinal", "quantitative", "temporal"]


class EvidenceRef(BaseModel):
    total: int = Field(description="Number of distinct contributing studies (== the count shown).")
    sample: list[str] = Field(description="Up to N contributing NCT IDs, sorted.")
    complete_inline: bool = Field(
        description="True when `sample` already lists every contributor (no need to follow ref)."
    )
    ref: str = Field(description="Relative URL returning the complete, paginated contributor list.")


class Channel(BaseModel):
    field: str
    type: FieldType
    title: str
    sort: list[str | int] | None = Field(
        None, description="Explicit category order; render in exactly this order."
    )
    scale: Literal["linear", "log"] | None = None


class Encoding(BaseModel):
    x: Channel | None = None
    y: Channel | None = None
    color: Channel | None = None
    size: Channel | None = None
    tooltip: list[str] = Field(default_factory=list, description="Row fields to show on hover.")


class RenderHints(BaseModel):
    orientation: Literal["vertical", "horizontal"] | None = None
    layout: Literal["force"] | None = Field(None, description="Networks: suggested layout.")
    legend: bool = Field(False, description="Show a legend (true whenever there are 2+ series).")
    value_labels: bool = Field(False, description="Label each bar's value at its tip.")


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
    color_ramp: list[str] = Field(
        description="Sequential single-hue ramp (light -> dark) for a choropleth of value_field."
    )


class VisualizationSpec(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    type: ChartType
    title: str
    subtitle: str
    encoding: Encoding = Field(default_factory=Encoding)
    hints: RenderHints = Field(default_factory=RenderHints)
    palette: dict[str, str] = Field(
        default_factory=dict,
        description="Series / node-group label -> color. Colors follow the entity, not its rank.",
    )
    data: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Rows (bar/line/scatter). Aggregated rows carry study_count + evidence; "
        "scatter rows are one study each.",
    )
    nodes: list[NetworkNode] | None = None
    edges: list[NetworkEdge] | None = None
    geo: GeoHint | None = None
    vega_lite: dict[str, Any] | None = Field(
        None, description="Optional ready-to-render Vega-Lite v5 spec of the same data."
    )
