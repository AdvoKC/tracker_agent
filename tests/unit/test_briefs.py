"""Coordinator briefs ("Write an update"): validation, access, verification and drafting.

A brief must take the same road as a detected change: its links are verified like any source,
the Writer gets it as data, QA still applies, and the draft lands in the review queue.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from promise_monitor import jobs
from promise_monitor.access import AccessError
from promise_monitor.briefs import create_brief, parse_urls
from promise_monitor.config import AppConfig
from promise_monitor.db.models import Article, ArticleGroup, ChangeEvent, Job, User
from tests.fakes import FakeLLM
from tests.unit.test_dashboard_full import NOW, Launcher, csrf, login
from tests.unit.test_dashboard_full import cfg as cfg  # pytest fixtures
from tests.unit.test_dashboard_full import client as client
from tests.unit.test_dashboard_full import factory as factory
from tests.unit.test_dashboard_full import launcher as launcher
from tests.unit.test_dashboard_full import world as world
from tests.unit.test_verifier import FakeFetcher, ok, relevant, verifier
from tests.unit.test_writer import GOOD_EDITOR, draft, drafter

BRIEF_URL = "https://punchng.com/fg-suspends-cama-review"
DESC = "The Federal Government has suspended the review of the Companies and Allied Matters Act."


def _user(s: Session, email: str) -> User:
    return s.scalars(select(User).where(User.email == email)).one()


def _brief(s: Session, email: str, pid: uuid.UUID, **kw: Any) -> Any:
    args: dict[str, Any] = {"description": DESC, "rating": "broken", "urls_raw": BRIEF_URL}
    args.update(kw)
    return create_brief(s, actor=_user(s, email), promise_id=pid, now=NOW, **args)


# ----------------------------------------------------------------------------- validation


def test_parse_urls_rules() -> None:
    assert parse_urls(f"{BRIEF_URL}\n{BRIEF_URL}/") == [BRIEF_URL]
    with pytest.raises(AccessError, match="at least one"):
        parse_urls("  ")
    with pytest.raises(AccessError, match="not a web link"):
        parse_urls("javascript:alert(1)")
    with pytest.raises(AccessError, match="at most"):
        parse_urls("\n".join(f"https://punchng.com/{i}" for i in range(6)))


def test_brief_validation_and_access(factory: sessionmaker[Session], world: dict[str, Any]) -> None:
    pid = world["promise"]
    with factory() as s:
        with pytest.raises(AccessError, match="characters"):
            _brief(s, "amina@x.ng", pid, description="too short")
        with pytest.raises(AccessError, match="rating"):
            _brief(s, "amina@x.ng", pid, rating="great")
        # Bayo coordinates Kano only: the President meter's promise is off limits.
        with pytest.raises(AccessError, match="access"):
            _brief(s, "bayo@x.ng", pid)
        b = _brief(s, "amina@x.ng", pid)
        s.commit()
        ev = s.get(ChangeEvent, b.event_id)
        assert ev is not None and ev.change_type == "coordinator_update"
        assert (ev.old_status, ev.new_status, ev.state) == ("kept", "broken", "queued")
        assert ev.brief == DESC and ev.brief_urls == [BRIEF_URL]
        # One brief at a time per promise while it is being processed.
        with pytest.raises(AccessError, match="already being written"):
            _brief(s, "admin@x.ng", pid)


# ----------------------------------------------------------------------------- pipeline


def test_brief_is_verified_then_drafted_as_a_rating_change(
    factory: sessionmaker[Session], world: dict[str, Any], cfg: AppConfig
) -> None:
    pid = world["promise"]
    with factory() as s:
        b = _brief(s, "amina@x.ng", pid)
        s.commit()
        fetcher = FakeFetcher({BRIEF_URL: ok(BRIEF_URL)})
        judge = FakeLLM(relevant("S1"))
        verifier(s, fetcher, judge, cfg).run(only=b.group_id)
        s.commit()
        group = s.get(ArticleGroup, b.group_id)
        assert group is not None and group.state == "verified"
        assert BRIEF_URL in fetcher.calls
        assert "coordinator's update (rating: Broken)" in judge.requests[0].user

        body = (
            "The Federal Government has suspended the review of the Companies and Allied "
            "Matters Act, according to [a report by Punch](" + BRIEF_URL + "). The report "
            "states that work on the amendment has stopped and that no new date has been set "
            "for the bill to return to the National Assembly. The suspension affects the "
            "promise made by the House of Representatives to review the law to remove "
            "barriers facing civil society organisations in Nigeria. "
            f"[This promise](https://promisetracker.ng/blog/{pid}) "
            "is now rated, \u2018BROKEN.\u2019"
        )
        writer = FakeLLM(draft("FG Suspends CAMA Review", body), GOOD_EDITOR)
        art = drafter(s, writer, cfg).draft(group)
        s.commit()
        wi = art.writer_input
        assert wi["promise"]["current_status"] == "Broken"
        types = [c["type"] for c in wi["changes"]]
        assert types == ["status_change", "coordinator_update"]
        assert wi["changes"][0] == {
            "type": "status_change",
            "old_status": "Kept",
            "new_status": "Broken",
        }
        assert wi["changes"][1]["description"] == DESC
        assert art.state == "in_review", art.qa_flags
        # The brief is data inside the input JSON, not part of the system prompt.
        assert DESC not in writer.requests[0].system


def test_unverifiable_brief_becomes_a_lead_not_a_draft(
    factory: sessionmaker[Session], world: dict[str, Any], cfg: AppConfig
) -> None:
    with factory() as s:
        b = _brief(s, "amina@x.ng", world["promise"])
        s.commit()
        verifier(s, FakeFetcher({}), FakeLLM(relevant("S1")), cfg).run(only=b.group_id)
        s.commit()
        group = s.get(ArticleGroup, b.group_id)
        assert group is not None and group.state == "needs_sources"
        assert not s.scalars(select(Article).where(Article.article_group_id == b.group_id)).all()


# ----------------------------------------------------------------------------- jobs + UI


def test_coordinator_can_start_only_brief_jobs_on_own_meters(
    factory: sessionmaker[Session], world: dict[str, Any]
) -> None:
    with factory() as s:
        b = _brief(s, "amina@x.ng", world["promise"])
        s.commit()
        amina, bayo = _user(s, "amina@x.ng"), _user(s, "bayo@x.ng")
    fake = Launcher()
    jid = jobs.start_job(
        factory,
        kind="write_brief",
        target=str(b.group_id),
        actor=amina,
        now=NOW,
        launcher=fake,
        wait=True,
    )
    assert fake.calls == [["write-brief", "--group", str(b.group_id)]]
    with factory() as s:
        job = s.get(Job, jid)
        assert job is not None and job.state == "done"
    with pytest.raises(AccessError, match="target not found"):
        jobs.start_job(
            factory,
            kind="write_brief",
            target=str(b.group_id),
            actor=bayo,
            now=NOW,
            launcher=fake,
            wait=True,
        )
    with pytest.raises(AccessError):
        jobs.start_job(
            factory, kind="run", target=None, actor=amina, now=NOW, launcher=fake, wait=True
        )


def test_write_update_from_promise_page(
    client: TestClient, world: dict[str, Any], launcher: Launcher
) -> None:
    pid = world["promise"]
    login(client, "amina@x.ng")
    page = client.get(f"/promises/{pid}").text
    assert "Write an update" in page and f'action="/promises/{pid}/brief"' in page
    token = csrf(client, f"/promises/{pid}")
    r = client.post(
        f"/promises/{pid}/brief",
        data={"csrf": token, "description": "short", "rating": "broken", "urls": BRIEF_URL},
    )
    assert "characters" in r.text
    r = client.post(
        f"/promises/{pid}/brief",
        data={"csrf": token, "description": DESC, "rating": "broken", "urls": BRIEF_URL},
    )
    assert r.status_code == 200 and "Update requested" in r.text
    assert launcher.calls[-1][:2] == ["write-brief", "--group"]
    assert "Updates requested" in r.text and DESC in r.text
    # A coordinator of another meter cannot even see the form's target.
    client.post("/logout", data={"csrf": csrf(client, "/overview")})
    login(client, "bayo@x.ng")
    r = client.post(
        f"/promises/{pid}/brief",
        data={
            "csrf": csrf(client, "/overview"),
            "description": DESC,
            "rating": "broken",
            "urls": BRIEF_URL,
        },
    )
    assert r.status_code == 404
