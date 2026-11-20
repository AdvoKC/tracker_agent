from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from promise_monitor import access
from promise_monitor.access import AccessError
from promise_monitor.db.base import Base
from promise_monitor.db.models import AuditLog, Meter, User

NOW = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)
PW = "correct horse battery staple"


def meter(s: Session, name: str = "Kano") -> Meter:
    m = Meter(
        id=uuid.uuid4(),
        meter_type="promise_ring",
        scope="state",
        state_name=name,
        name=f"Governor of {name}",
        is_current=True,
        registry_status="active",
        baseline_pending=False,
        priority_rank=100,
        source_key=f"state:{name}",
        list_endpoint="x",
        public_url="https://promisetracker.ng/x",
        promise_count=0,
        first_seen_at=NOW,
        last_seen_at=NOW,
        updated_at=NOW,
    )
    s.add(m)
    s.flush()
    return m


@pytest.fixture
def s() -> Iterator[Session]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture
def admin(s: Session) -> User:
    return access.create_user(
        s,
        email="Boss@AdvoKC.example",
        name="Boss",
        role="superadmin",
        password=PW,
        actor=None,
        now=NOW,
    )


def coord(s: Session, admin: User, email: str) -> User:
    return access.create_user(
        s,
        email=email,
        name=email.split("@")[0],
        role="coordinator",
        password=PW,
        actor=admin,
        now=NOW,
    )


def test_password_hashing() -> None:
    h = access.hash_password(PW)
    assert h.startswith("scrypt$") and PW not in h
    assert access.verify_password(PW, h)
    assert not access.verify_password(PW + "x", h)
    assert not access.verify_password(PW, "garbage")
    with pytest.raises(AccessError):
        access.hash_password("short")


def test_authenticate_and_inactive(s: Session, admin: User) -> None:
    assert access.authenticate(s, "boss@advokc.example", PW) is admin  # email case-insensitive
    assert access.authenticate(s, "boss@advokc.example", "wrong password!") is None
    assert access.authenticate(s, "nobody@x.ng", PW) is None
    admin.active = False
    assert access.authenticate(s, "boss@advokc.example", PW) is None


def test_only_superadmin_creates_users_and_emails_are_unique(s: Session, admin: User) -> None:
    c = coord(s, admin, "c1@x.ng")
    with pytest.raises(AccessError, match="only a superadmin"):
        access.create_user(
            s, email="c2@x.ng", name="c2", role="coordinator", password=PW, actor=c, now=NOW
        )
    with pytest.raises(AccessError, match="already exists"):
        coord(s, admin, "C1@x.ng")


def test_visibility_primary_and_temporary_windows(s: Session, admin: User) -> None:
    kano, edo = meter(s, "Kano"), meter(s, "Edo")
    amina, bayo = coord(s, admin, "amina@x.ng"), coord(s, admin, "bayo@x.ng")
    access.set_primary(s, meter_id=kano.id, user=amina, actor=admin, now=NOW)
    assert access.visible_meter_ids(s, admin, NOW) is None  # superadmin sees all
    assert access.visible_meter_ids(s, amina, NOW) == {kano.id}
    assert access.visible_meter_ids(s, bayo, NOW) == set()
    assert not access.can_view(s, amina, edo.id, NOW)

    start, end = NOW + timedelta(days=1), NOW + timedelta(days=8)
    grant = access.grant_temporary(
        s,
        meter_id=kano.id,
        user=bayo,
        starts_at=start,
        ends_at=end,
        reason="Amina on leave",
        actor=admin,
        now=NOW,
    )
    assert not access.can_view(s, bayo, kano.id, NOW)  # not started yet
    assert access.can_view(s, bayo, kano.id, NOW + timedelta(days=2))
    assert {
        u.email for u in access.coordinators_for_meter(s, kano.id, NOW + timedelta(days=2))
    } == {
        "amina@x.ng",
        "bayo@x.ng",
    }
    assert not access.can_view(s, bayo, kano.id, end + timedelta(minutes=1))  # expired by itself
    access.revoke(s, assignment_id=grant.assignment_id, actor=admin, now=NOW)
    assert not access.can_view(s, bayo, kano.id, NOW + timedelta(days=2))


def test_grant_validation(s: Session, admin: User) -> None:
    kano = meter(s)
    c = coord(s, admin, "c@x.ng")
    kw: dict[str, Any] = {"meter_id": kano.id, "user": c, "actor": admin, "now": NOW}
    with pytest.raises(AccessError, match="after the start"):
        access.grant_temporary(s, starts_at=NOW, ends_at=NOW, reason="r", **kw)
    with pytest.raises(AccessError, match="reason"):
        access.grant_temporary(s, starts_at=NOW, ends_at=NOW + timedelta(days=1), reason=" ", **kw)
    with pytest.raises(AccessError, match="superadmin"):
        access.grant_temporary(
            s,
            meter_id=kano.id,
            user=c,
            actor=c,
            now=NOW,
            starts_at=NOW,
            ends_at=NOW + timedelta(days=1),
            reason="r",
        )
    with pytest.raises(AccessError, match="coordinator account"):
        access.grant_temporary(
            s,
            meter_id=kano.id,
            user=admin,
            actor=admin,
            now=NOW,
            starts_at=NOW,
            ends_at=NOW + timedelta(days=1),
            reason="r",
        )


def test_new_primary_replaces_old(s: Session, admin: User) -> None:
    kano = meter(s)
    a, b = coord(s, admin, "a@x.ng"), coord(s, admin, "b@x.ng")
    access.set_primary(s, meter_id=kano.id, user=a, actor=admin, now=NOW)
    access.set_primary(s, meter_id=kano.id, user=b, actor=admin, now=NOW + timedelta(hours=1))
    later = NOW + timedelta(hours=2)
    assert access.primary_for_meter(s, kano.id, later) is b
    assert not access.can_view(s, a, kano.id, later)


def test_settings_permissions_and_audit(s: Session, admin: User) -> None:
    kano, edo = meter(s, "Kano"), meter(s, "Edo")
    c = coord(s, admin, "c@x.ng")
    access.set_primary(s, meter_id=kano.id, user=c, actor=admin, now=NOW)
    access.update_settings(
        s,
        meter=kano,
        actor=c,
        now=NOW,
        official_aliases=["Governor Abba Kabir Yusuf"],
        search_terms=["Kano"],
        exclude_terms=[],
        extra_outlets=None,
    )
    with pytest.raises(AccessError, match="superadmin"):
        access.update_settings(
            s,
            meter=kano,
            actor=c,
            now=NOW,
            official_aliases=[],
            search_terms=[],
            exclude_terms=[],
            extra_outlets=[{"domain": "x.ng", "name": "X"}],
        )
    with pytest.raises(AccessError, match="access"):
        access.update_settings(
            s,
            meter=edo,
            actor=c,
            now=NOW,
            official_aliases=["x"],
            search_terms=[],
            exclude_terms=[],
            extra_outlets=None,
        )
    st = access.get_settings(s, kano.id)
    assert st.official_aliases == ["Governor Abba Kabir Yusuf"] and st.updated_by == c.user_id
    entry = s.scalars(select(AuditLog).where(AuditLog.action == "meter.settings")).one()
    assert entry.before == {
        "official_aliases": [],
        "search_terms": [],
        "exclude_terms": [],
        "extra_outlets": [],
    }
    assert entry.after is not None and entry.after["official_aliases"] == [
        "Governor Abba Kabir Yusuf"
    ]
    assert {a.action for a in s.scalars(select(AuditLog))} >= {
        "user.create",
        "assignment.primary",
        "meter.settings",
    }
