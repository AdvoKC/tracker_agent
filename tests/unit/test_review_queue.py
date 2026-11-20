"""End-to-end review loop on an in-memory sheet: push → coordinator acts → sync."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from promise_monitor.agents.review_queue import COORD_HEADER, HEADER, ReviewQueue
from promise_monitor.config import AppConfig, load_app_config
from promise_monitor.db.models import Article, ArticleGroup, ChangeEvent, Meter
from promise_monitor.sheets.backend import GSpreadBackend, MemorySheets, a1
from tests.conftest import REPO_ROOT
from tests.fakes import FakeLLM
from tests.unit.test_monitor import METER_ID, FakeSite
from tests.unit.test_monitor import session as session
from tests.unit.test_monitor import site as site
from tests.unit.test_qa import BODY, CLOSER, PDF, PROMISE, VOTES
from tests.unit.test_verifier import EVIDENCE, NOW, FakeFetcher, blocked, queue_status_and_evidence
from tests.unit.test_writer import GOOD_EDITOR, draft, drafter, verified_group


@pytest.fixture
def app_cfg() -> AppConfig:
    return load_app_config(REPO_ROOT / "config")


def good_body(session: Session) -> str:
    group = session.scalars(select(ArticleGroup)).one()
    return (
        BODY.replace(PROMISE, f"https://promisetracker.ng/blog/{group.promise_id}")
        .replace(PDF, EVIDENCE)
        .replace(f"[House Votes and Proceedings of 12 November 2026]({VOTES})", "House records")
        .replace(CLOSER, "is now rated, \u2018KEPT.\u2019")  # rating changed to Kept
    )


@pytest.fixture
def drafted(session: Session, site: FakeSite, app_cfg: AppConfig) -> Article:
    verified_group(session, site, app_cfg)
    body = good_body(session)
    drafter(
        session, FakeLLM(draft("House CAMA bill passes second reading", body), GOOD_EDITOR), app_cfg
    ).run()
    session.commit()
    art = session.scalars(select(Article)).one()
    assert art.state == "in_review", art.qa_flags
    return art


class Sheet:
    def __init__(self, session: Session, app_cfg: AppConfig) -> None:
        self.session = session
        self.cfg = app_cfg
        self.mem = MemorySheets()
        self.now = NOW

    def sync(self, days: int = 0) -> Any:
        res = ReviewQueue(
            self.session,
            self.mem,
            self.cfg,
            now=self.now + timedelta(days=days),
            admin_email="admin@x.ng",
        ).sync()
        self.session.commit()
        return res

    def meter_tab(self) -> str:
        return next(t for t in self.mem.tabs if t.endswith(str(METER_ID)[:8]))

    def rows(self) -> list[dict[str, str]]:
        grid = self.mem.tabs[self.meter_tab()]
        return [dict(zip(grid[0], r, strict=False)) for r in grid[1:]]

    def set(self, row_id: uuid.UUID, **values: str) -> None:
        grid = self.mem.tabs[self.meter_tab()]
        for r in grid[1:]:
            if r[0] == str(row_id):
                for k, v in values.items():
                    r[grid[0].index(k)] = v
                return
        raise AssertionError("row not found")

    def set_coordinator(self, email: str) -> None:
        grid = self.mem.tabs["coordinators"]
        for r in grid[1:]:
            if r[0] == str(METER_ID):
                r[3] = email


@pytest.fixture
def sheet(session: Session, app_cfg: AppConfig) -> Sheet:
    return Sheet(session, app_cfg)


def test_push_creates_tabs_rows_and_is_idempotent(
    drafted: Article, sheet: Sheet, session: Session
) -> None:
    res = sheet.sync()
    assert res.pushed_articles == 1
    assert sheet.mem.tabs["coordinators"][0] == COORD_HEADER
    assert [r[0] for r in sheet.mem.tabs["coordinators"][1:]] == [str(METER_ID)]
    assert sheet.mem.tabs[sheet.meter_tab()][0] == HEADER
    [row] = sheet.rows()
    assert row["article_id"] == str(drafted.article_id)
    assert row["kind"] == "article" and row["state"] == "in_review"
    assert row["body_markdown"] == drafted.body_markdown
    assert "status:" in row["change_summary"] and EVIDENCE in row["sources"]
    group = session.get(ArticleGroup, drafted.article_group_id)
    assert group is not None and group.state == "in_review"
    assert sheet.sync().pushed_articles == 0
    assert len(sheet.rows()) == 1
    assert "President" in res.unassigned_meters[0]


def test_approve_uses_coordinator_email(drafted: Article, sheet: Sheet, session: Session) -> None:
    sheet.sync()
    sheet.set_coordinator("coord@advokc.example")
    sheet.set(drafted.article_id, decision="approve", decision_note="Looks fine")
    res = sheet.sync()
    assert res.approved == [drafted.article_id]
    art = session.get(Article, drafted.article_id)
    assert art is not None
    assert art.state == "approved" and art.approved_by == "coord@advokc.example"
    assert art.decision_note == "Looks fine"
    [row] = sheet.rows()
    assert (
        row["state"] == "approved" and row["decision"] == "" and row["qa_result"].startswith("OK")
    )
    assert set(session.scalars(select(ChangeEvent.state))) >= {"approved"}
    meter = session.get(Meter, METER_ID)
    assert meter is not None and meter.coordinator_email == "coord@advokc.example"


def test_approving_an_edit_that_breaks_qa_is_refused(
    drafted: Article, sheet: Sheet, session: Session
) -> None:
    sheet.sync()
    original = drafted.body_markdown
    edited = original.replace("passed second reading", "sadly passed second reading")
    sheet.set(drafted.article_id, body_markdown=edited, decision="approve")
    res = sheet.sync()
    assert res.qa_failed == [drafted.article_id] and res.approved == []
    art = session.get(Article, drafted.article_id)
    assert art is not None and art.state == "needs_attention"
    assert art.body_markdown == edited  # the coordinator's text is kept
    [row] = sheet.rows()
    assert "sadly" in row["qa_result"] and row["decision"] == ""
    # coordinator fixes it and approves again
    sheet.set(drafted.article_id, body_markdown=original, decision="approve")
    assert sheet.sync().approved == [drafted.article_id]


def test_edit_decision_rechecks_without_approving(
    drafted: Article, sheet: Sheet, session: Session
) -> None:
    sheet.sync()
    edited = drafted.body_markdown.replace("passed second reading", "passed its second reading")
    sheet.set(drafted.article_id, body_markdown=edited, decision="edit")
    res = sheet.sync()
    assert res.edited_ok == [drafted.article_id]
    art = session.get(Article, drafted.article_id)
    assert art is not None and art.state == "in_review" and art.body_markdown == edited
    assert sheet.rows()[0]["qa_result"].startswith("OK: edited")


def test_reject_consumes_the_change(drafted: Article, sheet: Sheet, session: Session) -> None:
    sheet.sync()
    sheet.set(drafted.article_id, decision="Reject", decision_note="Not newsworthy")
    assert sheet.sync().rejected == [drafted.article_id]
    art = session.get(Article, drafted.article_id)
    group = session.get(ArticleGroup, drafted.article_group_id)
    assert art is not None and art.state == "rejected" and art.decision_note == "Not newsworthy"
    assert group is not None and group.state == "rejected"
    assert set(
        session.scalars(
            select(ChangeEvent.state).where(ChangeEvent.article_group_id == group.group_id)
        )
    ) == {"rejected"}
    # a later decision on a closed article is ignored and the state cell is restored
    sheet.set(drafted.article_id, decision="approve", state="in_review")
    sheet.sync()
    assert sheet.rows()[0]["state"] == "rejected"
    assert session.get(Article, drafted.article_id).state == "rejected"  # type: ignore[union-attr]


def test_unknown_decision_explained(drafted: Article, sheet: Sheet) -> None:
    sheet.sync()
    sheet.set(drafted.article_id, decision="publish it")
    assert sheet.sync().unknown_decisions == 1
    assert "unknown decision" in sheet.rows()[0]["qa_result"]


def test_stale_after_seven_days_but_still_approvable(
    drafted: Article, sheet: Sheet, session: Session
) -> None:
    sheet.sync()
    assert sheet.sync(days=3).stale == []
    assert sheet.sync(days=8).stale == [drafted.article_id]
    assert sheet.rows()[0]["state"] == "stale"
    assert sheet.sync(days=9).stale == []  # reported once
    sheet.set(drafted.article_id, decision="approve")
    assert sheet.sync(days=10).approved == [drafted.article_id]


def test_rows_matched_by_id_after_sorting(drafted: Article, sheet: Sheet) -> None:
    sheet.sync()
    grid = sheet.mem.tabs[sheet.meter_tab()]
    grid.insert(1, ["not-a-uuid", "note row"])  # coordinator adds a junk row above
    sheet.set(drafted.article_id, decision="approve")
    assert sheet.sync().approved == [drafted.article_id]


def test_needs_sources_group_becomes_lead_and_can_be_dismissed(
    session: Session,
    site: FakeSite,
    app_cfg: AppConfig,
    sheet: Sheet,
) -> None:
    from tests.unit.test_verifier import verifier

    gid = queue_status_and_evidence(session, site, app_cfg)
    llm = FakeLLM({"searched": True, "queries": ["q"], "__search_hits__": []})
    verifier(session, FakeFetcher({EVIDENCE: blocked(EVIDENCE)}), llm, app_cfg).run()
    session.commit()
    res = sheet.sync()
    assert res.pushed_leads == 1
    [row] = sheet.rows()
    assert (
        row["kind"] == "lead"
        and row["state"] == "needs_sources"
        and "[unverifiable]" in row["sources"]
    )
    sheet.set(gid, decision="dismiss", decision_note="Checked, nothing new")
    assert sheet.sync().dismissed_leads == [gid]
    group = session.get(ArticleGroup, gid)
    assert group is not None and group.state == "rejected"
    assert sheet.rows()[0]["state"] == "dismissed"


def test_a1_notation() -> None:
    assert [a1(1, 1), a1(2, 26), a1(3, 27), a1(10, 52)] == ["A1", "Z2", "AA3", "AZ10"]


def test_gspread_writes_are_raw() -> None:
    from gspread.utils import ValueInputOption

    calls: list[dict[str, Any]] = []

    class WS:
        def batch_update(self, data: Any, **kw: Any) -> None:
            calls.append(kw)

        def append_rows(self, rows: Any, **kw: Any) -> None:
            calls.append(kw)

    backend = GSpreadBackend.__new__(GSpreadBackend)
    backend._ws = {"t": WS()}  # type: ignore[dict-item]
    backend.write_cells("t", [(2, 9, "=IMPORTXML(evil)")])
    backend.append("t", [["=1+1"]])
    assert all(c["value_input_option"] == ValueInputOption.raw for c in calls)
