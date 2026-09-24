"""LLM planner (implemented in the next milestone)."""

from __future__ import annotations

from app.ctgov.client import CTGovClient
from app.planner.base import QuestionPlanner
from app.settings import Settings


def make_planner(settings: Settings, client: CTGovClient) -> QuestionPlanner | None:
    return None
