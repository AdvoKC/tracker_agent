from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from promise_monitor.agents.news_scout import run_news_scout, select_candidates
from promise_monitor.config import AppConfig, load_app_config
from promise_monitor.db.models import NewsItem, NewsSearchState, Run
from promise_monitor.domain.urls import allowed_outlet, normalize_url
from promise_monitor.llm.base import LLMAuthError, LLMError
from tests.conftest import REPO_ROOT
from tests.fakes import FakeLLM
from tests.unit.test_monitor import FakeSite, Runner
from tests.unit.test_monitor import session as session
from tests.unit.test_monitor import site as site

NOW = datetime(2026, 11, 20, 7, 0, tzinfo=UTC)


@pytest.fixture
def app_cfg() -> AppConfig:
    return load_app_config(REPO_ROOT / "config")


@pytest.fixture
def baselined(session: Session, site: FakeSite, app_cfg: AppConfig) -> Session:
    Runner(session, site, app_cfg.tunables.monitor)()
    return session


def new_run(session: Session) -> uuid.UUID:
    run = Run(started_at=NOW, trigger="test")
    session.add(run)
    session.flush()
    return run.run_id


def item(ref: str = "P1", **over: Any) -> dict[str, Any]:
    base = {
        "promise_ref": ref,
        "url": "https://www.premiumtimesng.com/news/2026/11/bill-passes.html?utm_source=x",
        "outlet": "Premium Times",
        "headline": "Senate passes bill at third reading",
        "published_date": "2026-11-18",
        "summary": "The Senate passed the bill at third reading on Tuesday.",
        "development_type": "bill_passed",
        "is_concrete_development": True,
        "relevance": 0.92,
    }
    base.update(over)
    return base


def scout(session: Session, llm: FakeLLM, cfg: AppConfig, max_calls: int = 1) -> Any:
    return run_news_scout(
        session,
        llm,
        run_id=new_run(session),
        now=NOW,
        cfg=cfg.tunables.news,
        sources=cfg.news_sources,
        max_calls=max_calls,
    )


# ------------------------------------------------------------------------- url helpers


def test_normalize_url() -> None:
    assert (
        normalize_url("HTTP://WWW.Punchng.com/a/b/?utm_medium=x&id=3#frag")
        == "https://punchng.com/a/b?id=3"
    )
    assert normalize_url("ftp://x.com/a") is None
    assert normalize_url("not a url") is None


def test_allowlist(app_cfg: AppConfig) -> None:
    s = app_cfg.news_sources
    assert allowed_outlet("https://punchng.com/x", s) == "Punch"
    assert allowed_outlet("https://news.punchng.com/x", s) == "Punch"
    assert allowed_outlet("https://notpunchng.com/x", s) is None
    assert allowed_outlet("https://statehouse.gov.ng/x", s) == "Official (statehouse.gov.ng)"
    assert allowed_outlet("https://randomblog.ng/x", s) is None


# ------------------------------------------------------------------------- scouting


def test_accepts_valid_and_rejects_each_bad_case(baselined: Session, app_cfg: AppConfig) -> None:
    out = {
        "results": [
            item(),
            item(url="https://randomblog.ng/story"),  # outlet_not_allowed
            item(url="https://punchng.com/old", published_date="2020-01-01"),  # too_old
            item(url="https://punchng.com/month", published_date="2026-11"),  # date_imprecise
            item(url="https://punchng.com/fut", published_date="2027-01-01"),  # future_date
            item(url="https://punchng.com/op", is_concrete_development=False),  # not_concrete
            item(url="https://punchng.com/weak", relevance=0.5),  # low_relevance
            item(ref="P99", url="https://punchng.com/x"),  # unknown ref
            item(url="javascript:alert(1)"),  # bad_url
        ]
    }
    llm = FakeLLM(out)
    res = scout(baselined, llm, app_cfg)
    assert res.accepted == 1
    assert res.rejected == {
        "outlet_not_allowed": 1,
        "too_old": 1,
        "date_imprecise": 1,
        "future_date": 1,
        "not_concrete": 1,
        "low_relevance": 1,
        "unknown_promise_ref": 1,
        "bad_url": 1,
    }
    [findings] = res.findings.values()
    assert findings[0].url == "https://premiumtimesng.com/news/2026/11/bill-passes.html"
    assert findings[0].outlet == "Premium Times"
    stored = baselined.scalar(select(func.count()).select_from(NewsItem))
    assert stored == 7  # everything with a URL we could parse and a known ref is audited
    req = llm.requests[0]
    assert req.tools == ("WebSearch",)
    assert "premiumtimesng.com" in req.system
    assert "P10:" in req.user  # batch_size promises sent


def test_rotation_and_batching(baselined: Session, app_cfg: AppConfig) -> None:
    llm = FakeLLM(default={"results": []})
    res = scout(baselined, llm, app_cfg, max_calls=2)
    assert res.calls == 2 and res.searched == 20
    first = set(baselined.scalars(select(NewsSearchState.promise_id)))
    assert len(first) == 20
    # next run: the 20 searched promises go to the back of the queue
    nxt = select_candidates(baselined, 10, NOW.date(), app_cfg.tunables.news)
    assert len(nxt) == 10
    assert not {c.promise_id for c in nxt} & first


def test_failed_call_is_not_marked_searched(baselined: Session, app_cfg: AppConfig) -> None:
    res = scout(baselined, FakeLLM(LLMError("timeout")), app_cfg)
    assert res.failed_calls == 1 and res.searched == 0
    assert baselined.scalar(select(func.count()).select_from(NewsSearchState)) == 0


def test_auth_failure_stops_scouting(baselined: Session, app_cfg: AppConfig) -> None:
    llm = FakeLLM(LLMAuthError("Not logged in"), default={"results": []})
    res = scout(baselined, llm, app_cfg, max_calls=3)
    assert res.auth_failed and res.calls == 1


def test_same_url_is_not_reported_twice(baselined: Session, app_cfg: AppConfig) -> None:
    first = scout(baselined, FakeLLM({"results": [item()]}), app_cfg)
    assert first.accepted == 1
    # make the SAME 10 promises come round again: everyone else was searched just now
    searched = set(baselined.scalars(select(NewsSearchState.promise_id)))
    run_id = new_run(baselined)
    for c in select_candidates(baselined, 1000, NOW.date(), app_cfg.tunables.news):
        if c.promise_id not in searched:
            baselined.add(
                NewsSearchState(promise_id=c.promise_id, last_searched_at=NOW, last_run_id=run_id)
            )
    for st in baselined.scalars(select(NewsSearchState)):
        if st.promise_id in searched:
            st.last_searched_at = NOW - timedelta(days=30)
    baselined.flush()
    again = scout(baselined, FakeLLM({"results": [item()]}), app_cfg)
    assert again.accepted == 0 and again.rejected == {}


def test_max_results_per_promise(baselined: Session, app_cfg: AppConfig) -> None:
    urls = [f"https://punchng.com/s{i}" for i in range(5)]
    res = scout(baselined, FakeLLM({"results": [item(url=u) for u in urls]}), app_cfg)
    assert res.accepted == app_cfg.tunables.news.max_results_per_promise
    assert res.rejected == {"over_limit": 5 - res.accepted}


def test_cutoff_is_after_last_tracker_update(baselined: Session, app_cfg: AppConfig) -> None:
    cands = select_candidates(baselined, 100, NOW.date(), app_cfg.tunables.news)
    floor = NOW.date() - timedelta(days=app_cfg.tunables.news.lookback_days)
    assert all(c.cutoff >= floor for c in cands)
    assert all(isinstance(c.cutoff, date) for c in cands)


def test_one_story_is_accepted_for_one_promise_only(baselined: Session, app_cfg: AppConfig) -> None:
    url = "https://channelstv.com/2026/11/16/ports"
    out = {
        "search_queries": ["ports"],
        "results": [item("P1", url=url, relevance=0.8), item("P2", url=url, relevance=0.95)],
    }
    res = scout(baselined, FakeLLM(out), app_cfg)
    assert res.accepted == 1
    assert res.rejected == {"same_story_other_promise": 1}
    [f] = next(iter(res.findings.values()))
    assert f.relevance == 0.95  # the most relevant promise keeps the story
    # a later run can't attach the same story to yet another promise
    for st in baselined.scalars(select(NewsSearchState)):
        st.last_searched_at = NOW - timedelta(days=30)
    baselined.flush()
    later = scout(
        baselined, FakeLLM({"search_queries": [], "results": [item("P3", url=url)]}), app_cfg
    )
    assert later.accepted == 0
