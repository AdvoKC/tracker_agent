"""A page that blocked the agent's reader can be confirmed by a coordinator (PRD §3.4)."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from promise_monitor import access, review
from promise_monitor.access import AccessError
from promise_monitor.agents.verifier import Verifier
from promise_monitor.agents.writer import prepare_group
from promise_monitor.config import AppConfig
from promise_monitor.db.models import (
    ArticleGroup,
    ArticleGroupSource,
    AuditLog,
    ChangeEvent,
    Job,
    Source,
    User,
)
from tests.fakes import FakeLLM
from tests.unit.test_access import PW
from tests.unit.test_dashboard_full import (  # noqa: F401 (fixtures)
    NOW,
    Launcher,
    cfg,
    client,
    csrf,
    factory,
    launcher,
    login,
    world,
)
from tests.unit.test_monitor import METER_ID, FakeSite
from tests.unit.test_monitor import session as session
from tests.unit.test_monitor import site as site
from tests.unit.test_verifier import (
    EVIDENCE,
    NEWS,
    FakeFetcher,
    app_cfg,  # noqa: F401
    blocked,
    queue_news,
    queue_status_and_evidence,
    verdicts,
    verifier,
)

NO_HITS = {"searched": True, "queries": ["q"], "__search_hits__": []}


@pytest.fixture
def boss(session: Session) -> User:
    return access.create_user(
        session,
        email="boss@x.ng",
        name="Boss",
        role="superadmin",
        password=PW,
        actor=None,
        now=NOW,
    )


def blocked_lead(session: Session, site: FakeSite, conf: AppConfig) -> uuid.UUID:
    gid = queue_status_and_evidence(session, site, conf)
    fetcher = FakeFetcher({EVIDENCE: blocked(EVIDENCE)})
    res = verifier(session, fetcher, FakeLLM(NO_HITS), conf).run()
    assert res.needs_sources == 1
    return gid


def evidence_id(session: Session) -> int:
    return session.scalars(select(Source.source_id).where(Source.url == EVIDENCE)).one()


def test_blocked_page_is_flagged_for_confirmation_not_rejected(
    session: Session,
    site: FakeSite,
    app_cfg: AppConfig,  # noqa: F811
) -> None:
    gid = blocked_lead(session, site, app_cfg)
    group = session.get(ArticleGroup, gid)
    assert group is not None and group.state == "needs_sources"
    assert any(n.startswith("blocked:") and "confirm" in n for n in group.notes)
    ags = session.get(ArticleGroupSource, (gid, evidence_id(session)))
    src = session.get(Source, evidence_id(session))
    assert ags is not None and src is not None and review.confirmable(ags, src)


def test_confirming_makes_the_lead_draftable(
    session: Session,
    site: FakeSite,
    app_cfg: AppConfig,  # noqa: F811
    boss: User,
) -> None:
    gid = blocked_lead(session, site, app_cfg)
    group = session.get(ArticleGroup, gid)
    assert group is not None
    ready = review.confirm_source(
        session, group, evidence_id(session), actor=boss, now=NOW, title="CAMA bill passes"
    )
    assert ready and group.state == "verified" and group.sufficient
    assert verdicts(session, gid)[EVIDENCE] == ("required", "verified_partial")
    events = session.scalars(select(ChangeEvent).where(ChangeEvent.article_group_id == gid))
    assert {e.state for e in events} == {"queued"}
    audit = session.scalars(select(AuditLog).where(AuditLog.action == "source.confirm")).one()
    assert audit.after is not None and audit.after["url"] == EVIDENCE
    # the writer cites it, titled as the coordinator saw it
    prep = prepare_group(
        session,
        group,
        app_cfg.tunables.article,
        app_cfg.banned_terms,
        app_cfg.tunables.api.public_promise_url,
        NOW,
    )
    assert EVIDENCE in prep.url_to_source
    titles = [s["title"] for s in prep.writer_input["sources"]]
    assert "CAMA bill passes" in titles
    with pytest.raises(AccessError):  # once only
        review.confirm_source(session, group, evidence_id(session), actor=boss, now=NOW)


def test_confirmation_survives_re_verification(
    session: Session,
    site: FakeSite,
    app_cfg: AppConfig,  # noqa: F811
    boss: User,
) -> None:
    gid = blocked_lead(session, site, app_cfg)
    group = session.get(ArticleGroup, gid)
    assert group is not None
    review.confirm_source(session, group, evidence_id(session), actor=boss, now=NOW)
    Verifier(
        session,
        FakeFetcher({EVIDENCE: blocked(EVIDENCE)}),
        FakeLLM(),  # no LLM call needed: nothing left to judge
        cfg=app_cfg.tunables.verifier,
        public_promise_url=app_cfg.tunables.api.public_promise_url,
        now=NOW,
    ).run(only=gid)
    assert group.state == "verified"
    ags = session.get(ArticleGroupSource, (gid, evidence_id(session)))
    assert ags is not None and ags.confirmed_by == boss.user_id and ags.citable


def test_confirming_a_blocked_news_report(
    session: Session,
    site: FakeSite,
    app_cfg: AppConfig,  # noqa: F811
    boss: User,
) -> None:
    gid = queue_news(session, site, app_cfg, NEWS, date(2026, 11, 16), grounded=False)
    verifier(session, FakeFetcher({NEWS: blocked(NEWS)}), FakeLLM(NO_HITS), app_cfg).run()
    group = session.get(ArticleGroup, gid)
    assert group is not None and group.state == "needs_sources"
    sid = session.scalars(select(Source.source_id).where(Source.url == NEWS)).one()
    assert review.confirm_source(session, group, sid, actor=boss, now=NOW)
    ev = session.scalars(select(ChangeEvent).where(ChangeEvent.article_group_id == gid)).one()
    assert ev.state == "queued" and group.state == "verified"


def test_readable_or_foreign_sources_cannot_be_confirmed(
    session: Session,
    site: FakeSite,
    app_cfg: AppConfig,  # noqa: F811
    boss: User,
) -> None:
    gid = blocked_lead(session, site, app_cfg)
    group = session.get(ArticleGroup, gid)
    assert group is not None
    page = session.scalars(
        select(ArticleGroupSource.source_id).where(
            ArticleGroupSource.group_id == gid, ArticleGroupSource.role == "promise_page"
        )
    ).one()
    with pytest.raises(AccessError):
        review.confirm_source(session, group, page, actor=boss, now=NOW)
    with pytest.raises(AccessError):
        review.confirm_source(session, group, 99999, actor=boss, now=NOW)


# --------------------------------------------------------------------------- dashboard


def add_blocked_lead(factory: sessionmaker[Session], world: dict[str, Any]) -> uuid.UUID:  # noqa: F811
    url = "https://channelstv.com/2026/11/08/kaduna-kano-rail-set-for-december-3-inauguration"
    with factory() as s:
        gid = uuid.uuid4()
        s.add(
            ArticleGroup(
                group_id=gid,
                meter_id=METER_ID,
                promise_id=world["promise"],
                state="needs_sources",
                notes=[f"blocked: {url} blocked the agent's reader"],
                created_at=NOW,
            )
        )
        ev = ChangeEvent(
            run_id=world["run"],
            meter_id=METER_ID,
            promise_id=world["promise"],
            change_type="new_evidence",
            dedupe_key="blocked-lead",
            detected_at=NOW,
            state="needs_sources",
            article_group_id=gid,
        )
        src = Source(url=url, fetch_status="blocked", http_status=403, checked_at=NOW)
        s.add_all([ev, src])
        s.flush()
        s.add(
            ArticleGroupSource(
                group_id=gid,
                source_id=src.source_id,
                role="required",
                event_id=ev.event_id,
                verdict="unverifiable",
                citable=False,
            )
        )
        s.commit()
        return gid


def test_dashboard_confirm_then_draft(
    client: TestClient,  # noqa: F811
    factory: sessionmaker[Session],  # noqa: F811
    world: dict[str, Any],  # noqa: F811
    launcher: Launcher,  # noqa: F811
) -> None:
    gid = add_blocked_lead(factory, world)
    login(client, "amina@x.ng")
    path = f"/review/leads/{gid}"
    page = client.get(path).text
    assert "The agent was blocked" in page and "Confirm source" in page
    assert "Kaduna kano rail set for december 3 inauguration" in page  # headline prefill
    assert "agent blocked: confirm source" in client.get("/review").text
    with factory() as s:
        sid = s.scalars(
            select(ArticleGroupSource.source_id).where(ArticleGroupSource.group_id == gid)
        ).one()
    form = {"action": "confirm", "source_id": str(sid), "title": "Kaduna-Kano rail set"}
    r = client.post(path, data={**form, "csrf": csrf(client, path)})
    assert "Tick the box" in r.text
    r = client.post(path, data={**form, "checked": "yes", "csrf": csrf(client, path)})
    assert "Source confirmed" in r.text and "drafting" in r.text
    assert ["write", "--group", str(gid)] in launcher.calls
    with factory() as s:
        group = s.get(ArticleGroup, gid)
        assert group is not None and group.state == "verified"
        job = s.scalars(select(Job).where(Job.kind == "draft_group")).one()
        assert job.requested_by == world["amina"]


def test_dashboard_confirm_hidden_from_other_meters(
    client: TestClient,  # noqa: F811
    factory: sessionmaker[Session],  # noqa: F811
    world: dict[str, Any],  # noqa: F811
) -> None:
    gid = add_blocked_lead(factory, world)
    login(client, "bayo@x.ng")
    token = csrf(client, "/review")
    r = client.post(
        f"/review/leads/{gid}", data={"action": "confirm", "source_id": "1", "csrf": token}
    )
    assert r.status_code == 404
