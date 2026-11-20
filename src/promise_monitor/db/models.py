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
    coordinator_name: Mapped[str | None] = mapped_column(String(255))
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
    "coordinator_update",
    "own_submission",  # our own update, published by Promise Tracker (§8d): never reported
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
ARTICLE_TYPES = (
    "status_change",
    "new_evidence",
    "new_promise",
    "news_update",
    "coordinator_update",
)


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
    # coordinator_update only: the coordinator's description, their source links, who asked.
    brief: Mapped[str | None] = mapped_column(Text)
    brief_urls: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.user_id"))


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
    search_grounded: Mapped[bool] = mapped_column(Boolean, default=False)
    search_title: Mapped[str | None] = mapped_column(Text)  # title from the RAW search result
    state: Mapped[str] = mapped_column(String(16))
    reject_reason: Mapped[str | None] = mapped_column(String(64))
    found_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class NewsSearchState(Base):
    """Rotation cursor: least-recently-searched promises go first."""

    __tablename__ = "news_search_state"

    promise_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    last_searched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_run_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("runs.run_id"))


FETCH_STATUSES = ("ok", "blocked", "paywalled", "broken", "unsupported", "unsafe", "error")


class Source(Base):
    """Fetch cache per normalized URL (PRD §4.5). Relevance is per group, not stored here."""

    __tablename__ = "sources"
    __table_args__ = (CheckConstraint(_in("fetch_status", FETCH_STATUSES), name="fetch_status"),)

    source_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    url: Mapped[str] = mapped_column(Text, unique=True)
    final_url: Mapped[str | None] = mapped_column(Text)
    http_status: Mapped[int | None] = mapped_column(Integer)
    fetch_status: Mapped[str] = mapped_column(String(16))
    content_type: Mapped[str | None] = mapped_column(String(128))
    title: Mapped[str | None] = mapped_column(Text)
    publisher: Mapped[str | None] = mapped_column(String(255))
    published_date: Mapped[date | None] = mapped_column(Date)
    snippet: Mapped[str | None] = mapped_column(Text)
    search_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    search_title: Mapped[str | None] = mapped_column(Text)
    search_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


GROUP_STATES = (
    "queued",
    "verified",
    "needs_sources",
    "drafted",
    "in_review",
    "published",
    "rejected",
)


class ArticleGroup(Base):
    """The unit an article is written for: one promise, one run's qualifying events."""

    __tablename__ = "article_groups"
    __table_args__ = (CheckConstraint(_in("state", GROUP_STATES), name="state"),)

    group_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    meter_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("meters.id"), index=True)
    promise_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    state: Mapped[str] = mapped_column(String(16), index=True)
    sufficient: Mapped[bool | None] = mapped_column(Boolean)
    notes: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # lead row in Sheet


VERDICTS = ("verified", "verified_partial", "unverifiable", "broken")
SOURCE_ROLES = ("promise_page", "required", "context")


class ArticleGroupSource(Base):
    """A source's verdict for one article group (relevance depends on the promise)."""

    __tablename__ = "article_group_sources"
    __table_args__ = (
        CheckConstraint(_in("verdict", VERDICTS), name="verdict"),
        CheckConstraint(_in("role", SOURCE_ROLES), name="role"),
    )

    group_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("article_groups.group_id"), primary_key=True
    )
    source_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("sources.source_id"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(16))
    event_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)  # the event this source backs
    relevant: Mapped[bool | None] = mapped_column(Boolean)
    relevance_confidence: Mapped[float | None] = mapped_column()
    reason: Mapped[str | None] = mapped_column(Text)
    verdict: Mapped[str] = mapped_column(String(20))
    citable: Mapped[bool] = mapped_column(Boolean)
    # A coordinator opened a page that blocked the agent and confirmed it (kept on re-verify).
    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.user_id"))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_title: Mapped[str | None] = mapped_column(Text)  # headline as the coordinator saw it


ARTICLE_STATES = ("in_review", "needs_attention", "approved", "rejected", "stale", "published")


class Article(Base):
    """A drafted article (PRD §4.4). One per article group, ever (UNIQUE)."""

    __tablename__ = "articles"
    __table_args__ = (CheckConstraint(_in("state", ARTICLE_STATES), name="state"),)

    article_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    article_group_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("article_groups.group_id"), unique=True
    )
    meter_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("meters.id"), index=True)
    promise_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    event_ids: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list)
    headline: Mapped[str] = mapped_column(Text)
    body_markdown: Mapped[str] = mapped_column(Text)
    body_html: Mapped[str | None] = mapped_column(Text)
    word_count: Mapped[int] = mapped_column(Integer)
    source_ids: Mapped[list[int]] = mapped_column(JSON_TYPE, default=list)
    qa_flags: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list)
    rewrite_count: Mapped[int] = mapped_column(Integer, default=0)
    writer_input: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE)  # audit: exactly what was given
    state: Mapped[str] = mapped_column(String(20), index=True)
    coordinator_email: Mapped[str | None] = mapped_column(String(255))
    decision_note: Mapped[str | None] = mapped_column(Text)
    approved_by: Mapped[str | None] = mapped_column(String(255))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # row in Sheet
    stale_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


# --------------------------------------------------------------------------- dashboard (P10)

ROLES = ("superadmin", "coordinator")
ASSIGNMENT_KINDS = ("primary", "temporary")


class User(Base):
    """Dashboard account. Passwords are scrypt hashes (never stored or logged in clear)."""

    __tablename__ = "users"
    __table_args__ = (CheckConstraint(_in("role", ROLES), name="role"),)

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True)  # stored lowercase
    name: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16))
    password_hash: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MeterAssignment(Base):
    """Who covers a meter. ``primary`` = the meter's coordinator (one active at a time);
    ``temporary`` = time-boxed cover (leave, absence) between ``starts_at`` and ``ends_at``."""

    __tablename__ = "meter_assignments"
    __table_args__ = (
        CheckConstraint(_in("kind", ASSIGNMENT_KINDS), name="kind"),
        CheckConstraint("kind = 'primary' OR ends_at IS NOT NULL", name="temporary_has_end"),
    )

    assignment_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    meter_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("meters.id"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.user_id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(Text)
    granted_by: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.user_id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.user_id"))


class MeterSettings(Base):
    """Per-meter news-search context (dashboard-edited; read by the News Scout)."""

    __tablename__ = "meter_settings"

    meter_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("meters.id"), primary_key=True)
    official_aliases: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list)
    search_terms: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list)
    exclude_terms: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list)
    # [{"domain": "...", "name": "..."}] — superadmin-only (editorial balance)
    extra_outlets: Mapped[list[dict[str, str]]] = mapped_column(JSON_TYPE, default=list)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.user_id"))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    """Every dashboard change: who, what, before → after. Append-only."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.user_id"))
    action: Mapped[str] = mapped_column(String(64))
    entity: Mapped[str] = mapped_column(String(64))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    before: Mapped[dict[str, Any] | None] = mapped_column(JSON_TYPE)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSON_TYPE)


class AppSetting(Base):
    """Dashboard overrides of config/*.yaml (whitelisted keys only, validated, audited)."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)  # e.g. "news.max_calls_per_run"
    value: Mapped[Any] = mapped_column(JSON_TYPE)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.user_id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


JOB_STATES = ("queued", "running", "done", "failed", "busy", "lost")


class Job(Base):
    """A background operation started from the dashboard (whitelisted commands only)."""

    __tablename__ = "jobs"
    __table_args__ = (CheckConstraint(_in("state", JOB_STATES), name="state"),)

    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(32))
    target: Mapped[str | None] = mapped_column(String(64))  # meter / group / article id
    argv: Mapped[list[str]] = mapped_column(JSON_TYPE)
    state: Mapped[str] = mapped_column(String(16), index=True)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    output: Mapped[str | None] = mapped_column(Text)  # tail of stdout/stderr
    requested_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.user_id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# --------------------------------------------------------------------------- submissions (P12)


class PtToken(Base):
    """A coordinator's Promise Tracker submission token (issued by the backend, PRD §8d).

    Only the Fernet ciphertext is stored; the token is never shown again or logged. One active
    (``revoked_at IS NULL``) token per user; replacing one revokes the old row.
    """

    __tablename__ = "pt_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.user_id"), index=True)
    token_ciphertext: Mapped[str] = mapped_column(Text)
    remote_token_id: Mapped[str] = mapped_column(String(128))  # the backend's id (whoami)
    last4: Mapped[str] = mapped_column(String(4))
    meter_ids: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list)  # from whoami
    holder_name: Mapped[str | None] = mapped_column(String(255))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


SUBMISSION_STATES = ("pending_review", "published", "rejected", "withdrawn")


class PtSubmission(Base):
    """One approved article sent to Promise Tracker's review queue. One per article, ever."""

    __tablename__ = "pt_submissions"
    __table_args__ = (CheckConstraint(_in("remote_state", SUBMISSION_STATES), name="remote_state"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    article_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("articles.article_id"), unique=True
    )
    meter_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("meters.id"), index=True)
    promise_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    submitted_by: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.user_id"))
    token_row_id: Mapped[int] = mapped_column(Integer, ForeignKey("pt_tokens.id"))
    payload_sha256: Mapped[str] = mapped_column(String(64))
    body_text: Mapped[str] = mapped_column(Text)  # plain text we sent (self-update matching)
    remote_id: Mapped[str] = mapped_column(String(128), unique=True)
    remote_state: Mapped[str] = mapped_column(String(20), index=True)
    remote_update_id: Mapped[str | None] = mapped_column(String(64), index=True)
    reviewer_note: Mapped[str | None] = mapped_column(Text)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
