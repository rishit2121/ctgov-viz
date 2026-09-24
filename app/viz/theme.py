"""Color and mark conventions shared by every emitted chart.

The categorical palette is a fixed, CVD-validated order (adjacent-pair ΔE ≥ 8 in OKLab ×100);
colors are assigned to entities in that order and never cycled — the engine folds a 9th series
into "Other". Forms where any two marks can touch (scatter, network, map) use at most the first
three slots, which validate across *all* pairs.
"""

from __future__ import annotations

from typing import Any

CATEGORICAL = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7",
               "#e34948"]
ALL_PAIRS_SLOTS = 3
MUTED = "#898781"  # folded / "Other" groups
SEQUENTIAL_BLUE = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]

SURFACE = "#fcfcfb"
INK_PRIMARY = "#0b0b0b"
INK_SECONDARY = "#52514e"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
FONT = "system-ui, -apple-system, 'Segoe UI', sans-serif"

BAR_SIZE = 20  # px; bars stay thin (≤ 24) rather than filling the band


def series_palette(labels: list[str]) -> dict[str, str]:
    return {lab: CATEGORICAL[i] for i, lab in enumerate(labels[: len(CATEGORICAL)])}


def group_palette(groups_by_size: list[str]) -> dict[str, str]:
    """Node groups: the three largest get validated slots, the rest share the muted gray."""
    return {g: CATEGORICAL[i] if i < ALL_PAIRS_SLOTS else MUTED
            for i, g in enumerate(groups_by_size)}


def vega_config() -> dict[str, Any]:
    return {
        "background": SURFACE,
        "font": FONT,
        "view": {"stroke": None},
        "title": {"color": INK_PRIMARY, "subtitleColor": INK_SECONDARY, "anchor": "start",
                  "fontSize": 15, "subtitleFontSize": 12},
        "axis": {"gridColor": GRID, "gridWidth": 1, "domainColor": AXIS, "tickColor": AXIS,
                 "labelColor": INK_SECONDARY, "titleColor": INK_SECONDARY, "labelFontSize": 11,
                 "titleFontWeight": "normal", "labelLimit": 220},
        "legend": {"labelColor": INK_SECONDARY, "titleColor": INK_SECONDARY, "orient": "top",
                   "labelLimit": 320},
        "range": {"category": CATEGORICAL},
        "line": {"strokeWidth": 2, "strokeCap": "round", "strokeJoin": "round"},
    }
