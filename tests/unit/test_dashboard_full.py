"""P11 dashboard: every page, review in the dashboard, assignment from user pages, settings
overrides, background jobs, and coordinator isolation — on realistic seeded data."""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from promise_monitor import access, settings_store
from promise_monitor.config import AppConfig, Settings, load_app_config
from promise_monitor.dashboard.app import create_app
from promise_monitor.db.base import Base
from promise_monitor.db.models import (
    Article,
    ArticleGroup,
    ChangeEvent,
    Job,
    LLMCall,
    Meter,
    MeterAssignment,
    NewsItem,
    Run,
)
from tests.conftest import REPO_ROOT
from tests.fakes import FakeLLM
from tests.unit.test_access import PW, meter
from tests.unit.test_monitor import METER_ID, T0, FakeSite
from tests.unit.test_review_queue import good_body
from tests.unit.test_verifier import (
    EVIDENCE,
    FakeFetcher,
    ok,
    queue_status_and_evidence,
    relevant,
    verifier,
)
from tests.unit.test_writer import GOOD_EDITOR, draft, drafter

NOW = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)


@pytest.fixture
def cfg() -> AppConfig:
    return load_app_config(REPO_ROOT / "config")


@pytest.fixture
def factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _president(s: Session) -> None:
    s.add(
        Meter(
            id=METER_ID,
            meter_type="promise_tracker",
            scope="presidential",
            state_name=None,
            name="President",
            is_current=True,
            registry_status="active",
            baseline_pending=True,
            priority_rank=1,
            source_key="presidential",
            list_endpoint=f"meter/{METER_ID}/presidential",
            public_url="https://promisetracker.ng/presidentialTracker",
            promise_count=53,
            first_seen_at=T0,
            last_seen_at=T0,
            updated_at=T0,
        )
    )
    s.commit()


@pytest.fixture
def world(factory: sessionmaker[Session], cfg: AppConfig) -> dict[str, Any]:
    with factory() as s:
        _president(s)
        gid = queue_status_and_evidence(s, FakeSite(), cfg)
        verifier(s, FakeFetcher({EVIDENCE: ok(EVIDENCE)}), FakeLLM(relevant("S1")), cfg).run()
        s.commit()
        body = good_body(s)
        drafter(
            s, FakeLLM(draft("House CAMA bill passes second reading", body), GOOD_EDITOR), cfg
        ).run()
        s.commit()
        art = s.scalars(select(Article)).one()
        # an evidence lead on another promise
        run = s.scalars(select(Run)).first()
        assert run is not None
        lead_pid = uuid.uuid4()
        lead = ArticleGroup(
            group_id=uuid.uuid4(),
            meter_id=METER_ID,
            promise_id=lead_pid,
            state="needs_sources",
            notes=["blocked: https://x.ng/a blocked the agent's reader"],
            created_at=NOW,
        )
        s.add(lead)
        s.add(
            ChangeEvent(
                run_id=run.run_id,
                meter_id=METER_ID,
                promise_id=lead_pid,
                change_type="news_update",
                dedupe_key="lead-key",
                detected_at=NOW,
                state="needs_sources",
                article_group_id=lead.group_id,
            )
        )
        s.add(
            NewsItem(
                run_id=run.run_id,
                promise_id=lead_pid,
                meter_id=METER_ID,
                url="https://punchng.com/x",
                outlet="Punch",
                headline="Road project begins",
                published_date=NOW.date(),
                summary="s",
                is_concrete=True,
                relevance=0.9,
                search_grounded=True,
                state="accepted",
                found_at=NOW,
            )
        )
        s.add(
            LLMCall(
                run_id=run.run_id,
                agent="news_scout",
                prompt_name="news_scout",
                subject_id=f"meter:{METER_ID}:batch:0",
                model="m",
                prompt_sha256="0" * 64,
                output={
                    "search_queries": ["Tinubu ports Apapa"],
                    "results": [{"url": "https://punchng.com/x"}],
                },
                latency_ms=1000,
                cost_usd=0.2,
                ok=True,
                created_at=NOW,
            )
        )
        s.add(
            Run(
                started_at=NOW,
                finished_at=NOW,
                trigger="cli:run",
                status="degraded",
                stats={"warnings": ["shadow mode: publishing is off"], "stage_errors": {}},
            )
        )
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
        kano = meter(s, "Kano")
        access.set_primary(
            s, meter_id=METER_ID, user=amina, actor=admin, now=NOW - timedelta(days=1)
        )
        access.set_primary(s, meter_id=kano.id, user=bayo, actor=admin, now=NOW - timedelta(days=1))
        s.add(Job(kind="run", argv=["run"], state="done", exit_code=0, output="ok", created_at=NOW))
        s.commit()
        return {
            "article": art.article_id,
            "group": art.article_group_id,
            "lead": lead.group_id,
            "kano": kano.id,
            "admin": admin.user_id,
            "amina": amina.user_id,
            "bayo": bayo.user_id,
            "promise": art.promise_id,
            "run": run.run_id,
            "gid": gid,
        }


class Launcher:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], cwd: Path) -> tuple[int, str]:
        self.calls.append(argv)
        return 0, "fake job output"


@pytest.fixture
def launcher() -> Launcher:
    return Launcher()


@pytest.fixture
def client(
    factory: sessionmaker[Session], cfg: AppConfig, launcher: Launcher, world: dict[str, Any]
) -> Iterator[TestClient]:
    app = create_app(
        factory,
        cfg,
        Settings(dashboard_secret="t" * 48),
        clock=lambda: NOW,
        launcher=launcher,
        jobs_wait=True,
    )
    with TestClient(app, base_url="http://localhost") as c:
        yield c


def csrf(c: TestClient, path: str) -> str:
    m = re.search(r'name="csrf" value="([^"]+)"', c.get(path).text)
    assert m, path
    return m.group(1)


def login(c: TestClient, email: str) -> None:
    r = c.post(
        "/login",
        data={"email": email, "password": PW, "csrf": csrf(c, "/login")},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/overview"


# ----------------------------------------------------------------------------- pages


def test_every_page_renders_for_superadmin(client: TestClient, world: dict[str, Any]) -> None:
    login(client, "admin@x.ng")
    src = re.search(r'href="/sources/(\d+)"', client.get("/sources").text)
    assert src
    pages = [
        "/overview",
        "/review",
        "/review?state=approved",
        "/meters",
        f"/meters/{METER_ID}",
        f"/promises/{world['promise']}",
        f"/review/articles/{world['article']}",
        f"/review/leads/{world['lead']}",
        "/updates",
        "/updates?type=status_change",
        "/searches",
        "/searches?state=accepted",
        "/sources",
        f"/sources/{src.group(1)}",
        "/admin/users",
        f"/admin/users/{world['amina']}",
        "/admin/settings",
        "/admin/jobs",
        "/admin/runs",
        f"/admin/runs/{world['run']}",
        "/admin/llm",
        "/admin/audit",
    ]
    for p in pages:
        r = client.get(p)
        assert r.status_code == 200, (p, r.text[:300])
        # The CSP blocks inline styles/scripts silently, so none may be written.
        assert not re.search(r"\sstyle=|<style|<script>|\son[a-z]+=", r.text), p
        assert '<script src="/static/dashboard.js"></script>' in r.text
    csp = client.get("/overview").headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp
    for asset in ("dashboard.css", "dashboard.js", "advokc-wordmark.svg", "favicon.svg"):
        assert client.get(f"/static/{asset}").status_code == 200, asset
    assert "Tinubu ports Apapa" in client.get("/searches").text  # the query that was run
    assert "Road project begins" in client.get("/searches").text
    assert (
        EVIDENCE in client.get(f"/review/articles/{world['article']}").text
    )  # sources with verdicts
    assert "shadow mode" in client.get("/overview").text


def test_coordinator_isolation_across_pages(client: TestClient, world: dict[str, Any]) -> None:
    login(client, "bayo@x.ng")  # Kano coordinator, not President
    for p in (
        f"/review/articles/{world['article']}",
        f"/review/leads/{world['lead']}",
        f"/promises/{world['promise']}",
        f"/meters/{METER_ID}",
        "/admin/settings",
        "/admin/jobs",
    ):
        assert client.get(p).status_code == 404, p
    assert "Road project begins" not in client.get("/searches").text
    assert EVIDENCE not in client.get("/sources").text
    assert "House CAMA" not in client.get("/review").text
    r = client.post(
        f"/review/articles/{world['article']}",
        data={"action": "approve", "csrf": csrf(client, "/review")},
    )
    assert r.status_code == 404


# ----------------------------------------------------------------------------- review


def test_review_edit_approve_flow(
    client: TestClient, factory: sessionmaker[Session], world: dict[str, Any]
) -> None:
    login(client, "amina@x.ng")
    path = f"/review/articles/{world['article']}"
    with factory() as s:
        art = s.get(Article, world["article"])
        assert art is not None
        good_headline, good = art.headline, art.body_markdown
    bad = good.replace("passed second reading", "sadly passed second reading")
    r = client.post(
        path,
        data={"action": "save", "headline": good_headline, "body": bad, "csrf": csrf(client, path)},
    )
    assert "QA found issues" in r.text and "sadly" in r.text
    with factory() as s:
        assert s.get(Article, world["article"]).state == "needs_attention"  # type: ignore[union-attr]
    r = client.post(
        path,
        data={
            "action": "approve",
            "headline": good_headline,
            "body": good,
            "note": "ok",
            "csrf": csrf(client, path),
        },
    )
    assert "Approved" in r.text and "Shadow mode" in r.text
    with factory() as s:
        art = s.get(Article, world["article"])
        assert art is not None and art.state == "approved" and art.approved_by == "amina@x.ng"
        assert {
            e.state
            for e in s.scalars(
                select(ChangeEvent).where(ChangeEvent.article_group_id == art.article_group_id)
            )
        } == {"approved"}
    r = client.post(path, data={"action": "reject", "csrf": csrf(client, path)})
    assert "can&#39;t be rejected" in r.text or "can't be rejected" in r.text


def test_reject_and_dismiss(
    client: TestClient, factory: sessionmaker[Session], world: dict[str, Any]
) -> None:
    login(client, "amina@x.ng")
    path = f"/review/articles/{world['article']}"
    client.post(
        path, data={"action": "reject", "note": "not newsworthy", "csrf": csrf(client, path)}
    )
    lp = f"/review/leads/{world['lead']}"
    r = client.post(lp, data={"note": "checked", "csrf": csrf(client, lp)})
    assert "Lead dismissed" in r.text
    with factory() as s:
        assert s.get(Article, world["article"]).state == "rejected"  # type: ignore[union-attr]
        assert s.get(ArticleGroup, world["lead"]).state == "rejected"  # type: ignore[union-attr]


# ----------------------------------------------------------------------------- admin


def test_assign_multiple_meters_from_user_page(
    client: TestClient, factory: sessionmaker[Session], world: dict[str, Any]
) -> None:
    login(client, "admin@x.ng")
    up = f"/admin/users/{world['amina']}"
    r = client.post(
        f"{up}/assign",
        data={"meter_ids": [str(METER_ID), str(world["kano"])], "csrf": csrf(client, up)},
    )
    assert "1 meter(s) assigned, 0 removed" in r.text
    with factory() as s:
        prim = {
            a.meter_id
            for a in access.active_assignments(s, NOW, user_id=world["amina"])
            if a.kind == "primary"
        }
        assert prim == {METER_ID, world["kano"]}
        assert access.primary_for_meter(s, world["kano"], NOW).user_id == world["amina"]  # type: ignore[union-attr]
    r = client.post(
        f"{up}/assign", data={"meter_ids": [str(world["kano"])], "csrf": csrf(client, up)}
    )
    assert "0 meter(s) assigned, 1 removed" in r.text
    r = client.post(
        f"{up}/grant",
        data={
            "meter_id": str(METER_ID),
            "starts_at": "2026-11-20T10:00",
            "ends_at": "2026-11-30T10:00",
            "reason": "cover",
            "csrf": csrf(client, up),
        },
    )
    assert "Temporary access to President granted" in r.text
    with factory() as s:
        kinds = {
            a.kind
            for a in s.scalars(
                select(MeterAssignment).where(
                    MeterAssignment.user_id == world["amina"], MeterAssignment.revoked_at.is_(None)
                )
            )
        }
        assert kinds == {"primary", "temporary"}


def test_settings_overrides(
    client: TestClient, factory: sessionmaker[Session], cfg: AppConfig
) -> None:
    login(client, "admin@x.ng")
    r = client.post(
        "/admin/settings",
        data={
            "key": "news.max_calls_per_run",
            "value": "3",
            "action": "save",
            "csrf": csrf(client, "/admin/settings"),
        },
    )
    assert "Saved" in r.text
    r = client.post(
        "/admin/settings",
        data={
            "key": "news.max_calls_per_run",
            "value": "500",
            "action": "save",
            "csrf": csrf(client, "/admin/settings"),
        },
    )
    assert "between" in r.text
    r = client.post(
        "/admin/settings",
        data={
            "key": "publish.enabled",
            "value": "true",
            "action": "save",
            "csrf": csrf(client, "/admin/settings"),
        },
    )
    r = client.post(
        "/admin/settings",
        data={
            "key": "outlets",
            "value": "punchng.com | Punch\nnot a domain",
            "action": "save",
            "csrf": csrf(client, "/admin/settings"),
        },
    )
    assert "not a valid domain" in r.text
    r = client.post(
        "/admin/settings",
        data={
            "key": "secret.key",
            "value": "1",
            "action": "save",
            "csrf": csrf(client, "/admin/settings"),
        },
    )
    assert "can&#39;t be changed" in r.text or "can't be changed" in r.text
    with factory() as s:
        eff = settings_store.effective_config(s, cfg)
        assert eff.tunables.news.max_calls_per_run == 3 and eff.tunables.publish.enabled is True
        assert eff.tunables.news.batch_size == cfg.tunables.news.batch_size
    client.post(
        "/admin/settings",
        data={
            "key": "news.max_calls_per_run",
            "action": "reset",
            "csrf": csrf(client, "/admin/settings"),
        },
    )
    with factory() as s:
        assert (
            settings_store.effective_config(s, cfg).tunables.news.max_calls_per_run
            == cfg.tunables.news.max_calls_per_run
        )


def test_jobs_whitelist_and_targets(
    client: TestClient, factory: sessionmaker[Session], launcher: Launcher, world: dict[str, Any]
) -> None:
    login(client, "admin@x.ng")
    mp = f"/meters/{METER_ID}"
    r = client.post(
        "/admin/jobs",
        data={
            "kind": "news_scan_meter",
            "target": str(METER_ID),
            "back": mp,
            "csrf": csrf(client, mp),
        },
    )
    assert "fake job output" in r.text
    assert launcher.calls[-1] == ["news-scan", "--meter", str(METER_ID)]
    r = client.post(
        "/admin/jobs",
        data={
            "kind": "news_scan_meter",
            "target": "; rm -rf /",
            "back": mp,
            "csrf": csrf(client, mp),
        },
    )
    assert "invalid target" in r.text
    r = client.post("/admin/jobs", data={"kind": "shell", "csrf": csrf(client, "/admin/jobs")})
    assert "unknown operation" in r.text
    client.post(
        "/admin/jobs",
        data={
            "kind": "redraft_article",
            "target": str(world["article"]),
            "csrf": csrf(client, "/admin/jobs"),
        },
    )
    assert launcher.calls[-1] == ["redraft", "--article", str(world["article"])]
    with factory() as s:
        assert all(j.state == "done" for j in s.scalars(select(Job)))
    client.post("/logout", data={"csrf": csrf(client, "/overview")})
    login(client, "amina@x.ng")
    r = client.post("/admin/jobs", data={"kind": "run", "csrf": csrf(client, "/overview")})
    assert r.status_code == 404  # coordinators can't start operations


def test_open_redirect_is_blocked(client: TestClient, world: dict[str, Any]) -> None:
    login(client, "admin@x.ng")
    mp = f"/meters/{METER_ID}"
    r = client.post(
        f"/meters/{METER_ID}/primary",
        data={"user_id": str(world["amina"]), "back": "//evil.example/x", "csrf": csrf(client, mp)},
        follow_redirects=False,
    )
    assert r.headers["location"] == mp
