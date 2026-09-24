"""Orchestration: validated plan -> complete retrieval -> analysis -> verified response.

    validate -> lint -> compile -> preflight counts -> fetch (all pages) -> normalize
    -> re-verify filters locally -> engine -> viz builder -> evidence -> verifier -> cache

Every stage is a separate, tested module; this file only sequences them and turns failures into
explicit statuses/errors. Nothing here computes a chart value.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, TypeVar

from app.analysis import engine
from app.contracts.analysis import AnalysisResult
from app.contracts.plan import CohortFilters, OverallStatus, Phase, QueryPlan, StudyType
from app.contracts.response import (
    ApiQuery,
    Clarification,
    ClarificationOption,
    Completeness,
    LLMInfo,
    Meta,
    QueryResponse,
)
from app.contracts.trial import Trial
from app.ctgov.client import CTGovClient, FetchResult
from app.ctgov.compiler import CTGovQuery, compile_cohort, fields_for, missing_start_params
from app.ctgov.errors import CTGovError, CTGovTimeout
from app.evidence.store import EvidenceBundle, ResultCache
from app.normalize.trial import NormalizationError, normalize_study
from app.registry.linter import disclosures
from app.registry.validator import validate_plan
from app.settings import Settings
from app.verify.checks import CohortCheck, VerificationError, ensure_verified
from app.viz.builder import build

log = logging.getLogger(__name__)
T = TypeVar("T")


class PipelineError(Exception):
    """A failure the API maps to an HTTP error with a stable machine-readable ``code``."""

    def __init__(self, code: str, message: str, http_status: int, detail: Any = None):
        super().__init__(message)
        self.code, self.message, self.http_status, self.detail = code, message, http_status, detail


@dataclass
class CohortRun:
    label: str
    query: CTGovQuery
    total: int = 0
    fetch: FetchResult | None = None
    trials: list[Trial] = field(default_factory=list)
    invalid_records: int = 0
    failed_local_filter: int = 0
    missing_start: int = 0


def query_id(plan: QueryPlan, data_timestamp: str | None) -> str:
    """Deterministic: same plan against the same CT.gov data snapshot -> same id."""
    canonical = json.dumps(plan.model_dump(mode="json", exclude={"assumptions"}),
                           sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(f"{canonical}|{data_timestamp}".encode()).hexdigest()
    return f"q_{digest[:16]}"


class Pipeline:
    def __init__(self, client: CTGovClient, settings: Settings, cache: ResultCache):
        self.client = client
        self.settings = settings
        self.cache = cache
        self._sem = asyncio.Semaphore(settings.ctgov_concurrency)

    # ------------------------------------------------------------------ public

    async def run_plan(self, plan: QueryPlan, *, question: str | None = None,
                       llm: LLMInfo | None = None) -> QueryResponse:
        if errors := validate_plan(plan):
            raise PipelineError("plan_invalid", "The query plan is not valid.", 422, errors)
        timings: dict[str, int] = {}
        clock = time.perf_counter()

        version = await self._upstream(self.client.version())
        qid = query_id(plan, version.get("dataTimestamp"))
        if cached := self.cache.get(qid):
            response = QueryResponse.model_validate(cached[0])
            response.query = question or response.query
            return response

        fields = fields_for(plan)
        runs = [CohortRun(c.label, compile_cohort(c, plan.analysis.time, fields))
                for c in plan.cohorts]
        try:
            async with asyncio.timeout(self.settings.request_deadline_s):
                totals = await self._gather([self.client.count(r.query.params) for r in runs])
                for run, total in zip(runs, totals, strict=True):
                    run.total = total
                timings["preflight"] = _ms(clock)

                if too_broad := [r for r in runs if r.total > self.settings.max_studies_per_cohort]:
                    return await self._too_broad(plan, qid, question, llm, too_broad, version)
                if all(r.total == 0 for r in runs):
                    return self._no_matches(plan, qid, question, llm, runs, version)

                fetches = await self._gather(
                    [self.client.fetch_all(r.query.params, self.settings.max_studies_per_cohort)
                     for r in runs])
                for run, fetched in zip(runs, fetches, strict=True):
                    run.fetch = fetched
                if plan.analysis.time is not None:
                    missing = await self._gather(
                        [self.client.count(missing_start_params(c)) for c in plan.cohorts])
                    for run, n in zip(runs, missing, strict=True):
                        run.missing_start = n
                timings["fetch"] = _ms(clock) - timings["preflight"]
                version_after = await self._upstream(self.client.version())
        except TimeoutError as e:
            raise PipelineError(
                "upstream_timeout",
                f"ClinicalTrials.gov retrieval exceeded {self.settings.request_deadline_s:.0f}s.",
                504) from e

        t0 = time.perf_counter()
        for run in runs:
            self._normalize(run)
        cohort_trials = {r.label: r.trials for r in runs}
        result = engine.run(plan, cohort_trials, today=date.today())
        timings["analyze"] = _ms(t0)

        response, bundle = self._respond(plan, qid, question, llm, runs, result, version,
                                         version_after)
        t1 = time.perf_counter()
        checks = {r.label: CohortCheck(r.trials, r.query.predicate) for r in runs}
        try:
            ensure_verified(response, result, bundle, checks)
        except VerificationError as e:
            log.error("verification failed for %s: %s", qid, e.violations)
            raise PipelineError("verification_failed",
                                "The computed result failed internal consistency checks and "
                                "was not returned.", 500, e.violations) from e
        timings["verify"] = _ms(t1)
        timings["total"] = _ms(clock)
        response.meta.timings_ms = timings
        self.cache.put(qid, response.model_dump(mode="json"), bundle)
        return response

    def evidence(self, qid: str) -> EvidenceBundle | None:
        entry = self.cache.get(qid)
        return entry[1] if entry else None

    # ------------------------------------------------------------------ stages

    async def _upstream(self, call: Awaitable[T]) -> T:
        try:
            return await call
        except CTGovTimeout as e:
            raise PipelineError("upstream_timeout", str(e), 504) from e
        except CTGovError as e:
            if e.status_code is not None and 400 <= e.status_code < 500 and e.status_code != 429:
                raise PipelineError("upstream_rejected", str(e), 502) from e
            raise PipelineError("upstream_unavailable",
                                f"ClinicalTrials.gov is unavailable: {e}", 502) from e

    async def _gather(self, calls: Sequence[Awaitable[T]]) -> list[T]:
        async def limited(call: Awaitable[T]) -> T:
            async with self._sem:
                return await self._upstream(call)
        return list(await asyncio.gather(*(limited(c) for c in calls)))

    def _normalize(self, run: CohortRun) -> None:
        assert run.fetch is not None
        for raw in run.fetch.studies:
            try:
                t = normalize_study(raw)
            except NormalizationError:
                run.invalid_records += 1
                continue
            if run.query.predicate(t):
                run.trials.append(t)
            else:
                run.failed_local_filter += 1

    # ------------------------------------------------------------------ responses

    def _meta(self, plan: QueryPlan, runs: Sequence[CohortRun], llm: LLMInfo | None,
              version: dict[str, str]) -> Meta:
        assumptions, definitions = disclosures(plan)
        base = self.client.base_url
        queries = [ApiQuery(
            cohort=r.label, url=r.query.url(base), total=r.total,
            fetched=len(r.fetch.studies) if r.fetch else 0, pages=r.fetch.pages if r.fetch else 0,
            complete=r.fetch.complete if r.fetch else r.total == 0) for r in runs]
        filters = {c.label: {k: v for k, v in {
            **c.model_dump(mode="json", include={"condition", "intervention", "term", "sponsor"}),
            **c.filters.model_dump(mode="json")}.items() if v not in (None, [])}
            for c in plan.cohorts}
        return Meta(
            filters=filters,
            api_version=version.get("apiVersion"), data_timestamp=version.get("dataTimestamp"),
            retrieved_at=datetime.now(UTC).isoformat(timespec="seconds"),
            api_queries=queries,
            completeness=Completeness(complete=all(q.complete for q in queries)),
            assumptions=[*plan.assumptions, *assumptions], definitions=definitions, llm=llm,
        )

    def _respond(self, plan: QueryPlan, qid: str, question: str | None, llm: LLMInfo | None,
                 runs: Sequence[CohortRun], result: AnalysisResult, version: dict[str, str],
                 version_after: dict[str, str]) -> tuple[QueryResponse, EvidenceBundle]:
        meta = self._meta(plan, runs, llm, version)
        reasons = [f"{r.label}: {r.fetch.reason}" for r in runs if r.fetch and r.fetch.reason]
        if reasons:
            meta.completeness.reason = "; ".join(reasons)
        meta.studies_analyzed = len({t.nct_id for r in runs for t in r.trials})
        meta.excluded = dict(result.excluded)
        for key, attr in (("invalid_record", "invalid_records"),
                          ("failed_local_filter", "failed_local_filter"),
                          ("missing_start_date_not_retrieved", "missing_start")):
            if n := sum(getattr(r, attr) for r in runs):
                meta.excluded[key] = n
        if len(runs) > 1:
            ids = {r.label: {t.nct_id for t in r.trials} for r in runs}
            meta.cohort_overlap = {f"{a} ∩ {b}": len(ids[a] & ids[b])
                                   for i, a in enumerate(ids) for b in list(ids)[i + 1:]}
        meta.assumptions += [x for x in result.assumptions if x not in meta.assumptions]
        meta.definitions.update(result.definitions)
        meta.warnings = list(result.warnings)
        if version_after.get("dataTimestamp") != version.get("dataTimestamp"):
            meta.warnings.append("ClinicalTrials.gov refreshed its data during retrieval; "
                                 "pages may mix two snapshots.")

        trials = {t.nct_id: t for r in runs for t in r.trials}
        bundle = EvidenceBundle(qid, trials, self.settings.evidence_sample_size)
        if result.is_empty:
            return QueryResponse(
                status="empty", query_id=qid, query=question, plan=plan, meta=meta,
                message="Studies matched the search, but none had a value for the requested "
                        "breakdown (see meta.excluded)."), bundle
        spec = build(plan, result, bundle)
        status = "ok" if meta.completeness.complete else "partial"
        return QueryResponse(status=status, query_id=qid, query=question, plan=plan,
                             visualization=spec, meta=meta), bundle

    def _no_matches(self, plan: QueryPlan, qid: str, question: str | None, llm: LLMInfo | None,
                    runs: Sequence[CohortRun], version: dict[str, str]) -> QueryResponse:
        return QueryResponse(
            status="empty", query_id=qid, query=question, plan=plan,
            meta=self._meta(plan, runs, llm, version),
            message="No ClinicalTrials.gov studies match this query. Check the spelling of "
                    "search terms or remove a filter (the exact API queries are in "
                    "meta.api_queries).")

    async def _too_broad(self, plan: QueryPlan, qid: str, question: str | None,
                         llm: LLMInfo | None, too_broad: Sequence[CohortRun],
                         version: dict[str, str]) -> QueryResponse:
        """Refuse rather than analyze a biased first-N sample; offer counted narrowings."""
        cap = self.settings.max_studies_per_cohort
        options: list[ClarificationOption] = []
        for label, description, narrowed in _narrowings(plan, {r.label for r in too_broad}):
            queries = [compile_cohort(c, narrowed.analysis.time) for c in narrowed.cohorts]
            counts = await self._gather([self.client.count(q.params) for q in queries])
            if max(counts) <= cap:
                options.append(ClarificationOption(
                    label=label, plan=narrowed,
                    description=f"{description} ({', '.join(f'{n:,}' for n in counts)} "
                                "studies)"))
        names = ", ".join(f"'{r.label}' ({r.total:,} studies)" for r in too_broad)
        meta = self._meta(plan, too_broad, llm, version)
        meta.completeness = Completeness(complete=False, reason="not retrieved: too broad")
        return QueryResponse(
            status="needs_clarification", query_id=qid, query=question, plan=plan, meta=meta,
            message=f"{names} exceeds the {cap:,}-study limit for a complete analysis.",
            clarification=Clarification(
                question="This question matches too many studies to analyze completely. "
                         "Narrow it with one of these, or ask a more specific question.",
                options=options))


def _narrowings(plan: QueryPlan,
                labels: set[str]) -> list[tuple[str, str, QueryPlan]]:
    """Candidate narrowed plans: each adds one filter to the over-broad cohorts."""
    five_years_ago = date(date.today().year - 5, 1, 1)
    candidates: list[tuple[str, str, Callable[[CohortFilters], CohortFilters | None]]] = [
        ("Recruiting only", "Only studies currently recruiting",
         lambda f: None if f.overall_status else f.model_copy(
             update={"overall_status": [OverallStatus.RECRUITING]})),
        (f"Started since {five_years_ago.year}", f"Studies starting {five_years_ago.year} or later",
         lambda f: None if f.start_date_from else f.model_copy(
             update={"start_date_from": five_years_ago})),
        ("Interventional Phase 3", "Interventional studies that include Phase 3",
         lambda f: None if f.phase else f.model_copy(
             update={"phase": [Phase.PHASE3], "study_type": StudyType.INTERVENTIONAL})),
    ]
    out = []
    for label, description, narrow in candidates:
        cohorts = []
        for c in plan.cohorts:
            new = narrow(c.filters) if c.label in labels else c.filters
            cohorts.append(c.model_copy(update={"filters": new or c.filters}))
        if any(c.filters != o.filters for c, o in zip(cohorts, plan.cohorts, strict=True)):
            out.append((label, description, plan.model_copy(update={"cohorts": cohorts})))
    return out


def _ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)
