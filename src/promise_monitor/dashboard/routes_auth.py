from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from promise_monitor import access
from promise_monitor.access import AccessError
from promise_monitor.dashboard.core import (
    current_user,
    db,
    field_str,
    flash,
    form,
    get_ctx,
    page,
)
from promise_monitor.db.models import User
from promise_monitor.logging_setup import get_logger

log = get_logger(__name__)
router = APIRouter()


@router.get("/", include_in_schema=False)
def root() -> Response:
    return RedirectResponse("/overview", status_code=303)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> Response:
    if request.query_params.get("csrf"):
        flash(request, "err", "Your session expired. Please try again.")
    return page(request, "login.html", None, {})


@router.post("/login")
async def login(request: Request, s: Session = Depends(db)) -> Response:
    ctx = get_ctx(request)
    data = await form(request)
    email = field_str(data, "email").strip().lower()
    client = request.client.host if request.client else "?"
    now_t = time.monotonic()
    if ctx.throttle.locked(email, now_t) or ctx.throttle.locked(f"ip:{client}", now_t):
        flash(request, "err", "Too many failed attempts. Try again in 15 minutes.")
        return RedirectResponse("/login", status_code=303)
    user = access.authenticate(s, email, field_str(data, "password"))
    if user is None:
        ctx.throttle.fail(email, now_t)
        ctx.throttle.fail(f"ip:{client}", now_t)
        log.warning("dashboard_login_failed")
        flash(request, "err", "Wrong email or password.")
        return RedirectResponse("/login", status_code=303)
    ctx.throttle.clear(email)
    request.session.clear()  # fresh session on login (fixation)
    request.session["uid"] = str(user.user_id)
    user.last_login_at = ctx.clock()
    access.audit(s, user, "user.login", "user", user.user_id, ctx.clock())
    s.commit()
    return RedirectResponse("/overview", status_code=303)


@router.post("/logout")
async def logout(request: Request) -> Response:
    await form(request)
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@router.get("/account/password", response_class=HTMLResponse)
def password_page(request: Request, user: User = Depends(current_user)) -> Response:
    return page(
        request, "password.html", user, {"nav": "account", "min_len": access.MIN_PASSWORD_LEN}
    )


@router.post("/account/password")
async def change_password(
    request: Request, user: User = Depends(current_user), s: Session = Depends(db)
) -> Response:
    ctx = get_ctx(request)
    data = await form(request)
    me = s.get(User, user.user_id)
    assert me is not None
    current, new, again = (
        field_str(data, "current"),
        field_str(data, "new"),
        field_str(data, "again"),
    )
    if not access.verify_password(current, me.password_hash):
        flash(request, "err", "Current password is wrong.")
    elif new != again:
        flash(request, "err", "The new passwords don't match.")
    elif new == current:
        flash(request, "err", "Choose a password different from the current one.")
    else:
        try:
            me.password_hash = access.hash_password(new)
        except AccessError as exc:
            flash(request, "err", str(exc))
            return RedirectResponse("/account/password", status_code=303)
        me.must_change_password = False
        access.audit(s, me, "user.password", "user", me.user_id, ctx.clock())
        s.commit()
        flash(request, "ok", "Password changed.")
        return RedirectResponse("/overview", status_code=303)
    return RedirectResponse("/account/password", status_code=303)
