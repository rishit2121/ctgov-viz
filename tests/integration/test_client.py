"""CT.gov client against the fake API: paging, retries, error mapping, completeness."""

from __future__ import annotations

import httpx
import pytest

from app.ctgov.errors import CTGovError, CTGovTimeout
from tests.conftest import study
from tests.integration.fake_ctgov import FakeCTGov

STUDIES = [study(f"NCT{i:08}", conditions=["Melanoma"]) for i in range(1, 6)]
PARAMS = {"query.cond": "melanoma"}


async def test_fetches_every_page() -> None:
    fake = FakeCTGov(STUDIES, page_size=2)
    result = await fake.client().fetch_all(PARAMS, max_studies=100)
    assert (result.total, result.pages, result.complete) == (5, 3, True)
    assert [s["protocolSection"]["identificationModule"]["nctId"] for s in result.studies] == \
        [f"NCT{i:08}" for i in range(1, 6)]
    first = fake.requests[0].url.params
    assert first["countTotal"] == "true" and "pageToken" not in first
    assert fake.requests[1].url.params["pageToken"] == "2"


async def test_duplicate_records_across_pages_are_dropped() -> None:
    fake = FakeCTGov([*STUDIES[:2], STUDIES[1], *STUDIES[2:]], page_size=2)
    result = await fake.client().fetch_all(PARAMS, max_studies=100)
    assert len(result.studies) == 5
    assert not result.complete  # API said 6 but only 5 distinct came back
    assert "reported 6" in (result.reason or "")


async def test_transient_errors_are_retried() -> None:
    fake = FakeCTGov(STUDIES, page_size=10)
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response | None:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="busy")
        if calls["n"] == 2:
            return httpx.Response(429, text="slow down", headers={"Retry-After": "0"})
        if calls["n"] == 3:
            return httpx.Response(200, text="<html>not json</html>")
        return None
    fake.faults.append(flaky)
    result = await fake.client(max_retries=3).fetch_all(PARAMS, max_studies=100)
    assert result.complete and len(result.studies) == 5
    assert calls["n"] == 4


async def test_bad_request_is_not_retried() -> None:
    fake = FakeCTGov(STUDIES)
    fake.fail_all_studies(400, "Error parsing query in advanced filter")
    with pytest.raises(CTGovError) as exc:
        await fake.client(max_retries=3).count(PARAMS)
    assert exc.value.status_code == 400
    assert "advanced filter" in str(exc.value)
    assert len(fake.requests) == 1


async def test_first_page_failure_raises() -> None:
    fake = FakeCTGov(STUDIES)
    fake.fail_all_studies(503)
    with pytest.raises(CTGovError):
        await fake.client(max_retries=1).fetch_all(PARAMS, max_studies=100)


async def test_later_page_failure_yields_labelled_partial_result() -> None:
    fake = FakeCTGov(STUDIES, page_size=2)
    fake.fail_page("2")
    result = await fake.client(max_retries=1).fetch_all(PARAMS, max_studies=100)
    assert not result.complete
    assert len(result.studies) == 2 and result.total == 5
    assert "page 2 failed" in (result.reason or "")


async def test_safety_cap_stops_and_says_so() -> None:
    fake = FakeCTGov(STUDIES, page_size=2)
    result = await fake.client().fetch_all(PARAMS, max_studies=3)
    assert not result.complete and len(result.studies) == 4
    assert "safety cap" in (result.reason or "")


async def test_timeouts_map_to_ctgov_timeout() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)
    fake = FakeCTGov(STUDIES)
    fake.faults.append(boom)
    with pytest.raises(CTGovTimeout):
        await fake.client(max_retries=1).count(PARAMS)


async def test_version_is_cached() -> None:
    fake = FakeCTGov(STUDIES)
    client = fake.client()
    assert (await client.version())["apiVersion"] == "2.0.5"
    await client.version()
    assert sum(r.url.path.endswith("/version") for r in fake.requests) == 1
