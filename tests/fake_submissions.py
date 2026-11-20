"""A fake Promise Tracker submissions backend (PRD §8d contract), served via httpx.MockTransport.

Behaves like the contract in docs/integrations/promisetracker-submissions.md: bearer tokens
scoped to meter ids, idempotent create, 401/403/404/409/422, and a per-submission state the
test can move (``publish`` / ``reject``). Records every request so tests can assert on them.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import HttpUrl

from promise_monitor.config import AppConfig

BASE = "https://pt.test/api/v1/promise-tracker/submissions"


@dataclass
class FakeToken:
    meter_ids: list[str]
    token_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    holder_name: str = "Test Coordinator"
    scopes: list[str] = field(default_factory=lambda: ["submissions:write"])
    expires_at: str | None = "2027-02-17T00:00:00+00:00"
    revoked: bool = False


@dataclass
class FakeSubmissionBackend:
    tokens: dict[str, FakeToken] = field(default_factory=dict)
    promise_meters: dict[str, str] = field(default_factory=dict)  # promise_id → meter_id
    submissions: dict[str, dict[str, Any]] = field(default_factory=dict)
    by_key: dict[str, str] = field(default_factory=dict)  # idempotency key → submission id
    requests: list[httpx.Request] = field(default_factory=list)
    fail_next: list[int] = field(default_factory=list)  # status codes to return before working

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def publish(self, submission_id: str, update_id: str, note: str | None = None) -> None:
        sub = self.submissions[submission_id]
        sub.update(state="published", update_id=update_id, reviewer_note=note)

    def reject(self, submission_id: str, note: str) -> None:
        self.submissions[submission_id].update(state="rejected", reviewer_note=note)

    # ------------------------------------------------------------------ handler

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_next:
            return _env(self.fail_next.pop(0), None, "temporary failure")
        auth = request.headers.get("authorization", "")
        tok = self.tokens.get(auth.removeprefix("Bearer ")) if auth.startswith("Bearer ") else None
        if tok is None or tok.revoked:
            return _env(401, None, "invalid token")
        path = request.url.path.removeprefix(httpx.URL(BASE).path)
        if request.method == "GET" and path == "/whoami":
            return _env(
                200,
                {
                    "token_id": tok.token_id,
                    "holder_name": tok.holder_name,
                    "meter_ids": tok.meter_ids,
                    "scopes": tok.scopes,
                    "expires_at": tok.expires_at,
                },
            )
        if request.method == "POST" and path in ("", "/"):
            return self._create(request, tok)
        if request.method == "GET" and path.startswith("/"):
            sub = self.submissions.get(path[1:])
            if sub is None or sub["token_id"] != tok.token_id:
                return _env(404, None, "not found")
            return _env(200, {k: v for k, v in sub.items() if k not in ("payload", "token_id")})
        return _env(404, None, "no route")

    def _create(self, request: httpx.Request, tok: FakeToken) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("meter_id") not in tok.meter_ids:
            return _env(403, None, "meter not in token scope")
        if self.promise_meters.get(body.get("promise_id", "")) != body["meter_id"]:
            return _env(
                422,
                {"errors": [{"field": "promise_id", "message": "not on this meter"}]},
                "invalid",
            )
        key = request.headers.get("idempotency-key", "")
        if key in self.by_key:
            sub = self.submissions[self.by_key[key]]
            if sub["payload"] != body:
                return _env(409, None, "idempotency key reused with a different payload")
            return _env(200, _receipt(sub), "existing")
        sid = uuid.uuid4().hex
        sub = {
            "submission_id": sid,
            "state": "pending_review",
            "update_id": None,
            "reviewer_note": None,
            "created_at": "2026-11-20T09:00:00+00:00",
            "updated_at": "2026-11-20T09:00:00+00:00",
            "payload": body,
            "token_id": tok.token_id,
        }
        self.submissions[sid] = sub
        if key:
            self.by_key[key] = sid
        return _env(201, _receipt(sub), "created")


def _receipt(sub: dict[str, Any]) -> dict[str, Any]:
    return {k: sub[k] for k in ("submission_id", "state", "created_at")}


def _env(status: int, data: Any, message: str = "ok") -> httpx.Response:
    return httpx.Response(
        status,
        json={"success": 200 <= status < 300, "data": data, "message": message},
        headers={"content-type": "application/json"},
    )


def with_submissions(cfg: AppConfig, *, enabled: bool = True, publish: bool = True) -> AppConfig:
    """``cfg`` pointed at the fake backend, with the two P12 switches set."""
    t = cfg.tunables
    sub = t.submissions.model_copy(update={"base_url": HttpUrl(BASE), "enabled": enabled})
    pub = t.publish.model_copy(update={"enabled": publish})
    return cfg.model_copy(
        update={"tunables": t.model_copy(update={"submissions": sub, "publish": pub})}
    )
