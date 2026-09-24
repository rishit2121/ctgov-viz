"""Async ClinicalTrials.gov v2 client: complete pagination, bounded retries, honest completeness.

Contract: ``fetch_all`` either returns every study the API reports (``complete=True``) or says
precisely why it stopped (``complete=False`` + ``reason``). A failed page never masquerades as
a complete result. A failure on the *first* page raises, since there is nothing to report.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.ctgov.errors import CTGovError, CTGovTimeout

log = logging.getLogger(__name__)

PAGE_SIZE = 1000  # API maximum (larger values are silently capped)
_RETRY_STATUS = {429, 500, 502, 503, 504}


@dataclass
class FetchResult:
    studies: list[dict[str, Any]] = field(default_factory=list)
    total: int = 0
    pages: int = 0
    complete: bool = True
    reason: str | None = None


class CTGovClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 20.0,
        max_retries: int = 3,
        http: httpx.AsyncClient | None = None,
        backoff_base_s: float = 0.5,
    ):
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.backoff_base_s = backoff_base_s
        self._http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s),
            headers={"User-Agent": "ctgov-viz/0.1 (research prototype)"},
        )
        self._version: tuple[float, dict[str, str]] | None = None

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = await self._http.get(url, params=params)
            except httpx.TimeoutException as e:
                last_error = CTGovTimeout(f"timeout calling {path}")
                log.warning("CT.gov timeout (attempt %d): %s", attempt + 1, e)
            except httpx.TransportError as e:
                last_error = CTGovError(f"network error calling {path}: {e}")
                log.warning("CT.gov transport error (attempt %d): %s", attempt + 1, e)
            else:
                if resp.status_code == 200:
                    try:
                        return resp.json()  # type: ignore[no-any-return]
                    except ValueError as e:
                        raise CTGovError(f"invalid JSON from {path}: {e}", 200) from e
                message = resp.text.strip()[:500] or resp.reason_phrase
                if resp.status_code not in _RETRY_STATUS:
                    raise CTGovError(f"ClinicalTrials.gov rejected the query: {message}",
                                     resp.status_code)
                last_error = CTGovError(f"HTTP {resp.status_code}: {message}", resp.status_code)
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit() and attempt < self.max_retries:
                    await asyncio.sleep(min(float(retry_after), 10.0))
                    continue
            if attempt < self.max_retries:
                delay = self.backoff_base_s * 2**attempt
                await asyncio.sleep(delay + random.uniform(0, delay / 2))
        assert last_error is not None
        raise last_error

    async def version(self) -> dict[str, str]:
        """``{"apiVersion": ..., "dataTimestamp": ...}``, cached for 10 minutes."""
        now = time.monotonic()
        if self._version and now - self._version[0] < 600:
            return self._version[1]
        data = await self._get("/version")
        self._version = (now, data)
        return data

    async def count(self, params: dict[str, str]) -> int:
        q = {k: v for k, v in params.items() if k != "fields"}
        data = await self._get("/studies", {**q, "countTotal": "true", "pageSize": "1",
                                            "fields": "NCTId"})
        return int(data.get("totalCount", 0))

    async def sample(self, params: dict[str, str], n: int = 3) -> list[dict[str, Any]]:
        q = {k: v for k, v in params.items() if k != "fields"}
        data = await self._get("/studies", {**q, "pageSize": str(n), "fields": "NCTId,BriefTitle"})
        return list(data.get("studies", []))

    async def fetch_all(self, params: dict[str, str], max_studies: int) -> FetchResult:
        result = FetchResult()
        seen: set[str] = set()
        token: str | None = None
        while True:
            page_params = {**params, "pageSize": str(PAGE_SIZE)}
            if token:
                page_params["pageToken"] = token
            else:
                page_params["countTotal"] = "true"
            try:
                data = await self._get("/studies", page_params)
            except CTGovError as e:
                if result.pages == 0:
                    raise
                result.complete = False
                result.reason = f"page {result.pages + 1} failed after retries: {e}"
                return result
            result.pages += 1
            if token is None:
                result.total = int(data.get("totalCount", 0))
            for study in data.get("studies", []):
                nct = study.get("protocolSection", {}).get("identificationModule", {}).get("nctId")
                if nct and nct not in seen:
                    seen.add(nct)
                    result.studies.append(study)
            token = data.get("nextPageToken")
            if not token:
                break
            if len(result.studies) >= max_studies:
                result.complete = False
                result.reason = (f"stopped at the {max_studies:,}-study safety cap "
                                 f"({result.total:,} match); narrow the question")
                return result
        if result.total and len(result.studies) < result.total:
            result.complete = False
            result.reason = (f"API reported {result.total:,} studies but returned "
                             f"{len(result.studies):,}")
        return result
