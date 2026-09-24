"""``AnalysisResult`` -> ``VisualizationSpec``.

Chart type is a pure function of the analysis *shape* (never chosen by the LLM):

    category         -> bar            temporal         -> line
    category_series  -> grouped_bar    temporal_series  -> line (one per series)
    geo              -> choropleth_bar network          -> network
    binned numeric   -> histogram      scatter          -> scatter

The builder only reshapes: every count it emits is ``len(contributors)`` from the engine, and every
datum is registered with the evidence bundle so its ``EvidenceRef.total`` is the same number, and
carries inline deep citations for its sample studies.
"""

from __future__ import annotations

import math
from typing import Any

from app.analysis.engine import OTHER_KEY
from app.contracts.analysis import AnalysisResult, Row
from app.contracts.plan import Cohort, Dimension, Measure, QueryPlan
from app.contracts.viz import (
    Channel,
    ChartType,
    Encoding,
    FieldType,
    GeoHint,
    NetworkEdge,
    NetworkNode,
    RenderHints,
    VisualizationSpec,
)
from app.evidence.citations import Claim, DimClaim, MeasureClaim, YearClaim
from app.evidence.store import EvidenceBundle
from app.registry.fields import REGISTRY
from app.viz import theme
from app.viz.titles import MEASURE_TITLES, dimension_title, subtitle, title

COUNT = "study_count"
COUNT_TITLE = "Studies"
VEGA_SCHEMA = "https://vega.github.io/schema/vega-lite/v5.json"
LONG_LABEL = 14  # characters; longer category labels read better on horizontal bars


def build(plan: QueryPlan, result: AnalysisResult, bundle: EvidenceBundle) -> VisualizationSpec:
    if result.shape == "network":
        return _network(plan, result, bundle)
    if result.shape == "scatter":
        return _scatter(plan, result, bundle)
    return _rows_chart(plan, result, bundle)


# --------------------------------------------------------------------------- bars and lines


def _row_claims(plan: QueryPlan, row: Row) -> tuple[list[Claim], Cohort | None]:
    """What a row asserts about each contributing study, and which cohort it came from."""
    claims: list[Claim] = []
    cohort = plan.cohorts[0] if len(plan.cohorts) == 1 else None
    for field, key in row.keys.items():
        if field == "start_year":
            claims.append(YearClaim(int(key)))
        elif field == Dimension.cohort.value:
            cohort = next(c for c in plan.cohorts if c.label == key)
        elif key != OTHER_KEY:  # "Other" folds several values; each study cites its own below
            claims.append(DimClaim(Dimension(field), str(key)))
    return claims, cohort


def _row_data(plan: QueryPlan, result: AnalysisResult,
              bundle: EvidenceBundle) -> list[dict[str, Any]]:
    data = []
    for i, row in enumerate(result.rows):
        datum: dict[str, Any] = {**row.values, **row.extra, COUNT: row.count}
        ref, citations = bundle.register(f"r{i}", row.contributors, *_row_claims(plan, row))
        datum["evidence"] = ref.model_dump()
        datum["citations"] = [c.model_dump() for c in citations]
        data.append(datum)
    return data


def _category_type(result: AnalysisResult) -> FieldType:
    if result.x_field == "start_year":
        return "ordinal"
    fdef = REGISTRY.get(Dimension(result.x_field)) if result.x_field else None
    return "ordinal" if fdef is not None and fdef.order == "domain" else "nominal"


def _series_title(field: str) -> str:
    return dimension_title(Dimension(field))


def _rows_chart(plan: QueryPlan, result: AnalysisResult,
                bundle: EvidenceBundle) -> VisualizationSpec:
    assert result.x_field is not None
    temporal = result.shape in ("temporal", "temporal_series")
    histogram = not temporal and REGISTRY[Dimension(result.x_field)].histogram
    series = result.series_field
    chart: ChartType = ("line" if temporal else "histogram" if histogram
                        else "grouped_bar" if series
                        else "choropleth_bar" if result.shape == "geo" else "bar")

    ranked = plan.analysis.sort.by == "count_desc" or (
        not temporal and REGISTRY[Dimension(result.x_field)].order == "count")
    long_labels = any(len(str(c)) > LONG_LABEL for c in result.category_order)
    horizontal = not temporal and not histogram and (ranked or long_labels)

    category = Channel(field=result.x_field, type=_category_type(result),
                       title=result.x_label or result.x_field,
                       sort=list(result.category_order))
    count = Channel(field=COUNT, type="quantitative", title=COUNT_TITLE)
    color = (Channel(field=series, type="nominal", title=_series_title(series),
                     sort=list(result.series_order)) if series else None)
    x, y = (count, category) if horizontal else (category, count)
    tooltip = [result.x_field, *([series] if series else []), COUNT]
    encoding = Encoding(x=x, y=y, color=color, tooltip=tooltip)

    labels = list(result.series_order) if series else [plan.cohorts[0].label]
    hints = RenderHints(orientation=None if temporal else
                        "horizontal" if horizontal else "vertical",
                        legend=bool(series), value_labels=not temporal and not series)
    data = _row_data(plan, result, bundle)
    spec = VisualizationSpec(
        type=chart, title=title(plan), subtitle=subtitle(plan), encoding=encoding, hints=hints,
        palette=theme.series_palette(labels), data=data,
        geo=GeoHint(color_ramp=theme.SEQUENTIAL_BLUE) if chart == "choropleth_bar" else None,
    )
    spec.vega_lite = _vega_rows(spec, temporal, histogram)
    return spec


def _vl_channel(ch: Channel, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"field": ch.field, "type": ch.type, "title": ch.title}
    if ch.sort is not None:
        out["sort"] = ch.sort
    if ch.scale == "log":
        out["scale"] = {"type": "log"}
    return {**out, **(extra or {})}


def _vl_values(data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Chart values without evidence payloads; ``_row`` maps a clicked mark back to ``data``."""
    return [{**{k: v for k, v in d.items() if k not in ("evidence", "citations")}, "_row": i}
            for i, d in enumerate(data)]


def _tooltip(spec: VisualizationSpec) -> list[dict[str, Any]]:
    titles = {c.field: c.title for c in (spec.encoding.x, spec.encoding.y, spec.encoding.color)
              if c is not None}
    return [{"field": f, "title": titles.get(f, f),
             **({"format": ","} if f == COUNT else {})} for f in spec.encoding.tooltip]


def _vega_rows(spec: VisualizationSpec, temporal: bool, histogram: bool) -> dict[str, Any]:
    enc = spec.encoding
    assert enc.x is not None and enc.y is not None
    horizontal = spec.hints.orientation == "horizontal"
    cat_axis = "y" if horizontal else "x"
    color: dict[str, Any] | None = None
    if enc.color is not None:
        domain = list(spec.palette)
        color = _vl_channel(enc.color, {"scale": {"domain": domain,
                                                  "range": [spec.palette[d] for d in domain]}})
    vl_enc: dict[str, Any] = {
        "x": _vl_channel(enc.x, {"axis": {"format": ",d", "tickMinStep": 1}}
                         if enc.x.field == COUNT else
                         {"axis": {"labelAngle": 0}}),
        "y": _vl_channel(enc.y, {"axis": {"format": ",d", "tickMinStep": 1}}
                         if enc.y.field == COUNT else None),
        "tooltip": _tooltip(spec),
    }
    if color is not None:
        vl_enc["color"] = color

    if temporal:
        mark: dict[str, Any] = {"type": "line", "point": {
            "filled": True, "size": 64, "stroke": theme.SURFACE, "strokeWidth": 2}}
        if color is None:
            mark["color"] = theme.CATEGORICAL[0]
        layers: list[dict[str, Any]] = [{"mark": mark, "encoding": vl_enc}]
    else:
        bar: dict[str, Any] = {"type": "bar", "cornerRadiusEnd": 4}
        if color is None:
            bar["color"] = theme.CATEGORICAL[0]
            # histogram bins are contiguous, so bars fill the band (minus a thin surface gap)
            bar.update({"width": {"band": 0.94}} if histogram else {"size": theme.BAR_SIZE})
        else:
            vl_enc[f"{cat_axis}Offset"] = {"field": color["field"], "sort": color["sort"]}
        layers = [{"mark": bar, "encoding": vl_enc}]
        if spec.hints.value_labels:
            layers.append({
                "mark": {"type": "text", "align": "left" if horizontal else "center",
                         "baseline": "middle" if horizontal else "bottom",
                         "dx": 4 if horizontal else 0, "dy": 0 if horizontal else -4,
                         "color": theme.INK_SECONDARY, "fontSize": 11},
                "encoding": {k: v for k, v in vl_enc.items() if k in ("x", "y")}
                | {"text": {"field": COUNT, "type": "quantitative", "format": ","}},
            })
    size = ({"height": {"step": 26 if enc.color is None else 14}} if horizontal
            else {"height": 320})
    return {
        "$schema": VEGA_SCHEMA,
        "title": {"text": spec.title, "subtitle": spec.subtitle},
        "width": "container",
        **size,
        "data": {"values": _vl_values(spec.data)},
        "layer": layers,
        "config": theme.vega_config(),
    }


# --------------------------------------------------------------------------- network


def _network(plan: QueryPlan, result: AnalysisResult,
             bundle: EvidenceBundle) -> VisualizationSpec:
    cohort = plan.cohorts[0]

    def claim(node_id: str) -> DimClaim:
        dim, key = node_id.split(":", 1)
        return DimClaim(Dimension(dim), key)

    nodes: list[NetworkNode] = []
    for i, node in enumerate(result.nodes):
        ref, citations = bundle.register(f"n{i}", node.contributors, [claim(node.id)], cohort)
        nodes.append(NetworkNode(id=node.id, label=node.label, group=node.group,
                                 study_count=node.count, evidence=ref, citations=citations))
    edges: list[NetworkEdge] = []
    for i, e in enumerate(result.edges):
        ref, citations = bundle.register(f"e{i}", e.contributors,
                                         [claim(e.source), claim(e.target)], cohort)
        edges.append(NetworkEdge(source=e.source, target=e.target, weight=e.count,
                                 evidence=ref, citations=citations))
    group_sizes: dict[str, int] = {}
    for nn in nodes:
        group_sizes[nn.group] = group_sizes.get(nn.group, 0) + 1
    groups = sorted(group_sizes, key=lambda g: (-group_sizes[g], g))
    return VisualizationSpec(
        type="network", title=title(plan), subtitle=subtitle(plan),
        encoding=Encoding(
            size=Channel(field=COUNT, type="quantitative", title="Studies listing the node"),
            color=Channel(field="group", type="nominal", title="Group", sort=list(groups)),
            tooltip=["label", "group", COUNT],
        ),
        hints=RenderHints(layout="force", legend=len(groups) > 1),
        palette=theme.group_palette(groups), nodes=nodes, edges=edges,
    )


# --------------------------------------------------------------------------- scatter


def _numeric_axis(data: list[dict[str, Any]], field: str, scale: str | None) -> dict[str, Any]:
    """Recessive numeric axis; log axes get one gridline per power of ten only."""
    if scale != "log":
        return {"tickCount": 8, "format": ",~r"}
    values = [d[field] for d in data if d[field] > 0]
    if not values:
        return {}
    lo, hi = math.floor(math.log10(min(values))), math.ceil(math.log10(max(values)))
    return {"values": [10**e for e in range(lo, hi + 1)], "format": ",~r"}


def _scatter(plan: QueryPlan, result: AnalysisResult,
             bundle: EvidenceBundle) -> VisualizationSpec:
    assert result.x_measure and result.y_measure
    xm, ym = result.x_measure, result.y_measure
    contributors = {p.nct_id: p.evidence for p in result.points if p.evidence is not None}
    bundle.register("points", contributors, [MeasureClaim(Measure(xm)), MeasureClaim(Measure(ym))],
                    plan.cohorts[0])
    data = [{"nct_id": p.nct_id, xm: p.x, ym: p.y, **p.attrs,
             "url": f"https://clinicaltrials.gov/study/{p.nct_id}",
             # every point cites its plotted values; full citations via the evidence endpoint
             "citations": [bundle.cite("points", p.nct_id, membership=False).model_dump()]}
            for p in result.points]

    def channel(m: str) -> Channel:
        return Channel(field=m, type="quantitative", title=MEASURE_TITLES[Measure(m)],
                       scale="log" if m == "enrollment" else "linear")

    spec = VisualizationSpec(
        type="scatter", title=title(plan), subtitle=subtitle(plan),
        encoding=Encoding(x=channel(xm), y=channel(ym),
                          tooltip=["nct_id", xm, ym, "phase", "enrollment_type"]),
        palette=theme.series_palette([plan.cohorts[0].label]), data=data,
    )
    enc = spec.encoding
    assert enc.x is not None and enc.y is not None
    spec.vega_lite = {
        "$schema": VEGA_SCHEMA,
        "title": {"text": spec.title, "subtitle": spec.subtitle},
        "width": "container", "height": 360,
        "data": {"values": _vl_values(data)},
        "mark": {"type": "point", "filled": True, "size": 64, "opacity": 0.55,
                 "color": theme.CATEGORICAL[0], "stroke": theme.SURFACE, "strokeWidth": 1},
        "encoding": {
            "x": _vl_channel(enc.x, {"axis": _numeric_axis(data, xm, enc.x.scale)}),
            "y": _vl_channel(enc.y, {"axis": _numeric_axis(data, ym, enc.y.scale)}),
            "href": {"field": "url"},
            "tooltip": [{"field": "nct_id", "title": "Study"},
                        {"field": xm, "title": enc.x.title, "format": ",.1~f"},
                        {"field": ym, "title": enc.y.title, "format": ",.1~f"},
                        {"field": "phase", "title": "Phase"},
                        {"field": "enrollment_type", "title": "Enrollment type"}],
        },
        "config": theme.vega_config(),
    }
    return spec

