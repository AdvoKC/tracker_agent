from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from markupsafe import Markup
from sqlalchemy import select
from sqlalchemy.orm import Session

from promise_monitor import jobs, review, submissions
from promise_monitor.access import AccessError
from promise_monitor.agents.publisher import render_markdown
from promise_monitor.dashboard.core import (
    NotFoundError,
    current_user,
    db,
    field_str,
    flash,
    form,
    get_ctx,
    page,
    to_uuid,
    visible,
    visible_meter,
)
from promise_monitor.db.models import (
    Article,
    ArticleGroup,
    ArticleGroupSource,
    ChangeEvent,
    Meter,
    NewsItem,
    PromiseSnapshot,
    Source,
    User,
)
from promise_monitor.domain.qa import word_count

router = APIRouter()


def _titles(s: Session, promise_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    out: dict[uuid.UUID, str] = {}
    for pid, title in s.execute(
        select(PromiseSnapshot.promise_id, PromiseSnapshot.title)
        .where(PromiseSnapshot.promise_id.in_(promise_ids))
        .order_by(PromiseSnapshot.snapshot_id)
    ):
        out[pid] = title  # last one wins = latest
    return out


@router.get("/review", response_class=HTMLResponse)
def queue(
    request: Request, user: User = Depends(current_user), s: Session = Depends(db)
) -> Response:
    ctx = get_ctx(request)
    vis = visible(s, user, ctx)
    meter_q = request.query_params.get("meter", "")
    state_q = request.query_params.get("state", "open")
    states = review.OPEN_STATES if state_q == "open" else (state_q,)
    aq = (
        select(Article)
        .where(Article.state.in_(states))
        .order_by(Article.created_at.desc())
        .limit(200)
    )
    lq = (
        select(ArticleGroup)
        .where(ArticleGroup.state == "needs_sources")
        .order_by(ArticleGroup.created_at.desc())
        .limit(200)
    )
    if vis is not None:
        aq, lq = aq.where(Article.meter_id.in_(vis)), lq.where(ArticleGroup.meter_id.in_(vis))
    if meter_q:
        mid = to_uuid(meter_q)
        aq, lq = aq.where(Article.meter_id == mid), lq.where(ArticleGroup.meter_id == mid)
    articles = list(s.scalars(aq))
    leads = list(s.scalars(lq)) if state_q == "open" else []
    meters = {m.id: m for m in s.scalars(select(Meter))}
    titles = _titles(s, [a.promise_id for a in articles] + [g.promise_id for g in leads])
    visible_meters = [m for m in meters.values() if vis is None or m.id in vis]
    return page(
        request,
        "review.html",
        user,
        {
            "nav": "review",
            "articles": articles,
            "leads": leads,
            "meters": meters,
            "titles": titles,
            "meter_q": meter_q,
            "state_q": state_q,
            "meter_options": sorted(visible_meters, key=lambda m: (m.priority_rank, m.name)),
            "summary_of": review.summary_of,
        },
    )


def _sources(s: Session, group_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = s.execute(
        select(ArticleGroupSource, Source)
        .join(Source, Source.source_id == ArticleGroupSource.source_id)
        .where(ArticleGroupSource.group_id == group_id)
        .order_by(ArticleGroupSource.role, Source.source_id)
    ).all()
    return [
        {
            "id": src.source_id,
            "url": src.url,
            "title": src.title or src.search_title,
            "publisher": src.publisher,
            "date": src.published_date,
            "fetch": src.fetch_status,
            "http": src.http_status,
            "role": ags.role,
            "verdict": ags.verdict,
            "citable": ags.citable,
            "conf": ags.relevance_confidence,
            "reason": ags.reason,
            "confirmed": src.search_confirmed,
            "blocked": src.fetch_status in review.BLOCKED_FETCH,
            "confirmable": review.confirmable(ags, src),
            "confirmed_at": ags.confirmed_at,
            "confirmed_title": ags.confirmed_title,
        }
        for ags, src in rows
    ]


@router.get("/review/articles/{article_id}", response_class=HTMLResponse)
def article_page(
    article_id: str, request: Request, user: User = Depends(current_user), s: Session = Depends(db)
) -> Response:
    ctx = get_ctx(request)
    art = s.get(Article, to_uuid(article_id))
    if art is None:
        raise NotFoundError
    meter = visible_meter(s, user, ctx, art.meter_id)
    events = list(
        s.scalars(
            select(ChangeEvent).where(
                ChangeEvent.event_id.in_([uuid.UUID(e) for e in art.event_ids])
            )
        )
    )
    group = s.get(ArticleGroup, art.article_group_id)
    sendable = art.state in submissions.SENDABLE_STATES
    return page(
        request,
        "article.html",
        user,
        {
            "nav": "review",
            "a": art,
            "meter": meter,
            "group": group,
            "title": _titles(s, [art.promise_id]).get(art.promise_id, ""),
            "preview": Markup(render_markdown(art.body_markdown)),  # noqa: S704 (markdown-it, raw html disabled)
            "changes": review.summary_of(art.writer_input or {}),
            "sources": _sources(s, art.article_group_id),
            "events": events,
            "open": art.state in review.OPEN_STATES,
            "live_words": word_count(art.body_markdown),
            "limits": ctx.base_cfg.tunables.article,
            "sendable": sendable,
            "submission": submissions.submission_for(s, art.article_id),
            "send_blockers": (
                submissions.blockers(s, art, user, ctx.cfg(s), ctx.clock(), box=ctx.token_box)
                if sendable
                else []
            ),
        },
    )


@router.post("/review/articles/{article_id}")
async def article_action(
    article_id: str, request: Request, user: User = Depends(current_user), s: Session = Depends(db)
) -> Response:
    ctx = get_ctx(request)
    data = await form(request)
    art = s.get(Article, to_uuid(article_id))
    if art is None:
        raise NotFoundError
    visible_meter(s, user, ctx, art.meter_id)
    cfg = ctx.cfg(s)
    action = field_str(data, "action")
    note = field_str(data, "note")
    try:
        if action in ("save", "approve") and (
            field_str(data, "headline") != art.headline
            or field_str(data, "body") != art.body_markdown
        ):
            rep = review.save_edit(
                s,
                art,
                headline=field_str(data, "headline"),
                body=field_str(data, "body"),
                actor=user,
                cfg=cfg,
                now=ctx.clock(),
            )
            if action == "save":
                flash(
                    request,
                    "ok" if rep.passed else "err",
                    "Saved; passes all checks."
                    if rep.passed
                    else "Saved, but QA found issues (see below).",
                )
        elif action == "save":
            flash(request, "ok", "No changes.")
        if action == "approve":
            rep = review.approve(s, art, actor=user, cfg=cfg, now=ctx.clock(), note=note)
            if rep.passed:
                flash(
                    request,
                    "ok",
                    "Approved."
                    + (
                        "" if cfg.tunables.publish.enabled else " Shadow mode: held, not published."
                    ),
                )
            else:
                flash(
                    request,
                    "err",
                    "Not approved: the text fails QA. Fix the issues below and approve again.",
                )
        elif action == "reject":
            review.reject(s, art, actor=user, now=ctx.clock(), note=note)
            flash(request, "ok", "Rejected: this change will not be reported.")
        elif action not in ("save",):
            raise AccessError("unknown action")
        s.commit()
    except AccessError as exc:
        s.rollback()
        flash(request, "err", str(exc))
    return RedirectResponse(f"/review/articles/{art.article_id}", status_code=303)


@router.get("/review/leads/{group_id}", response_class=HTMLResponse)
def lead_page(
    group_id: str, request: Request, user: User = Depends(current_user), s: Session = Depends(db)
) -> Response:
    ctx = get_ctx(request)
    group = s.get(ArticleGroup, to_uuid(group_id))
    if group is None:
        raise NotFoundError
    meter = visible_meter(s, user, ctx, group.meter_id)
    events = list(
        s.scalars(select(ChangeEvent).where(ChangeEvent.article_group_id == group.group_id))
    )
    news = list(
        s.scalars(
            select(NewsItem).where(
                NewsItem.id.in_([e.news_item_id for e in events if e.news_item_id])
            )
        )
    )
    sources = _sources(s, group.group_id)
    headline = {n.url: n.headline for n in news}
    for src in sources:  # prefill the confirm form with the best headline we have
        slug = src["url"].rstrip("/").rsplit("/", 1)[-1].replace("-", " ")
        src["hint"] = src["title"] or headline.get(src["url"]) or slug.capitalize()
    article = s.scalars(
        select(Article)
        .where(Article.article_group_id == group.group_id)
        .order_by(Article.created_at.desc())
        .limit(1)
    ).first()
    return page(
        request,
        "lead.html",
        user,
        {
            "nav": "review",
            "g": group,
            "article": article,
            "meter": meter,
            "title": _titles(s, [group.promise_id]).get(group.promise_id, ""),
            "events": events,
            "news": news,
            "sources": sources,
        },
    )


@router.post("/review/leads/{group_id}")
async def lead_action(
    group_id: str, request: Request, user: User = Depends(current_user), s: Session = Depends(db)
) -> Response:
    ctx = get_ctx(request)
    data = await form(request)
    group = s.get(ArticleGroup, to_uuid(group_id))
    if group is None:
        raise NotFoundError
    visible_meter(s, user, ctx, group.meter_id)
    back = f"/review/leads/{group.group_id}"
    if field_str(data, "action") == "confirm":
        return _confirm(request, ctx, s, user, group, data, back)
    try:
        review.dismiss_lead(s, group, actor=user, now=ctx.clock(), note=field_str(data, "note"))
        s.commit()
        flash(request, "ok", "Lead dismissed.")
    except AccessError as exc:
        s.rollback()
        flash(request, "err", str(exc))
    return RedirectResponse(back, status_code=303)


def _confirm(
    request: Request,
    ctx: Any,
    s: Session,
    user: User,
    group: ArticleGroup,
    data: Any,
    back: str,
) -> Response:
    if field_str(data, "checked") != "yes":
        flash(request, "err", "Tick the box to say you opened the page and checked it.")
        return RedirectResponse(back, status_code=303)
    try:
        sid = int(field_str(data, "source_id"))
    except ValueError:
        flash(request, "err", "Unknown source.")
        return RedirectResponse(back, status_code=303)
    try:
        ready = review.confirm_source(
            s, group, sid, actor=user, now=ctx.clock(), title=field_str(data, "title")
        )
        s.commit()
    except AccessError as exc:
        s.rollback()
        flash(request, "err", str(exc))
        return RedirectResponse(back, status_code=303)
    if not ready:
        flash(request, "ok", "Source confirmed. This lead still needs another source to draft.")
        return RedirectResponse(back, status_code=303)
    try:
        jobs.start_job(
            ctx.factory,
            kind="draft_group",
            target=str(group.group_id),
            actor=user,
            now=ctx.clock(),
            launcher=ctx.launcher,
            wait=ctx.jobs_wait,
        )
        flash(
            request,
            "ok",
            "Source confirmed. Claude is drafting the update; it appears here and in the "
            "review queue in a few minutes, ready for you to approve.",
        )
    except AccessError as exc:
        flash(
            request,
            "err",
            f"Source confirmed, but drafting could not start now ({exc}); "
            "the next daily run will draft it.",
        )
    return RedirectResponse(back, status_code=303)
