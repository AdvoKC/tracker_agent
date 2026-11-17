"""Monitor Agent (PRD §3.2): fetch one meter, snapshot changed promises, emit RawDiffs.

Order of work, all for ONE meter:
1. Cheap pass: list promises; fingerprint each; decide which need a detail fetch.
2. Detail pass: fetch + normalize + hash only those.
3. Decide everything in memory (no DB writes yet).
4. Anomaly guard: too many changes → write nothing at all.
5. Write snapshots (append-only), update the promise index, clear baseline flag.

Rules:
- Baseline (new meter, or a meter whose promises we've never seen): snapshots only, no diffs.
- Only published promises/updates count. An unpublished promise is ignored this run.
- A failed detail fetch keeps the old state; it is retried next run (no false diff).
- A listed-then-gone promise is reported in ``removed`` (logged/alerted, never written up).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from promise_monitor.api.client import ApiError, RateLimitedError, SchemaError
from promise_monitor.api.models import ApiPromiseDetail, MeterPage
from promise_monitor.config import MonitorConfig
from promise_monitor.db.models import Meter, PromiseIndex, PromiseSnapshot
from promise_monitor.domain.hashing import content_hash, fingerprint
from promise_monitor.domain.normalize import NormalizedPromise, normalize_promise
from promise_monitor.logging_setup import get_logger

log = get_logger(__name__)

MonitorStatus = Literal["ok", "baseline", "degraded", "skipped", "anomaly"]
# Normalized fields compared for ``fields_changed``; everything else is identity/bookkeeping.
DIFF_FIELDS = (
    "title",
    "description",
    "category",
    "status",
    "priority",
    "source_text",
    "promise_start_date",
    "promise_end_date",
    "blog_text",
    "blog_links",
    "updates",
)


class MonitorClient(Protocol):
    def meter_page(self, list_endpoint: str) -> MeterPage: ...
    def promise_detail(self, promise_id: uuid.UUID | str) -> ApiPromiseDetail: ...


@dataclass(frozen=True)
class RawDiff:
    promise_id: uuid.UUID
    prev_snapshot_id: int | None
    new_snapshot_id: int
    fields_changed: tuple[str, ...]
    added_update_ids: tuple[str, ...]
    removed_update_ids: tuple[str, ...]
    is_new_promise: bool


@dataclass
class MonitorResult:
    meter_id: uuid.UUID
    status: MonitorStatus
    diffs: list[RawDiff] = field(default_factory=list)
    removed: list[uuid.UUID] = field(default_factory=list)
    listed: int = 0
    details_fetched: int = 0
    snapshots_written: int = 0
    errors: list[str] = field(default_factory=list)
    change_ratio: float | None = None


@dataclass
class _Pending:
    promise_id: uuid.UUID
    fingerprint: str
    normalized: NormalizedPromise
    content_hash: str
    prev: PromiseIndex | None


def diff_contents(
    old: dict[str, object], new: dict[str, object]
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Return (fields_changed, added_update_ids, removed_update_ids)."""
    changed = tuple(f for f in DIFF_FIELDS if old.get(f) != new.get(f))
    old_ids = {u["update_id"] for u in _updates(old)}
    new_ids = {u["update_id"] for u in _updates(new)}
    added = tuple(u["update_id"] for u in _updates(new) if u["update_id"] not in old_ids)
    removed = tuple(u["update_id"] for u in _updates(old) if u["update_id"] not in new_ids)
    return changed, added, removed


def _updates(content: dict[str, object]) -> list[dict[str, str]]:
    updates = content.get("updates") or []
    assert isinstance(updates, list)
    return updates


def monitor_meter(
    session: Session,
    client: MonitorClient,
    meter: Meter,
    run_id: uuid.UUID,
    now: datetime,
    cfg: MonitorConfig,
    *,
    force_full: bool = False,
) -> MonitorResult:
    """Monitor one meter. Caller owns the transaction. Raises RateLimitedError to stop the run."""
    bound = log.bind(meter_id=str(meter.id), agent="monitor")
    result = MonitorResult(meter_id=meter.id, status="ok")

    # 1. cheap pass ------------------------------------------------------------------
    try:
        page = client.meter_page(meter.list_endpoint)
    except RateLimitedError:
        raise
    except (SchemaError, ApiError) as exc:
        result.status = "skipped"
        result.errors.append(str(exc))
        bound.error("meter_skipped", error=str(exc), schema=isinstance(exc, SchemaError))
        return result

    index = {
        pi.promise_id: pi
        for pi in session.scalars(select(PromiseIndex).where(PromiseIndex.meter_id == meter.id))
    }
    baseline = meter.baseline_pending or not any(pi.present for pi in index.values())
    listed_ids: set[uuid.UUID] = set()
    pending: list[_Pending] = []
    fingerprint_only: list[tuple[PromiseIndex, str]] = []
    restored: list[PromiseIndex] = []

    for item in page.promises:
        listed_ids.add(item.id)
        if not item.is_published:
            continue
        result.listed += 1
        fp = fingerprint(item)
        prev = index.get(item.id)
        if not (
            baseline or force_full or prev is None or not prev.present or prev.fingerprint != fp
        ):
            continue

        # 2. detail pass -------------------------------------------------------------
        try:
            detail = client.promise_detail(item.id)
        except RateLimitedError:
            raise
        except (SchemaError, ApiError) as exc:
            result.errors.append(str(exc))
            bound.warning("detail_failed", promise_id=str(item.id), error=str(exc))
            continue
        result.details_fetched += 1
        if not detail.is_published:
            continue
        normalized = normalize_promise(detail)
        h = content_hash(normalized.content())
        if prev is not None and prev.content_hash == h:
            if not prev.present:
                restored.append(prev)
            fingerprint_only.append((prev, fp))
            continue
        pending.append(_Pending(item.id, fp, normalized, h, prev))

    removed = [pi for pi in index.values() if pi.present and pi.promise_id not in listed_ids]

    # 3/4. anomaly guard (changes to already-known promises only) -----------------------
    if not baseline:
        known = sum(pi.present for pi in index.values())
        changed_known = sum(1 for p in pending if p.prev is not None and p.prev.present)
        result.change_ratio = changed_known / known if known else None
        if (
            result.change_ratio is not None
            and changed_known >= cfg.anomaly_min_changes
            and result.change_ratio > cfg.anomaly_change_ratio
        ):
            result.status = "anomaly"
            bound.error(
                "anomaly_guard_tripped",
                changed=changed_known,
                known=known,
                ratio=round(result.change_ratio, 3),
            )
            return result

    # 5. write ----------------------------------------------------------------------
    prev_contents = _load_contents(session, [p.prev.latest_snapshot_id for p in pending if p.prev])

    for p in pending:
        content = p.normalized.content()
        snap = PromiseSnapshot(
            promise_id=p.promise_id,
            meter_id=meter.id,
            run_id=run_id,
            captured_at=now,
            title=p.normalized.title,
            status=p.normalized.status,
            category=p.normalized.category,
            content=content,
            fingerprint=p.fingerprint,
            content_hash=p.content_hash,
        )
        session.add(snap)
        session.flush()  # assigns snapshot_id
        result.snapshots_written += 1

        if p.prev is None:
            session.add(
                PromiseIndex(
                    promise_id=p.promise_id,
                    meter_id=meter.id,
                    latest_snapshot_id=snap.snapshot_id,
                    content_hash=p.content_hash,
                    fingerprint=p.fingerprint,
                    present=True,
                    first_seen_at=now,
                    last_seen_at=now,
                )
            )
            if not baseline:
                result.diffs.append(
                    RawDiff(p.promise_id, None, snap.snapshot_id, DIFF_FIELDS, (), (), True)
                )
            continue

        prev_id = p.prev.latest_snapshot_id
        changed, added, removed_updates = diff_contents(prev_contents.get(prev_id, {}), content)
        p.prev.latest_snapshot_id = snap.snapshot_id
        p.prev.content_hash = p.content_hash
        p.prev.fingerprint = p.fingerprint
        p.prev.present = True
        p.prev.removed_at = None
        p.prev.last_seen_at = now
        if not baseline:
            result.diffs.append(
                RawDiff(
                    p.promise_id, prev_id, snap.snapshot_id, changed, added, removed_updates, False
                )
            )

    for pi, fp in fingerprint_only:
        pi.fingerprint = fp
        pi.last_seen_at = now

    for pi in restored:
        pi.present = True
        pi.removed_at = None
        bound.warning("promise_restored", promise_id=str(pi.promise_id))

    for pi in removed:
        pi.present = False
        pi.removed_at = now
        result.removed.append(pi.promise_id)
        bound.error("promise_removed", promise_id=str(pi.promise_id))

    if baseline:
        result.status = "baseline"
        if result.errors:
            bound.warning("baseline_incomplete", errors=len(result.errors))
        else:
            meter.baseline_pending = False
    elif result.errors:
        result.status = "degraded"

    session.flush()
    bound.info(
        "meter_monitored",
        status=result.status,
        listed=result.listed,
        details=result.details_fetched,
        snapshots=result.snapshots_written,
        diffs=len(result.diffs),
        removed=len(result.removed),
    )
    return result


def _load_contents(session: Session, snapshot_ids: list[int]) -> dict[int, dict[str, object]]:
    if not snapshot_ids:
        return {}
    rows = session.execute(
        select(PromiseSnapshot.snapshot_id, PromiseSnapshot.content).where(
            PromiseSnapshot.snapshot_id.in_(snapshot_ids)
        )
    )
    return {sid: content for sid, content in rows}
