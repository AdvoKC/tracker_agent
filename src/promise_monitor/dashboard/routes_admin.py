from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from promise_monitor import access, jobs, settings_store
from promise_monitor.access import AccessError
from promise_monitor.dashboard.core import (
    NotFoundError,
    db,
    field_str,
    flash,
    form,
    get_ctx,
    page,
    parse_outlets,
    safe_back,
    superadmin,
    to_uuid,
)
from promise_monitor.db.models import AuditLog, Job, LLMCall, Meter, MeterAssignment, Run, User

router = APIRouter(prefix="/admin")


# --------------------------------------------------------------------------- users


@router.get("/users", response_class=HTMLResponse)
def users_page(
    request: Request, user: User = Depends(superadmin), s: Session = Depends(db)
) -> Response:
    ctx = get_ctx(request)
    now = ctx.clock()
    meters = {m.id: m.name for m in s.scalars(select(Meter))}
    rows = []
    for u in s.scalars(select(User).order_by(User.role.desc(), User.name)):
        names = [
            meters.get(a.meter_id, "?") + (" (temporary)" if a.kind == "temporary" else "")
            for a in access.active_assignments(s, now, user_id=u.user_id)
        ]
        rows.append({"u": u, "meters": names, "last_login": ctx.local(u.last_login_at)})
    return page(request, "users.html", user, {"nav": "users", "rows": rows})


@router.get("/users/{user_id}", response_class=HTMLResponse)
def user_page(
    user_id: str, request: Request, user: User = Depends(superadmin), s: Session = Depends(db)
) -> Response:
    ctx = get_ctx(request)
    target = s.get(User, to_uuid(user_id))
    if target is None:
        raise NotFoundError
    now = ctx.clock()
    assigns = list(
        s.scalars(
            select(MeterAssignment)
            .where(MeterAssignment.user_id == target.user_id, MeterAssignment.revoked_at.is_(None))
            .order_by(MeterAssignment.kind, MeterAssignment.starts_at)
        )
    )
    meters = list(
        s.scalars(
            select(Meter)
            .where(Meter.registry_status == "active")
            .order_by(Meter.priority_rank, Meter.name)
        )
    )
    names = {m.id: m.name for m in s.scalars(select(Meter))}
    primary_of = {
        a.meter_id
        for a in access.active_assignments(s, now, user_id=target.user_id)
        if a.kind == "primary"
    }
    current = []
    for a in assigns:
        ends = (
            a.ends_at.replace(tzinfo=now.tzinfo)
            if a.ends_at and a.ends_at.tzinfo is None
            else a.ends_at
        )
        if ends is not None and ends <= now:
            continue
        current.append(
            {
                "id": a.assignment_id,
                "meter": names.get(a.meter_id, "?"),
                "meter_id": a.meter_id,
                "kind": a.kind,
                "reason": a.reason,
                "window": f"{ctx.local(a.starts_at)} → "
                + (ctx.local(a.ends_at) if a.ends_at else "until changed"),
            }
        )
    return page(
        request,
        "user.html",
        user,
        {
            "nav": "users",
            "t": target,
            "current": current,
            "meters": meters,
            "primary_of": primary_of,
            "now_local": ctx.local(now, "%Y-%m-%dT%H:%M"),
            "last_login": ctx.local(target.last_login_at),
        },
    )


@router.post("/users/{user_id}/assign")
async def assign_primary(
    user_id: str, request: Request, user: User = Depends(superadmin), s: Session = Depends(db)
) -> Response:
    ctx = get_ctx(request)
    data = await form(request)
    target = s.get(User, to_uuid(user_id))
    if target is None:
        raise NotFoundError
    now = ctx.clock()
    wanted = {to_uuid(str(v)) for v in data.getlist("meter_ids")}
    current = {
        a.meter_id
        for a in access.active_assignments(s, now, user_id=target.user_id)
        if a.kind == "primary"
    }
    try:
        for mid in sorted(wanted - current):
            if s.get(Meter, mid) is None:
                raise AccessError("unknown meter")
            access.set_primary(s, meter_id=mid, user=target, actor=user, now=now)
        for a in access.active_assignments(s, now, user_id=target.user_id):
            if a.kind == "primary" and a.meter_id in current - wanted:
                access.revoke(s, assignment_id=a.assignment_id, actor=user, now=now)
        s.commit()
        added, removed = len(wanted - current), len(current - wanted)
        flash(request, "ok", f"Saved: {added} meter(s) assigned, {removed} removed.")
    except AccessError as exc:
        s.rollback()
        flash(request, "err", str(exc))
    return RedirectResponse(f"/admin/users/{target.user_id}", status_code=303)


@router.post("/users/{user_id}/grant")
async def grant_cover(
    user_id: str, request: Request, user: User = Depends(superadmin), s: Session = Depends(db)
) -> Response:
    ctx = get_ctx(request)
    data = await form(request)
    target = s.get(User, to_uuid(user_id))
    if target is None:
        raise NotFoundError
    meter = s.get(Meter, to_uuid(field_str(data, "meter_id")))
    try:
        if meter is None:
            raise AccessError("choose a meter")
        access.grant_temporary(
            s,
            meter_id=meter.id,
            user=target,
            starts_at=ctx.parse_local(field_str(data, "starts_at")),
            ends_at=ctx.parse_local(field_str(data, "ends_at")),
            reason=field_str(data, "reason"),
            actor=user,
            now=ctx.clock(),
        )
        s.commit()
        flash(request, "ok", f"Temporary access to {meter.name} granted. It ends automatically.")
    except AccessError as exc:
        s.rollback()
        flash(request, "err", str(exc))
    return RedirectResponse(f"/admin/users/{target.user_id}", status_code=303)


@router.post("/users")
async def add_user(
    request: Request, user: User = Depends(superadmin), s: Session = Depends(db)
) -> Response:
    ctx = get_ctx(request)
    data = await form(request)
    temp = access.temporary_password()
    try:
        new = access.create_user(
            s,
            email=field_str(data, "email"),
            name=field_str(data, "name"),
            role=field_str(data, "role") or "coordinator",
            password=temp,
            actor=user,
            now=ctx.clock(),
            must_change=True,
        )
        s.commit()
        flash(
            request,
            "ok",
            f"Added {new.email}. One-time password (shown once, share it privately): {temp}",
        )
        return RedirectResponse(f"/admin/users/{new.user_id}", status_code=303)
    except AccessError as exc:
        s.rollback()
        flash(request, "err", str(exc))
    return RedirectResponse("/admin/users", status_code=303)


def _target(s: Session, raw: str) -> User:
    u = s.get(User, to_uuid(raw))
    if u is None:
        raise NotFoundError
    return u


@router.post("/users/{user_id}/toggle")
async def toggle_user(
    user_id: str, request: Request, user: User = Depends(superadmin), s: Session = Depends(db)
) -> Response:
    ctx = get_ctx(request)
    data = await form(request)
    target = _target(s, user_id)
    if target.user_id == user.user_id:
        flash(request, "err", "You can't disable your own account.")
    else:
        target.active = not target.active
        access.audit(
            s,
            user,
            "user.toggle",
            "user",
            target.user_id,
            ctx.clock(),
            after={"active": target.active},
        )
        s.commit()
        flash(request, "ok", f"{target.email} {'enabled' if target.active else 'disabled'}.")
    return RedirectResponse(safe_back(field_str(data, "back"), "/admin/users"), status_code=303)


@router.post("/users/{user_id}/role")
async def change_role(
    user_id: str, request: Request, user: User = Depends(superadmin), s: Session = Depends(db)
) -> Response:
    ctx = get_ctx(request)
    data = await form(request)
    target = _target(s, user_id)
    role = field_str(data, "role")
    if role not in ("superadmin", "coordinator"):
        flash(request, "err", "unknown role")
    elif target.user_id == user.user_id:
        flash(request, "err", "You can't change your own role.")
    else:
        before = target.role
        target.role = role
        access.audit(
            s,
            user,
            "user.role",
            "user",
            target.user_id,
            ctx.clock(),
            before={"role": before},
            after={"role": role},
        )
        s.commit()
        flash(request, "ok", f"{target.email} is now {role}.")
    return RedirectResponse(f"/admin/users/{target.user_id}", status_code=303)


@router.post("/users/{user_id}/reset")
async def reset_user(
    user_id: str, request: Request, user: User = Depends(superadmin), s: Session = Depends(db)
) -> Response:
    ctx = get_ctx(request)
    data = await form(request)
    target = _target(s, user_id)
    temp = access.temporary_password()
    target.password_hash = access.hash_password(temp)
    target.must_change_password = True
    access.audit(s, user, "user.reset_password", "user", target.user_id, ctx.clock())
    s.commit()
    flash(request, "ok", f"New one-time password for {target.email} (shown once): {temp}")
    return RedirectResponse(safe_back(field_str(data, "back"), "/admin/users"), status_code=303)


# --------------------------------------------------------------------------- settings


@router.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request, user: User = Depends(superadmin), s: Session = Depends(db)
) -> Response:
    ctx = get_ctx(request)
    eff = ctx.cfg(s)
    overrides = settings_store.current_overrides(s)
    rows = []
    for spec in settings_store.EDITABLE:
        value = settings_store.current_value(eff, spec.key)
        default = settings_store.current_value(ctx.base_cfg, spec.key)
        if spec.kind == "outlets":
            value = "\n".join(f"{o['domain']} | {o['name']}" for o in value)
            default = f"{len(default)} outlets in config/news_sources.yaml"
        rows.append(
            {"spec": spec, "value": value, "default": default, "overridden": spec.key in overrides}
        )
    return page(request, "settings.html", user, {"nav": "settings", "rows": rows})


@router.post("/settings")
async def save_setting(
    request: Request, user: User = Depends(superadmin), s: Session = Depends(db)
) -> Response:
    ctx = get_ctx(request)
    data = await form(request)
    key = field_str(data, "key")
    try:
        if field_str(data, "action") == "reset":
            settings_store.reset_setting(s, ctx.base_cfg, key, user, ctx.clock())
            flash(request, "ok", "Reset to the config file value.")
        else:
            spec = settings_store.SPECS.get(key)
            raw: Any = field_str(data, "value")
            if spec is not None and spec.kind == "bool":
                raw = field_str(data, "value") == "true"
            if spec is not None and spec.kind == "outlets":
                raw = parse_outlets(field_str(data, "value"), max_items=60)
                if not raw:
                    raise AccessError("the outlet allowlist can't be empty")
            settings_store.set_setting(s, ctx.base_cfg, key, raw, user, ctx.clock())
            flash(request, "ok", "Saved. It applies from the next run.")
        s.commit()
    except (AccessError, settings_store.SettingError) as exc:
        s.rollback()
        flash(request, "err", str(exc))
    return RedirectResponse("/admin/settings", status_code=303)


# --------------------------------------------------------------------------- jobs


@router.get("/jobs", response_class=HTMLResponse)
def jobs_page(
    request: Request, user: User = Depends(superadmin), s: Session = Depends(db)
) -> Response:
    rows = list(s.scalars(select(Job).order_by(Job.created_at.desc()).limit(100)))
    names = {u.user_id: u.email for u in s.scalars(select(User))}
    return page(
        request,
        "jobs.html",
        user,
        {"nav": "jobs", "rows": rows, "names": names, "kinds": jobs.KINDS},
    )


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_page(
    job_id: str, request: Request, user: User = Depends(superadmin), s: Session = Depends(db)
) -> Response:
    job = s.get(Job, to_uuid(job_id))
    if job is None:
        raise NotFoundError
    return page(
        request,
        "job.html",
        user,
        {"nav": "jobs", "job": job, "label": jobs.KINDS.get(job.kind, (job.kind,))[0]},
    )


@router.post("/jobs")
async def start_job(
    request: Request, user: User = Depends(superadmin), s: Session = Depends(db)
) -> Response:
    ctx = get_ctx(request)
    data = await form(request)
    kind = field_str(data, "kind")
    try:
        jid = jobs.start_job(
            ctx.factory,
            kind=kind,
            target=field_str(data, "target") or None,
            actor=user,
            now=ctx.clock(),
            launcher=ctx.launcher,
            wait=ctx.jobs_wait,
        )
        flash(request, "ok", f"Started: {jobs.KINDS[kind][0]}. This page shows its progress.")
        return RedirectResponse(f"/admin/jobs/{jid}", status_code=303)
    except AccessError as exc:
        flash(request, "err", str(exc))
        return RedirectResponse(safe_back(field_str(data, "back"), "/admin/jobs"), status_code=303)


# --------------------------------------------------------------------------- runs, llm, audit


@router.get("/runs", response_class=HTMLResponse)
def runs_page(
    request: Request, user: User = Depends(superadmin), s: Session = Depends(db)
) -> Response:
    rows = list(s.scalars(select(Run).order_by(Run.started_at.desc()).limit(100)))
    return page(request, "runs.html", user, {"nav": "runs", "rows": rows})


@router.get("/runs/{run_id}", response_class=HTMLResponse)
def run_page(
    run_id: str, request: Request, user: User = Depends(superadmin), s: Session = Depends(db)
) -> Response:
    run = s.get(Run, to_uuid(run_id))
    if run is None:
        raise NotFoundError
    calls = list(
        s.scalars(select(LLMCall).where(LLMCall.run_id == run.run_id).order_by(LLMCall.created_at))
    )
    return page(
        request,
        "run.html",
        user,
        {
            "nav": "runs",
            "run": run,
            "stats": json.dumps(run.stats or {}, indent=1, ensure_ascii=False),
            "calls": calls,
            "cost": sum(float(c.cost_usd or 0) for c in calls),
        },
    )


@router.get("/llm", response_class=HTMLResponse)
def llm_page(
    request: Request, user: User = Depends(superadmin), s: Session = Depends(db)
) -> Response:
    ctx = get_ctx(request)
    since = ctx.clock() - timedelta(days=30)
    rows = s.execute(
        select(LLMCall.agent, LLMCall.model, func.count(), func.sum(LLMCall.cost_usd))
        .where(LLMCall.created_at >= since)
        .group_by(LLMCall.agent, LLMCall.model)
    ).all()
    errors = list(
        s.scalars(
            select(LLMCall)
            .where(LLMCall.ok.is_(False))
            .order_by(LLMCall.created_at.desc())
            .limit(30)
        )
    )
    recent = list(s.scalars(select(LLMCall).order_by(LLMCall.created_at.desc()).limit(50)))
    total = sum(float(r[3] or 0) for r in rows)
    return page(
        request,
        "llm.html",
        user,
        {"nav": "llm", "rows": rows, "errors": errors, "recent": recent, "total": total},
    )


@router.get("/audit", response_class=HTMLResponse)
def audit_page(
    request: Request, user: User = Depends(superadmin), s: Session = Depends(db)
) -> Response:
    ctx = get_ctx(request)
    emails = {u.user_id: u.email for u in s.scalars(select(User))}
    meters = {str(m.id): m.name for m in s.scalars(select(Meter))}
    rows = [
        {
            "when": ctx.local(r.at),
            "who": emails.get(r.user_id, "system") if r.user_id else "system",
            "action": r.action,
            "on": meters.get(r.entity_id or "", f"{r.entity} {str(r.entity_id or '')[:8]}"),
            "before": json.dumps(r.before, indent=1, ensure_ascii=False) if r.before else "",
            "after": json.dumps(r.after, indent=1, ensure_ascii=False) if r.after else "",
        }
        for r in s.scalars(
            select(AuditLog).order_by(AuditLog.at.desc(), AuditLog.id.desc()).limit(300)
        )
    ]
    return page(request, "audit.html", user, {"nav": "audit", "rows": rows})
