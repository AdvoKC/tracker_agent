"""ORM models. Must match the JSON Schemas in docs/PRD.md §4 (update both together)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from promise_monitor.db.base import Base

METER_TYPES = ("promise_tracker", "promise_ring")
SCOPES = ("presidential", "senate", "house", "state")
REGISTRY_STATUSES = ("active", "archived", "missing")

JSON_TYPE = JSON().with_variant(JSONB(), "postgresql")


def _in(col: str, values: tuple[str, ...]) -> str:
    return f"{col} IN ({', '.join(repr(v) for v in values)})"


class Meter(Base):
    """Meter registry (PRD §4.1). Rows are never deleted; vanished meters become ``missing``."""

    __tablename__ = "meters"
    __table_args__ = (
        CheckConstraint(_in("meter_type", METER_TYPES), name="meter_type"),
        CheckConstraint(_in("scope", SCOPES), name="scope"),
        CheckConstraint(_in("registry_status", REGISTRY_STATUSES), name="registry_status"),
        CheckConstraint("(scope = 'state') = (state_name IS NOT NULL)", name="state_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    meter_type: Mapped[str] = mapped_column(String(32))
    scope: Mapped[str] = mapped_column(String(16))
    state_name: Mapped[str | None] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(255))
    meter_name: Mapped[str | None] = mapped_column(String(255))
    slug: Mapped[str | None] = mapped_column(String(255))
    official_name: Mapped[str | None] = mapped_column(String(255))
    parent_meter_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    tenure_start: Mapped[date | None] = mapped_column(Date)
    tenure_end: Mapped[date | None] = mapped_column(Date)
    is_current: Mapped[bool] = mapped_column(Boolean)
    registry_status: Mapped[str] = mapped_column(String(16), index=True)
    baseline_pending: Mapped[bool] = mapped_column(Boolean, default=True)
    priority_rank: Mapped[int] = mapped_column(Integer, default=100)
    source_key: Mapped[str] = mapped_column(String(80))
    list_endpoint: Mapped[str] = mapped_column(Text)
    public_url: Mapped[str] = mapped_column(Text)
    coordinator_email: Mapped[str | None] = mapped_column(String(255))
    promise_count: Mapped[int] = mapped_column(Integer, default=0)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


RUN_STATUSES = ("running", "ok", "degraded", "failed")


class Run(Base):
    """One pipeline invocation (PRD §4.5)."""

    __tablename__ = "runs"
    __table_args__ = (CheckConstraint(_in("status", RUN_STATUSES), name="status"),)

    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="running")
    trigger: Mapped[str] = mapped_column(String(32))
    stats: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict)


class PromiseSnapshot(Base):
    """Append-only promise content history (PRD §4.2). A row exists per content change.

    Scalar columns are for querying; ``content`` is the full normalized promise
    (see domain/normalize.py) and is what diffs are computed from.
    """

    __tablename__ = "promise_snapshots"

    snapshot_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    promise_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    meter_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("meters.id"), index=True)
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("runs.run_id"))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    title: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32))
    category: Mapped[str | None] = mapped_column(String(128))
    content: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE)
    fingerprint: Mapped[str] = mapped_column(String(64))
    content_hash: Mapped[str] = mapped_column(String(64))


class PromiseIndex(Base):
    """Mutable per-promise cursor: latest snapshot, last fingerprint, presence on the meter.

    Kept separate so ``promise_snapshots`` stays strictly append-only.
    """

    __tablename__ = "promise_index"

    promise_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    meter_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("meters.id"), index=True)
    latest_snapshot_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("promise_snapshots.snapshot_id")
    )
    content_hash: Mapped[str] = mapped_column(String(64))
    fingerprint: Mapped[str] = mapped_column(String(64))
    present: Mapped[bool] = mapped_column(Boolean, default=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


CHANGE_TYPES = (
    "status_change",
    "new_evidence",
    "new_promise",
    "news_update",
    "cosmetic",
    "substantive_edit",
    "promise_removed",
)
EVENT_STATES = (
    "logged",
    "queued",
    "needs_sources",
    "drafted",
    "in_review",
    "approved",
    "rejected",
    "stale",
    "published",
)
ARTICLE_TYPES = ("status_change", "new_evidence", "new_promise", "news_update")


class ChangeEvent(Base):
    """A classified change (PRD §4.3). ``dedupe_key`` UNIQUE = the never-report-twice guarantee."""

    __tablename__ = "change_events"
    __table_args__ = (
        CheckConstraint(_in("change_type", CHANGE_TYPES), name="change_type"),
        CheckConstraint(_in("state", EVENT_STATES), name="state"),
        CheckConstraint("score BETWEEN 0 AND 100", name="score"),
    )

    event_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("runs.run_id"), index=True)
    meter_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("meters.id"), index=True)
    promise_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    change_type: Mapped[str] = mapped_column(String(32))
    prev_snapshot_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("promise_snapshots.snapshot_id")
    )
    new_snapshot_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("promise_snapshots.snapshot_id")
    )
    news_item_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("news_items.id"))
    old_status: Mapped[str | None] = mapped_column(String(32))
    new_status: Mapped[str | None] = mapped_column(String(32))
    added_update_ids: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list)
    fields_changed: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list)
    edit_distance: Mapped[float | None] = mapped_column()
    score: Mapped[int] = mapped_column(Integer, default=0)
    score_base: Mapped[int] = mapped_column(Integer, default=0)
    score_llm_delta: Mapped[int] = mapped_column(Integer, default=0)
    score_reason: Mapped[str] = mapped_column(Text, default="")
    evidence_lead: Mapped[bool] = mapped_column(Boolean, default=False)
    article_group_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    dedupe_key: Mapped[str] = mapped_column(String(64), unique=True)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(16), index=True)


class LLMCall(Base):
    """Audit row for every LLM call, successful or not (PRD §4.5)."""

    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("runs.run_id"), index=True)
    agent: Mapped[str] = mapped_column(String(32))
    prompt_name: Mapped[str] = mapped_column(String(64))
    subject_id: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(64))
    prompt_sha256: Mapped[str] = mapped_column(String(64))
    output: Mapped[dict[str, Any] | None] = mapped_column(JSON_TYPE)
    error: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int] = mapped_column(Integer)
    cost_usd: Mapped[float | None] = mapped_column()
    ok: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


NEWS_ITEM_STATES = ("accepted", "rejected")


class NewsItem(Base):
    """Every result the News Scout returned, accepted or not, with the reason (audit trail)."""

    __tablename__ = "news_items"
    __table_args__ = (
        CheckConstraint(_in("state", NEWS_ITEM_STATES), name="state"),
        UniqueConstraint("promise_id", "url", name="uq_news_items_promise_url"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("runs.run_id"), index=True)
    promise_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    meter_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("meters.id"))
    url: Mapped[str] = mapped_column(Text)  # normalized
    outlet: Mapped[str | None] = mapped_column(String(128))
    headline: Mapped[str] = mapped_column(Text)
    published_date: Mapped[date | None] = mapped_column(Date)
    summary: Mapped[str] = mapped_column(Text)
    development_type: Mapped[str | None] = mapped_column(String(64))
    is_concrete: Mapped[bool] = mapped_column(Boolean)
    relevance: Mapped[float] = mapped_column()
    state: Mapped[str] = mapped_column(String(16))
    reject_reason: Mapped[str | None] = mapped_column(String(64))
    found_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class NewsSearchState(Base):
    """Rotation cursor: least-recently-searched promises go first."""

    __tablename__ = "news_search_state"

    promise_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    last_searched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_run_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("runs.run_id"))
