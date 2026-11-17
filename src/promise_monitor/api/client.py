"""PromiseTrackerClient — the ONLY module that talks to admin.promisetracker.ng.

Politeness (PRD §5.3): one request at a time, >= min_interval between requests,
identifying User-Agent, bounded retries with exponential backoff + jitter, Retry-After honoured.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import Any, Literal
from urllib.parse import quote
from uuid import UUID

import httpx
from pydantic import BaseModel, TypeAdapter, ValidationError

from promise_monitor.api.models import (
    ApiPromiseDetail,
    Envelope,
    MeterPage,
    MeterSummary,
    MeterType,
    RingState,
)
from promise_monitor.config import ApiConfig
from promise_monitor.logging_setup import get_logger

log = get_logger(__name__)

Chamber = Literal["upper-chamber", "lower-chamber"]


class ApiError(RuntimeError):
    """Non-retryable API failure (4xx, success=false, exhausted retries)."""

    def __init__(self, message: str, *, path: str, status: int | None = None) -> None:
        super().__init__(f"{message} [{path}]")
        self.path = path
        self.status = status


class ApiUnavailableError(ApiError):
    """Retries exhausted on 5xx / timeouts / connection errors."""


class RateLimitedError(ApiError):
    """429 with Retry-After beyond our ceiling: stop fetching for this run (PRD §5.3)."""


class SchemaError(ApiError):
    """Response was not the JSON shape we expect. Never write snapshots from it."""


class PromiseTrackerClient:
    def __init__(
        self,
        cfg: ApiConfig,
        *,
        contact: str | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._cfg = cfg
        self._sleep = sleep
        self._clock = clock
        self._jitter = jitter
        self._last_request_at: float | None = None
        ua = cfg.user_agent + (f" (+contact: {contact})" if contact else "")
        self._http = httpx.Client(
            base_url=str(cfg.base_url).rstrip("/") + "/",
            headers={"User-Agent": ua, "Accept": "application/json"},
            timeout=cfg.timeout_seconds,
            transport=transport,
            follow_redirects=False,
        )
        self.request_count = 0

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> PromiseTrackerClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ---------------------------------------------------------------- transport

    def _throttle(self) -> None:
        if self._last_request_at is not None:
            wait = self._cfg.min_interval_seconds - (self._clock() - self._last_request_at)
            if wait > 0:
                self._sleep(wait)
        self._last_request_at = self._clock()

    def _backoff(self, attempt: int) -> float:
        return float(self._cfg.backoff_base_seconds * (2**attempt) + self._jitter())

    def get_data(self, path: str, params: dict[str, str] | None = None) -> Any:
        """GET ``path`` (relative to base_url) and return the envelope's ``data``."""
        path = path.lstrip("/")
        attempts = self._cfg.max_retries + 1
        for attempt in range(attempts):
            self._throttle()
            self.request_count += 1
            try:
                resp = self._http.get(path, params=params)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt + 1 >= attempts:
                    raise ApiUnavailableError(f"{type(exc).__name__}", path=path) from exc
                delay = self._backoff(attempt)
                log.warning("api_retry", path=path, error=type(exc).__name__, delay=delay)
                self._sleep(delay)
                continue

            if resp.status_code == 429:
                retry_after = _retry_after(resp)
                delay = self._backoff(attempt) if retry_after is None else retry_after
                if delay > self._cfg.max_retry_after_seconds or attempt + 1 >= attempts:
                    raise RateLimitedError("rate limited", path=path, status=429)
                log.warning("api_rate_limited", path=path, delay=delay)
                self._sleep(delay)
                continue

            if resp.status_code >= 500:
                if attempt + 1 >= attempts:
                    raise ApiUnavailableError(
                        f"HTTP {resp.status_code}", path=path, status=resp.status_code
                    )
                delay = self._backoff(attempt)
                log.warning("api_retry", path=path, status=resp.status_code, delay=delay)
                self._sleep(delay)
                continue

            if resp.status_code != 200:
                raise ApiError(f"HTTP {resp.status_code}", path=path, status=resp.status_code)

            return _unwrap(resp, path)

        raise AssertionError("unreachable")  # pragma: no cover

    # ---------------------------------------------------------------- typed endpoints

    def presidential(self) -> MeterPage:
        return _parse(MeterPage, self.get_data("presidential"), "presidential")

    def legislative(self, chamber: Chamber) -> MeterPage:
        path = f"legislative/{chamber}"
        return _parse(MeterPage, self.get_data(path), path)

    def state(self, name: str) -> MeterPage:
        path = f"state/{quote(name)}"
        return _parse(MeterPage, self.get_data(path), path)

    def state_tenures(self, name: str, kind: MeterType) -> list[MeterSummary]:
        path = f"state/{quote(name)}/tenures"
        data = self.get_data(path, params={"kind": kind})
        return _parse_list(MeterSummary, data, path)

    def promise_ring_states(self) -> list[RingState]:
        path = "all/promise-ring/states"
        return _parse_list(RingState, self.get_data(path), path)

    def meter_page(self, list_endpoint: str) -> MeterPage:
        """Fetch a meter by its registry ``list_endpoint`` (e.g. ``meter/{id}/presidential``)."""
        return _parse(MeterPage, self.get_data(list_endpoint), list_endpoint)

    def promise_detail(self, promise_id: UUID | str) -> ApiPromiseDetail:
        path = f"view/promise/{promise_id}"
        return _parse(ApiPromiseDetail, self.get_data(path), path)


def _retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None  # HTTP-date form: fall back to our own backoff


def _unwrap(resp: httpx.Response, path: str) -> Any:
    ctype = resp.headers.get("content-type", "")
    if "json" not in ctype:
        # e.g. the Laravel admin panel's HTML for unknown routes
        raise SchemaError(f"expected JSON, got {ctype or 'no content-type'}", path=path)
    try:
        env = Envelope.model_validate(resp.json())
    except (ValueError, ValidationError) as exc:
        raise SchemaError(f"bad envelope: {exc}", path=path) from exc
    if not env.success:
        raise ApiError(f"success=false: {env.message}", path=path)
    return env.data


def _parse[M: BaseModel](model: type[M], data: Any, path: str) -> M:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise SchemaError(f"{model.__name__} validation failed: {exc}", path=path) from exc


def _parse_list[M: BaseModel](model: type[M], data: Any, path: str) -> list[M]:
    try:
        return TypeAdapter(list[model]).validate_python(data)  # type: ignore[valid-type]
    except ValidationError as exc:
        raise SchemaError(f"list[{model.__name__}] validation failed: {exc}", path=path) from exc
