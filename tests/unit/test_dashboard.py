"""Dashboard over HTTP (FastAPI TestClient + SQLite), including access control."""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from promise_monitor import access
from promise_monitor.config import Settings, load_app_config
from promise_monitor.dashboard.app import create_app, parse_lines, parse_outlets
from promise_monitor.db.base import Base
from promise_monitor.db.models import AuditLog, MeterSettings, User
from tests.conftest import REPO_ROOT
from tests.unit.test_access import PW, meter

NOW = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def world(factory: sessionmaker[Session]) -> dict[str, uuid.UUID]:
    with factory() as s:
        admin = access.create_user(
            s, email="admin@x.ng", name="Admin", role="superadmin", password=PW, actor=None, now=NOW
        )
        amina = access.create_user(
            s,
            email="amina@x.ng",
            name="Amina",
            role="coordinator",
            password=PW,
            actor=admin,
            now=NOW,
        )
        bayo = access.create_user(
            s, email="bayo@x.ng", name="Bayo", role="coordinator", password=PW, actor=admin, now=NOW
        )
        kano, edo = meter(s, "Kano"), meter(s, "Edo")
        access.set_primary(s, meter_id=kano.id, user=amina, actor=admin, now=NOW)
        s.commit()
        return {
            "admin": admin.user_id,
            "amina": amina.user_id,
            "bayo": bayo.user_id,
            "kano": kano.id,
            "edo": edo.id,
        }


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def app_client(factory: sessionmaker[Session], clock: Clock) -> Iterator[TestClient]:
    app = create_app(
        factory,
        load_app_config(REPO_ROOT / "config"),
        Settings(dashboard_secret="t" * 48),
        clock=clock,
    )
    with TestClient(app, base_url="http://localhost") as c:
        yield c


def csrf(client: TestClient, path: str = "/login") -> str:
    m = re.search(r'name="csrf" value="([^"]+)"', client.get(path).text)
    assert m, path
    return m.group(1)


def login(client: TestClient, email: str, password: str = PW) -> None:
    r = client.post(
        "/login",
        data={"email": email, "password": password, "csrf": csrf(client)},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/overview", r.text


# ----------------------------------------------------------------------------- auth


def test_pages_require_login(app_client: TestClient, world: dict[str, uuid.UUID]) -> None:
    for path in ("/meters", f"/meters/{world['kano']}", "/admin/users", "/admin/audit"):
        r = app_client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"


def test_security_headers(app_client: TestClient) -> None:
    r = app_client.get("/login")
    assert "default-src 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["cache-control"] == "no-store"
    assert app_client.get("/docs").status_code == 404  # API docs disabled


def test_wrong_password_and_lockout(app_client: TestClient, world: dict[str, uuid.UUID]) -> None:
    for _ in range(5):
        app_client.post(
            "/login", data={"email": "amina@x.ng", "password": "nope", "csrf": csrf(app_client)}
        )
    r = app_client.post(
        "/login", data={"email": "amina@x.ng", "password": PW, "csrf": csrf(app_client)}
    )
    assert "Too many failed attempts" in r.text  # locked even with the right password


def test_post_without_csrf_is_rejected(app_client: TestClient, world: dict[str, uuid.UUID]) -> None:
    login(app_client, "amina@x.ng")
    r = app_client.post(
        f"/meters/{world['kano']}/settings", data={"official_aliases": "X"}, follow_redirects=False
    )
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_must_change_password_flow(
    app_client: TestClient, factory: sessionmaker[Session], world: dict[str, uuid.UUID]
) -> None:
    with factory() as s:
        u = s.get(User, world["amina"])
        assert u is not None
        u.must_change_password = True
        s.commit()
    login(app_client, "amina@x.ng")
    r = app_client.get("/meters", follow_redirects=False)
    assert r.headers["location"] == "/account/password"
    token = csrf(app_client, "/account/password")
    new = "a much better passphrase"
    r = app_client.post(
        "/account/password", data={"current": PW, "new": new, "again": new, "csrf": token}
    )
    assert "Password changed" in r.text
    with factory() as s:
        u = s.get(User, world["amina"])
        assert (
            u is not None
            and not u.must_change_password
            and access.verify_password(new, u.password_hash)
        )


# ----------------------------------------------------------------------------- coordinator view


def test_coordinator_sees_only_assigned_meters(
    app_client: TestClient, world: dict[str, uuid.UUID]
) -> None:
    login(app_client, "amina@x.ng")
    page = app_client.get("/meters").text
    assert "Governor of Kano" in page and "Governor of Edo" not in page
    assert app_client.get(f"/meters/{world['edo']}").status_code == 404  # not even "forbidden"
    assert app_client.get("/admin/users").status_code == 404
    assert app_client.get(f"/meters/{world['kano']}").status_code == 200


def test_coordinator_edits_search_context_but_not_outlets(
    app_client: TestClient, factory: sessionmaker[Session], world: dict[str, uuid.UUID]
) -> None:
    login(app_client, "amina@x.ng")
    path = f"/meters/{world['kano']}"
    r = app_client.post(
        f"{path}/settings",
        data={
            "official_aliases": (
                "Governor Abba Kabir Yusuf\nKano State Government\n\nkano state government"
            ),
            "search_terms": "Kano\nKano metropolitan",
            "exclude_terms": "Kano Pillars",
            "extra_outlets": "evil.example | Partisan Blog",  # ignored: not a superadmin
            "csrf": csrf(app_client, path),
        },
    )
    assert "Search context saved" in r.text
    with factory() as s:
        st = s.get(MeterSettings, world["kano"])
        assert st is not None
        assert st.official_aliases == [
            "Governor Abba Kabir Yusuf",
            "Kano State Government",
        ]  # deduped
        assert st.exclude_terms == ["Kano Pillars"] and st.extra_outlets == []
        assert s.scalars(select(AuditLog).where(AuditLog.action == "meter.settings")).one()


def test_temporary_access_appears_and_expires(
    app_client: TestClient, clock: Clock, world: dict[str, uuid.UUID]
) -> None:
    login(app_client, "admin@x.ng")
    path = f"/meters/{world['kano']}"
    r = app_client.post(
        f"{path}/grant",
        data={
            "user_id": str(world["bayo"]),
            "starts_at": "2026-11-20T10:00",  # WAT
            "ends_at": "2026-11-27T18:00",
            "reason": "Covering for Amina on leave",
            "csrf": csrf(app_client, path),
        },
    )
    assert "Temporary access granted" in r.text
    app_client.post("/logout", data={"csrf": csrf(app_client, "/meters")})
    login(app_client, "bayo@x.ng")
    clock.now = NOW - timedelta(hours=1)  # 09:00 WAT: before the 10:00 WAT start
    assert "Governor of Kano" not in app_client.get("/meters").text
    clock.now = NOW + timedelta(hours=2)
    assert "Governor of Kano" in app_client.get("/meters").text
    clock.now = datetime(2026, 11, 27, 18, 1, tzinfo=UTC)
    assert "Governor of Kano" not in app_client.get("/meters").text


# ----------------------------------------------------------------------------- superadmin


def test_superadmin_sets_outlets_and_rejects_bad_ones(
    app_client: TestClient, factory: sessionmaker[Session], world: dict[str, uuid.UUID]
) -> None:
    login(app_client, "admin@x.ng")
    path = f"/meters/{world['kano']}"
    good = {"official_aliases": "", "search_terms": "", "exclude_terms": ""}
    r = app_client.post(
        f"{path}/settings",
        data={
            **good,
            "extra_outlets": "https://www.KanoFocus.com/news | Kano Focus",
            "csrf": csrf(app_client, path),
        },
    )
    assert "saved" in r.text
    with factory() as s:
        st = s.get(MeterSettings, world["kano"])
        assert st is not None and st.extra_outlets == [
            {"domain": "kanofocus.com", "name": "Kano Focus"}
        ]
    r = app_client.post(
        f"{path}/settings",
        data={**good, "extra_outlets": "not a domain | X", "csrf": csrf(app_client, path)},
    )
    assert "not a valid domain" in r.text


def test_superadmin_adds_user_with_one_time_password(
    app_client: TestClient, factory: sessionmaker[Session], world: dict[str, uuid.UUID]
) -> None:
    login(app_client, "admin@x.ng")
    r = app_client.post(
        "/admin/users",
        data={
            "name": "Chidi",
            "email": "chidi@x.ng",
            "role": "coordinator",
            "csrf": csrf(app_client, "/admin/users"),
        },
    )
    m = re.search(r"One-time password \(shown once, share it privately\): ([A-Za-z0-9_-]+)", r.text)
    assert m
    with factory() as s:
        u = s.scalars(select(User).where(User.email == "chidi@x.ng")).one()
        assert u.must_change_password and access.verify_password(m.group(1), u.password_hash)
    assert "Audit log" in app_client.get("/admin/audit").text


def test_superadmin_cannot_disable_self(
    app_client: TestClient, world: dict[str, uuid.UUID]
) -> None:
    login(app_client, "admin@x.ng")
    r = app_client.post(
        f"/admin/users/{world['admin']}/toggle", data={"csrf": csrf(app_client, "/admin/users")}
    )
    assert "disable your own account" in r.text


def test_meter_page_lists_promises(
    app_client: TestClient, factory: sessionmaker[Session], world: dict[str, uuid.UUID]
) -> None:
    from promise_monitor.db.models import PromiseIndex, PromiseSnapshot, Run

    with factory() as s:
        run = Run(started_at=NOW, trigger="t")
        s.add(run)
        s.flush()
        pid = uuid.uuid4()
        snap = PromiseSnapshot(
            promise_id=pid,
            meter_id=world["kano"],
            run_id=run.run_id,
            captured_at=NOW,
            title="Build 10 flyovers",
            status="in_the_works",
            category="infrastructure",
            content={
                "title": "Build 10 flyovers",
                "status": "in_the_works",
                "category": "infrastructure",
                "updates": [{"created_at": "2026-10-01T00:00:00+00:00"}],
            },
            fingerprint="f",
            content_hash="h",
        )
        s.add(snap)
        s.flush()
        s.add(
            PromiseIndex(
                promise_id=pid,
                meter_id=world["kano"],
                latest_snapshot_id=snap.snapshot_id,
                content_hash="h",
                fingerprint="f",
                present=True,
                first_seen_at=NOW,
                last_seen_at=NOW,
            )
        )
        s.commit()
    login(app_client, "amina@x.ng")
    page = app_client.get(f"/meters/{world['kano']}").text
    assert "Build 10 flyovers" in page and "In the Works" in page and "2026-10-01" in page


def test_form_parsers() -> None:
    assert parse_lines("  a  b \n\nA B\nc") == ["a b", "c"]
    assert parse_outlets("Daily-Nigerian.com | Daily Nigerian\n") == [
        {"domain": "daily-nigerian.com", "name": "Daily Nigerian"}
    ]
    with pytest.raises(access.AccessError):
        parse_outlets("promisetracker.ng | PT")
