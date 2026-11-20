"""Shared dashboard plumbing: context, DB session, auth/permission dependencies, CSRF,
flash messages, page rendering and small helpers. Routers live in ``dashboard/routes_*.py``."""

from __future__ import annotations

import re
import secrets
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import Depends, Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, sessionmaker
from starlette.datastructures import FormData

from promise_monitor import access
from promise_monitor.access import AccessError
from promise_monitor.config import AppConfig, Settings
from promise_monitor.db.models import Meter, User
from promise_monitor.jobs import Launcher, default_launcher
from promise_monitor.settings_store import effective_config
from promise_monitor.submissions import ClientFactory, TokenBox

MAX_FAILS, LOCK_SECONDS = 5, 15 * 60
_DOMAIN = re.compile(r"^(?=.{4,253}$)([a-z0-9-]{1,63}\.)+[a-z]{2,63}$")


class RedirectError(Exception):
    def __init__(self, url: str) -> None:
        self.url = url


class NotFoundError(Exception):
    pass


@dataclass
class Throttle:
    fails: dict[str, list[float]] = field(default_factory=dict)

    def locked(self, key: str, now: float) -> bool:
        recent = [t for t in self.fails.get(key, []) if now - t < LOCK_SECONDS]
        self.fails[key] = recent
        return len(recent) >= MAX_FAILS

    def fail(self, key: str, now: float) -> None:
        self.fails.setdefault(key, []).append(now)

    def clear(self, key: str) -> None:
        self.fails.pop(key, None)


@dataclass
class Ctx:
    factory: sessionmaker[Session]
    base_cfg: AppConfig
    settings: Settings
    clock: Callable[[], datetime]
    templates: Jinja2Templates
    tz: ZoneInfo
    throttle: Throttle = field(default_factory=Throttle)
    launcher: Launcher = default_launcher
    jobs_wait: bool = False  # tests: run jobs synchronously
    token_box: TokenBox | None = None  # PM_TOKEN_KEY; None = tokens can't be stored
    submission_client: ClientFactory | None = None  # token → SubmissionClient (P12)

    def cfg(self, s: Session) -> AppConfig:
        return effective_config(s, self.base_cfg)

    def local(self, dt: datetime | None, fmt: str = "%d %b %Y %H:%M") -> str:
        if dt is None:
            return ""
        aware = dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        return aware.astimezone(self.tz).strftime(fmt)

    def parse_local(self, raw: str) -> datetime:
        try:
            return datetime.fromisoformat(raw).replace(tzinfo=self.tz).astimezone(UTC)
        except ValueError as exc:
            raise AccessError("invalid date/time") from exc


def get_ctx(request: Request) -> Ctx:
    ctx: Ctx = request.app.state.ctx
    return ctx


def db(request: Request) -> Iterator[Session]:
    with get_ctx(request).factory() as s:
        yield s


def csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(32)
        request.session["csrf"] = tok
    return str(tok)


def flash(request: Request, kind: str, msg: str) -> None:
    request.session.setdefault("flash", []).append([kind, msg])


def page(
    request: Request, name: str, user: User | None, data: dict[str, Any], status: int = 200
) -> Response:
    ctx = get_ctx(request)
    flashes = request.session.pop("flash", [])
    return ctx.templates.TemplateResponse(
        request,
        name,
        {"user": user, "csrf": csrf_token(request), "flashes": flashes, "local": ctx.local, **data},
        status_code=status,
    )


async def form(request: Request) -> FormData:
    data = await request.form()
    sent = str(data.get("csrf", ""))
    expected = str(request.session.get("csrf", ""))
    if not expected or not secrets.compare_digest(sent, expected):
        raise RedirectError("/login?csrf=1")
    return data


def field_str(data: FormData, name: str) -> str:
    v = data.get(name, "")
    return v if isinstance(v, str) else ""


def current_user(request: Request, s: Session = Depends(db)) -> User:
    uid = request.session.get("uid")
    try:
        user = s.get(User, uuid.UUID(uid)) if uid else None
    except ValueError:
        user = None
    if user is None or not user.active:
        request.session.pop("uid", None)
        raise RedirectError("/login")
    if user.must_change_password and request.url.path not in ("/account/password", "/logout"):
        raise RedirectError("/account/password")
    return user


def superadmin(user: User = Depends(current_user)) -> User:
    if user.role != "superadmin":
        raise NotFoundError
    return user


def safe_back(raw: str, default: str) -> str:
    """Only same-site relative paths (no open redirects)."""
    return raw if raw.startswith("/") and not raw.startswith("//") and "\\" not in raw else default


def to_uuid(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise NotFoundError from exc


def visible(s: Session, user: User, ctx: Ctx) -> set[uuid.UUID] | None:
    """None = all meters (superadmin)."""
    return access.visible_meter_ids(s, user, ctx.clock())


def visible_meter(s: Session, user: User, ctx: Ctx, meter_id: uuid.UUID) -> Meter:
    meter = s.get(Meter, meter_id)
    if meter is None or not access.can_view(s, user, meter_id, ctx.clock()):
        raise NotFoundError
    return meter


def where(m: Meter) -> str:
    return {
        "presidential": "Federal · President",
        "senate": "Federal · Senate",
        "house": "Federal · House of Reps",
    }.get(m.scope, f"{m.state_name} State")


def parse_lines(raw: str, *, max_items: int = 30, max_len: int = 80) -> list[str]:
    out: list[str] = []
    for line in raw.splitlines():
        item = " ".join(line.split())[:max_len]
        if item and item.lower() not in {x.lower() for x in out}:
            out.append(item)
    return out[:max_items]


def parse_outlets(raw: str, max_items: int = 20) -> list[dict[str, str]]:
    outlets: list[dict[str, str]] = []
    for n, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        domain, _, name = line.partition("|")
        domain = (
            domain.strip()
            .lower()
            .removeprefix("https://")
            .removeprefix("http://")
            .removeprefix("www.")
        )
        domain = domain.split("/")[0]
        if not _DOMAIN.match(domain):
            raise AccessError(f"outlet line {n}: '{domain}' is not a valid domain")
        if domain.endswith("promisetracker.ng"):
            raise AccessError("promisetracker.ng is not a news outlet")
        if domain in {o["domain"] for o in outlets}:
            continue
        outlets.append({"domain": domain, "name": " ".join(name.split())[:80] or domain})
    if len(outlets) > max_items:
        raise AccessError(f"at most {max_items} outlets")
    return outlets
