"""Request/response envelopes for the public HTTP API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.contracts.plan import QueryPlan
from app.contracts.viz import VisualizationSpec

Status = Literal["ok", "partial", "empty", "needs_clarification", "unsupported", "error"]


class QueryRequest(BaseModel):
    question: str | None = Field(None, max_length=1000)
    plan: QueryPlan | None = Field(
        None,
        description="Execute this plan directly and skip the LLM (e.g. a clarification option).",
    )

    @model_validator(mode="after")
    def _one_input(self) -> QueryRequest:
        if (self.question is None) == (self.plan is None):
            raise ValueError("provide exactly one of `question` or `plan`")
        return self


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
    cohort_overlap: dict[str, int] = Field(default_factory=dict)
    excluded: dict[str, int] = Field(default_factory=dict)
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    llm: LLMInfo | None = None


class QueryResponse(BaseModel):
    status: Status
    query_id: str | None = None
    question: str | None = None
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


class EvidenceItem(BaseModel):
    nct_id: str
    url: str
    title: str | None
    fields_used: dict[str, Any]


class EvidencePage(BaseModel):
    query_id: str
    item: str
    total: int
    page: int
    page_size: int
    items: list[EvidenceItem]
