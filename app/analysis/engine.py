"""Executes a validated ``QueryPlan`` analysis over normalized, cohort-tagged trials.

Three analysis kinds cover every supported question class:
  aggregate     category or year axis, optional series (cohort or dimension) -> bar/line
  cooccurrence  pairs of values within a study (or within an arm)           -> network
  numeric_pair  two numeric measures per study                               -> scatter
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

from app.contracts.analysis import AnalysisResult, Contributor, Edge, Node, Point, Row
from app.contracts.plan import Analysis, Dimension, Measure, PairSpec, QueryPlan
from app.contracts.trial import Intervention, Trial
from app.normalize import trial as paths
from app.normalize.labels import INTERVENTION_TYPE_LABELS, label, phase_bucket
from app.registry.fields import REGISTRY, FieldDef, duration_months

from .ops import (
    LabelResolver,
    arm_pairs,
    bipartite_pairs,
    contributor,
    dedupe_keys,
    order_categories,
    unordered_pairs,
    year_range,
)

CohortTrials = Mapping[str, Sequence[Trial]]
MAX_SCATTER_POINTS = 5000
MAX_SERIES = 8  # categorical color slots; the tail folds into "Other" rather than a 9th hue
OTHER_KEY, OTHER_LABEL = "\x00other", "Other"
COHORT_PATH = "cohort_query"  # evidence key recording which cohort query returned the study

DEF_UNIT = "Each bar/point/edge counts distinct studies (NCT IDs), never sites or arms."


def run(plan: QueryPlan, cohort_trials: CohortTrials, today: date | None = None) -> AnalysisResult:
    a = plan.analysis
    if a.kind == "aggregate":
        if a.time is not None:
            return _temporal(plan, cohort_trials, today or date.today())
        return _categorical(plan, cohort_trials)
    if a.kind == "cooccurrence":
        assert a.pair is not None
        (trials,) = cohort_trials.values()
        return _network(a.pair, a.top_k, trials)
    (trials,) = cohort_trials.values()
    return _scatter(a, trials)


# --------------------------------------------------------------------------- aggregate


def _fold_series[C](
    rows: dict[tuple[str, C], Row], series_keys: list[str], labels: LabelResolver,
    title: str, warnings: list[str],
) -> list[str]:
    """Keep the ``MAX_SERIES - 1`` largest series; merge the rest into one "Other" series.

    "Other" counts distinct studies across the folded series (a study in two folded series
    counts once), so it is a real distinct count, not a sum.
    """
    if len(series_keys) <= MAX_SERIES:
        return series_keys
    members: dict[str, set[str]] = {s: set() for s in series_keys}
    for (s, _), row in rows.items():
        members[s].update(row.contributors)
    ranked = sorted(series_keys, key=lambda s: (-len(members[s]), labels[s].casefold()))
    keep = set(ranked[: MAX_SERIES - 1])
    for (s, c), row in list(rows.items()):
        if s not in keep:
            rows.setdefault((OTHER_KEY, c), Row(values={})).contributors.update(row.contributors)
    labels.add(OTHER_KEY, OTHER_LABEL)
    folded = len(series_keys) - len(keep)
    warnings.append(f"The {folded} smallest {title.lower()} series are grouped into "
                    f"'{OTHER_LABEL}'.")
    return [s for s in series_keys if s in keep] + [OTHER_KEY]


def _series_values(
    a: Analysis, t: Trial, cohort_label: str, sdef: FieldDef | None
) -> list[tuple[str, str]]:
    if a.series_by == Dimension.cohort:
        return [(cohort_label, cohort_label)]
    if sdef is not None:
        return dedupe_keys(sdef.extractor(t))
    return [("", "")]


def _categorical(plan: QueryPlan, cohort_trials: CohortTrials) -> AnalysisResult:
    a = plan.analysis
    assert a.dimension is not None
    fdef = REGISTRY[a.dimension]
    sdef = REGISTRY.get(a.series_by) if a.series_by not in (None, Dimension.cohort) else None
    series_field = a.series_by.value if a.series_by else None
    ev_paths = fdef.source_paths + (sdef.source_paths if sdef else ())

    rows: dict[tuple[str, str], Row] = {}
    cat_labels, series_labels = LabelResolver(), LabelResolver()
    cat_members: dict[str, set[str]] = {}
    iso3: dict[str, str | None] = {}
    missing: set[str] = set()
    for cohort_label, trials in cohort_trials.items():
        for t in trials:
            cats = dedupe_keys(fdef.extractor(t))
            if not cats:
                missing.add(t.nct_id)
                continue
            for s_key, s_label in _series_values(a, t, cohort_label, sdef):
                series_labels.add(s_key, s_label)
                for c_key, c_label in cats:
                    cat_labels.add(c_key, c_label)
                    cat_members.setdefault(c_key, set()).add(t.nct_id)
                    row = rows.setdefault((s_key, c_key), Row(values={}))
                    row.contributors[t.nct_id] = contributor(
                        t, ev_paths, **{COHORT_PATH: cohort_label} if series_field == "cohort"
                        else {})
                    if a.dimension == Dimension.country:
                        iso3[c_key] = t.country_iso3.get(c_key)

    result = AnalysisResult(shape="category", x_field=fdef.name.value, x_label=fdef.title,
                            series_field=series_field)
    if missing:
        result.excluded[f"no_{fdef.name.value}_reported"] = len(missing)

    # Category order + top-k (ranked by distinct studies across all series).
    totals = {k: len(v) for k, v in cat_members.items()}
    labels = {k: cat_labels[k] for k in totals}
    how = a.sort.by if not (a.sort.by == "domain" and fdef.order == "count") else "count_desc"
    ordered = order_categories(totals, labels, how, fdef.domain_order)
    k = a.top_k or (fdef.default_top_k if fdef.order == "count" else None)
    if k and len(ordered) > k:
        if how != "count_desc":
            keep = set(order_categories(totals, labels, "count_desc")[:k])
            ordered = [c for c in ordered if c in keep]
        else:
            ordered = ordered[:k]
        result.warnings.append(
            f"Showing the top {k} of {len(totals)} {fdef.title.lower()} values by study count.")

    # Series order: cohorts in plan order; dimension series by registry/domain rules.
    if a.series_by == Dimension.cohort:
        series_keys = [c.label for c in plan.cohorts]
    elif sdef is not None:
        s_totals = {s: 0 for s, _ in rows}
        for (s, _), row in rows.items():
            s_totals[s] += row.count
        s_labels = {s: series_labels[s] for s in s_totals}
        s_how = "domain" if sdef.order == "domain" else "count_desc"
        series_keys = order_categories(s_totals, s_labels, s_how, sdef.domain_order)
        series_keys = _fold_series(rows, series_keys, series_labels, sdef.title, result.warnings)
    else:
        series_keys = [""]

    for s_key in series_keys:
        for c_key in ordered:
            row = rows.get((s_key, c_key)) or Row(values={})  # explicit zero for aligned series
            row.values = {fdef.name.value: labels[c_key]}
            row.keys = {fdef.name.value: c_key}
            if series_field:
                row.values[series_field] = series_labels[s_key]
                row.keys[series_field] = s_key
            if a.dimension == Dimension.country:
                row.extra["iso3"] = iso3.get(c_key)
            result.rows.append(row)

    result.category_order = [labels[c] for c in ordered]
    result.series_order = [series_labels[s] for s in series_keys] if series_field else []
    if series_field:
        result.shape = "category_series"
        result.totals, result.series_partition = _category_totals(
            rows, series_keys, ordered, fdef.name.value, labels, iso3)
        result.definitions["total"] = (
            "Total = distinct studies in the category across all series"
            + (". Each study is in exactly one series here, so the series add up to it."
               if result.series_partition else
               "; a study can be in several series, so the series do not add up to it."))
    elif a.dimension == Dimension.country:
        result.shape = "geo"
    result.definitions["unit"] = DEF_UNIT
    if fdef.description:
        result.definitions[fdef.name.value] = fdef.description
    if fdef.multi_valued:
        result.definitions["multi_valued"] = (
            f"A study listing several {fdef.title.lower()} values counts once in each, so "
            "counts can sum to more than the number of studies.")
    return result


def _category_totals(
    rows: dict[tuple[str, str], Row], series_keys: list[str], ordered: list[str], field: str,
    labels: dict[str, str], iso3: dict[str, str | None],
) -> tuple[list[Row], bool]:
    """Distinct-study total per category, and whether the series partition every category."""
    totals: list[Row] = []
    partition = True
    for c_key in ordered:
        members: dict[str, Contributor] = {}
        series_sum = 0
        for s_key in series_keys:
            row = rows.get((s_key, c_key))
            if row is not None:
                series_sum += row.count
                for nct, c in row.contributors.items():
                    members.setdefault(nct, c)
        partition = partition and series_sum == len(members)
        total = Row(values={field: labels[c_key]}, contributors=members, keys={field: c_key})
        if c_key in iso3:
            total.extra["iso3"] = iso3[c_key]
        totals.append(total)
    return totals, partition


def _temporal(plan: QueryPlan, cohort_trials: CohortTrials, today: date) -> AnalysisResult:
    a = plan.analysis
    assert a.time is not None
    sdef = REGISTRY.get(a.series_by) if a.series_by not in (None, Dimension.cohort) else None
    series_field = a.series_by.value if a.series_by else None
    ev_paths = (paths.P_START,) + (sdef.source_paths if sdef else ())

    rows: dict[tuple[str, int], Row] = {}
    series_labels = LabelResolver()
    series_totals: dict[str, int] = {}
    missing: set[str] = set()
    outside: set[str] = set()
    future: set[str] = set()
    lo, hi = a.time.from_year, a.time.to_year
    for cohort_label, trials in cohort_trials.items():
        for t in trials:
            year = t.start.year
            if year is None:
                missing.add(t.nct_id)
                continue
            if (lo is not None and year < lo) or (hi is not None and year > hi):
                outside.add(t.nct_id)
                continue
            if year > today.year:
                future.add(t.nct_id)
            for s_key, s_label in _series_values(a, t, cohort_label, sdef):
                series_labels.add(s_key, s_label)
                series_totals[s_key] = series_totals.get(s_key, 0) + 1
                row = rows.setdefault((s_key, year), Row(values={}))
                row.contributors[t.nct_id] = contributor(
                    t, ev_paths, **{COHORT_PATH: cohort_label} if series_field == "cohort" else {})

    years = year_range({y for _, y in rows}, lo, hi)
    if a.series_by == Dimension.cohort:
        series_keys = [c.label for c in plan.cohorts]
    elif sdef is not None:
        s_labels = {s: series_labels[s] for s in series_totals}
        series_keys = order_categories(series_totals, s_labels,
                                       "domain" if sdef.order == "domain" else "count_desc",
                                       sdef.domain_order)
    else:
        series_keys = [""]

    result = AnalysisResult(shape="temporal_series" if series_field else "temporal",
                            x_field="start_year", x_label="Start year", series_field=series_field)
    if sdef is not None:
        series_keys = _fold_series(rows, series_keys, series_labels, sdef.title, result.warnings)
    for s_key in series_keys:
        for y in years:
            row = rows.get((s_key, y)) or Row(values={})  # zero-filled gap years
            row.values = {"start_year": y}
            row.keys = {"start_year": y}
            if series_field:
                row.values[series_field] = series_labels[s_key]
                row.keys[series_field] = s_key
            result.rows.append(row)
    result.category_order = list(years)
    result.series_order = [series_labels[s] for s in series_keys] if series_field else []

    if missing:
        result.excluded["missing_start_date"] = len(missing)
    if outside:
        result.excluded["start_year_outside_range"] = len(outside)
    if future:
        result.warnings.append(
            f"{len(future)} studies have anticipated start dates after {today.year}.")
    if years and years[0] <= today.year <= years[-1]:
        result.warnings.append(f"{today.year} is still in progress; its count is partial.")
    result.definitions["unit"] = DEF_UNIT
    result.definitions["start_year"] = (
        "Year of the registered study start date (actual, or anticipated if not yet started). "
        "This is not the same as being active during that year.")
    return result


# --------------------------------------------------------------------------- network


def _eligible_interventions(t: Trial, pair: PairSpec) -> list[Intervention]:
    return [
        i for i in t.interventions
        if not (pair.exclude_placebo and i.is_placebo)
        and not (pair.exclude_ancillary and i.is_ancillary)
        and not (pair.drugs_only and i.type not in ("DRUG", "BIOLOGICAL"))
    ]


def _side_values(t: Trial, dim: Dimension, pair: PairSpec) -> list[tuple[str, str]]:
    if dim == Dimension.intervention:
        return [(i.key, i.label) for i in _eligible_interventions(t, pair)]
    return dedupe_keys(REGISTRY[dim].extractor(t))


def _network(pair: PairSpec, top_k: int | None, trials: Sequence[Trial]) -> AnalysisResult:
    same = pair.left == pair.right
    ldef, rdef = REGISTRY[pair.left], REGISTRY[pair.right]
    ev_paths = ldef.source_paths + (() if same else rdef.source_paths)
    if pair.scope == "arm":
        ev_paths += (paths.P_ARMS,)

    def node_id(dim: Dimension, key: str) -> str:
        return f"{dim.value}:{key}"

    nodes: dict[str, Node] = {}
    edges: dict[tuple[str, str], Edge] = {}
    node_labels = LabelResolver()
    node_groups: dict[str, str] = {}
    no_pairs = 0
    for t in trials:
        left = dedupe_keys(_side_values(t, pair.left, pair))
        right = left if same else dedupe_keys(_side_values(t, pair.right, pair))
        for dim, values in ((pair.left, left), (pair.right, right)):
            for key, lab in values:
                nid = node_id(dim, key)
                node_labels.add(nid, lab)
                nodes.setdefault(nid, Node(id=nid, label="", group=dim.value))
                nodes[nid].contributors[t.nct_id] = contributor(t, ev_paths)
                if dim == Dimension.intervention and same:
                    itype = next((i.type for i in t.interventions if i.key == key), None)
                    node_groups.setdefault(nid, label(INTERVENTION_TYPE_LABELS, itype))
        if same and pair.scope == "arm":
            pairs = arm_pairs(t.arm_intervention_keys, {k for k, _ in left})
        elif same:
            pairs = unordered_pairs(k for k, _ in left)
        else:
            pairs = bipartite_pairs((k for k, _ in left), (k for k, _ in right))
        if not pairs:
            no_pairs += 1
        for a_key, b_key in pairs:
            src, dst = node_id(pair.left, a_key), node_id(pair.right, b_key)
            edge = edges.setdefault((src, dst), Edge(source=src, target=dst))
            edge.contributors[t.nct_id] = contributor(t, ev_paths)

    result = AnalysisResult(shape="network")
    total_edges = len(edges)
    # "Top N pairs" is max_edges over *all* pairs. top_k is a separate, explicit node cap
    # ("among the 20 most common drugs"): only edges between those nodes are then eligible.
    node_capped = bool(top_k) and len(nodes) > (top_k or 0)
    if node_capped:
        keep = set(sorted(nodes, key=lambda n: (-nodes[n].count, n))[:top_k])
        edges = {k: e for k, e in edges.items() if k[0] in keep and k[1] in keep}
    ranked = sorted(edges.values(), key=lambda e: (-e.count, e.source, e.target))
    kept = ranked[: pair.max_edges]
    used = {e.source for e in kept} | {e.target for e in kept}
    for nid in sorted(used, key=lambda n: (-nodes[n].count, n)):
        node = nodes[nid]
        node.label = node_labels[nid]
        node.group = node_groups.get(nid, node.group)
        result.nodes.append(node)
    result.edges = kept
    if node_capped:
        result.warnings.append(
            f"Only connections among the {top_k} most frequent of {len(nodes)} nodes are "
            f"considered; showing the {len(kept)} strongest of those {len(ranked)} "
            f"({total_edges} connections in total).")
    elif total_edges > len(kept):
        result.warnings.append(
            f"Showing the {len(kept)} strongest of {total_edges} connections.")
    if no_pairs:
        result.excluded["studies_without_a_pair"] = no_pairs
    result.definitions["unit"] = DEF_UNIT
    result.definitions["edge_weight"] = (
        "Distinct studies in which both endpoints appear together in the same arm group "
        "(given together)." if pair.scope == "arm" else
        "Distinct studies listing both endpoints. Being listed in the same study does not "
        "imply they were given together (they may be in different arms).")
    result.definitions["node_size"] = "Distinct studies listing the node's value."
    if pair.exclude_placebo:
        result.assumptions.append(
            "Placebo, sham, usual-care and observation comparators are excluded.")
    if pair.exclude_ancillary:
        result.assumptions.append(
            "Assessment and data-collection entries registered as interventions (imaging, "
            "biospecimen collection, questionnaires, ...) are excluded.")
    if pair.drugs_only:
        result.assumptions.append("Only drug and biological interventions included.")
    return result


# --------------------------------------------------------------------------- scatter


def _measure(t: Trial, m: Measure) -> float | None:
    if m == Measure.enrollment:
        return float(t.enrollment) if t.enrollment else None
    return duration_months(t)


def _scatter(a: Analysis, trials: Sequence[Trial]) -> AnalysisResult:
    assert a.x_measure and a.y_measure
    result = AnalysisResult(shape="scatter", x_measure=a.x_measure.value,
                            y_measure=a.y_measure.value)
    missing = 0
    for t in sorted(trials, key=lambda t: t.nct_id):
        x, y = _measure(t, a.x_measure), _measure(t, a.y_measure)
        if x is None or y is None:
            missing += 1
            continue
        result.points.append(Point(nct_id=t.nct_id, x=x, y=y, attrs={
            "enrollment_type": t.enrollment_type or "UNKNOWN",
            "phase": phase_bucket(t.phases),
        }, evidence=contributor(t, (paths.P_START, paths.P_PCD, paths.P_ENROLLMENT))))
    if missing:
        result.excluded["missing_or_invalid_measure"] = missing
    if len(result.points) > MAX_SCATTER_POINTS:
        step = len(result.points) / MAX_SCATTER_POINTS
        result.warnings.append(f"Showing an evenly spaced sample of {MAX_SCATTER_POINTS} of "
                               f"{len(result.points)} studies.")
        result.points = [result.points[int(i * step)] for i in range(MAX_SCATTER_POINTS)]
    result.definitions["unit"] = "Each point is one study."
    result.definitions["enrollment"] = (
        "Registered enrollment (actual if completed, otherwise anticipated; see enrollment_type).")
    result.definitions["duration_months"] = (
        "Months from study start to primary completion (actual or anticipated); requires "
        "month-level precision on both dates.")
    return result
