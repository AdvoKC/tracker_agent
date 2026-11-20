"""SubmissionClient — the ONLY code that writes to admin.promisetracker.ng (PRD §8d, P12).

Contract: docs/integrations/promisetracker-submissions.md. Each client carries ONE
coordinator's backend-issued bearer token. The token is never logged, never put in an
exception message, and never sent across a redirect (redirects are not followed).

Politeness as PRD §5.3: one request at a time, >= min_interval apart (``Pacer``, shareable
across clients), identifying User-Agent, bounded retries with backoff, Retry-After honoured.
A POST is retried only because it always carries an ``Idempotency-Key`` (the article id), so a
retry can never create a second submission.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from promise_monitor.api.client import _retry_after
from promise_monitor.api.models import Envelope
from promise_monitor.config import ApiConfig, SubmissionsConfig
from promise_monitor.logging_setup import get_logger

log = get_logger(__name__)

RemoteState = Literal["pending_review", "published", "rejected", "withdrawn"]


class SubmissionError(RuntimeError):
    """A submission request failed. ``status`` is the HTTP status (None = no response)."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class TokenRejectedError(SubmissionError):
    """401: the token is unknown, expired or revoked."""


class ScopeDeniedError(SubmissionError):
    """403: the token is valid but doesn't cover this meter."""


class SubmissionUnavailableError(SubmissionError):
    """Retries exhausted (5xx, timeouts, connection errors, 429)."""


# --------------------------------------------------------------------------- payloads


class _In(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class TokenInfo(_In):
    token_id: str = Field(min_length=1, max_length=128)
    holder_name: str | None = None
    meter_ids: list[str]
    scopes: list[str] = Field(default_factory=list)
    expires_at: datetime | None = None


class SubmissionReceipt(_In):
    submission_id: str = Field(min_length=1, max_length=128)
    state: RemoteState


class SubmissionStatus(_In):
    submission_id: str
    state: RemoteState
    update_id: str | None = Field(default=None, max_length=64)
    reviewer_note: str | None = None


class SourceRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    url: str
    title: str | None = None
    publisher: str | None = None
    published_date: str | None = None


class SubmissionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    meter_id: str
    promise_id: str
    headline: str
    body_markdown: str
    body_html: str
    sources: list[SourceRef]
    external_ref: str
    approved_at: str


# --------------------------------------------------------------------------- transport


class Pacer:
    """At most one request per ``interval`` seconds, shared by every client that holds it."""

    def __init__(
        self,
        interval: float,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.interval = interval
        self.sleep = sleep
        self.clock = clock
        self._last: float | None = None

    def wait(self) -> None:
        if self._last is not None:
            gap = self.interval - (self.clock() - self._last)
            if gap > 0:
                self.sleep(gap)
        self._last = self.clock()


class SubmissionClient:
    def __init__(
        self,
        cfg: SubmissionsConfig,
        api_cfg: ApiConfig,
        token: str,
        *,
        contact: str | None = None,
        transport: httpx.BaseTransport | None = None,
        pacer: Pacer | None = None,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._api = api_cfg
        self._pacer = pacer or Pacer(api_cfg.min_interval_seconds)
        self._jitter = jitter
        ua = api_cfg.user_agent + (f" (+contact: {contact})" if contact else "")
        self._http = httpx.Client(
            base_url=str(cfg.base_url).rstrip("/") + "/",
            headers={
                "User-Agent": ua,
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
            },
            timeout=cfg.timeout_seconds,
            transport=transport,
            follow_redirects=False,  # never hand the token to another host
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> SubmissionClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------------ endpoints

    def whoami(self) -> TokenInfo:
        return _parse(TokenInfo, self._request("GET", "whoami"))

    def create(self, payload: SubmissionPayload, *, idempotency_key: str) -> SubmissionReceipt:
        data = self._request(
            "POST",
            "",
            json=payload.model_dump(mode="json"),
            headers={"Idempotency-Key": idempotency_key},
        )
        return _parse(SubmissionReceipt, data)

    def get(self, submission_id: str) -> SubmissionStatus:
        return _parse(SubmissionStatus, self._request("GET", submission_id))

    # ------------------------------------------------------------------ plumbing

    def _backoff(self, attempt: int) -> float:
        return float(self._api.backoff_base_seconds * (2**attempt) + self._jitter())

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        attempts = self._api.max_retries + 1
        for attempt in range(attempts):
            self._pacer.wait()
            last = attempt + 1 >= attempts
            try:
                resp = self._http.request(method, path, json=json, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if last:
                    raise SubmissionUnavailableError(type(exc).__name__) from None
                self._retry(method, path, type(exc).__name__, self._backoff(attempt))
                continue
            if resp.status_code == 429:
                ra = _retry_after(resp)
                delay = self._backoff(attempt) if ra is None else ra
                if last or delay > self._api.max_retry_after_seconds:
                    raise SubmissionUnavailableError("rate limited", status=429)
                self._retry(method, path, "429", delay)
                continue
            if resp.status_code >= 500:
                if last:
                    raise SubmissionUnavailableError(
                        f"HTTP {resp.status_code}", status=resp.status_code
                    )
                self._retry(method, path, str(resp.status_code), self._backoff(attempt))
                continue
            return _unwrap(resp)
        raise AssertionError("unreachable")  # pragma: no cover

    def _retry(self, method: str, path: str, why: str, delay: float) -> None:
        log.warning("submission_retry", method=method, path=path or "/", why=why, delay=delay)
        self._pacer.sleep(delay)


def _unwrap(resp: httpx.Response) -> Any:
    status = resp.status_code
    try:
        env = Envelope.model_validate(resp.json())
    except (ValueError, ValidationError):
        env = None
    message = (env.message if env else "") or f"HTTP {status}"
    if status == 401:
        raise TokenRejectedError(
            "Promise Tracker rejected the token (expired, revoked or wrong)", status=401
        )
    if status == 403:
        raise ScopeDeniedError("this token doesn't cover the article's meter", status=403)
    if status == 422 and env is not None and isinstance(env.data, dict):
        errs = env.data.get("errors") or []
        detail = "; ".join(
            f"{e.get('field')}: {e.get('message')}" for e in errs if isinstance(e, dict)
        )
        raise SubmissionError(
            f"Promise Tracker refused the update: {detail or message}", status=422
        )
    if status not in (200, 201):
        raise SubmissionError(f"Promise Tracker said: {message[:200]}", status=status)
    if env is None or not env.success:
        raise SubmissionError(f"unexpected response: {message[:200]}", status=status)
    return env.data


def _parse[M: BaseModel](model: type[M], data: Any) -> M:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise SubmissionError(
            f"unexpected {model.__name__} shape: {exc.error_count()} error(s)"
        ) from None
