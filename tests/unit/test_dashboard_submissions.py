"""P12 in the dashboard: token page, Send to Promise Tracker, guards (PRD §8d)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from promise_monitor.api.submissions import Pacer, SubmissionClient
from promise_monitor.config import AppConfig, Settings
from promise_monitor.dashboard.app import create_app
from promise_monitor.db.models import Article, AuditLog, PtSubmission, PtToken
from tests.fake_submissions import FakeSubmissionBackend, FakeToken, with_submissions
from tests.unit.test_dashboard_full import (  # noqa: F401 (fixtures)
    NOW,
    Launcher,
    cfg,
    csrf,
    factory,
    login,
    world,
)
from tests.unit.test_monitor import METER_ID

TOKEN = "ptsub_" + "d" * 40


@pytest.fixture
def backend(world: dict[str, Any]) -> FakeSubmissionBackend:  # noqa: F811
    b = FakeSubmissionBackend(promise_meters={str(world["promise"]): str(METER_ID)})
    b.tokens[TOKEN] = FakeToken(meter_ids=[str(METER_ID)])
    return b


def make_client(
    factory: sessionmaker[Session],  # noqa: F811
    conf: AppConfig,
    backend: FakeSubmissionBackend,
    *,
    token_key: str | None,
) -> TestClient:
    pacer = Pacer(1.0, sleep=lambda _s: None, clock=lambda: 0.0)

    def sub_client(token: str) -> SubmissionClient:
        return SubmissionClient(
            conf.tunables.submissions,
            conf.tunables.api,
            token,
            transport=backend.transport(),
            pacer=pacer,
            jitter=lambda: 0.0,
        )

    app = create_app(
        factory,
        conf,
        Settings(dashboard_secret="t" * 48, token_key=token_key),
        clock=lambda: NOW,
        launcher=Launcher(),
        jobs_wait=True,
        submission_client=sub_client,
    )
    return TestClient(app, base_url="http://localhost")


@pytest.fixture
def live(
    factory: sessionmaker[Session],  # noqa: F811
    cfg: AppConfig,  # noqa: F811
    backend: FakeSubmissionBackend,
) -> Iterator[TestClient]:
    with make_client(
        factory, with_submissions(cfg), backend, token_key=Fernet.generate_key().decode()
    ) as c:
        yield c


def approve(c: TestClient, factory: sessionmaker[Session], art_id: Any) -> None:  # noqa: F811
    path = f"/review/articles/{art_id}"
    with factory() as s:
        art = s.get(Article, art_id)
        assert art is not None
        data = {"headline": art.headline, "body": art.body_markdown}
    r = c.post(path, data={"action": "approve", **data, "csrf": csrf(c, path)})
    assert "Approved" in r.text


def save_token(c: TestClient, raw: str = TOKEN) -> str:
    page = "/account/promise-tracker"
    return str(c.post(page, data={"action": "save", "token": raw, "csrf": csrf(c, page)}).text)


def test_token_page_saves_and_never_shows_the_token(
    live: TestClient,
    factory: sessionmaker[Session],  # noqa: F811
) -> None:
    login(live, "amina@x.ng")
    r = live.get("/account/promise-tracker")
    assert r.status_code == 200 and "No token saved" in r.text
    r = save_token(live)
    assert "Token saved" in r and TOKEN[-4:] in r and TOKEN not in r
    assert "President" in r  # the meter the token covers
    with factory() as s:
        tok = s.scalars(select(PtToken)).one()
        assert TOKEN not in tok.token_ciphertext
    assert "token check failed" in save_token(live, "ptsub_" + "x" * 40)


def test_approve_then_send(
    live: TestClient,
    factory: sessionmaker[Session],  # noqa: F811
    world: dict[str, Any],  # noqa: F811
    backend: FakeSubmissionBackend,
) -> None:
    login(live, "amina@x.ng")
    path = f"/review/articles/{world['article']}"
    approve(live, factory, world["article"])
    assert "add your Promise Tracker token first" in live.get(path).text
    save_token(live)
    page = live.get(path).text
    assert "Send to Promise Tracker" in page
    r = live.post(f"{path}/send", data={"csrf": csrf(live, path)})
    assert "Sent to Promise Tracker for review" in r.text and "pending review" in r.text
    assert len(backend.submissions) == 1
    r = live.post(f"{path}/send", data={"csrf": csrf(live, path)})
    assert "already sent" in r.text and len(backend.submissions) == 1
    with factory() as s:
        assert s.scalars(select(PtSubmission)).one().article_id == world["article"]


def test_other_meters_coordinator_gets_404(
    live: TestClient,
    world: dict[str, Any],  # noqa: F811
) -> None:
    login(live, "bayo@x.ng")
    path = f"/review/articles/{world['article']}/send"
    token = csrf(live, "/account/promise-tracker")
    assert live.post(path, data={"csrf": token}).status_code == 404


def test_superadmin_without_assignment_cannot_send(
    live: TestClient,
    factory: sessionmaker[Session],  # noqa: F811
    world: dict[str, Any],  # noqa: F811
    backend: FakeSubmissionBackend,
) -> None:
    login(live, "admin@x.ng")
    approve(live, factory, world["article"])
    save_token(live)
    path = f"/review/articles/{world['article']}"
    page = live.get(path).text
    assert "currently assigned" in page and 'action="' + path + '/send"' not in page
    r = live.post(f"{path}/send", data={"csrf": csrf(live, path)})
    assert "Not sent" in r.text and not backend.submissions


def test_shadow_mode_refuses_and_keeps_the_audit_row(
    factory: sessionmaker[Session],  # noqa: F811
    cfg: AppConfig,  # noqa: F811
    world: dict[str, Any],  # noqa: F811
    backend: FakeSubmissionBackend,
) -> None:
    shadow = with_submissions(cfg, publish=False)
    with make_client(factory, shadow, backend, token_key=Fernet.generate_key().decode()) as c:
        login(c, "amina@x.ng")
        approve(c, factory, world["article"])
        save_token(c)
        path = f"/review/articles/{world['article']}"
        assert "shadow mode" in c.get(path).text
        r = c.post(f"{path}/send", data={"csrf": csrf(c, path)})
        assert "Not sent" in r.text and not backend.submissions
    with factory() as s:
        assert s.scalars(select(AuditLog).where(AuditLog.action == "pt_submission.refused")).first()


def test_without_token_key_the_page_explains(
    factory: sessionmaker[Session],  # noqa: F811
    cfg: AppConfig,  # noqa: F811
    world: dict[str, Any],  # noqa: F811
    backend: FakeSubmissionBackend,
) -> None:
    with make_client(factory, with_submissions(cfg), backend, token_key=None) as c:
        login(c, "amina@x.ng")
        r = c.get("/account/promise-tracker")
        assert "PM_TOKEN_KEY" in r.text and 'name="token"' not in r.text


def test_send_requires_csrf(
    live: TestClient,
    world: dict[str, Any],  # noqa: F811
    backend: FakeSubmissionBackend,
) -> None:
    login(live, "amina@x.ng")
    r = live.post(
        f"/review/articles/{world['article']}/send",
        data={"csrf": "wrong"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "/login" in r.headers["location"]
    assert not backend.submissions
