"""Admin dashboard (P10/P11): review, activity, meters, users and operations.

Security:
- every page except /login requires a signed-in, active user; coordinators get 404 for
  meters (and their drafts, searches, sources) they aren't assigned to;
- signed session cookie (HttpOnly, SameSite=Strict, Secure when ``dashboard_https``), 8h;
- CSRF token on every POST; strict CSP (same-origin script/style only, nothing inline);
  no framing;
- login lockout: 5 failures per email (or per client) → 15 minutes;
- every change written to ``audit_log``; operations run a fixed whitelist of commands only.
Default bind is 127.0.0.1; put it behind HTTPS before exposing it anywhere (VPS, later).
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, sessionmaker
from starlette.middleware.sessions import SessionMiddleware

from promise_monitor.config import AppConfig, Settings
from promise_monitor.dashboard import (
    routes_activity,
    routes_admin,
    routes_auth,
    routes_meters,
    routes_review,
    routes_submissions,
)
from promise_monitor.dashboard.core import (
    Ctx,
    NotFoundError,
    RedirectError,
    page,
    parse_lines,
    parse_outlets,
)
from promise_monitor.jobs import Launcher, default_launcher, recover_lost
from promise_monitor.logging_setup import get_logger
from promise_monitor.submissions import ClientFactory, TokenBox, default_client_factory, token_box

__all__ = ["create_app", "parse_lines", "parse_outlets"]

log = get_logger(__name__)
HERE = Path(__file__).resolve().parent


def create_app(
    session_factory: sessionmaker[Session],
    cfg: AppConfig,
    settings: Settings,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    launcher: Launcher = default_launcher,
    jobs_wait: bool = False,
    submission_client: ClientFactory | None = None,
) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    secret = settings.dashboard_secret or secrets.token_urlsafe(48)
    if not settings.dashboard_secret:
        log.warning(
            "dashboard_secret_missing", note="sessions reset on restart; set PM_DASHBOARD_SECRET"
        )
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret,
        session_cookie="pm_session",
        max_age=8 * 3600,
        same_site="strict",
        https_only=settings.dashboard_https,
    )
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    box: TokenBox | None = None
    try:
        box = token_box(settings)
    except ValueError:
        log.error("token_key_invalid", note="PM_TOKEN_KEY is not a valid Fernet key")
    app.state.ctx = Ctx(
        factory=session_factory,
        base_cfg=cfg,
        settings=settings,
        clock=clock,
        templates=templates,
        tz=ZoneInfo(cfg.tunables.timezone_display),
        launcher=launcher,
        jobs_wait=jobs_wait,
        token_box=box,
        submission_client=submission_client or default_client_factory(cfg, settings.admin_email),
    )
    with session_factory() as s:
        lost = recover_lost(s, clock())
        if lost:
            log.warning("jobs_marked_lost", count=lost)

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Callable[[Request], Any]) -> Response:
        response: Response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
            "form-action 'self'; "
            "frame-ancestors 'none'; base-uri 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RedirectError)
    async def _redirect(_r: Request, exc: RedirectError) -> Response:
        return RedirectResponse(exc.url, status_code=303)

    @app.exception_handler(NotFoundError)
    async def _nf(request: Request, _exc: NotFoundError) -> Response:
        return page(
            request, "error.html", None, {"title": "Not found", "message": "Nothing here."}, 404
        )

    for module in (
        routes_auth,
        routes_activity,
        routes_meters,
        routes_review,
        routes_submissions,
        routes_admin,
    ):
        app.include_router(module.router)
    return app
