"""pydantic models of raw promisetracker.ng API payloads.

Models are tolerant of known quirks (PRD §1): numeric strings, 0/1 booleans, "null"/"None"
strings, 1970-01-01 placeholder dates. Unknown keys are ignored, but every field we rely on
is required, so a real shape change fails validation instead of producing wrong data.
Staff data (``admin``/``admin_id``) is never modelled.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

MeterType = Literal["promise_tracker", "promise_ring"]

_NULLISH = {"", "null", "none", "nil", "undefined"}
_PLACEHOLDER_DATES = {date(1970, 1, 1)}


def _nullish(v: Any) -> Any:
    if isinstance(v, str) and v.strip().lower() in _NULLISH:
        return None
    return v


def _to_bool(v: Any) -> bool:
    v = _nullish(v)
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, int | float):
        return bool(v)
    if isinstance(v, str) and v.strip() in {"0", "1"}:
        return v.strip() == "1"
    if isinstance(v, str) and v.strip().lower() in {"true", "false"}:
        return v.strip().lower() == "true"
    raise ValueError(f"not a boolean flag: {v!r}")


def _to_date(v: Any) -> Any:
    v = _nullish(v)
    if v is None:
        return None
    if isinstance(v, str):
        v = v.strip()[:10]  # tolerate 'YYYY-MM-DD HH:MM:SS' and ISO datetimes
    d = date.fromisoformat(v) if isinstance(v, str) else v
    return None if d in _PLACEHOLDER_DATES else d


def _to_int(v: Any) -> Any:
    v = _nullish(v)
    if v is None:
        return 0
    if isinstance(v, str):
        return int(v.strip())
    return v


Flag = Annotated[bool, BeforeValidator(_to_bool)]
OptDate = Annotated[date | None, BeforeValidator(_to_date)]
Count = Annotated[int, BeforeValidator(_to_int), Field(ge=0)]
OptStr = Annotated[str | None, BeforeValidator(_nullish)]
OptUUID = Annotated[UUID | None, BeforeValidator(_nullish)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class Envelope(_Model):
    success: bool
    data: Any = None
    message: str | None = None


class MeterSummary(_Model):
    """Meter as it appears in ``all_meters`` / tenures / ring lists (no ``meter_type``)."""

    id: UUID
    name: str
    meter_name: OptStr = None
    tenure_date_start: OptDate = None
    tenure_date_end: OptDate = None
    is_archived: Flag = False
    parent_meter_id: OptUUID = None
    state_id: OptUUID = None
    chamber: OptStr = None
    promises_count: Count | None = None


class MeterFull(MeterSummary):
    """``current_meter`` objects carry the type and slug."""

    meter_type: MeterType
    slug: OptStr = None
    description: OptStr = None


Status = Literal["kept", "broken", "stalled", "in_the_works", "compromised", "not_yet_rated"]


class ApiPromise(_Model):
    """A promise as listed on a meter page. Raw strings are kept for fingerprinting."""

    id: UUID
    meter_id: UUID
    status: Status
    last_published_status: Status | None = None
    title: str
    source: OptStr = None
    subject_area: OptStr = None
    promise_blog: OptStr = None
    priority: OptStr = None
    description: OptStr = None
    promise_start_date: OptDate = None
    promise_end_date: OptDate = None
    update_count: Count = 0
    last_updated_at: OptStr = None  # 'YYYY-MM-DD HH:MM:SS' (kept raw)
    updated_at: OptStr = None  # ISO-8601 (kept raw)
    published_status: OptStr = None

    @property
    def is_published(self) -> bool:
        return (self.published_status or "").lower() == "published"

    @property
    def published_rating(self) -> Status:
        """The rating the public sees. ``status`` may hold an unpublished draft rating."""
        return self.last_published_status or self.status


# Admin-form placeholders seen in real update rows (e.g. House promise 7845cb92, 2026-11-17).
_STATUS_PLACEHOLDERS = {"select option", "select status", "select", "choose", "--"}


def _update_status(v: Any) -> Any:
    v = _nullish(v)
    if isinstance(v, str) and v.strip().lower() in _STATUS_PLACEHOLDERS:
        return None
    return v


class ApiUpdate(_Model):
    """One entry of a promise's evidence timeline.

    Update-level status is informational (the promise's own rating is authoritative), so
    form placeholders become None. Genuinely unknown values still fail validation.
    """

    id: UUID
    blog: OptStr = None
    status: Annotated[Status | None, BeforeValidator(_update_status)] = None
    update_type: OptStr = None
    is_published: Flag = False
    created_at: str
    rating_source_url: OptStr = None


class ApiPromiseDetail(ApiPromise):
    """Response of /view/promise/{id}."""

    updates: list[ApiUpdate] = Field(default_factory=list)


class MeterPage(_Model):
    """Response of /presidential, /legislative/*, /state/{name}, /meter/{id}/... ."""

    current_meter: MeterFull | None = None
    all_meters: list[MeterSummary] = Field(default_factory=list)
    promise_count: Count = 0
    promises: list[ApiPromise] = Field(default_factory=list)


class RingState(_Model):
    """One entry of /all/promise-ring/states."""

    id: UUID
    name: str
    meter: list[MeterSummary] = Field(default_factory=list)
