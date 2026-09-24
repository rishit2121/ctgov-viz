"""Protections for a public deployment: every planner call spends the operator's LLM credits.

- ``PlanCache``: identical questions (same text + structured fields) reuse the earlier plan, so
  repeated demo questions cost nothing and return in seconds. Retrieval still runs live (and the
  response cache applies), so numbers stay current.
- ``LLMRateLimiter``: a per-client hourly limit and a global daily cap on planner calls.

Both are in-process, which fits the single-instance deployment this project targets.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque
from datetime import UTC, datetime
from typing import Any

from app.planner.base import PlannerResult


class PlanCache:
    def __init__(self, max_entries: int = 256):
        self.max_entries = max_entries
        self._items: OrderedDict[str, PlannerResult] = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def key(query: str, fields: dict[str, Any]) -> str:
        normalized = " ".join(query.casefold().split())
        return normalized + "|" + repr(sorted(fields.items()))

    def get(self, key: str) -> PlannerResult | None:
        with self._lock:
            hit = self._items.get(key)
            if hit is not None:
                self._items.move_to_end(key)
            return hit

    def put(self, key: str, result: PlannerResult) -> None:
        with self._lock:
            self._items[key] = result
            self._items.move_to_end(key)
            while len(self._items) > self.max_entries:
                self._items.popitem(last=False)


class RateLimited(Exception):
    def __init__(self, message: str, retry_after_s: int):
        super().__init__(message)
        self.message, self.retry_after_s = message, retry_after_s


class LLMRateLimiter:
    """``per_client_per_hour`` / ``per_day`` of 0 mean unlimited."""

    def __init__(self, per_client_per_hour: int = 0, per_day: int = 0,
                 clock: Any = time.monotonic):
        self.per_client_per_hour = per_client_per_hour
        self.per_day = per_day
        self._clock = clock
        self._recent: dict[str, deque[float]] = {}
        self._day = datetime.now(UTC).date()
        self._today = 0
        self._lock = threading.Lock()

    def acquire(self, client: str) -> None:
        with self._lock:
            today = datetime.now(UTC).date()
            if today != self._day:
                self._day, self._today = today, 0
            if self.per_day and self._today >= self.per_day:
                raise RateLimited("This demo has reached its daily limit of new questions; "
                                  "example questions and repeats still work.", 3600)
            if self.per_client_per_hour:
                now = self._clock()
                recent = self._recent.setdefault(client, deque())
                while recent and now - recent[0] > 3600:
                    recent.popleft()
                if len(recent) >= self.per_client_per_hour:
                    wait = int(3600 - (now - recent[0])) + 1
                    raise RateLimited(f"Limit of {self.per_client_per_hour} new questions per "
                                      "hour reached; try again later.", wait)
                recent.append(now)
            self._today += 1
