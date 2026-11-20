from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from promise_monitor.agents.classifier import NewsFinding, classify
from promise_monitor.agents.verifier import Verifier
from promise_monitor.config import AppConfig, load_app_config
from promise_monitor.db.models import (
    ArticleGroup,
    ArticleGroupSource,
    ChangeEvent,
    Meter,
    NewsItem,
    Run,
    Source,
)
from promise_monitor.domain.fetch import FetchResult
from promise_monitor.llm.base import LLMError
from tests.conftest import REPO_ROOT
from tests.fakes import FakeLLM
from tests.unit.test_extract_fetch import ARTICLE
from tests.unit.test_monitor import METER_ID, FakeSite, Runner
from tests.unit.test_monitor import session as session
from tests.unit.test_monitor import site as site

NOW = datetime(2026, 11, 20, 7, 0, tzinfo=UTC)
EVIDENCE = "https://premiumtimesng.com/news/cama-bill-passes"


class FakeFetcher:
    def __init__(self, pages: dict[str, FetchResult]) -> None:
        self.pages = pages
        self.calls: list[str] = []

    def fetch(self, url: str) -> FetchResult:
        self.calls.append(url)
        return self.pages.get(url) or FetchResult(url, url, "broken", 404, None, b"")


def ok(url: str, body: bytes = ARTICLE) -> FetchResult:
    return FetchResult(url, url, "ok", 200, "text/html", body)


def blocked(url: str) -> FetchResult:
    return FetchResult(url, url, "blocked", 403, "text/html", b"")


def relevant(*refs: str, conf: float = 0.9, yes: bool = True) -> dict[str, Any]:
    return {
        "sources": [
            {"source_ref": r, "relevant": yes, "confidence": conf, "reason": "Same bill."}
            for r in refs
        ]
    }


@pytest.fixture
def app_cfg() -> AppConfig:
    return load_app_config(REPO_ROOT / "config")


def queue_status_and_evidence(
    session: Session,
    site: FakeSite,
    cfg: AppConfig,
    link: str = EVIDENCE,
) -> uuid.UUID:
    """Baseline, then a status change + new evidence on one promise → one queued group."""
    runner = Runner(session, site, cfg.tunables.monitor)
    runner()
    pid = site.ids()[0]
    site.set_status(pid, "kept")
    site.add_update(pid, str(uuid.uuid4()), link)
    res = runner()
    meter = session.get(Meter, METER_ID)
    assert meter is not None
    run = Run(started_at=NOW, trigger="test")
    session.add(run)
    session.flush()
    cls = classify(
        session,
        run_id=run.run_id,
        meter=meter,
        diffs=res.diffs,
        removed=res.removed,
        news=[],
        cfg=cfg.tunables.classifier,
        llm=FakeLLM(default={"score_delta": 0, "reason": "ok"}),
        now=NOW,
    )
    session.commit()
    [gid] = cls.groups
    return gid


def verifier(
    session: Session,
    fetcher: FakeFetcher,
    llm: FakeLLM,
    cfg: AppConfig,
) -> Verifier:
    return Verifier(
        session,
        fetcher,
        llm,
        cfg=cfg.tunables.verifier,
        public_promise_url=cfg.tunables.api.public_promise_url,
        now=NOW,
    )


def verdicts(session: Session, gid: uuid.UUID) -> dict[str, tuple[str, str]]:
    rows = session.execute(
        select(Source.url, ArticleGroupSource.role, ArticleGroupSource.verdict)
        .join(Source, Source.source_id == ArticleGroupSource.source_id)
        .where(ArticleGroupSource.group_id == gid)
    )
    return {url: (role, verdict) for url, role, verdict in rows}


def test_fetched_relevant_source_verifies_group(
    session: Session,
    site: FakeSite,
    app_cfg: AppConfig,
) -> None:
    gid = queue_status_and_evidence(session, site, app_cfg)
    llm = FakeLLM(relevant("S1"))
    res = verifier(session, FakeFetcher({EVIDENCE: ok(EVIDENCE)}), llm, app_cfg).run()
    assert (res.groups, res.verified, res.needs_sources) == (1, 1, 0)
    group = session.get(ArticleGroup, gid)
    assert group is not None and group.state == "verified" and group.sufficient
    v = verdicts(session, gid)
    assert v[EVIDENCE] == ("required", "verified")
    assert any(r == ("promise_page", "verified") for r in v.values())
    src = session.scalars(select(Source).where(Source.url == EVIDENCE)).one()
    assert src.title == "Senate passes CAMA amendment bill"
    assert src.published_date == date(2026, 11, 14)
    assert "Senate passes CAMA" in llm.requests[0].user  # the judge saw the page title


def test_broken_source_means_needs_sources(
    session: Session,
    site: FakeSite,
    app_cfg: AppConfig,
) -> None:
    gid = queue_status_and_evidence(session, site, app_cfg)
    res = verifier(session, FakeFetcher({}), FakeLLM(), app_cfg).run()
    assert res.needs_sources == 1
    assert verdicts(session, gid)[EVIDENCE] == ("required", "broken")
    states = set(
        session.scalars(select(ChangeEvent.state).where(ChangeEvent.article_group_id == gid))
    )
    assert states == {"needs_sources"}


def test_blocked_source_confirmed_by_raw_search_is_partial(
    session: Session,
    site: FakeSite,
    app_cfg: AppConfig,
) -> None:
    gid = queue_status_and_evidence(session, site, app_cfg)
    llm = FakeLLM(
        {"searched": True, "queries": ["q"], "__search_hits__": [EVIDENCE + "/"]},
        relevant("S1"),
    )
    res = verifier(session, FakeFetcher({EVIDENCE: blocked(EVIDENCE)}), llm, app_cfg).run()
    assert res.verified == 1
    assert verdicts(session, gid)[EVIDENCE] == ("required", "verified_partial")
    assert llm.requests[0].tools == ("WebSearch",)
    assert "title only" in llm.requests[1].user


def test_blocked_source_not_in_search_is_unverifiable(
    session: Session,
    site: FakeSite,
    app_cfg: AppConfig,
) -> None:
    gid = queue_status_and_evidence(session, site, app_cfg)
    llm = FakeLLM({"searched": True, "queries": ["q"], "__search_hits__": ["https://x.ng/other"]})
    res = verifier(session, FakeFetcher({EVIDENCE: blocked(EVIDENCE)}), llm, app_cfg).run()
    assert res.needs_sources == 1
    assert verdicts(session, gid)[EVIDENCE] == ("required", "unverifiable")
    group = session.get(ArticleGroup, gid)
    assert group is not None and any(n.startswith("blocked:") for n in group.notes)


def test_low_relevance_is_unverifiable(
    session: Session,
    site: FakeSite,
    app_cfg: AppConfig,
) -> None:
    gid = queue_status_and_evidence(session, site, app_cfg)
    llm = FakeLLM(relevant("S1", conf=0.95, yes=False))
    verifier(session, FakeFetcher({EVIDENCE: ok(EVIDENCE)}), llm, app_cfg).run()
    assert verdicts(session, gid)[EVIDENCE] == ("required", "unverifiable")


def test_llm_failure_defers_without_writing(
    session: Session,
    site: FakeSite,
    app_cfg: AppConfig,
) -> None:
    gid = queue_status_and_evidence(session, site, app_cfg)
    res = verifier(
        session, FakeFetcher({EVIDENCE: ok(EVIDENCE)}), FakeLLM(LLMError("x")), app_cfg
    ).run()
    assert res.deferred == 1
    group = session.get(ArticleGroup, gid)
    assert group is not None and group.state == "queued"
    assert verdicts(session, gid) == {}
    # next run succeeds and picks it up again
    res2 = verifier(
        session, FakeFetcher({EVIDENCE: ok(EVIDENCE)}), FakeLLM(relevant("S1")), app_cfg
    ).run()
    assert res2.verified == 1


def test_source_cache_avoids_refetch(
    session: Session,
    site: FakeSite,
    app_cfg: AppConfig,
) -> None:
    queue_status_and_evidence(session, site, app_cfg)
    fetcher = FakeFetcher({EVIDENCE: ok(EVIDENCE)})
    verifier(session, fetcher, FakeLLM(relevant("S1")), app_cfg).run()
    v = verifier(session, fetcher, FakeLLM(), app_cfg)
    assert v._get_or_fetch(EVIDENCE).title  # cached row returned
    assert fetcher.calls.count(EVIDENCE) == 1
    assert v.result.cache_hits == 1


# ----------------------------------------------------------------------------- news


def queue_news(
    session: Session,
    site: FakeSite,
    cfg: AppConfig,
    url: str,
    claimed: date,
    grounded: bool,
) -> uuid.UUID:
    Runner(session, site, cfg.tunables.monitor)()
    pid = uuid.UUID(site.ids()[4])
    run = Run(started_at=NOW, trigger="test")
    session.add(run)
    session.flush()
    item = NewsItem(
        run_id=run.run_id,
        promise_id=pid,
        meter_id=METER_ID,
        url=url,
        outlet="Channels Television",
        headline="FG begins port modernisation",
        published_date=claimed,
        summary="s",
        is_concrete=True,
        relevance=0.95,
        search_grounded=grounded,
        search_title="FG begins port modernisation" if grounded else None,
        state="accepted",
        found_at=NOW,
    )
    session.add(item)
    session.flush()
    meter = session.get(Meter, METER_ID)
    assert meter is not None
    f = NewsFinding(
        item.id, pid, METER_ID, url, "Channels Television", item.headline, claimed, "s", "x", 0.95
    )
    cls = classify(
        session,
        run_id=run.run_id,
        meter=meter,
        diffs=[],
        removed=[],
        news=[f],
        cfg=cfg.tunables.classifier,
        llm=FakeLLM(),
        now=NOW,
    )
    session.commit()
    [gid] = cls.groups
    return gid


NEWS = "https://channelstv.com/2026/11/16/fg-begins-port-modernisation"


def test_grounded_blocked_news_needs_no_extra_search(
    session: Session,
    site: FakeSite,
    app_cfg: AppConfig,
) -> None:
    gid = queue_news(session, site, app_cfg, NEWS, date(2026, 11, 16), grounded=True)
    llm = FakeLLM(relevant("S1"))  # only the relevance call is allowed
    res = verifier(session, FakeFetcher({NEWS: blocked(NEWS)}), llm, app_cfg).run()
    assert res.verified == 1
    assert verdicts(session, gid)[NEWS] == ("required", "verified_partial")
    assert len(llm.requests) == 1


def test_ungrounded_unconfirmed_news_event_needs_sources(
    session: Session,
    site: FakeSite,
    app_cfg: AppConfig,
) -> None:
    gid = queue_news(session, site, app_cfg, NEWS, date(2026, 11, 16), grounded=False)
    llm = FakeLLM({"searched": True, "queries": ["q"], "__search_hits__": []})
    res = verifier(session, FakeFetcher({NEWS: blocked(NEWS)}), llm, app_cfg).run()
    assert res.needs_sources == 1
    ev = session.scalars(select(ChangeEvent).where(ChangeEvent.article_group_id == gid)).one()
    assert ev.state == "needs_sources"


def test_news_page_older_than_claimed_is_date_mismatch(
    session: Session,
    site: FakeSite,
    app_cfg: AppConfig,
) -> None:
    # page says 2026-11-14 (ARTICLE fixture); scout claimed 2026-11-19
    gid = queue_news(session, site, app_cfg, NEWS, date(2026, 11, 19), grounded=True)
    res = verifier(session, FakeFetcher({NEWS: ok(NEWS)}), FakeLLM(relevant("S1")), app_cfg).run()
    assert res.needs_sources == 1
    group = session.get(ArticleGroup, gid)
    assert group is not None and any("date_mismatch" in n for n in group.notes)


def test_verify_ignores_groups_already_done(
    session: Session,
    site: FakeSite,
    app_cfg: AppConfig,
) -> None:
    queue_status_and_evidence(session, site, app_cfg)
    verifier(session, FakeFetcher({EVIDENCE: ok(EVIDENCE)}), FakeLLM(relevant("S1")), app_cfg).run()
    assert verifier(session, FakeFetcher({}), FakeLLM(), app_cfg).run().groups == 0


def test_negative_search_result_is_cached(
    session: Session,
    site: FakeSite,
    app_cfg: AppConfig,
) -> None:
    queue_status_and_evidence(session, site, app_cfg)
    fetcher = FakeFetcher({EVIDENCE: blocked(EVIDENCE)})
    llm = FakeLLM({"searched": True, "queries": ["q"], "__search_hits__": []})
    verifier(session, fetcher, llm, app_cfg).run()
    v = verifier(session, fetcher, FakeLLM(), app_cfg)  # any LLM call would fail the test
    row = v._get_or_fetch(EVIDENCE)
    assert row.search_checked_at is not None and not row.search_confirmed
    group = session.scalars(select(ArticleGroup)).one()
    group.state = "queued"
    for ev in session.scalars(
        select(ChangeEvent).where(ChangeEvent.article_group_id == group.group_id)
    ):
        ev.state = "queued"
    session.flush()
    res = v.run()
    assert res.needs_sources == 1 and res.llm_calls == 0


def test_relevance_judge_sees_the_promise_tracker_update_text(
    session: Session,
    site: FakeSite,
    app_cfg: AppConfig,
) -> None:
    # A blocked page leaves the judge only a title; the published update that cites it
    # is what links the article to the promise, so the judge must see that text.
    queue_status_and_evidence(session, site, app_cfg)
    llm = FakeLLM(
        {"searched": True, "queries": ["q"], "__search_hits__": [EVIDENCE]}, relevant("S1")
    )
    verifier(session, FakeFetcher({EVIDENCE: blocked(EVIDENCE)}), llm, app_cfg).run()
    judge = llm.requests[-1]
    assert "New evidence report" in judge.user


def test_search_hit_with_same_host_and_slug_confirms(
    session: Session,
    site: FakeSite,
    app_cfg: AppConfig,
) -> None:
    gid = queue_news(session, site, app_cfg, NEWS, date(2026, 11, 16), grounded=False)
    amp = "https://www.channelstv.com/2026/11/16/fg-begins-port-modernisation/amp/"
    other = "https://evil.example/2026/11/16/fg-begins-port-modernisation"
    llm = FakeLLM(
        {"searched": True, "queries": ["q"], "__search_hits__": [other, amp]}, relevant("S1")
    )
    verifier(session, FakeFetcher({NEWS: blocked(NEWS)}), llm, app_cfg).run()
    assert verdicts(session, gid)[NEWS] == ("required", "verified_partial")


def test_negative_search_result_is_retried_after_a_day(
    session: Session,
    site: FakeSite,
    app_cfg: AppConfig,
) -> None:
    queue_status_and_evidence(session, site, app_cfg)
    fetcher = FakeFetcher({EVIDENCE: blocked(EVIDENCE)})
    llm = FakeLLM({"searched": True, "queries": ["q"], "__search_hits__": []})
    verifier(session, fetcher, llm, app_cfg).run()
    group = session.scalars(select(ArticleGroup)).one()
    group.state = "queued"
    for ev in session.scalars(
        select(ChangeEvent).where(ChangeEvent.article_group_id == group.group_id)
    ):
        ev.state = "queued"
    session.flush()
    later = Verifier(
        session,
        fetcher,
        FakeLLM(
            {"searched": True, "queries": ["q"], "__search_hits__": [EVIDENCE]}, relevant("S1")
        ),
        cfg=app_cfg.tunables.verifier,
        public_promise_url=app_cfg.tunables.api.public_promise_url,
        now=NOW + timedelta(days=2),
    )
    assert later.run().verified == 1
