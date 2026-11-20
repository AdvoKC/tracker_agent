from __future__ import annotations

import uuid
from datetime import UTC
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from promise_monitor import access, jobs
from promise_monitor.access import AccessError
from promise_monitor.briefs import MAX_CHARS, MAX_URLS, MIN_CHARS, create_brief
from promise_monitor.dashboard.core import (
    NotFoundError,
    current_user,
    db,
    field_str,
    flash,
    form,
    get_ctx,
    page,
    parse_lines,
    parse_outlets,
    safe_back,
    to_uuid,
    visible,
    visible_meter,
    where,
)
from promise_monitor.db.models import (
    Article,
    ArticleGroup,
    ChangeEvent,
    Job,
    Meter,
    MeterAssignment,
    MeterSettings,
    NewsItem,
    NewsSearchState,
    PromiseIndex,
    PromiseSnapshot,
    User,
)
from promise_monitor.domain.qa import STATUS_LABELS

router = APIRouter()


def _assignments(s: Session, meter_id: uuid.UUID, ctx: Any) -> list[dict[str, Any]]:
    now = ctx.clock()
    out = []
    for a in s.scalars(
        select(MeterAssignment)
        .where(MeterAssignment.meter_id == meter_id, MeterAssignment.revoked_at.is_(None))
        .order_by(MeterAssignment.kind, MeterAssignment.starts_at)
    ):
        ends = a.ends_at if a.ends_at is None or a.ends_at.tzinfo else a.ends_at.replace(tzinfo=UTC)
        if ends is not None and ends <= now:
            continue
        starts = a.starts_at if a.starts_at.tzinfo else a.starts_at.replace(tzinfo=UTC)
        u = s.get(User, a.user_id)
        out.append(
            {
                "id": a.assignment_id,
                "user_id": a.user_id,
                "name": u.name if u else "?",
                "email": u.email if u else "",
                "kind": a.kind,
                "reason": a.reason,
                "upcoming": starts > now,
                "window": f"{ctx.local(a.starts_at)} → "
                + (ctx.local(a.ends_at) if a.ends_at else "until changed"),
            }
        )
    return out


@router.get("/meters", response_class=HTMLResponse)
def meters(
    request: Request, user: User = Depends(current_user), s: Session = Depends(db)
) -> Response:
    ctx = get_ctx(request)
    vis = visible(s, user, ctx)
    q = select(Meter).order_by(Meter.registry_status, Meter.priority_rank, Meter.name)
    if vis is not None:
        q = q.where(Meter.id.in_(vis))

    def counts(stmt: Any) -> dict[Any, int]:
        return {k: int(v) for k, v in s.execute(stmt).all()}

    promises = counts(
        select(PromiseIndex.meter_id, func.count())
        .where(PromiseIndex.present.is_(True))
        .group_by(PromiseIndex.meter_id)
    )
    drafts = counts(
        select(Article.meter_id, func.count())
        .where(Article.state.in_(("in_review", "needs_attention", "stale")))
        .group_by(Article.meter_id)
    )
    leads = counts(
        select(ArticleGroup.meter_id, func.count())
        .where(ArticleGroup.state == "needs_sources")
        .group_by(ArticleGroup.meter_id)
    )
    customised = set(
        s.scalars(select(MeterSettings.meter_id).where(MeterSettings.updated_at.is_not(None)))
    )
    rows = []
    for m in s.scalars(q):
        assigns = access.active_assignments(s, ctx.clock(), meter_id=m.id)
        primary = next((a for a in assigns if a.kind == "primary"), None)
        pu = s.get(User, primary.user_id) if primary else None
        rows.append(
            {
                "id": m.id,
                "name": m.name,
                "where": where(m),
                "type": m.meter_type.replace("_", " "),
                "status": m.registry_status,
                "promises": promises.get(m.id, 0),
                "primary": pu.name if pu else None,
                "cover": sum(1 for a in assigns if a.kind == "temporary"),
                "drafts": drafts.get(m.id, 0),
                "leads": leads.get(m.id, 0),
                "customised": m.id in customised,
            }
        )
    return page(request, "meters.html", user, {"nav": "meters", "rows": rows})


@router.get("/meters/{meter_id}", response_class=HTMLResponse)
def meter_page(
    meter_id: str, request: Request, user: User = Depends(current_user), s: Session = Depends(db)
) -> Response:
    ctx = get_ctx(request)
    meter = visible_meter(s, user, ctx, to_uuid(meter_id))
    st = access.get_settings(s, meter.id)
    idx = list(s.scalars(select(PromiseIndex).where(PromiseIndex.meter_id == meter.id)))
    contents: dict[int, dict[str, Any]] = dict(
        s.execute(
            select(PromiseSnapshot.snapshot_id, PromiseSnapshot.content).where(
                PromiseSnapshot.snapshot_id.in_([i.latest_snapshot_id for i in idx])
            )
        ).all()
    )
    searched = dict(
        s.execute(
            select(NewsSearchState.promise_id, NewsSearchState.last_searched_at).where(
                NewsSearchState.promise_id.in_([i.promise_id for i in idx])
            )
        ).all()
    )
    news_counts = {
        k: int(v)
        for k, v in s.execute(
            select(NewsItem.promise_id, func.count())
            .where(NewsItem.meter_id == meter.id, NewsItem.state == "accepted")
            .group_by(NewsItem.promise_id)
        ).all()
    }
    promises = []
    for i in idx:
        c = contents.get(i.latest_snapshot_id, {})
        ups = c.get("updates") or []
        status = str(c.get("status") or "not_yet_rated")
        promises.append(
            {
                "id": i.promise_id,
                "title": c.get("title") or "(untitled)",
                "status": status,
                "status_label": STATUS_LABELS.get(status, status),
                "category": c.get("category"),
                "updates": len(ups),
                "last_update": max((str(u.get("created_at", ""))[:10] for u in ups), default=""),
                "searched": ctx.local(searched.get(i.promise_id), "%d %b %Y"),
                "news": news_counts.get(i.promise_id, 0),
                "present": i.present,
            }
        )
    promises.sort(key=lambda p: (not p["present"], p["status"], str(p["title"]).lower()))
    titles = {i.promise_id: contents.get(i.latest_snapshot_id, {}).get("title", "") for i in idx}
    events = [
        {
            "when": ctx.local(e.detected_at),
            "type": e.change_type.replace("_", " "),
            "promise_id": e.promise_id,
            "promise": str(titles.get(e.promise_id, ""))[:90],
            "score": e.score,
            "state": e.state,
        }
        for e in s.scalars(
            select(ChangeEvent)
            .where(ChangeEvent.meter_id == meter.id)
            .order_by(ChangeEvent.detected_at.desc())
            .limit(20)
        )
    ]
    coordinators = (
        list(
            s.scalars(
                select(User)
                .where(User.role == "coordinator", User.active.is_(True))
                .order_by(User.name)
            )
        )
        if user.role == "superadmin"
        else []
    )
    return page(
        request,
        "meter.html",
        user,
        {
            "nav": "meters",
            "meter": meter,
            "where": where(meter),
            "settings": access.settings_dict(st),
            "updated": ctx.local(st.updated_at),
            "can_edit": access.can_edit_settings(s, user, meter.id, ctx.clock()),
            "promises": promises,
            "events": events,
            "assignments": _assignments(s, meter.id, ctx),
            "coordinators": coordinators,
            "now_local": ctx.local(ctx.clock(), "%Y-%m-%dT%H:%M"),
            "promise_url": ctx.base_cfg.tunables.api.public_promise_url,
        },
    )


@router.post("/meters/{meter_id}/settings")
async def save_settings(
    meter_id: str, request: Request, user: User = Depends(current_user), s: Session = Depends(db)
) -> Response:
    ctx = get_ctx(request)
    data = await form(request)
    meter = visible_meter(s, user, ctx, to_uuid(meter_id))
    try:
        outlets = (
            parse_outlets(field_str(data, "extra_outlets")) if user.role == "superadmin" else None
        )
        access.update_settings(
            s,
            meter=meter,
            actor=user,
            now=ctx.clock(),
            official_aliases=parse_lines(field_str(data, "official_aliases")),
            search_terms=parse_lines(field_str(data, "search_terms")),
            exclude_terms=parse_lines(field_str(data, "exclude_terms")),
            extra_outlets=outlets,
        )
        s.commit()
        flash(request, "ok", "Search context saved. It applies from the next run.")
    except AccessError as exc:
        s.rollback()
        flash(request, "err", str(exc))
    return RedirectResponse(f"/meters/{meter.id}", status_code=303)


def _user(s: Session, raw: str) -> User:
    try:
        u = s.get(User, uuid.UUID(raw))
    except ValueError as exc:
        raise AccessError("choose a coordinator") from exc
    if u is None:
        raise AccessError("choose a coordinator")
    return u


@router.post("/meters/{meter_id}/primary")
async def set_primary(
    meter_id: str, request: Request, user: User = Depends(current_user), s: Session = Depends(db)
) -> Response:
    ctx = get_ctx(request)
    data = await form(request)
    meter = visible_meter(s, user, ctx, to_uuid(meter_id))
    try:
        access.set_primary(
            s,
            meter_id=meter.id,
            user=_user(s, field_str(data, "user_id")),
            actor=user,
            now=ctx.clock(),
        )
        s.commit()
        flash(request, "ok", "Primary coordinator set.")
    except AccessError as exc:
        s.rollback()
        flash(request, "err", str(exc))
    return RedirectResponse(
        safe_back(field_str(data, "back"), f"/meters/{meter.id}"), status_code=303
    )


@router.post("/meters/{meter_id}/grant")
async def grant(
    meter_id: str, request: Request, user: User = Depends(current_user), s: Session = Depends(db)
) -> Response:
    ctx = get_ctx(request)
    data = await form(request)
    meter = visible_meter(s, user, ctx, to_uuid(meter_id))
    try:
        access.grant_temporary(
            s,
            meter_id=meter.id,
            user=_user(s, field_str(data, "user_id")),
            starts_at=ctx.parse_local(field_str(data, "starts_at")),
            ends_at=ctx.parse_local(field_str(data, "ends_at")),
            reason=field_str(data, "reason"),
            actor=user,
            now=ctx.clock(),
        )
        s.commit()
        flash(request, "ok", "Temporary access granted. It ends automatically.")
    except AccessError as exc:
        s.rollback()
        flash(request, "err", str(exc))
    return RedirectResponse(
        safe_back(field_str(data, "back"), f"/meters/{meter.id}"), status_code=303
    )


@router.post("/assignments/{assignment_id}/revoke")
async def revoke(
    assignment_id: str,
    request: Request,
    user: User = Depends(current_user),
    s: Session = Depends(db),
) -> Response:
    ctx = get_ctx(request)
    data = await form(request)
    a = s.get(MeterAssignment, to_uuid(assignment_id))
    if a is None:
        raise NotFoundError
    meter_id = a.meter_id
    try:
        access.revoke(s, assignment_id=a.assignment_id, actor=user, now=ctx.clock())
        s.commit()
        flash(request, "ok", "Access revoked.")
    except AccessError as exc:
        s.rollback()
        flash(request, "err", str(exc))
    return RedirectResponse(
        safe_back(field_str(data, "back"), f"/meters/{meter_id}"), status_code=303
    )


@router.get("/promises/{promise_id}", response_class=HTMLResponse)
def promise_page(
    promise_id: str, request: Request, user: User = Depends(current_user), s: Session = Depends(db)
) -> Response:
    ctx = get_ctx(request)
    pid = to_uuid(promise_id)
    idx = s.get(PromiseIndex, pid)
    if idx is None:
        raise NotFoundError
    meter = visible_meter(s, user, ctx, idx.meter_id)
    snaps = list(
        s.scalars(
            select(PromiseSnapshot)
            .where(PromiseSnapshot.promise_id == pid)
            .order_by(PromiseSnapshot.snapshot_id.desc())
        )
    )
    latest = snaps[0].content if snaps else {}
    history = []
    for i, snap in enumerate(snaps):
        prev = snaps[i + 1] if i + 1 < len(snaps) else None
        what = (
            "first seen (baseline)"
            if prev is None
            else ", ".join(
                f
                for f in ("status", "title", "category", "updates")
                if (prev.content or {}).get(f) != (snap.content or {}).get(f)
            )
            or "content"
        )
        history.append(
            {
                "when": ctx.local(snap.captured_at),
                "status": snap.status,
                "status_label": STATUS_LABELS.get(snap.status, snap.status),
                "what": what,
            }
        )
    events = list(
        s.scalars(
            select(ChangeEvent)
            .where(ChangeEvent.promise_id == pid)
            .order_by(ChangeEvent.detected_at.desc())
        )
    )
    news = list(
        s.scalars(
            select(NewsItem)
            .where(NewsItem.promise_id == pid)
            .order_by(NewsItem.found_at.desc())
            .limit(50)
        )
    )
    articles = list(
        s.scalars(
            select(Article).where(Article.promise_id == pid).order_by(Article.created_at.desc())
        )
    )
    groups = {
        g.group_id: g for g in s.scalars(select(ArticleGroup).where(ArticleGroup.promise_id == pid))
    }
    status = str(latest.get("status") or "not_yet_rated")
    brief_events = [e for e in events if e.change_type == "coordinator_update"]
    names = {
        u.user_id: u.name
        for u in s.scalars(
            select(User).where(User.user_id.in_([e.requested_by for e in brief_events]))
        )
    }
    art_by_group = {a.article_group_id: a for a in articles}
    briefs: list[dict[str, Any]] = []
    brief_open = False
    for e in brief_events:
        g = groups.get(e.article_group_id) if e.article_group_id else None
        job = s.scalars(
            select(Job)
            .where(Job.kind == "write_brief", Job.target == str(e.article_group_id))
            .order_by(Job.created_at.desc())
            .limit(1)
        ).first()
        briefs.append(
            {
                "when": ctx.local(e.detected_at),
                "by": names.get(e.requested_by, "") if e.requested_by else "",
                "rating": e.new_status or status,
                "text": e.brief or "",
                "urls": e.brief_urls or [],
                "group": g,
                "article": art_by_group.get(e.article_group_id) if e.article_group_id else None,
                "job": job,
            }
        )
        if job is not None and job.state in ("queued", "running"):
            brief_open = True
    return page(
        request,
        "promise.html",
        user,
        {
            "nav": "meters",
            "meter": meter,
            "pid": pid,
            "p": latest,
            "status": status,
            "status_label": STATUS_LABELS.get(status, status),
            "url": ctx.base_cfg.tunables.api.public_promise_url.format(promise_id=pid),
            "history": history,
            "events": events,
            "news": news,
            "articles": articles,
            "groups": groups,
            "briefs": briefs,
            "brief_open": brief_open,
            "statuses": STATUS_LABELS,
            "brief_limits": {"min": MIN_CHARS, "max": MAX_CHARS, "urls": MAX_URLS},
            "present": idx.present,
            "searched": ctx.local(
                s.scalars(
                    select(NewsSearchState.last_searched_at).where(
                        NewsSearchState.promise_id == pid
                    )
                ).first()
            ),
        },
    )


@router.post("/promises/{promise_id}/brief")
async def write_update(
    promise_id: str, request: Request, user: User = Depends(current_user), s: Session = Depends(db)
) -> Response:
    """Coordinator brief → verified sources → house-style draft in the review queue."""
    ctx = get_ctx(request)
    data = await form(request)
    pid = to_uuid(promise_id)
    idx = s.get(PromiseIndex, pid)
    if idx is None:
        raise NotFoundError
    visible_meter(s, user, ctx, idx.meter_id)  # 404 for meters the user can't see
    back = f"/promises/{pid}"
    try:
        brief = create_brief(
            s,
            actor=user,
            promise_id=pid,
            description=field_str(data, "description"),
            rating=field_str(data, "rating"),
            urls_raw=field_str(data, "urls"),
            now=ctx.clock(),
        )
        s.commit()
    except AccessError as exc:
        s.rollback()
        flash(request, "err", str(exc))
        return RedirectResponse(back + "#write", status_code=303)
    try:
        jobs.start_job(
            ctx.factory,
            kind="write_brief",
            target=str(brief.group_id),
            actor=user,
            now=ctx.clock(),
            launcher=ctx.launcher,
            wait=ctx.jobs_wait,
        )
        flash(
            request,
            "ok",
            "Update requested. Its sources are being checked, then Claude drafts it in house "
            "style; it appears below and in the review queue in a few minutes.",
        )
    except AccessError as exc:
        flash(
            request,
            "err",
            f"Saved, but drafting could not start now ({exc}); the next daily run will draft it.",
        )
    return RedirectResponse(back + "#updates-requested", status_code=303)
