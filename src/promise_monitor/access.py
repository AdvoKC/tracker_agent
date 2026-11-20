"""Users, roles and meter access (dashboard + agents).

Roles:
- ``superadmin``: sees and edits everything, manages users, assigns coordinators, grants and
  revokes temporary access, edits per-meter extra outlets (editorial-balance decision).
- ``coordinator``: sees ONLY meters they are actively assigned to — a primary assignment,
  or a temporary grant whose window includes "now" — and edits those meters' search context.

Every change goes through ``audit()``. Passwords: scrypt (stdlib), never stored or logged in clear.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from promise_monitor.db.models import AuditLog, Meter, MeterAssignment, MeterSettings, User

MIN_PASSWORD_LEN = 12
_N, _R, _P = 2**14, 8, 1


class AccessError(ValueError):
    """A request the caller isn't allowed to make, or invalid input."""


# --------------------------------------------------------------------------- passwords


def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LEN:
        raise AccessError(f"password must be at least {MIN_PASSWORD_LEN} characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    b64 = base64.b64encode
    return f"scrypt${_N}${_R}${_P}${b64(salt).decode()}${b64(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode(),
            salt=base64.b64decode(salt_b64),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=32,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, base64.b64decode(hash_b64))


def temporary_password() -> str:
    return secrets.token_urlsafe(12)  # 16 chars


# --------------------------------------------------------------------------- audit


def audit(
    session: Session,
    actor: User | None,
    action: str,
    entity: str,
    entity_id: object,
    now: datetime,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    session.add(
        AuditLog(
            at=now,
            user_id=actor.user_id if actor else None,
            action=action,
            entity=entity,
            entity_id=str(entity_id) if entity_id is not None else None,
            before=before,
            after=after,
        )
    )


# --------------------------------------------------------------------------- users


def create_user(
    session: Session,
    *,
    email: str,
    name: str,
    role: str,
    password: str,
    actor: User | None,
    now: datetime,
    must_change: bool = False,
) -> User:
    email = email.strip().lower()
    if "@" not in email or len(email) > 255:
        raise AccessError("a valid email is required")
    if role not in ("superadmin", "coordinator"):
        raise AccessError("role must be superadmin or coordinator")
    if actor is not None and actor.role != "superadmin":
        raise AccessError("only a superadmin can create users")
    if session.scalars(select(User).where(User.email == email)).first():
        raise AccessError("a user with that email already exists")
    user = User(
        email=email,
        name=name.strip()[:255] or email,
        role=role,
        password_hash=hash_password(password),
        must_change_password=must_change,
        active=True,
        created_at=now,
    )
    session.add(user)
    session.flush()
    audit(
        session,
        actor,
        "user.create",
        "user",
        user.user_id,
        now,
        after={"email": email, "role": role},
    )
    return user


def authenticate(session: Session, email: str, password: str) -> User | None:
    user = session.scalars(select(User).where(User.email == email.strip().lower())).first()
    if user is None:
        hash_password("x" * MIN_PASSWORD_LEN)  # equalise timing for unknown emails
        return None
    if not user.active or not verify_password(password, user.password_hash):
        return None
    return user


# --------------------------------------------------------------------------- assignments


def _active_clause(now: datetime) -> Any:
    return (
        MeterAssignment.revoked_at.is_(None)
        & (MeterAssignment.starts_at <= now)
        & or_(MeterAssignment.ends_at.is_(None), MeterAssignment.ends_at > now)
    )


def active_assignments(
    session: Session,
    now: datetime,
    *,
    meter_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
) -> list[MeterAssignment]:
    q = select(MeterAssignment).where(_active_clause(now))
    if meter_id is not None:
        q = q.where(MeterAssignment.meter_id == meter_id)
    if user_id is not None:
        q = q.where(MeterAssignment.user_id == user_id)
    return list(session.scalars(q.order_by(MeterAssignment.kind, MeterAssignment.starts_at)))


def visible_meter_ids(session: Session, user: User, now: datetime) -> set[uuid.UUID] | None:
    """None = everything (superadmin)."""
    if user.role == "superadmin":
        return None
    return {a.meter_id for a in active_assignments(session, now, user_id=user.user_id)}


def can_view(session: Session, user: User, meter_id: uuid.UUID, now: datetime) -> bool:
    visible = visible_meter_ids(session, user, now)
    return visible is None or meter_id in visible


def can_edit_settings(session: Session, user: User, meter_id: uuid.UUID, now: datetime) -> bool:
    return can_view(session, user, meter_id, now)


def require_superadmin(user: User) -> None:
    if user.role != "superadmin":
        raise AccessError("superadmin only")


def coordinators_for_meter(session: Session, meter_id: uuid.UUID, now: datetime) -> list[User]:
    """Active primary + temporary coordinators (digest recipients), active users only."""
    users = []
    for a in active_assignments(session, now, meter_id=meter_id):
        u = session.get(User, a.user_id)
        if u is not None and u.active and u not in users:
            users.append(u)
    return users


def primary_for_meter(session: Session, meter_id: uuid.UUID, now: datetime) -> User | None:
    for a in active_assignments(session, now, meter_id=meter_id):
        if a.kind == "primary":
            u = session.get(User, a.user_id)
            if u is not None and u.active:
                return u
    return None


def set_primary(
    session: Session, *, meter_id: uuid.UUID, user: User, actor: User, now: datetime
) -> MeterAssignment:
    require_superadmin(actor)
    if user.role != "coordinator" or not user.active:
        raise AccessError("the primary coordinator must be an active coordinator account")
    previous = [
        a for a in active_assignments(session, now, meter_id=meter_id) if a.kind == "primary"
    ]
    for a in previous:
        a.revoked_at, a.revoked_by = now, actor.user_id
    assignment = MeterAssignment(
        meter_id=meter_id,
        user_id=user.user_id,
        kind="primary",
        starts_at=now,
        ends_at=None,
        granted_by=actor.user_id,
        created_at=now,
    )
    session.add(assignment)
    session.flush()
    audit(
        session,
        actor,
        "assignment.primary",
        "meter",
        meter_id,
        now,
        before={"primary": [str(a.user_id) for a in previous]},
        after={"primary": str(user.user_id)},
    )
    return assignment


def grant_temporary(
    session: Session,
    *,
    meter_id: uuid.UUID,
    user: User,
    starts_at: datetime,
    ends_at: datetime,
    reason: str,
    actor: User,
    now: datetime,
) -> MeterAssignment:
    require_superadmin(actor)
    if user.role != "coordinator" or not user.active:
        raise AccessError("temporary access can only be granted to an active coordinator account")
    if ends_at <= starts_at:
        raise AccessError("the end must be after the start")
    if ends_at <= now:
        raise AccessError("the end must be in the future")
    if not reason.strip():
        raise AccessError("a reason is required (e.g. 'covering for X on leave')")
    assignment = MeterAssignment(
        meter_id=meter_id,
        user_id=user.user_id,
        kind="temporary",
        starts_at=starts_at,
        ends_at=ends_at,
        reason=reason.strip()[:500],
        granted_by=actor.user_id,
        created_at=now,
    )
    session.add(assignment)
    session.flush()
    audit(
        session,
        actor,
        "assignment.temporary",
        "meter",
        meter_id,
        now,
        after={
            "user": str(user.user_id),
            "starts_at": starts_at.isoformat(),
            "ends_at": ends_at.isoformat(),
            "reason": assignment.reason,
        },
    )
    return assignment


def revoke(session: Session, *, assignment_id: uuid.UUID, actor: User, now: datetime) -> None:
    require_superadmin(actor)
    a = session.get(MeterAssignment, assignment_id)
    if a is None or a.revoked_at is not None:
        raise AccessError("assignment not found or already revoked")
    a.revoked_at, a.revoked_by = now, actor.user_id
    audit(
        session,
        actor,
        "assignment.revoke",
        "meter",
        a.meter_id,
        now,
        before={"assignment": str(a.assignment_id)},
    )


# --------------------------------------------------------------------------- meter settings


def get_settings(session: Session, meter_id: uuid.UUID) -> MeterSettings:
    s = session.get(MeterSettings, meter_id)
    if s is None:
        s = MeterSettings(
            meter_id=meter_id,
            official_aliases=[],
            search_terms=[],
            exclude_terms=[],
            extra_outlets=[],
        )
    return s


def settings_dict(s: MeterSettings) -> dict[str, Any]:
    return {
        "official_aliases": list(s.official_aliases or []),
        "search_terms": list(s.search_terms or []),
        "exclude_terms": list(s.exclude_terms or []),
        "extra_outlets": list(s.extra_outlets or []),
    }


def update_settings(
    session: Session,
    *,
    meter: Meter,
    actor: User,
    now: datetime,
    official_aliases: list[str],
    search_terms: list[str],
    exclude_terms: list[str],
    extra_outlets: list[dict[str, str]] | None,
) -> MeterSettings:
    if not can_edit_settings(session, actor, meter.id, now):
        raise AccessError("you don't have access to this meter")
    if extra_outlets is not None:
        require_superadmin(actor)
    s = session.get(MeterSettings, meter.id)
    if s is None:
        s = get_settings(session, meter.id)
        session.add(s)
    before = settings_dict(s)
    s.official_aliases, s.search_terms, s.exclude_terms = (
        official_aliases,
        search_terms,
        exclude_terms,
    )
    if extra_outlets is not None:
        s.extra_outlets = extra_outlets
    s.updated_by, s.updated_at = actor.user_id, now
    after = settings_dict(s)
    if before != after:
        audit(session, actor, "meter.settings", "meter", meter.id, now, before=before, after=after)
    session.flush()
    return s
