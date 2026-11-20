"""P12 (PRD §8d): a user's Promise Tracker token, and "Send to Promise Tracker" on approved
articles. All rules live in ``promise_monitor.submissions``; these routes only add the
dashboard's usual guards (signed in, CSRF, 404 for meters the user can't see)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from promise_monitor import access, submissions
from promise_monitor.access import AccessError
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
    visible_meter,
)
from promise_monitor.db.models import Article, Meter, PtSubmission, User

router = APIRouter()
TOKEN_PAGE = "/account/promise-tracker"  # noqa: S105 (a URL path)


@router.get(TOKEN_PAGE, response_class=HTMLResponse)
def token_page(
    request: Request, user: User = Depends(current_user), s: Session = Depends(db)
) -> Response:
    ctx = get_ctx(request)
    now = ctx.clock()
    tok = submissions.active_token(s, user.user_id, now)
    assigned = {a.meter_id for a in access.active_assignments(s, now, user_id=user.user_id)}
    names = {m.id: m.name for m in s.scalars(select(Meter))}
    covered = []
    for raw in tok.meter_ids if tok else []:
        mid = uuid.UUID(raw)
        covered.append({"name": names.get(mid, raw), "assigned": mid in assigned})
    sent = list(
        s.scalars(
            select(PtSubmission)
            .where(PtSubmission.submitted_by == user.user_id)
            .order_by(PtSubmission.submitted_at.desc())
            .limit(50)
        )
    )
    headlines = {
        a.article_id: a.headline
        for a in s.scalars(
            select(Article).where(Article.article_id.in_([x.article_id for x in sent]))
        )
    }
    cfg = ctx.cfg(s)
    return page(
        request,
        "pt_token.html",
        user,
        {
            "nav": "pt_token",
            "tok": tok,
            "covered": covered,
            "sent": sent,
            "headlines": headlines,
            "key_set": ctx.token_box is not None,
            "enabled": cfg.tunables.submissions.enabled,
            "shadow": not cfg.tunables.publish.enabled,
        },
    )


@router.post(TOKEN_PAGE)
async def token_action(
    request: Request, user: User = Depends(current_user), s: Session = Depends(db)
) -> Response:
    ctx = get_ctx(request)
    data = await form(request)
    me = s.get(User, user.user_id)
    assert me is not None and ctx.submission_client is not None
    action = field_str(data, "action")
    try:
        if action == "save":
            tok = submissions.save_token(
                s,
                me,
                field_str(data, "token"),
                box=ctx.token_box,
                client_for=ctx.submission_client,
                now=ctx.clock(),
            )
            flash(request, "ok", f"Token saved (ends …{tok.last4}). It won't be shown again.")
        elif action == "remove":
            removed = submissions.remove_token(s, me, ctx.clock())
            flash(request, "ok", "Token removed." if removed else "No token to remove.")
        else:
            raise AccessError("unknown action")
        s.commit()
    except AccessError as exc:
        s.rollback()
        flash(request, "err", str(exc))
    return RedirectResponse(TOKEN_PAGE, status_code=303)


@router.post("/review/articles/{article_id}/send")
async def send_article(
    article_id: str, request: Request, user: User = Depends(current_user), s: Session = Depends(db)
) -> Response:
    ctx = get_ctx(request)
    await form(request)
    art = s.get(Article, to_uuid(article_id))
    if art is None:
        raise NotFoundError
    visible_meter(s, user, ctx, art.meter_id)  # other meters: 404, as everywhere
    assert ctx.submission_client is not None
    try:
        sub = submissions.send(
            s,
            art,
            actor=user,
            cfg=ctx.cfg(s),
            box=ctx.token_box,
            client_for=ctx.submission_client,
            now=ctx.clock(),
        )
        flash(
            request,
            "ok",
            f"Sent to Promise Tracker for review ({sub.remote_state.replace('_', ' ')}).",
        )
    except AccessError as exc:
        flash(request, "err", str(exc))
    s.commit()  # keeps the audit row of a refusal or failure too
    return RedirectResponse(f"/review/articles/{art.article_id}", status_code=303)
