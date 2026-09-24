"""Response envelopes for the public HTTP API (the request is in ``app.contracts.request``)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.contracts.plan import QueryPlan
from app.contracts.viz import Citation, VisualizationSpec

Status = Literal["ok", "partial", "empty", "needs_clarification", "unsupported", "error"]


class ClarificationOption(BaseModel):
    label: str
    description: str
    plan: QueryPlan


class Clarification(BaseModel):
    question: str
    options: list[ClarificationOption]


class ApiQuery(BaseModel):
    cohort: str
    url: str = Field(description="Reproducible ClinicalTrials.gov API URL for this cohort.")
    total: int = Field(description="totalCount reported by the API.")
    fetched: int
    pages: int
    complete: bool


class Completeness(BaseModel):
    complete: bool
    reason: str | None = None


class LLMInfo(BaseModel):
    model: str
    tool_calls: list[str] = Field(default_factory=list)
    repaired: bool = False


class Meta(BaseModel):
    source: str = "ClinicalTrials.gov API v2"
    api_version: str | None = None
    data_timestamp: str | None = None
    retrieved_at: str | None = None
    unit: str = "distinct studies (NCT IDs)"
    definitions: dict[str, str] = Field(default_factory=dict)
    api_queries: list[ApiQuery] = Field(default_factory=list)
    completeness: Completeness = Field(default_factory=lambda: Completeness(complete=True))
    studies_analyzed: int = 0
    filters: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="Effective search terms and filters per cohort (after request fields).")
    request_fields: dict[str, Any] = Field(
        default_factory=dict, description="Optional structured fields supplied with the request.")
    cohort_overlap: dict[str, int] = Field(default_factory=dict)
    excluded: dict[str, int] = Field(default_factory=dict)
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    llm: LLMInfo | None = None
    timings_ms: dict[str, int] = Field(default_factory=dict)


class QueryResponse(BaseModel):
    status: Status
    query_id: str | None = None
    query: str | None = Field(None, description="The request's natural-language query.")
    plan: QueryPlan | None = None
    visualization: VisualizationSpec | None = None
    clarification: Clarification | None = None
    message: str | None = Field(None, description="Human-readable note for empty/unsupported.")
    meta: Meta = Field(default_factory=Meta)


class ErrorResponse(BaseModel):
    status: Literal["error"] = "error"
    code: str
    message: str
    detail: Any = None


class EvidenceItem(Citation):
    fields_used: dict[str, Any] = Field(
        description="Normalized source values the grouping used (by API field path).")


class EvidencePage(BaseModel):
    query_id: str
    item: str
    total: int
    page: int
    page_size: int
    items: list[EvidenceItem]
