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

ChartType = Literal["bar", "grouped_bar", "stacked_bar", "histogram", "line", "choropleth_bar",
                    "network", "scatter"]
FieldType = Literal["nominal", "ordinal", "quantitative", "temporal"]


class EvidenceRef(BaseModel):
    total: int = Field(description="Number of distinct contributing studies (== the count shown).")
    sample: list[str] = Field(description="Up to N contributing NCT IDs, sorted.")
    complete_inline: bool = Field(
        description="True when `sample` already lists every contributor (no need to follow ref)."
    )
    ref: str = Field(description="Relative URL returning the complete, paginated contributor list.")


class Excerpt(BaseModel):
    field: str = Field(description="Exact path in the API study record, list index included, "
                       "e.g. 'protocolSection.designModule.phases[0]'.")
    text: str | None = Field(description="The value at that path, verbatim. null when the claim "
                             "rests on the value being absent, or on search expansion.")
    supports: str = Field(description="What this excerpt supports, e.g. 'phase: Phase 3'.")


class Citation(BaseModel):
    nct_id: str
    title: str | None = Field(description="The study's brief title, verbatim.")
    url: str = Field(description="Study page on ClinicalTrials.gov.")
    record_url: str = Field(description="The study's API record; every excerpt resolves in it.")
    excerpt: str | None = Field(description="The primary supporting text (first excerpt).")
    excerpts: list[Excerpt]


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
    stacked: bool = Field(False, description="Series are stacked: bar length is the category "
                          "total (only when every study is in exactly one series).")
    show_totals: bool = Field(False, description="Draw each category's distinct total (from "
                              "`totals`): at the stack's end, or as a marker beside grouped bars.")


class NetworkNode(BaseModel):
    id: str
    label: str
    group: str
    study_count: int
    evidence: EvidenceRef
    citations: list[Citation] = Field(default_factory=list)


class NetworkEdge(BaseModel):
    source: str
    target: str
    weight: int = Field(description="Distinct studies containing both endpoints.")
    evidence: EvidenceRef
    citations: list[Citation] = Field(default_factory=list)


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
        description="Rows (bar/histogram/line/scatter). Aggregated rows carry study_count, "
        "evidence and citations (for the evidence sample); scatter rows are one study each.",
    )
    totals: list[dict[str, Any]] | None = Field(
        None, description="Series breakdowns only: one row per category (same order as the "
        "category sort) with the distinct studies across all series, plus evidence and "
        "citations. Use this for totals and rankings; never sum series rows yourself, since "
        "series can overlap (see hints.stacked).")
    nodes: list[NetworkNode] | None = None
    edges: list[NetworkEdge] | None = None
    geo: GeoHint | None = None
    vega_lite: dict[str, Any] | None = Field(
        None, description="Optional ready-to-render Vega-Lite v5 spec of the same data."
    )
