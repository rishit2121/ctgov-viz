"""A small in-process stand-in for the ClinicalTrials.gov v2 API.

Supports what the client uses: ``/version``; ``/studies`` with case-insensitive substring search
on ``query.cond`` / ``query.intr``, ``filter.overallStatus``, ``countTotal``, ``pageSize`` and
``pageToken`` cursor paging. ``filter.advanced`` is ignored here, so any structured filter is
enforced by the pipeline's local re-verification (which is what those tests check).

Faults can be scripted per request number to exercise retries and partial results.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx

from app.ctgov.client import CTGovClient

Fault = Callable[[httpx.Request], httpx.Response | None]


class FakeCTGov:
    def __init__(self, studies: list[dict[str, Any]], page_size: int = 2,
                 data_timestamp: str = "2026-09-23T09:00:05"):
        self.studies = studies
        self.page_size = page_size
        self.data_timestamp = data_timestamp
        self.requests: list[httpx.Request] = []
        self.faults: list[Fault] = []

    def client(self, **kwargs: Any) -> CTGovClient:
        http = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
        return CTGovClient("https://fake.ctgov/api/v2", http=http, backoff_base_s=0, **kwargs)

    # -------------------------------------------------------------- matching

    def _matches(self, s: dict[str, Any], params: dict[str, str]) -> bool:
        ps = s["protocolSection"]
        conds = " ".join(ps.get("conditionsModule", {}).get("conditions", [])).lower()
        intrs = " ".join(i["name"] for i in ps.get("armsInterventionsModule", {})
                         .get("interventions", [])).lower()
        if (q := params.get("query.cond")) and q.lower() not in conds:
            return False
        if (q := params.get("query.intr")) and q.lower() not in intrs:
            return False
        statuses = params.get("filter.overallStatus")
        return not (statuses and ps["statusModule"]["overallStatus"] not in statuses.split(","))

    # -------------------------------------------------------------- handler

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for fault in self.faults:
            if (response := fault(request)) is not None:
                return response
        if request.url.path.endswith("/version"):
            return httpx.Response(200, json={"apiVersion": "2.0.5",
                                             "dataTimestamp": self.data_timestamp})
        params = dict(request.url.params)
        matched = [s for s in self.studies if self._matches(s, params)]
        size = min(int(params.get("pageSize", "10")), self.page_size)
        start = int(params.get("pageToken", "0"))
        body: dict[str, Any] = {"studies": matched[start:start + size]}
        if params.get("countTotal") == "true":
            body["totalCount"] = len(matched)
        if start + size < len(matched):
            body["nextPageToken"] = str(start + size)
        return httpx.Response(200, content=json.dumps(body).encode(),
                              headers={"content-type": "application/json"})

    # -------------------------------------------------------------- fault helpers

    def fail_page(self, token: str, status: int = 503, times: int = 99) -> None:
        remaining = {"n": times}

        def fault(request: httpx.Request) -> httpx.Response | None:
            if request.url.params.get("pageToken") == token and remaining["n"] > 0:
                remaining["n"] -= 1
                return httpx.Response(status, text="upstream trouble")
            return None
        self.faults.append(fault)

    def fail_all_studies(self, status: int, text: str = "error") -> None:
        self.faults.append(lambda r: httpx.Response(status, text=text)
                           if r.url.path.endswith("/studies") else None)
