"""Fixture-backed fake of the promisetracker API, served through httpx.MockTransport."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

FIXTURES = Path(__file__).parent / "fixtures" / "api"
EMPTY_PAGE: dict[str, Any] = {
    "success": True,
    "data": {"promises": [], "all_meters": [], "promise_count": 0},
    "message": "No current meter found",
}
EMPTY_LIST: dict[str, Any] = {"success": True, "data": [], "message": "Tenures fetched"}

# path (relative to base, decoded, with ?kind=) -> fixture file
ROUTES: dict[str, str] = {
    "presidential": "api_presidential.json",
    "legislative/upper-chamber": "api_legislative_upper-chamber.json",
    "legislative/lower-chamber": "api_legislative_lower-chamber.json",
    "state/Edo": "api_state_edo.json",
    "state/Osun": "api_state_osun.json",
    "state/Ekiti": "api_state_ekiti.json",
    "state/Edo/tenures?kind=promise_tracker": "api_tenures_edo_promise_tracker.json",
    "state/Edo/tenures?kind=promise_ring": "api_tenures_edo_promise_ring.json",
    "state/Ekiti/tenures?kind=promise_ring": "api_tenures_ekiti_promise_ring.json",
    "state/Osun/tenures?kind=promise_tracker": "api_tenures_osun_promise_tracker.json",
    "all/promise-ring/states": "api_all_promise-ring_states.json",
}
BASE_PREFIX = "/api/v1/promise-tracker/"


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def route_key(request: httpx.URL) -> str:
    path = request.path
    assert path.startswith(BASE_PREFIX), path
    key = path[len(BASE_PREFIX) :]
    kind = request.params.get("kind")
    return f"{key}?kind={kind}" if kind else key


def fixture_handler(
    overrides: dict[str, Callable[[httpx.Request], httpx.Response]] | None = None,
    log: list[str] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Serve fixtures; unknown states get an empty page / empty tenures list."""
    overrides = overrides or {}

    def handler(request: httpx.Request) -> httpx.Response:
        key = route_key(request.url)
        if log is not None:
            log.append(key)
        if key in overrides:
            return overrides[key](request)
        if key in ROUTES:
            return httpx.Response(200, json=load_fixture(ROUTES[key]))
        if "/tenures?kind=" in key:
            return httpx.Response(200, json=EMPTY_LIST)
        if key.startswith("state/"):
            return httpx.Response(200, json=EMPTY_PAGE)
        return httpx.Response(404, json={"success": False, "message": "not found"})

    return handler


class FakeLLM:
    """Scripted LLMClient: returns queued outputs (or raises queued exceptions) in order.

    ``default`` is returned when the queue is empty; ``None`` means fail loudly.
    Raw search hits: an output may carry ``"__search_hits__": [url, ...]``; otherwise, for
    News Scout-shaped outputs, every result URL is treated as found in search (grounded).
    """

    def __init__(self, *outputs: dict[str, Any] | Exception, default: dict[str, Any] | None = None):
        self.queue: list[dict[str, Any] | Exception] = list(outputs)
        self.default = default
        self.requests: list[Any] = []

    def run(self, request: Any) -> Any:
        from promise_monitor.llm.base import LLMResponse

        self.requests.append(request)
        item = self.queue.pop(0) if self.queue else self.default
        if item is None:
            raise AssertionError(f"unexpected LLM call: {request.task}")
        if isinstance(item, Exception):
            raise item
        from promise_monitor.llm.base import SearchHit

        output = dict(item)
        hits = output.pop("__search_hits__", None)
        if hits is None:
            hits = [r.get("url", "") for r in output.get("results", []) if isinstance(r, dict)]
        return LLMResponse(
            output=output,
            model="fake-model",
            latency_ms=1,
            cost_usd=0.0,
            search_hits=tuple(SearchHit(u, f"title of {u}", "q") for u in hits),
        )
