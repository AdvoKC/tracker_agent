"""Coordinator briefs: "write an update for this promise" from the dashboard.

A coordinator (or the superadmin) describes a development in their own words, picks the
rating the update reports and gives the source link(s) that back it. That becomes a
``coordinator_update`` change event in its own queued article group, so it goes through the
SAME path as a detected change: Verifier (fetch + relevance + sufficiency) → Writer ⇄ Editor
(house style, QA in code) → review queue → approval → publish. Nothing skips verification:
without at least one citable source the group becomes an evidence lead, not a draft.

The brief is data for the Writer, never instructions: it is passed as a field of the input
JSON, and every rule (facts, links, length, neutrality) is still enforced by ``domain/qa.py``.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from promise_monitor import access
from promise_monitor.access import AccessError
from promise_monitor.agents.classifier import STATUS_LABELS
from promise_monitor.db.models import (
    ArticleGroup,
    ChangeEvent,
    PromiseIndex,
    PromiseSnapshot,
    Run,
    User,
)
from promise_monitor.domain.urls import normalize_url

MIN_CHARS, MAX_CHARS = 30, 1500
MAX_URLS = 5
OPEN_GROUP_STATES = ("queued", "verified")


@dataclass(frozen=True)
class Brief:
    group_id: uuid.UUID
    event_id: uuid.UUID
    meter_id: uuid.UUID


def current_status(session: Session, promise_id: uuid.UUID) -> str:
    content = session.scalars(
        select(PromiseSnapshot.content)
        .where(PromiseSnapshot.promise_id == promise_id)
        .order_by(PromiseSnapshot.snapshot_id.desc())
        .limit(1)
    ).first()
    return str((content or {}).get("status") or "not_yet_rated")


def parse_urls(raw: str) -> list[str]:
    urls: list[str] = []
    for line in raw.replace(",", "\n").split():
        line = line.strip()
        if not line:
            continue
        if not line.lower().startswith(("http://", "https://")):
            raise AccessError(f"not a web link: {line[:80]}")
        url = normalize_url(line)
        if url is None:
            raise AccessError(f"not a usable link: {line[:80]}")
        if url not in urls:
            urls.append(url)
    if not urls:
        raise AccessError("add at least one source link that backs the update")
    if len(urls) > MAX_URLS:
        raise AccessError(f"at most {MAX_URLS} source links")
    return urls


def create_brief(
    session: Session,
    *,
    actor: User,
    promise_id: uuid.UUID,
    description: str,
    rating: str,
    urls_raw: str,
    now: datetime,
) -> Brief:
    idx = session.get(PromiseIndex, promise_id)
    if idx is None or not idx.present:
        raise AccessError("this promise is not on the tracker any more")
    if not access.can_view(session, actor, idx.meter_id, now):
        raise AccessError("you don't have access to this meter")
    text = " ".join(description.split())
    if not MIN_CHARS <= len(text) <= MAX_CHARS:
        raise AccessError(f"the description must be {MIN_CHARS}-{MAX_CHARS} characters")
    if rating not in STATUS_LABELS:
        raise AccessError("pick a rating")
    urls = parse_urls(urls_raw)
    busy = session.scalars(
        select(ArticleGroup.group_id)
        .join(ChangeEvent, ChangeEvent.article_group_id == ArticleGroup.group_id)
        .where(
            ArticleGroup.promise_id == promise_id,
            ArticleGroup.state.in_(OPEN_GROUP_STATES),
            ChangeEvent.change_type == "coordinator_update",
        )
    ).first()
    if busy is not None:
        raise AccessError("an update for this promise is already being written; wait for it")

    current = current_status(session, promise_id)
    run = Run(started_at=now, finished_at=now, status="ok", trigger="dashboard:brief", stats={})
    session.add(run)
    session.flush()
    group = ArticleGroup(
        group_id=uuid.uuid4(),
        meter_id=idx.meter_id,
        promise_id=promise_id,
        state="queued",
        notes=[],
        created_at=now,
    )
    session.add(group)
    event_id = uuid.uuid4()
    session.add(
        ChangeEvent(
            event_id=event_id,
            run_id=run.run_id,
            meter_id=idx.meter_id,
            promise_id=promise_id,
            change_type="coordinator_update",
            old_status=current,
            new_status=rating,
            score=100,
            score_base=100,
            score_reason=f"update requested by {actor.name}",
            article_group_id=group.group_id,
            dedupe_key=hashlib.sha256(f"brief:{promise_id}:{event_id}".encode()).hexdigest(),
            detected_at=now,
            state="queued",
            brief=text,
            brief_urls=urls,
            requested_by=actor.user_id,
        )
    )
    access.audit(
        session,
        actor,
        "promise.brief",
        "promise",
        promise_id,
        now,
        after={"rating": rating, "urls": urls, "description": text},
    )
    session.flush()
    return Brief(group.group_id, event_id, idx.meter_id)
