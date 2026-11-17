from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import date

import pytest
from sqlalchemy.orm import Session

from promise_monitor.agents.classifier import (
    ClassifyResult,
    NewsFinding,
    classify,
    edit_ratio,
)
from promise_monitor.agents.monitor import MonitorResult
from promise_monitor.config import AppConfig, ClassifierConfig, load_app_config
from promise_monitor.db.models import Meter, NewsItem, Run
from promise_monitor.llm.base import LLMError
from tests.conftest import REPO_ROOT
from tests.fakes import FakeLLM
from tests.unit.test_monitor import METER_ID, T0, FakeSite, Runner
from tests.unit.test_monitor import session as session
from tests.unit.test_monitor import site as site


@pytest.fixture
def app_cfg() -> AppConfig:
    return load_app_config(REPO_ROOT / "config")


@pytest.fixture
def ccfg(app_cfg: AppConfig) -> ClassifierConfig:
    return app_cfg.tunables.classifier


class Pipeline:
    """Monitor + classify, like the CLI does per meter."""

    def __init__(self, session: Session, site: FakeSite, app_cfg: AppConfig) -> None:
        self.session = session
        self.site = site
        self.cfg = app_cfg
        self.runner = Runner(session, site, app_cfg.tunables.monitor)
        self.last: MonitorResult | None = None

    def monitor(self) -> MonitorResult:
        self.last = self.runner()
        return self.last

    def classify(
        self, llm: FakeLLM | None = None, news: list[NewsFinding] | None = None
    ) -> ClassifyResult:
        assert self.last is not None
        meter = self.session.get(Meter, METER_ID)
        assert meter is not None
        run = Run(started_at=T0, trigger="test")
        self.session.add(run)
        self.session.flush()
        res = classify(
            self.session,
            run_id=run.run_id,
            meter=meter,
            diffs=self.last.diffs,
            removed=self.last.removed,
            news=news or [],
            cfg=self.cfg.tunables.classifier,
            llm=llm,
            now=T0,
        )
        self.session.commit()
        return res


@pytest.fixture
def pipe(session: Session, site: FakeSite, app_cfg: AppConfig) -> Iterator[Pipeline]:
    p = Pipeline(session, site, app_cfg)
    p.monitor()  # baseline
    yield p


def set_meter_rank(session: Session, rank: int) -> None:
    meter = session.get(Meter, METER_ID)
    assert meter is not None
    meter.priority_rank = rank
    session.commit()


def test_edit_ratio() -> None:
    assert edit_ratio("Build 10 roads", "build 10  roads") == 0.0
    assert edit_ratio("Build ten roads", "Build ten raods") < 0.15
    assert edit_ratio("Build ten roads", "Privatise the national grid") > 0.4


def test_status_change_always_queued_without_llm(pipe: Pipeline, site: FakeSite) -> None:
    pid = site.ids()[3]
    site.set_status(pid, "kept")
    pipe.monitor()
    llm = FakeLLM()  # any call would fail the test
    res = pipe.classify(llm)
    [ev] = res.events
    assert ev.change_type == "status_change"
    assert ev.state == "queued"
    assert ev.old_status is not None and ev.old_status != "kept"
    assert ev.new_status == "kept"
    assert ev.score >= 70 + 10  # base + final-status bonus (+ priority meter bonus)
    assert ev.article_group_id is not None
    assert llm.requests == []


def test_borderline_evidence_uses_llm_delta(
    pipe: Pipeline,
    session: Session,
    site: FakeSite,
) -> None:
    set_meter_rank(session, 100)  # no priority-meter bonus → base 50 (or 55 if high priority)
    pid = next(p for p in site.ids() if site.item(p)["priority"] != "high")
    site.add_update(pid, str(uuid.uuid4()), "https://example.org/a")
    pipe.monitor()
    llm = FakeLLM({"score_delta": 12, "reason": "Funds were released."})
    [ev] = pipe.classify(llm).events
    assert ev.change_type == "new_evidence"
    assert (ev.score_base, ev.score_llm_delta, ev.score) == (50, 12, 62)
    assert ev.state == "queued"
    assert llm.requests[0].task == "classifier"
    assert "New evidence update" in llm.requests[0].user


def test_llm_delta_is_clamped_and_can_drop_below_threshold(
    pipe: Pipeline,
    session: Session,
    site: FakeSite,
) -> None:
    set_meter_rank(session, 100)
    pid = next(p for p in site.ids() if site.item(p)["priority"] != "high")
    site.add_update(pid, str(uuid.uuid4()), "https://example.org/a")
    pipe.monitor()
    [ev] = pipe.classify(FakeLLM({"score_delta": -99, "reason": "Minor."})).events
    assert ev.score_llm_delta == -15
    assert ev.state == "logged"
    assert ev.article_group_id is None


def test_llm_failure_falls_back_to_rules(
    pipe: Pipeline,
    session: Session,
    site: FakeSite,
) -> None:
    set_meter_rank(session, 100)
    pid = next(p for p in site.ids() if site.item(p)["priority"] != "high")
    site.add_update(pid, str(uuid.uuid4()), "https://example.org/a")
    pipe.monitor()
    res = pipe.classify(FakeLLM(LLMError("down")))
    [ev] = res.events
    assert res.llm_failures == 1
    assert ev.score == 50 and ev.state == "logged"
    assert ev.score_reason.startswith("llm_unavailable")


def test_status_and_evidence_share_one_group(pipe: Pipeline, site: FakeSite) -> None:
    pid = site.ids()[0]
    site.set_status(pid, "broken")
    site.add_update(pid, str(uuid.uuid4()), "https://example.org/b")
    pipe.monitor()
    res = pipe.classify(FakeLLM(default={"score_delta": 0, "reason": "ok"}))
    types = sorted(e.change_type for e in res.events)
    assert types == ["new_evidence", "status_change"]
    assert len(res.groups) == 1
    assert len({e.article_group_id for e in res.events}) == 1


def test_cosmetic_vs_substantive(pipe: Pipeline, site: FakeSite) -> None:
    typo, rewrite = site.ids()[10], site.ids()[11]
    t = site.details[typo]["title"]
    site.details[typo]["title"] = t[:-2] + t[-1] + t[-2]
    site.details[rewrite]["title"] = "An entirely different commitment about electricity tariffs"
    pipe.last = pipe.runner(force_full=True)
    res = pipe.classify(FakeLLM())
    kinds = {str(e.promise_id): e.change_type for e in res.events}
    assert kinds == {typo: "cosmetic", rewrite: "substantive_edit"}
    assert all(e.state == "logged" and e.score == 0 for e in res.events)


def test_new_promise_and_removed_promise(pipe: Pipeline, site: FakeSite) -> None:
    new_pid = str(uuid.uuid4())
    site.add_promise(new_pid)
    gone = site.ids()[20]
    site.remove_promise(gone)
    pipe.monitor()
    res = pipe.classify(FakeLLM(default={"score_delta": 0, "reason": "ok"}))
    kinds = {str(e.promise_id): (e.change_type, e.state) for e in res.events}
    assert kinds[gone] == ("promise_removed", "logged")
    assert kinds[new_pid][0] == "new_promise"


def test_reclassifying_same_diffs_creates_nothing(pipe: Pipeline, site: FakeSite) -> None:
    site.set_status(site.ids()[2], "stalled")
    pipe.monitor()
    first = pipe.classify(FakeLLM())
    second = pipe.classify(FakeLLM())
    assert len(first.events) == 1
    assert second.events == [] and second.duplicates_skipped == 1


def _finding(pid: str, url: str, relevance: float = 0.8) -> NewsFinding:
    return NewsFinding(
        news_item_id=0,
        promise_id=uuid.UUID(pid),
        meter_id=METER_ID,
        url=url,
        outlet="Premium Times",
        headline="Senate passes bill",
        published_date=date(2026, 11, 15),
        summary="The Senate passed the bill at third reading.",
        development_type="bill_passed",
        relevance=relevance,
    )


def test_news_update_is_evidence_lead_and_deduped_by_url(
    pipe: Pipeline,
    session: Session,
    site: FakeSite,
) -> None:
    pid = site.ids()[4]
    run = Run(started_at=T0, trigger="test")
    session.add(run)
    session.flush()
    item = NewsItem(
        run_id=run.run_id,
        promise_id=uuid.UUID(pid),
        meter_id=METER_ID,
        url="https://premiumtimesng.com/x",
        outlet="Premium Times",
        headline="h",
        published_date=date(2026, 11, 15),
        summary="s",
        is_concrete=True,
        relevance=0.95,
        state="accepted",
        found_at=T0,
    )
    session.add(item)
    session.commit()
    f = _finding(pid, item.url, relevance=0.95)
    f = NewsFinding(**{**f.__dict__, "news_item_id": item.id})
    pipe.last = MonitorResult(meter_id=METER_ID, status="ok")
    res = pipe.classify(FakeLLM(), news=[f])
    [ev] = res.events
    assert ev.change_type == "news_update"
    assert ev.evidence_lead is True
    assert ev.news_item_id == item.id
    assert ev.score >= 55 + 10 + 10  # base + high-confidence + priority meter
    assert ev.state == "queued"
    again = pipe.classify(FakeLLM(), news=[f])
    assert again.events == [] and again.duplicates_skipped == 1
