from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from promise_monitor.agents.writer import Drafter, run_draft_loop
from promise_monitor.config import AppConfig, load_app_config
from promise_monitor.db.models import Article, ArticleGroup, ChangeEvent
from promise_monitor.domain.qa import QAContext
from promise_monitor.evals import build_cases, qa_context
from promise_monitor.llm.base import LLMError
from tests.conftest import REPO_ROOT
from tests.fakes import FakeLLM
from tests.unit.test_monitor import FakeSite
from tests.unit.test_monitor import session as session
from tests.unit.test_monitor import site as site
from tests.unit.test_qa import BODY, CLOSER, HEADLINE, PDF, PROMISE, VOTES
from tests.unit.test_verifier import (
    EVIDENCE,
    NOW,
    FakeFetcher,
    ok,
    queue_status_and_evidence,
    relevant,
    verifier,
)

GOOD_EDITOR: dict[str, Any] = {"neutral": True, "factually_consistent": True, "issues": []}


@pytest.fixture
def app_cfg() -> AppConfig:
    return load_app_config(REPO_ROOT / "config")


def draft(headline: str = HEADLINE, body: str = BODY) -> dict[str, Any]:
    return {"headline": headline, "body_markdown": body, "sources_used": [1, 2]}


def sample_qa(app_cfg: AppConfig) -> QAContext:
    return QAContext(
        citable_urls=frozenset({PROMISE, PDF, VOTES}),
        promise_url=PROMISE,
        current_status="in_the_works",
        old_status=None,
        has_status_change=False,
        banned_terms=app_cfg.banned_terms,
        min_words=60,
        max_words=100,
    )


def test_passes_first_time(app_cfg: AppConfig) -> None:
    llm = FakeLLM(draft(), GOOD_EDITOR)
    res = run_draft_loop(llm, {"x": 1}, sample_qa(app_cfg), app_cfg.tunables.article)
    assert res.passed and res.attempts == 1 and res.issues == []
    assert [r.task for r in llm.requests] == ["writer", "editor"]


def test_qa_failure_feeds_back_and_skips_editor(app_cfg: AppConfig) -> None:
    bad = draft(body=BODY + " Sadly this is extra.")
    llm = FakeLLM(draft(body=bad["body_markdown"]), draft(), GOOD_EDITOR)
    res = run_draft_loop(llm, {"x": 1}, sample_qa(app_cfg), app_cfg.tunables.article)
    assert res.passed and res.attempts == 2
    assert [r.task for r in llm.requests] == ["writer", "writer", "editor"]  # no editor on QA fail
    assert "editor_feedback" in llm.requests[1].user
    assert any("sadly" in i for i in res.history[0])


def test_editor_rejection_feeds_back(app_cfg: AppConfig) -> None:
    editor_no = {"neutral": False, "factually_consistent": True, "issues": ["'further' implies X"]}
    llm = FakeLLM(draft(), editor_no, draft(), GOOD_EDITOR)
    res = run_draft_loop(llm, {"x": 1}, sample_qa(app_cfg), app_cfg.tunables.article)
    assert res.passed and res.attempts == 2
    assert res.history[0] == ["editor: 'further' implies X"]
    assert "further" in llm.requests[2].user


def test_gives_up_after_max_rewrites_keeping_best(app_cfg: AppConfig) -> None:
    worse = draft(body="Too short.")
    nearly = draft(body=BODY + " Sadly.")
    llm = FakeLLM(worse, nearly, worse, worse)
    res = run_draft_loop(llm, {"x": 1}, sample_qa(app_cfg), app_cfg.tunables.article)
    assert not res.passed
    assert res.attempts == app_cfg.tunables.article.max_rewrites + 1
    assert res.body == nearly["body_markdown"]  # fewest issues wins
    assert res.issues and all(isinstance(i, str) for i in res.issues)


def test_llm_error_propagates(app_cfg: AppConfig) -> None:
    with pytest.raises(LLMError):
        run_draft_loop(FakeLLM(LLMError("x")), {}, sample_qa(app_cfg), app_cfg.tunables.article)


def test_eval_cases_build_and_have_valid_contexts(app_cfg: AppConfig) -> None:
    cases = build_cases()
    assert len(cases) >= 9
    assert {c.name.split(":")[1] for c in cases} >= {"evidence", "status", "verified_partial"}
    for c in cases:
        qa = qa_context(c, app_cfg.banned_terms, 95, 105)
        assert qa.promise_url in qa.citable_urls
        assert len(qa.citable_urls) >= 2


# ----------------------------------------------------------------------------- DB drafter


def verified_group(session: Session, site: FakeSite, app_cfg: AppConfig) -> uuid.UUID:
    gid = queue_status_and_evidence(session, site, app_cfg)
    verifier(session, FakeFetcher({EVIDENCE: ok(EVIDENCE)}), FakeLLM(relevant("S1")), app_cfg).run()
    session.commit()
    return gid


def drafter(session: Session, llm: FakeLLM, app_cfg: AppConfig) -> Drafter:
    return Drafter(
        session,
        llm,
        cfg=app_cfg.tunables.article,
        banned_terms=app_cfg.banned_terms,
        public_promise_url=app_cfg.tunables.api.public_promise_url,
        now=NOW,
    )


def test_drafter_builds_input_from_citable_sources_only(
    session: Session,
    site: FakeSite,
    app_cfg: AppConfig,
) -> None:
    gid = verified_group(session, site, app_cfg)
    llm = FakeLLM(draft("H", "short"), default=draft("H", "short"))
    res = drafter(session, llm, app_cfg).run()
    assert res.needs_attention == 1  # the canned body can't pass QA for this promise
    art = session.scalars(select(Article)).one()
    urls = [s["url"] for s in art.writer_input["sources"]]
    assert EVIDENCE in urls and any("promisetracker.ng/blog/" in u for u in urls)
    assert all(
        s["verdict"] in ("verified", "verified_partial") for s in art.writer_input["sources"]
    )
    types = {c["type"] for c in art.writer_input["changes"]}
    assert types == {"status_change", "new_evidence"}
    assert art.writer_input["promise"]["current_status"] == "Kept"
    assert art.qa_flags and art.rewrite_count == app_cfg.tunables.article.max_rewrites
    group = session.get(ArticleGroup, gid)
    assert group is not None and group.state == "drafted"
    assert set(
        session.scalars(select(ChangeEvent.state).where(ChangeEvent.article_group_id == gid))
    ) == {"drafted"}


def test_drafter_in_review_when_draft_passes(
    session: Session,
    site: FakeSite,
    app_cfg: AppConfig,
) -> None:
    gid = verified_group(session, site, app_cfg)
    group = session.get(ArticleGroup, gid)
    assert group is not None
    promise_url = f"https://promisetracker.ng/blog/{group.promise_id}"
    body = (
        BODY.replace(PROMISE, promise_url)
        .replace(PDF, EVIDENCE)
        .replace(f"[House Votes and Proceedings of 12 November 2026]({VOTES})", "House records")
        .replace(CLOSER, "is now rated, \u2018KEPT.\u2019")  # the promise's rating changed to Kept
    )
    llm = FakeLLM(draft("House CAMA bill passes second reading", body), GOOD_EDITOR)
    res = drafter(session, llm, app_cfg).run()
    art = session.scalars(select(Article)).one()
    assert res.in_review == 1, art.qa_flags
    assert art.state == "in_review"
    assert len(art.source_ids) == 2


def test_drafter_defers_on_llm_outage(
    session: Session,
    site: FakeSite,
    app_cfg: AppConfig,
) -> None:
    gid = verified_group(session, site, app_cfg)
    res = drafter(session, FakeLLM(LLMError("down")), app_cfg).run()
    assert res.deferred == 1
    group = session.get(ArticleGroup, gid)
    assert group is not None and group.state == "verified"
    assert session.scalars(select(Article)).first() is None


def test_one_article_per_group_ever(
    session: Session,
    site: FakeSite,
    app_cfg: AppConfig,
) -> None:
    verified_group(session, site, app_cfg)
    drafter(session, FakeLLM(default=draft("H", "short")), app_cfg).run()
    again = drafter(session, FakeLLM(), app_cfg).run()  # any LLM call would fail
    assert again.groups == 0
