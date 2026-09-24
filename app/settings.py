"""Runtime configuration, read from environment variables or a local .env file."""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    ctgov_base_url: str = "https://clinicaltrials.gov/api/v2"
    ctgov_timeout_s: float = 20.0
    ctgov_max_retries: int = 3
    ctgov_concurrency: int = 3
    max_studies_per_cohort: int = 20_000
    request_deadline_s: float = 90.0

    # Planner LLM. "anthropic" (default) or "openai" need the matching API key; "fake" answers
    # only the example questions, offline.
    llm_mode: Literal["anthropic", "openai", "fake"] = "anthropic"
    llm_timeout_s: float = 120.0
    anthropic_api_key: str | None = None
    anthropic_workspace_id: str | None = None  # needed only for keys not scoped to a workspace
    anthropic_model: str = "claude-opus-5"
    anthropic_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None  # API default
    openai_api_key: str | None = None
    openai_model: str = "gpt-5-mini"
    openai_reasoning_effort: str | None = "low"
    planner_max_tool_calls: int = 4

    evidence_sample_size: int = 5
    response_cache_size: int = 128


@lru_cache
def get_settings() -> Settings:
    return Settings()
