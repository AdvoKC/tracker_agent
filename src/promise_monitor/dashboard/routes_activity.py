from __future__ import annotations

import uuid
from collections import Counter
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from promise_monitor.dashboard.core import (
    NotFoundError,
    current_user,
    db,
    get_ctx,
    page,
    to_uuid,
    visible,
)
from promise_monitor.db.models import (
    Article,
    ArticleGroup,
    ArticleGroupSource,
    ChangeEvent,
    Job,
    LLMCall,
    Meter,
    NewsItem,
    PromiseSnapshot,
    Run,
    Source,
    User,
)

router = APIRouter()
CHANGE_TYPES = (
    "status_change",
    "new_evidence",
    "new_promise",
    "news_update",
    "cosmetic",
    "substantive_edit",
    "promise_removed",
    "coordinator_update",
)


def _titles(s: Session, ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    out: dict[uuid.UUID, str] = {}
    for pid, title in s.execute(
        select(PromiseSnapshot.promise_id, PromiseSnapshot.title)
        .where(PromiseSnapshot.promise_id.in_(ids))
        .order_by(PromiseSnapshot.snapshot_id)
    ):
        out[pid] = title
    return out


def _meter_options(s: Session, vis: set[uuid.UUID] | None) -> list[Meter]:
    q = select(Meter).order_by(Meter.priority_rank, Meter.name)
    if vis is not None:
        q = q.where(Meter.id.in_(vis))
    return list(s.scalars(q))


@router.get("/overview", response_class=HTMLResponse)
def overview(
    request: Request, user: User = Depends(current_user), s: Session = Depends(db)
) -> Response:
    ctx = get_ctx(request)
    vis = visible(s, user, ctx)
    now = ctx.clock()
    week = now - timedelta(days=7)

    def scoped(q: Any, col: Any) -> Any:
        return q if vis is None else q.where(col.in_(vis))

    open_articles = (
        s.scalar(
            scoped(
                select(func.count())
                .select_from(Article)
                .where(Article.state.in_(("in_review", "needs_attention", "stale"))),
                Article.meter_id,
            )
        )
        or 0
    )
    attention = (
        s.scalar(
            scoped(
                select(func.count()).select_from(Article).where(Article.state == "needs_attention"),
                Article.meter_id,
            )
        )
        or 0
    )
    leads = (
        s.scalar(
            scoped(
                select(func.count())
                .select_from(ArticleGroup)
                .where(ArticleGroup.state == "needs_sources"),
                ArticleGroup.meter_id,
            )
        )
        or 0
    )
    approved = (
        s.scalar(
            scoped(
                select(func.count()).select_from(Article).where(Article.state == "approved"),
                Article.meter_id,
            )
        )
        or 0
    )
    events_week = dict(
        s.execute(
            scoped(
                select(ChangeEvent.change_type, func.count()).where(
                    ChangeEvent.detected_at >= week
                ),
                ChangeEvent.meter_id,
            ).group_by(ChangeEvent.change_type)
        ).all()
    )
    news_week = dict(
        s.execute(
            scoped(
                select(NewsItem.state, func.count()).where(NewsItem.found_at >= week),
                NewsItem.meter_id,
            ).group_by(NewsItem.state)
        ).all()
    )
    data: dict[str, Any] = {
        "nav": "overview",
        "open_articles": open_articles,
        "attention": attention,
        "leads": leads,
        "approved": approved,
        "events_week": events_week,
        "news_week": news_week,
        "shadow": not ctx.cfg(s).tunables.publish.enabled,
    }
    if user.role == "superadmin":
        runs = list(
            s.scalars(
                select(Run)
                .where(Run.trigger.like("cli:run%"))
                .order_by(Run.started_at.desc())
                .limit(7)
            )
        )
        cost = dict(
            s.execute(
                select(LLMCall.agent, func.sum(LLMCall.cost_usd))
                .where(LLMCall.created_at >= week)
                .group_by(LLMCall.agent)
            ).all()
        )
        data.update(
            runs=runs,
            last_warnings=((runs[0].stats or {}).get("warnings", []) if runs else []),
            last_errors=((runs[0].stats or {}).get("stage_errors", {}) if runs else {}),
            cost=cost,
            cost_total=sum(float(v or 0) for v in cost.values()),
            jobs=list(s.scalars(select(Job).order_by(Job.created_at.desc()).limit(5))),
        )
    return page(request, "overview.html", user, data)


@router.get("/updates", response_class=HTMLResponse)
def updates(
    request: Request, user: User = Depends(current_user), s: Session = Depends(db)
) -> Response:
    ctx = get_ctx(request)
    vis = visible(s, user, ctx)
    q = select(ChangeEvent).order_by(ChangeEvent.detected_at.desc()).limit(300)
    if vis is not None:
        q = q.where(ChangeEvent.meter_id.in_(vis))
    meter_q, type_q, state_q = (request.query_params.get(k, "") for k in ("meter", "type", "state"))
    if meter_q:
        q = q.where(ChangeEvent.meter_id == to_uuid(meter_q))
    if type_q in CHANGE_TYPES:
        q = q.where(ChangeEvent.change_type == type_q)
    if state_q:
        q = q.where(ChangeEvent.state == state_q)
    events = list(s.scalars(q))
    articles = {
        a.article_group_id: a.article_id
        for a in s.scalars(
            select(Article).where(
                Article.article_group_id.in_(
                    [e.article_group_id for e in events if e.article_group_id]
                )
            )
        )
    }
    meters = {m.id: m for m in s.scalars(select(Meter))}
    return page(
        request,
        "updates.html",
        user,
        {
            "nav": "updates",
            "events": events,
            "titles": _titles(s, [e.promise_id for e in events]),
            "meters": meters,
            "articles": articles,
            "meter_options": _meter_options(s, vis),
            "types": CHANGE_TYPES,
            "meter_q": meter_q,
            "type_q": type_q,
            "state_q": state_q,
        },
    )


@router.get("/searches", response_class=HTMLResponse)
def searches(
    request: Request, user: User = Depends(current_user), s: Session = Depends(db)
) -> Response:
    ctx = get_ctx(request)
    vis = visible(s, user, ctx)
    meter_q = request.query_params.get("meter", "")
    calls = []
    for c in s.scalars(
        select(LLMCall)
        .where(LLMCall.agent == "news_scout")
        .order_by(LLMCall.created_at.desc())
        .limit(150)
    ):
        parts = (c.subject_id or "").split(":")
        mid = None
        if len(parts) >= 2 and parts[0] == "meter":
            try:
                mid = uuid.UUID(parts[1])
            except ValueError:
                mid = None
        if vis is not None and mid not in vis:
            continue
        if meter_q and str(mid) != meter_q:
            continue
        out = c.output or {}
        results = out.get("results") or []
        calls.append(
            {
                "call": c,
                "meter_id": mid,
                "queries": out.get("search_queries") or [],
                "results": len(results),
            }
        )
    # outcome per run+meter from news_items
    iq = select(NewsItem).order_by(NewsItem.found_at.desc()).limit(400)
    if vis is not None:
        iq = iq.where(NewsItem.meter_id.in_(vis))
    if meter_q:
        iq = iq.where(NewsItem.meter_id == to_uuid(meter_q))
    state_q = request.query_params.get("state", "")
    if state_q in ("accepted", "rejected"):
        iq = iq.where(NewsItem.state == state_q)
    items = list(s.scalars(iq))
    reasons = Counter(i.reject_reason or "accepted" for i in items)
    meters = {m.id: m for m in s.scalars(select(Meter))}
    return page(
        request,
        "searches.html",
        user,
        {
            "nav": "searches",
            "calls": calls,
            "items": items,
            "reasons": reasons,
            "titles": _titles(s, [i.promise_id for i in items]),
            "meters": meters,
            "meter_options": _meter_options(s, vis),
            "meter_q": meter_q,
            "state_q": state_q,
        },
    )


def _visible_source_ids(s: Session, vis: set[uuid.UUID] | None) -> Any:
    q = select(ArticleGroupSource.source_id).join(
        ArticleGroup, ArticleGroup.group_id == ArticleGroupSource.group_id
    )
    if vis is not None:
        q = q.where(ArticleGroup.meter_id.in_(vis))
    return q


@router.get("/sources", response_class=HTMLResponse)
def sources(
    request: Request, user: User = Depends(current_user), s: Session = Depends(db)
) -> Response:
    ctx = get_ctx(request)
    vis = visible(s, user, ctx)
    status_q = request.query_params.get("status", "")
    q = (
        select(Source)
        .where(Source.source_id.in_(_visible_source_ids(s, vis)))
        .order_by(Source.checked_at.desc())
        .limit(300)
    )
    if status_q:
        q = q.where(Source.fetch_status == status_q)
    rows = list(s.scalars(q))
    verdicts: dict[int, Counter[str]] = {}
    for sid, verdict in s.execute(
        select(ArticleGroupSource.source_id, ArticleGroupSource.verdict).where(
            ArticleGroupSource.source_id.in_([r.source_id for r in rows])
        )
    ):
        verdicts.setdefault(sid, Counter())[verdict] += 1
    totals = Counter(r.fetch_status for r in rows)
    return page(
        request,
        "sources.html",
        user,
        {
            "nav": "sources",
            "rows": rows,
            "verdicts": verdicts,
            "status_q": status_q,
            "totals": totals,
        },
    )


@router.get("/sources/{source_id}", response_class=HTMLResponse)
def source_page(
    source_id: int, request: Request, user: User = Depends(current_user), s: Session = Depends(db)
) -> Response:
    ctx = get_ctx(request)
    vis = visible(s, user, ctx)
    src = s.get(Source, source_id)
    if src is None or source_id not in set(s.scalars(_visible_source_ids(s, vis))):
        raise NotFoundError
    rows = s.execute(
        select(ArticleGroupSource, ArticleGroup)
        .join(ArticleGroup, ArticleGroup.group_id == ArticleGroupSource.group_id)
        .where(ArticleGroupSource.source_id == source_id)
    ).all()
    uses = [(ags, g) for ags, g in rows if vis is None or g.meter_id in vis]
    return page(
        request,
        "source.html",
        user,
        {
            "nav": "sources",
            "src": src,
            "uses": uses,
            "titles": _titles(s, [g.promise_id for _a, g in uses]),
            "meters": {m.id: m for m in s.scalars(select(Meter))},
        },
    )
