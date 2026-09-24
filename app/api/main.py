"""HTTP API. Thin: request parsing, routing, and error mapping only.

    GET  /                              demo UI (static page using the endpoints below)
    POST /query                         query (+ optional structured fields) -> verified chart
    GET  /query/{query_id}              a previously computed response (while cached)
    GET  /query/{query_id}/evidence/{item_id}?page=&page_size=
                                        complete, paginated contributors for one datum
    GET  /capabilities                  what can be asked (derived from the field registry)
    GET  /schema                        JSON Schemas of the request/plan/response contracts
    GET  /health                        liveness + ClinicalTrials.gov reachability
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from app.contracts.plan import Measure, QueryPlan
from app.contracts.request import QueryRequest
from app.contracts.response import ErrorResponse, EvidencePage, Meta, QueryResponse
from app.ctgov.client import CTGovClient
from app.evidence.store import ResultCache
from app.pipeline import Pipeline, PipelineError
from app.planner.base import PlannerError, PlannerResult, QuestionPlanner
from app.registry.fields import REGISTRY
from app.registry.request_fields import RequestFieldConflict, apply_request_fields, constraints_text
from app.settings import Settings, get_settings

log = logging.getLogger(__name__)
DEMO_PAGE = Path(__file__).resolve().parents[1] / "static" / "index.html"


def build_planner(settings: Settings, client: CTGovClient) -> QuestionPlanner | None:
    from app.planner.planner import make_planner  # imported lazily: optional LLM dependency

    return make_planner(settings, client)


def create_app(
    settings: Settings | None = None,
    client: CTGovClient | None = None,
    planner: QuestionPlanner | None = None,
) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        ct = client or CTGovClient(settings.ctgov_base_url, timeout_s=settings.ctgov_timeout_s,
                                   max_retries=settings.ctgov_max_retries)
        app.state.pipeline = Pipeline(ct, settings, ResultCache(settings.response_cache_size))
        app.state.planner = planner or build_planner(settings, ct)
        try:
            yield
        finally:
            await ct.aclose()

    app = FastAPI(title="ctgov-viz", version="0.1.0", lifespan=lifespan,
                  description="Natural-language questions about ClinicalTrials.gov -> verified, "
                              "visualization-ready JSON with study-level evidence.")
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"],
                       allow_headers=["*"])

    @app.exception_handler(PipelineError)
    async def _pipeline_error(_: Request, e: PipelineError) -> JSONResponse:
        return _error(e.http_status, e.code, e.message, e.detail)

    @app.exception_handler(PlannerError)
    async def _planner_error(_: Request, e: PlannerError) -> JSONResponse:
        status = 503 if e.code == "llm_unavailable" else 422
        return _error(status, e.code, e.message, e.detail)

    @app.exception_handler(RequestFieldConflict)
    async def _field_conflict(_: Request, e: RequestFieldConflict) -> JSONResponse:
        return _error(422, "conflicting_fields", str(e))

    @app.exception_handler(RequestValidationError)
    async def _invalid_request(_: Request, e: RequestValidationError) -> JSONResponse:
        detail = [{"loc": list(err.get("loc", [])), "msg": err.get("msg")} for err in e.errors()]
        return _error(422, "invalid_request", "The request body is not valid.", detail)

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, e: Exception) -> JSONResponse:
        log.exception("unhandled error")
        return _error(500, "internal_error", "Unexpected server error.")

    @app.get("/", include_in_schema=False)
    async def demo() -> FileResponse:
        """Small demo UI: ask a question, see the chart, click any datum for its citations."""
        return FileResponse(DEMO_PAGE)

    @app.post("/query", response_model=QueryResponse, response_model_exclude_none=True,
              responses={422: {"model": ErrorResponse}, 502: {"model": ErrorResponse},
                         503: {"model": ErrorResponse}, 504: {"model": ErrorResponse}})
    async def query(req: QueryRequest, request: Request) -> QueryResponse:
        pipeline: Pipeline = request.app.state.pipeline
        if req.plan is not None:  # advanced: skip interpretation
            planned = PlannerResult(kind="plan", llm=None, plan=req.plan)
        else:
            planner: QuestionPlanner | None = request.app.state.planner
            if planner is None:
                raise PlannerError("llm_unavailable", "No LLM is configured; set "
                                   "ANTHROPIC_API_KEY (or LLM_MODE), or submit a `plan`.")
            planned = await planner.plan(req.query, constraints_text(req))

        if planned.kind == "plan":
            assert planned.plan is not None
            response = await pipeline.run_plan(apply_request_fields(planned.plan, req),
                                               question=req.query, llm=planned.llm)
        else:
            clarification = planned.clarification
            if clarification is not None:  # options must honour the request fields too
                clarification = clarification.model_copy(update={"options": [
                    o.model_copy(update={"plan": apply_request_fields(o.plan, req)})
                    for o in clarification.options]})
            response = QueryResponse(
                status="needs_clarification" if planned.kind == "clarify" else "unsupported",
                query=req.query, clarification=clarification, message=planned.message,
                meta=Meta(llm=planned.llm))
        response.meta.request_fields = req.structured_fields()
        return response

    @app.get("/query/{query_id}", response_model=QueryResponse,
             response_model_exclude_none=True, responses={404: {"model": ErrorResponse}})
    async def cached(query_id: str, request: Request) -> Any:
        entry = request.app.state.pipeline.cache.get(query_id)
        if entry is None:
            return _expired(query_id)
        return entry[0]

    @app.get("/query/{query_id}/evidence/{item_id}", response_model=EvidencePage,
             responses={404: {"model": ErrorResponse}})
    async def evidence(query_id: str, item_id: str, request: Request,
                       page: int = Query(1, ge=1),
                       page_size: int = Query(100, ge=1, le=1000)) -> Any:
        bundle = request.app.state.pipeline.evidence(query_id)
        if bundle is None:
            return _expired(query_id)
        result = bundle.page(item_id, page, page_size)
        if result is None:
            return _error(404, "evidence_not_found", f"No evidence item '{item_id}' in "
                          f"query {query_id}.")
        return result

    @app.get("/capabilities")
    async def capabilities() -> dict[str, Any]:
        return {
            "analysis_kinds": {
                "aggregate": "counts by a category or by start year, optionally split into "
                             "series (compare cohorts, or cross-tabulate two dimensions)",
                "cooccurrence": "network of values appearing together in a study (or arm)",
                "numeric_pair": "scatter of two numeric measures, one point per study",
            },
            "dimensions": [
                {"name": f.name.value, "title": f.title, "operations": sorted(f.ops),
                 "multi_valued": f.multi_valued, "description": f.description}
                for f in REGISTRY.values()
            ],
            "measures": [m.value for m in Measure],
            "unit": "distinct studies (NCT IDs)",
        }

    @app.get("/schema")
    async def schema() -> dict[str, Any]:
        return {name: model.model_json_schema() for name, model in (
            ("QueryRequest", QueryRequest), ("QueryPlan", QueryPlan),
            ("QueryResponse", QueryResponse), ("EvidencePage", EvidencePage),
            ("ErrorResponse", ErrorResponse))}

    @app.get("/health")
    async def health(request: Request) -> dict[str, Any]:
        pipeline: Pipeline = request.app.state.pipeline
        try:
            version = await pipeline.client.version()
            upstream: dict[str, Any] = {"reachable": True, **version}
        except Exception as e:  # health must never raise
            upstream = {"reachable": False, "error": str(e)}
        return {"status": "ok", "ctgov": upstream,
                "llm": request.app.state.planner is not None}

    return app


def _error(status: int, code: str, message: str, detail: Any = None) -> JSONResponse:
    body = ErrorResponse(code=code, message=message, detail=detail)
    return JSONResponse(status_code=status, content=body.model_dump(exclude_none=True))


def _expired(query_id: str) -> JSONResponse:
    return _error(404, "query_not_found", f"Query {query_id} is unknown or has expired from the "
                  "cache; re-submit its plan to recompute.")


app = create_app()
