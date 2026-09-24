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

    llm_mode: Literal["openai", "fake"] = "openai"
    openai_api_key: str | None = None
    openai_model: str = "gpt-5-mini"
    openai_timeout_s: float = 60.0
    planner_max_tool_calls: int = 4

    evidence_sample_size: int = 5
    response_cache_size: int = 128


@lru_cache
def get_settings() -> Settings:
    return Settings()
