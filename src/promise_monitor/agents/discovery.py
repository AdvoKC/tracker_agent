"""Discovery Agent (PRD §3.1): build and refresh the meter registry.

Two steps, kept separate so each is testable:

1. ``collect``: network only. Walks every discovery endpoint and merges what it sees by meter id.
2. ``apply_discovery``: DB only. Upserts the registry, marks vanished meters ``missing``.

API facts this relies on (verified 2026-11-16):
- ``all_meters`` entries do NOT carry ``meter_type`` and a state's list mixes both families,
  so the family comes from ``current_meter``, the per-kind tenures endpoints, or the ring list.
- A state with no current promise_tracker meter returns an empty ``all_meters`` even when it
  has ring meters (e.g. Ekiti), so tenures + ring list are required, not optional.
- ``meter/{id}/state/{State}`` serves both families, so it is every state meter's list endpoint.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Literal, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from promise_monitor.api.client import ApiError, Chamber, RateLimitedError
from promise_monitor.api.models import MeterPage, MeterSummary, MeterType, RingState
from promise_monitor.config import AppConfig
from promise_monitor.db.models import Meter
from promise_monitor.logging_setup import get_logger

log = get_logger(__name__)

Scope = Literal["presidential", "senate", "house", "state"]
RING_LIST_SOURCE = "ring_states"

_FEDERAL: tuple[tuple[Scope, Chamber | None], ...] = (
    ("presidential", None),
    ("senate", "upper-chamber"),
    ("house", "lower-chamber"),
)
_FEDERAL_ROUTES: dict[Scope, str] = {
    "presidential": "presidentialTracker",
    "senate": "legislativeTracker/senate",
    "house": "legislativeTracker/representative",
}


class DiscoveryClient(Protocol):
    def presidential(self) -> MeterPage: ...
    def legislative(self, chamber: Chamber) -> MeterPage: ...
    def state(self, name: str) -> MeterPage: ...
    def state_tenures(self, name: str, kind: MeterType) -> list[MeterSummary]: ...
    def promise_ring_states(self) -> list[RingState]: ...


class SiteDownError(RuntimeError):
    """Every discovery source failed: the run is ``failed`` (PRD §3.1)."""


@dataclass(frozen=True)
class DiscoveredMeter:
    id: uuid.UUID
    scope: Scope
    state_name: str | None
    name: str
    meter_type: MeterType | None = None
    meter_name: str | None = None
    slug: str | None = None
    parent_meter_id: uuid.UUID | None = None
    tenure_start: date | None = None
    tenure_end: date | None = None
    is_archived: bool = False
    promise_count: int | None = None

    @property
    def source_key(self) -> str:
        return self.scope if self.state_name is None else f"state:{self.state_name}"

    def merged(self, other: DiscoveredMeter) -> DiscoveredMeter:
        """Fill gaps from ``other``; explicit values already present win."""
        if (self.meter_type and other.meter_type) and self.meter_type != other.meter_type:
            log.warning("meter_type_conflict", meter_id=str(self.id))
        return replace(
            self,
            meter_type=self.meter_type or other.meter_type,
            meter_name=self.meter_name or other.meter_name,
            slug=self.slug or other.slug,
            parent_meter_id=self.parent_meter_id or other.parent_meter_id,
            tenure_start=self.tenure_start or other.tenure_start,
            tenure_end=self.tenure_end or other.tenure_end,
            is_archived=self.is_archived or other.is_archived,
            promise_count=self.promise_count
            if self.promise_count is not None
            else other.promise_count,
        )


@dataclass
class CollectResult:
    meters: dict[uuid.UUID, DiscoveredMeter] = field(default_factory=dict)
    ok_sources: set[str] = field(default_factory=set)
    failed_sources: dict[str, str] = field(default_factory=dict)
    rate_limited: bool = False

    def add(self, m: DiscoveredMeter) -> None:
        existing = self.meters.get(m.id)
        self.meters[m.id] = m if existing is None else existing.merged(m)


def _from_summary(
    s: MeterSummary,
    scope: Scope,
    state: str | None,
    meter_type: MeterType | None,
) -> DiscoveredMeter:
    return DiscoveredMeter(
        id=s.id,
        scope=scope,
        state_name=state,
        name=s.name,
        meter_type=meter_type,
        meter_name=s.meter_name,
        parent_meter_id=s.parent_meter_id,
        tenure_start=s.tenure_date_start,
        tenure_end=s.tenure_date_end,
        is_archived=s.is_archived,
        promise_count=s.promises_count,
    )


def _from_page(
    page: MeterPage, scope: Scope, state: str | None, default_type: MeterType | None
) -> list[DiscoveredMeter]:
    out: list[DiscoveredMeter] = []
    cm = page.current_meter
    if cm is not None:
        out.append(
            replace(
                _from_summary(cm, scope, state, cm.meter_type),
                slug=cm.slug,
                promise_count=page.promise_count,
            )
        )
    out.extend(_from_summary(s, scope, state, default_type) for s in page.all_meters)
    return out


def collect(client: DiscoveryClient, states: Iterable[str]) -> CollectResult:
    """Walk every discovery endpoint. Never raises for a single failing source."""
    result = CollectResult()

    def guarded(source: str) -> _Guard:
        return _Guard(result, source)

    for scope, chamber in _FEDERAL:
        with guarded(scope):
            if result.rate_limited:
                continue
            page = client.presidential() if chamber is None else client.legislative(chamber)
            for m in _from_page(page, scope, None, "promise_tracker"):
                result.add(m)
            result.ok_sources.add(scope)

    for name in states:
        key = f"state:{name}"
        with guarded(key):
            if result.rate_limited:
                continue
            found = _from_page(client.state(name), "state", name, None)
            for kind in ("promise_tracker", "promise_ring"):
                found += [
                    _from_summary(s, "state", name, kind) for s in client.state_tenures(name, kind)
                ]
            for m in found:
                result.add(m)
            result.ok_sources.add(key)

    with guarded(RING_LIST_SOURCE):
        if not result.rate_limited:
            for ring_state in client.promise_ring_states():
                for s in ring_state.meter:
                    result.add(_from_summary(s, "state", ring_state.name, "promise_ring"))
            result.ok_sources.add(RING_LIST_SOURCE)

    for m in list(result.meters.values()):
        if m.meter_type is None:
            # Seen only in an untyped all_meters list. Every state meter should also appear in
            # a typed tenures list, so this means the API changed: flag it loudly.
            log.warning("meter_type_inferred", meter_id=str(m.id), source=m.source_key)
            result.meters[m.id] = replace(m, meter_type="promise_tracker")

    return result


class _Guard:
    """Context manager: record a source as failed instead of aborting discovery."""

    def __init__(self, result: CollectResult, source: str) -> None:
        self._result = result
        self._source = source

    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc_info: object) -> bool:
        exc = exc_info[1]
        if not isinstance(exc, Exception):
            return False
        if isinstance(exc, RateLimitedError):
            self._result.rate_limited = True
        if isinstance(exc, ApiError):
            self._result.failed_sources[self._source] = str(exc)
            log.error("discovery_source_failed", source=self._source, error=str(exc))
            return True
        return False


# --------------------------------------------------------------------------- registry


@dataclass
class DiscoveryResult:
    status: Literal["ok", "degraded"]
    new: list[uuid.UUID] = field(default_factory=list)
    updated: list[uuid.UUID] = field(default_factory=list)
    missing: list[uuid.UUID] = field(default_factory=list)
    reappeared: list[uuid.UUID] = field(default_factory=list)
    active: list[uuid.UUID] = field(default_factory=list)  # priority order
    failed_sources: dict[str, str] = field(default_factory=dict)


def priority_key(m: DiscoveredMeter) -> str:
    if m.scope != "state":
        return m.scope
    prefix = "ring" if m.meter_type == "promise_ring" else "state"
    return f"{prefix}:{m.state_name}"


def list_endpoint(m: DiscoveredMeter) -> str:
    if m.scope == "presidential":
        return f"meter/{m.id}/presidential"
    if m.scope == "senate":
        return f"meter/{m.id}/legislative/upper-chamber"
    if m.scope == "house":
        return f"meter/{m.id}/legislative/lower-chamber"
    return f"meter/{m.id}/state/{m.state_name}"


def public_url(m: DiscoveredMeter, site: str) -> str:
    site = site.rstrip("/")
    if m.slug:
        return f"{site}/meter/{m.slug}"
    if m.scope != "state":
        return f"{site}/{_FEDERAL_ROUTES[m.scope]}"
    if m.meter_type == "promise_ring":
        return f"{site}/promiseRing/{m.state_name}"
    return f"{site}/stateTracker/{(m.state_name or '').lower()}"


def _row_values(m: DiscoveredMeter, cfg: AppConfig) -> dict[str, object]:
    return {
        "meter_type": m.meter_type,
        "scope": m.scope,
        "state_name": m.state_name,
        "name": m.name,
        "meter_name": m.meter_name,
        "slug": m.slug,
        "official_name": None if m.scope in ("senate", "house") else m.name,
        "parent_meter_id": m.parent_meter_id,
        "tenure_start": m.tenure_start,
        "tenure_end": m.tenure_end,
        "is_current": not m.is_archived,
        "registry_status": "archived" if m.is_archived else "active",
        "priority_rank": cfg.priority.rank_for(priority_key(m)),
        "source_key": m.source_key,
        "list_endpoint": list_endpoint(m),
        "public_url": public_url(m, str(cfg.tunables.api.public_site_url)),
        "promise_count": m.promise_count or 0,
    }


def apply_discovery(
    session: Session, collected: CollectResult, cfg: AppConfig, now: datetime
) -> DiscoveryResult:
    """Upsert the registry from ``collected``. Caller owns the transaction."""
    if not collected.ok_sources:
        raise SiteDownError(f"all discovery sources failed: {sorted(collected.failed_sources)}")

    result = DiscoveryResult(
        status="degraded" if collected.failed_sources else "ok",
        failed_sources=dict(collected.failed_sources),
    )
    existing = {m.id: m for m in session.scalars(select(Meter))}

    for meter_id, found in collected.meters.items():
        values = _row_values(found, cfg)
        row = existing.get(meter_id)
        if row is None:
            session.add(
                Meter(
                    id=meter_id,
                    baseline_pending=True,
                    first_seen_at=now,
                    last_seen_at=now,
                    updated_at=now,
                    **values,
                )
            )
            result.new.append(meter_id)
            log.info("meter_new", meter_id=str(meter_id), name=found.name, source=found.source_key)
            continue
        if row.registry_status == "missing":
            result.reappeared.append(meter_id)
            log.warning("meter_reappeared", meter_id=str(meter_id))
        changed = {k: v for k, v in values.items() if getattr(row, k) != v}
        for k, v in changed.items():
            setattr(row, k, v)
        row.last_seen_at = now
        if changed:
            row.updated_at = now
            result.updated.append(meter_id)
            log.info("meter_updated", meter_id=str(meter_id), fields=sorted(changed))

    # A meter is missing only if the source that normally lists it succeeded this run.
    for meter_id, row in existing.items():
        if meter_id in collected.meters or row.registry_status == "missing":
            continue
        if row.source_key in collected.ok_sources:
            row.registry_status = "missing"
            row.updated_at = now
            result.missing.append(meter_id)
            log.error("meter_missing", meter_id=str(meter_id), name=row.name)

    session.flush()
    active = session.scalars(
        select(Meter)
        .where(Meter.registry_status == "active")
        .order_by(Meter.priority_rank, Meter.name, Meter.id)
    )
    result.active = [m.id for m in active]
    return result
