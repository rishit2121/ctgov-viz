"""Executes a validated ``QueryPlan`` analysis over normalized, cohort-tagged trials.

Three analysis kinds cover every supported question class:
  aggregate     category or year axis, optional series (cohort or dimension) -> bar/line
  cooccurrence  pairs of values within a study (or within an arm)           -> network
  numeric_pair  two numeric measures per study                               -> scatter
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

from app.contracts.analysis import AnalysisResult, Edge, Node, Point, Row
from app.contracts.plan import Analysis, Dimension, Measure, PairSpec, QueryPlan
from app.contracts.trial import Intervention, Trial
from app.normalize import trial as paths
from app.normalize.dates import months_between
from app.normalize.labels import INTERVENTION_TYPE_LABELS, label, phase_bucket
from app.registry.fields import REGISTRY, FieldDef

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
    else:
        series_keys = [""]

    for s_key in series_keys:
        for c_key in ordered:
            row = rows.get((s_key, c_key)) or Row(values={})  # explicit zero for aligned series
            row.values = {fdef.name.value: labels[c_key]}
            if series_field:
                row.values[series_field] = series_labels[s_key]
            if a.dimension == Dimension.country:
                row.extra["iso3"] = iso3.get(c_key)
            result.rows.append(row)

    result.category_order = [labels[c] for c in ordered]
    result.series_order = [series_labels[s] for s in series_keys] if series_field else []
    if series_field:
        result.shape = "category_series"
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
    for s_key in series_keys:
        for y in years:
            row = rows.get((s_key, y)) or Row(values={})  # zero-filled gap years
            row.values = {"start_year": y}
            if series_field:
                row.values[series_field] = series_labels[s_key]
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
    if top_k:  # restrict to the top-k most frequent nodes first
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
    if total_edges > len(kept):
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
        result.assumptions.append("Placebo, sham and standard-of-care interventions excluded.")
    if pair.drugs_only:
        result.assumptions.append("Only drug and biological interventions included.")
    return result


# --------------------------------------------------------------------------- scatter


def _measure(t: Trial, m: Measure) -> float | None:
    if m == Measure.enrollment:
        return float(t.enrollment) if t.enrollment else None
    d = months_between(t.start, t.primary_completion)
    return d if d is not None and d >= 0 else None


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
        }))
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
