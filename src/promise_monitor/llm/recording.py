"""Wrap any LLMClient so every call (success or failure) lands in ``llm_calls`` (PRD §4.5)."""

from __future__ import annotations

import hashlib
import time
import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from promise_monitor.db.models import LLMCall
from promise_monitor.llm.base import LLMAuthError, LLMClient, LLMError, LLMRequest, LLMResponse


class RecordingLLMClient:
    def __init__(self, inner: LLMClient, session: Session, run_id: uuid.UUID) -> None:
        self._inner = inner
        self._session = session
        self._run_id = run_id
        self.auth_failed = False

    def run(self, request: LLMRequest) -> LLMResponse:
        if self.auth_failed:
            raise LLMError("LLM disabled for this run after an auth failure")
        prompt_sha = hashlib.sha256(
            (request.system + "\n" + request.user).encode("utf-8")
        ).hexdigest()
        started = time.monotonic()
        try:
            resp = self._inner.run(request)
        except LLMError as exc:
            if isinstance(exc, LLMAuthError):
                self.auth_failed = True
            elapsed = int((time.monotonic() - started) * 1000)
            self._record(request, prompt_sha, None, None, elapsed, str(exc))
            raise
        self._record(request, prompt_sha, resp, resp.output, resp.latency_ms, None)
        return resp

    def _record(
        self,
        request: LLMRequest,
        prompt_sha: str,
        resp: LLMResponse | None,
        output: dict[str, object] | None,
        latency_ms: int,
        error: str | None,
    ) -> None:
        self._session.add(
            LLMCall(
                run_id=self._run_id,
                agent=request.task,
                prompt_name=request.prompt_name,
                subject_id=request.subject_id,
                model=resp.model if resp else None,
                prompt_sha256=prompt_sha,
                output=output,
                error=error,
                latency_ms=latency_ms,
                cost_usd=resp.cost_usd if resp else None,
                ok=error is None,
                created_at=datetime.now(UTC),
            )
        )
        self._session.flush()
