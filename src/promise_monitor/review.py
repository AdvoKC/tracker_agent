"""Coordinator review actions (dashboard channel, P11). Nothing here publishes anything.

- ``save_edit``: store edited headline/body and re-run the deterministic QA.
- ``approve``: QA must pass on the CURRENT text → ``approved`` (publisher/shadow mode decide
  what happens next). A failing check leaves it ``needs_attention`` with the issues.
- ``reject``: ``rejected`` — the change is consumed and never redrafted.
- ``dismiss_lead``: close a ``needs_sources`` evidence lead.
- ``confirm_source``: a coordinator opened a page that blocked the agent's reader and confirms
  it; the source becomes citable and, if that is enough, the lead goes back to drafting.
- ``mark_stale`` / ``announce_new``: used by the daily run for reminders and digests.
Every action checks the actor can see the meter and is written to the audit log.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from promise_monitor import access
from promise_monitor.access import AccessError
from promise_monitor.agents.verifier import COORDINATOR_CONFIRMED
from promise_monitor.agents.writer import qa_context_from_input
from promise_monitor.config import AppConfig
from promise_monitor.db.models import (
    Article,
    ArticleGroup,
    ArticleGroupSource,
    ChangeEvent,
    PromiseIndex,
    PromiseSnapshot,
    Source,
    User,
)
from promise_monitor.domain.qa import QAReport, check_article

OPEN_STATES = ("in_review", "needs_attention", "stale")


def _guard(session: Session, actor: User, meter_id: uuid.UUID, now: datetime) -> None:
    if not access.can_view(session, actor, meter_id, now):
        raise AccessError("you don't have access to this meter")


def _set_events(session: Session, art: Article, state: str) -> None:
    ids = [uuid.UUID(e) for e in art.event_ids]
    for ev in session.scalars(select(ChangeEvent).where(ChangeEvent.event_id.in_(ids))):
        ev.state = state


def check_text(
    session: Session, art: Article, headline: str, body: str, cfg: AppConfig, now: datetime
) -> QAReport:
    recent = tuple(
        session.scalars(
            select(Article.body_markdown).where(
                Article.promise_id == art.promise_id,
                Article.article_id != art.article_id,
                Article.state.in_(("approved", "published")),
                Article.created_at >= now - timedelta(days=cfg.tunables.article.dedupe_window_days),
            )
        )
    )
    qa = qa_context_from_input(art.writer_input, cfg.tunables.article, cfg.banned_terms, recent)
    return check_article(headline, body, qa)


def save_edit(
    session: Session,
    art: Article,
    *,
    headline: str,
    body: str,
    actor: User,
    cfg: AppConfig,
    now: datetime,
) -> QAReport:
    _guard(session, actor, art.meter_id, now)
    if art.state not in OPEN_STATES:
        raise AccessError(f"this article is {art.state}; it can no longer be edited")
    headline, body = headline.strip(), body.strip()
    rep = check_text(session, art, headline, body, cfg, now)
    before = {"headline": art.headline, "body_markdown": art.body_markdown}
    art.headline, art.body_markdown, art.word_count = headline, body, rep.word_count
    art.qa_flags = rep.issues
    art.state = "in_review" if rep.passed else "needs_attention"
    if before != {"headline": headline, "body_markdown": body}:
        access.audit(
            session,
            actor,
            "article.edit",
            "article",
            art.article_id,
            now,
            before=before,
            after={"headline": headline, "body_markdown": body, "qa_passed": rep.passed},
        )
    session.flush()
    return rep


def approve(
    session: Session,
    art: Article,
    *,
    actor: User,
    cfg: AppConfig,
    now: datetime,
    note: str | None = None,
) -> QAReport:
    _guard(session, actor, art.meter_id, now)
    if art.state not in OPEN_STATES:
        raise AccessError(f"this article is {art.state}; it can't be approved")
    rep = check_text(session, art, art.headline, art.body_markdown, cfg, now)
    art.qa_flags = rep.issues
    if not rep.passed:
        art.state = "needs_attention"
        session.flush()
        return rep
    art.state = "approved"
    art.approved_by, art.approved_at = actor.email, now
    art.decision_note = (note or "").strip()[:1000] or None
    _set_events(session, art, "approved")
    access.audit(
        session,
        actor,
        "article.approve",
        "article",
        art.article_id,
        now,
        after={"note": art.decision_note},
    )
    session.flush()
    return rep


def reject(
    session: Session, art: Article, *, actor: User, now: datetime, note: str | None = None
) -> None:
    _guard(session, actor, art.meter_id, now)
    if art.state not in OPEN_STATES:
        raise AccessError(f"this article is {art.state}; it can't be rejected")
    art.state = "rejected"
    art.decision_note = (note or "").strip()[:1000] or None
    _set_events(session, art, "rejected")
    group = session.get(ArticleGroup, art.article_group_id)
    if group is not None:
        group.state = "rejected"
    access.audit(
        session,
        actor,
        "article.reject",
        "article",
        art.article_id,
        now,
        after={"note": art.decision_note},
    )
    session.flush()


def dismiss_lead(
    session: Session, group: ArticleGroup, *, actor: User, now: datetime, note: str | None = None
) -> None:
    _guard(session, actor, group.meter_id, now)
    if group.state != "needs_sources":
        raise AccessError("only open leads (needs sources) can be dismissed")
    group.state = "rejected"
    for ev in session.scalars(
        select(ChangeEvent).where(ChangeEvent.article_group_id == group.group_id)
    ):
        ev.state = "rejected"
    access.audit(
        session,
        actor,
        "lead.dismiss",
        "group",
        group.group_id,
        now,
        after={"note": (note or "")[:1000]},
    )
    session.flush()


BLOCKED_FETCH = ("blocked", "paywalled")


def confirmable(ags: ArticleGroupSource, src: Source) -> bool:
    """A page the agent couldn't read or confirm, which a person can open and confirm."""
    return (
        ags.role != "promise_page"
        and src.fetch_status in BLOCKED_FETCH
        and not ags.citable
        and ags.confirmed_by is None
    )


def confirm_source(
    session: Session,
    group: ArticleGroup,
    source_id: int,
    *,
    actor: User,
    now: datetime,
    title: str | None = None,
) -> bool:
    """Confirm a blocked source. Returns True when the lead is now ready to draft.

    Only the confirmed source changes: every other verdict, the promise-present check and
    the writer's QA (citable links only) still apply.
    """
    _guard(session, actor, group.meter_id, now)
    if group.state != "needs_sources":
        raise AccessError("only open leads (needs sources) can have sources confirmed")
    ags = session.get(ArticleGroupSource, (group.group_id, source_id))
    src = session.get(Source, source_id)
    if ags is None or src is None:
        raise AccessError("that source isn't part of this lead")
    if not confirmable(ags, src):
        raise AccessError("only pages that blocked the agent's reader can be confirmed here")
    clean = " ".join((title or "").split())[:300] or None
    ags.verdict, ags.citable, ags.relevant, ags.relevance_confidence = (
        "verified_partial",
        True,
        True,
        1.0,
    )
    ags.reason = COORDINATOR_CONFIRMED
    ags.confirmed_by, ags.confirmed_at, ags.confirmed_title = actor.user_id, now, clean
    access.audit(
        session,
        actor,
        "source.confirm",
        "group",
        group.group_id,
        now,
        after={"source_id": source_id, "url": src.url, "title": clean},
    )
    ready = _reopen_if_sufficient(session, group, src.url)
    session.flush()
    return ready


def _reopen_if_sufficient(session: Session, group: ArticleGroup, url: str) -> bool:
    rows = list(
        session.scalars(
            select(ArticleGroupSource).where(ArticleGroupSource.group_id == group.group_id)
        )
    )
    backed = {a.event_id for a in rows if a.citable and a.event_id is not None}
    events = [
        ev
        for ev in session.scalars(
            select(ChangeEvent).where(
                ChangeEvent.article_group_id == group.group_id,
                ChangeEvent.state == "needs_sources",
            )
        )
        # a news report is only reportable through its own URL (verifier rule)
        if ev.change_type != "news_update" or ev.event_id in backed
    ]
    ids = {ev.event_id for ev in events}
    idx = session.get(PromiseIndex, group.promise_id)
    sufficient = bool(idx and idx.present) and any(
        a.citable and a.role == "required" and a.event_id in ids for a in rows
    )
    if not sufficient:
        return False
    for ev in events:
        ev.state = "queued"
    group.state, group.sufficient = "verified", True
    group.notes = [n for n in group.notes or [] if url not in n] + [
        f"confirmed by a coordinator: {url}"
    ]
    return True


def mark_stale(session: Session, cfg: AppConfig, now: datetime) -> dict[uuid.UUID, list[str]]:
    """Undecided drafts older than ``stale_after_days`` → stale (once). Returns lines by meter."""
    cutoff = now - timedelta(days=cfg.tunables.review.stale_after_days)
    out: dict[uuid.UUID, list[str]] = {}
    for art in session.scalars(
        select(Article).where(
            Article.state.in_(("in_review", "needs_attention")), Article.created_at <= cutoff
        )
    ):
        art.state, art.stale_at = "stale", now
        _set_events(session, art, "stale")
        out.setdefault(art.meter_id, []).append(art.headline)
    session.flush()
    return out


def announce_new(session: Session, now: datetime) -> dict[uuid.UUID, list[str]]:
    """New drafts and leads since the last run (marked so they're announced once)."""
    out: dict[uuid.UUID, list[str]] = {}
    for art in session.scalars(
        select(Article).where(
            Article.pushed_at.is_(None), Article.state.in_(("in_review", "needs_attention"))
        )
    ):
        art.pushed_at = now
        group = session.get(ArticleGroup, art.article_group_id)
        if group is not None and group.state == "drafted":
            group.state = "in_review"
        _set_events(session, art, "in_review")
        label = "draft" if art.state == "in_review" else "draft needing attention"
        out.setdefault(art.meter_id, []).append(f"{label}: {art.headline}")
    for group in session.scalars(
        select(ArticleGroup).where(
            ArticleGroup.pushed_at.is_(None), ArticleGroup.state == "needs_sources"
        )
    ):
        group.pushed_at = now
        title = session.scalars(
            select(PromiseSnapshot.title)
            .where(PromiseSnapshot.promise_id == group.promise_id)
            .order_by(PromiseSnapshot.snapshot_id.desc())
            .limit(1)
        ).first()
        out.setdefault(group.meter_id, []).append(
            f"evidence lead (sources to check): {(title or '')[:90]}"
        )
    session.flush()
    return out


def summary_of(writer_input: dict[str, Any]) -> list[str]:
    lines = []
    for c in writer_input.get("changes") or []:
        t = c.get("type")
        if t == "status_change":
            lines.append(f"Status: {c.get('old_status')} → {c.get('new_status')}")
        elif t == "news_update":
            lines.append(
                f"News: {c.get('outlet')} ({c.get('published_date')}): {c.get('headline')}"
            )
        elif t == "new_evidence":
            lines.append("New evidence published on Promise Tracker")
        elif t == "new_promise":
            lines.append("New promise added to the meter")
        elif t == "coordinator_update":
            lines.append(f"Coordinator's update: {str(c.get('description', ''))[:160]}")
    return lines
