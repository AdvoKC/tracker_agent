"""Whole daily run against a fake promisetracker API (real HTTP client, MockTransport)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from promise_monitor.api.client import PromiseTrackerClient
from promise_monitor.config import AppConfig, load_app_config
from promise_monitor.db.base import Base
from promise_monitor.db.models import ArticleGroup, Run
from promise_monitor.notify import MemoryMailer
from promise_monitor.pipeline import DailyRun, PipelineDeps
from promise_monitor.sheets.backend import MemorySheets
from tests.conftest import REPO_ROOT
from tests.fakes import BASE_PREFIX, FakeLLM, fixture_handler, load_fixture
from tests.unit.test_verifier import NOW, FakeFetcher

PRESIDENT = "e447b2f3-867c-4f12-a598-521572567fe7"


class FakeAPI:
    """Discovery fixtures + a mutable presidential meter with promise details."""

    def __init__(self) -> None:
        self.page = load_fixture("api_presidential.json")["data"]
        self.down = False
        self.base = fixture_handler()

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            return httpx.Response(503)
        path = request.url.path[len(BASE_PREFIX) :]
        if path == f"meter/{PRESIDENT}/presidential" or path == "presidential":
            return httpx.Response(200, json={"success": True, "data": self.page, "message": ""})
        if path.startswith("meter/"):
            empty = {"promises": [], "all_meters": [], "promise_count": 0}
            return httpx.Response(200, json={"success": True, "data": empty, "message": ""})
        if path.startswith("view/promise/"):
            pid = path.rsplit("/", 1)[-1]
            item = next(p for p in self.page["promises"] if p["id"] == pid)
            return httpx.Response(
                200, json={"success": True, "data": {**item, "updates": []}, "message": ""}
            )
        return self.base(request)

    def set_status(self, idx: int, status: str) -> str:
        p = self.page["promises"][idx]
        p["status"] = p["last_published_status"] = status
        p["updated_at"] = "2026-11-21T09:00:00.000000Z"
        return str(p["id"])


@pytest.fixture
def cfg() -> AppConfig:
    return load_app_config(REPO_ROOT / "config")


def publishing(cfg: AppConfig, channel: str = "sheets") -> AppConfig:
    """Same config with shadow mode off (and the review channel these tests exercise)."""
    t = cfg.tunables
    return cfg.model_copy(
        update={
            "tunables": t.model_copy(
                update={
                    "publish": t.publish.model_copy(update={"enabled": True}),
                    "review": t.review.model_copy(update={"channel": channel}),
                }
            )
        }
    )


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def api() -> FakeAPI:
    return FakeAPI()


def make_run(
    session: Session,
    cfg: AppConfig,
    api: FakeAPI,
    tmp_path: Path,
    sheets: MemorySheets,
    mailer: MemoryMailer,
) -> DailyRun:
    client = PromiseTrackerClient(
        cfg.tunables.api,
        transport=httpx.MockTransport(api.handler),
        sleep=lambda _s: None,
        jitter=lambda: 0.0,
    )
    llm = FakeLLM(by_task={"news_scout": {"search_queries": ["q"], "results": []}})
    deps = PipelineDeps(
        api=client,
        fetcher=FakeFetcher({}),
        llm=llm,
        sheets=sheets,
        mailer=mailer,
        admin_email="admin@x.ng",
        site_dir=tmp_path / "site",
        review_sheet_url="https://docs.google.com/spreadsheets/d/TEST/edit",
    )
    return DailyRun(session, cfg, deps, now=NOW, clock=lambda: NOW)


def test_two_daily_runs_baseline_then_change_reaches_coordinator(
    session: Session, cfg: AppConfig, api: FakeAPI, tmp_path: Path
) -> None:
    sheets, mailer = MemorySheets(), MemoryMailer()
    cfg = publishing(cfg)
    first = make_run(session, cfg, api, tmp_path, sheets, mailer).run()
    assert first.status == "ok", (first.stage_errors, first.warnings)
    assert first.stats["meters_active"] == 21
    assert (tmp_path / "site" / "index.html").exists()
    assert "coordinators" in sheets.tabs
    # admin hears about new meters + unassigned coordinators; no coordinator digest yet
    assert [s for _to, s, _b in mailer.sent if "item(s) to review" in s] == []

    pid = api.set_status(3, "broken")
    mailer.sent.clear()
    second = make_run(session, cfg, api, tmp_path, sheets, mailer).run()
    assert second.stage_errors == {}, second.stage_errors
    assert second.stats["monitor"]["diffs"] == 1  # type: ignore[index]
    # no evidence links on this promise → the verifier can't verify → evidence lead to the Sheet
    group = session.scalars(select(ArticleGroup)).one()
    assert str(group.promise_id) == pid and group.state == "needs_sources" and group.pushed_at
    tab = next(t for t in sheets.tabs if t.endswith(PRESIDENT[:8]))
    assert sheets.tabs[tab][1][1] == "lead"
    digest = [b for _to, s, b in mailer.sent if "item(s) to review" in s]
    assert digest and "evidence lead" in digest[0]
    runs = session.scalars(select(Run).where(Run.trigger == "cli:run")).all()
    assert all(r.finished_at for r in runs)


def test_injected_api_failure_sends_admin_alert(
    session: Session, cfg: AppConfig, api: FakeAPI, tmp_path: Path
) -> None:
    """P7 exit criterion: an alert email arrives on an injected failure."""
    api.down = True
    cfg = publishing(cfg)
    mailer = MemoryMailer()
    rep = make_run(session, cfg, api, tmp_path, MemorySheets(), mailer).run()
    assert rep.status == "failed"
    assert "API unreachable" in rep.stage_errors["discover"]
    assert (tmp_path / "site" / "index.html").exists()  # publishing still ran
    alerts = [(to, s, b) for to, s, b in mailer.sent if s.startswith("[promise-monitor]")]
    assert len(alerts) == 1
    to, subject, body = alerts[0]
    assert to == ["admin@x.ng"] and "FAILED" in subject
    assert "discover: promisetracker.ng API unreachable" in body
    run = session.scalars(select(Run).where(Run.trigger == "cli:run")).one()
    assert run.status == "failed" and json.dumps(run.stats)


def test_stage_crash_is_isolated(
    session: Session, cfg: AppConfig, api: FakeAPI, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import promise_monitor.pipeline as pipeline

    def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("verifier exploded")

    monkeypatch.setattr(pipeline, "Verifier", boom)
    mailer = MemoryMailer()
    rep = make_run(session, cfg, api, tmp_path, MemorySheets(), mailer).run()
    assert rep.status == "degraded"
    assert rep.stage_errors == {"verify": "RuntimeError: verifier exploded"}
    assert "notify" not in rep.stage_errors and "review_push" not in rep.stage_errors
    assert any("verifier exploded" in b for _t, _s, b in mailer.sent)


def test_shadow_mode_holds_approved_articles(
    session: Session, cfg: AppConfig, api: FakeAPI, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import promise_monitor.pipeline as pipeline

    assert cfg.tunables.publish.enabled is False  # shipped config = shadow mode
    called: list[int] = []

    def fake_publish(*_a: object, **_k: object) -> list[object]:
        called.append(1)
        return []

    monkeypatch.setattr(pipeline, "publish_approved", fake_publish)
    rep = make_run(session, cfg, api, tmp_path, MemorySheets(), MemoryMailer()).run()
    assert called == []
    assert rep.stats["published"] == 0
    assert any("shadow mode" in w for w in rep.warnings)
    assert not (tmp_path / "site").exists()


def test_dashboard_channel_announces_leads_without_a_sheet(
    session: Session, cfg: AppConfig, api: FakeAPI, tmp_path: Path
) -> None:
    cfg = publishing(cfg, channel="dashboard")
    sheets, mailer = MemorySheets(), MemoryMailer()
    make_run(session, cfg, api, tmp_path, sheets, mailer).run()
    api.set_status(3, "broken")
    mailer.sent.clear()
    rep = make_run(session, cfg, api, tmp_path, sheets, mailer).run()
    assert rep.stage_errors == {}
    assert sheets.tabs == {}  # nothing pushed to Google Sheets
    group = session.scalars(select(ArticleGroup)).one()
    assert group.state == "needs_sources" and group.pushed_at is not None
    digest = [b for _to, s, b in mailer.sent if "item(s) to review" in s]
    assert digest and "evidence lead" in digest[0] and "http://localhost:8800" in digest[0]
