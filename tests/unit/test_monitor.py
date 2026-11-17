from __future__ import annotations

import copy
import glob
import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from promise_monitor.agents.monitor import MonitorResult, monitor_meter
from promise_monitor.api.client import ApiError, RateLimitedError, SchemaError
from promise_monitor.api.models import ApiPromiseDetail, MeterPage
from promise_monitor.config import MonitorConfig, load_app_config
from promise_monitor.db.base import Base
from promise_monitor.db.models import Meter, PromiseIndex, PromiseSnapshot, Run
from tests.conftest import REPO_ROOT
from tests.fakes import FIXTURES, load_fixture

METER_ID = uuid.UUID("e447b2f3-867c-4f12-a598-521572567fe7")
T0 = datetime(2026, 11, 17, 7, 0, tzinfo=UTC)


class FakeSite:
    """Mutable in-memory copy of the presidential meter + promise details."""

    def __init__(self) -> None:
        self.page: dict[str, Any] = load_fixture("api_presidential.json")["data"]
        detail_files = {
            json.loads(Path(f).read_text())["data"]["id"]: json.loads(Path(f).read_text())["data"]
            for f in glob.glob(str(FIXTURES / "p_*.json"))
        }
        self.details: dict[str, dict[str, Any]] = {}
        for item in self.page["promises"]:
            self.details[item["id"]] = detail_files.get(item["id"]) or {**item, "updates": []}
        self.detail_calls: list[str] = []
        self.fail_details: dict[str, Exception] = {}
        self.page_error: Exception | None = None

    # MonitorClient protocol
    def meter_page(self, list_endpoint: str) -> MeterPage:
        if self.page_error:
            raise self.page_error
        return MeterPage.model_validate(self.page)

    def promise_detail(self, promise_id: uuid.UUID | str) -> ApiPromiseDetail:
        pid = str(promise_id)
        self.detail_calls.append(pid)
        if pid in self.fail_details:
            raise self.fail_details[pid]
        return ApiPromiseDetail.model_validate(self.details[pid])

    # mutations
    def ids(self) -> list[str]:
        return [p["id"] for p in self.page["promises"]]

    def item(self, pid: str) -> dict[str, Any]:
        return next(p for p in self.page["promises"] if p["id"] == pid)

    def set_status(self, pid: str, status: str) -> None:
        for d in (self.item(pid), self.details[pid]):
            d["status"] = d["last_published_status"] = status
            d["updated_at"] = "2026-11-18T09:00:00.000000Z"

    def add_update(self, pid: str, update_id: str, link: str) -> None:
        self.details[pid]["updates"].append(
            {
                "id": update_id,
                "blog": f'<p>New evidence <a href="{link}">report</a></p>',
                "status": self.details[pid]["status"],
                "update_type": "update",
                "is_published": "1",
                "created_at": "2026-11-18T08:00:00.000000Z",
            }
        )
        for d in (self.item(pid), self.details[pid]):
            d["update_count"] = int(d["update_count"]) + 1
            d["last_updated_at"] = "2026-11-18 08:00:00"

    def add_promise(self, pid: str) -> None:
        new = copy.deepcopy(self.page["promises"][0])
        new.update(id=pid, title="A brand new promise", update_count=0)
        self.page["promises"].append(new)
        self.details[pid] = {**new, "updates": []}

    def remove_promise(self, pid: str) -> dict[str, Any]:
        item = self.item(pid)
        self.page["promises"].remove(item)
        return item


@pytest.fixture
def cfg() -> MonitorConfig:
    return load_app_config(REPO_ROOT / "config").tunables.monitor


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
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
        yield s


@pytest.fixture
def site() -> FakeSite:
    return FakeSite()


class Runner:
    def __init__(self, session: Session, site: FakeSite, cfg: MonitorConfig) -> None:
        self.session, self.site, self.cfg = session, site, cfg
        self.n = 0

    def __call__(self, *, force_full: bool = False) -> MonitorResult:
        now = T0 + timedelta(days=self.n)
        self.n += 1
        run = Run(started_at=now, trigger="test")
        self.session.add(run)
        self.session.flush()
        self.site.detail_calls.clear()
        meter = self.session.get(Meter, METER_ID)
        assert meter is not None
        res = monitor_meter(
            self.session, self.site, meter, run.run_id, now, self.cfg, force_full=force_full
        )
        self.session.commit()
        return res


@pytest.fixture
def run(session: Session, site: FakeSite, cfg: MonitorConfig) -> Runner:
    return Runner(session, site, cfg)


def snapshot_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(PromiseSnapshot)) or 0


# ---------------------------------------------------------------------------- tests


def test_first_run_is_baseline(run: Runner, session: Session) -> None:
    res = run()
    assert res.status == "baseline"
    assert res.diffs == []
    assert res.snapshots_written == res.listed == 53
    assert session.get(Meter, METER_ID).baseline_pending is False  # type: ignore[union-attr]


def test_unchanged_second_run_has_zero_diffs_and_no_detail_calls(
    run: Runner, session: Session, site: FakeSite
) -> None:
    run()
    res = run()
    assert res.status == "ok"
    assert res.diffs == []
    assert res.details_fetched == 0
    assert snapshot_count(session) == 53


def test_status_change_detected(run: Runner, site: FakeSite) -> None:
    run()
    pid = site.ids()[3]
    site.set_status(pid, "kept")
    res = run()
    assert len(res.diffs) == 1
    d = res.diffs[0]
    assert str(d.promise_id) == pid
    assert d.fields_changed == ("status",)
    assert not d.is_new_promise
    assert d.prev_snapshot_id is not None


def test_new_evidence_detected(run: Runner, site: FakeSite) -> None:
    run()
    pid = site.ids()[0]
    upd = str(uuid.uuid4())
    site.add_update(pid, upd, "https://example.org/report")
    res = run()
    assert [d.added_update_ids for d in res.diffs] == [(upd,)]
    assert res.diffs[0].fields_changed == ("updates",)


def test_new_promise_detected(run: Runner, site: FakeSite) -> None:
    run()
    pid = str(uuid.uuid4())
    site.add_promise(pid)
    res = run()
    assert len(res.diffs) == 1
    assert res.diffs[0].is_new_promise
    assert res.diffs[0].prev_snapshot_id is None


def test_fingerprint_only_change_is_not_a_diff(
    run: Runner, session: Session, site: FakeSite
) -> None:
    run()
    pid = site.ids()[5]
    site.item(pid)["updated_at"] = "2026-11-18T10:00:00.000000Z"  # touched, nothing changed
    res = run()
    assert res.details_fetched == 1
    assert res.diffs == []
    assert snapshot_count(session) == 53
    assert run().details_fetched == 0  # new fingerprint was remembered


def test_removed_then_restored(run: Runner, session: Session, site: FakeSite) -> None:
    run()
    pid = site.ids()[7]
    item = site.remove_promise(pid)
    res = run()
    assert [str(r) for r in res.removed] == [pid]
    assert res.diffs == []
    idx = session.get(PromiseIndex, uuid.UUID(pid))
    assert idx is not None and idx.present is False
    site.page["promises"].append(item)
    res = run()
    assert res.diffs == []
    assert session.get(PromiseIndex, uuid.UUID(pid)).present is True  # type: ignore[union-attr]
    assert snapshot_count(session) == 53


def test_anomaly_guard_writes_nothing(run: Runner, session: Session, site: FakeSite) -> None:
    run()
    for pid in site.ids()[:20]:  # 20/53 = 38% > 30%
        site.set_status(pid, "broken")
    res = run()
    assert res.status == "anomaly"
    assert res.diffs == []
    assert snapshot_count(session) == 53
    # nothing was remembered, so a later run still sees the changes
    assert run().status == "anomaly"


def test_small_meter_changes_do_not_trip_guard(run: Runner, site: FakeSite) -> None:
    run()
    for pid in site.ids()[:4]:  # below anomaly_min_changes
        site.set_status(pid, "broken")
    res = run()
    assert res.status == "ok"
    assert len(res.diffs) == 4


def test_failed_detail_keeps_old_state_and_retries(run: Runner, site: FakeSite) -> None:
    run()
    pid = site.ids()[2]
    site.set_status(pid, "stalled")
    site.fail_details[pid] = ApiError("boom", path="x", status=500)
    res = run()
    assert res.status == "degraded"
    assert res.diffs == []
    site.fail_details.clear()
    res = run()
    assert [str(d.promise_id) for d in res.diffs] == [pid]


def test_failed_baseline_detail_keeps_baseline_pending(
    run: Runner, session: Session, site: FakeSite
) -> None:
    site.fail_details[site.ids()[0]] = ApiError("boom", path="x", status=500)
    res = run()
    assert res.status == "baseline"
    assert session.get(Meter, METER_ID).baseline_pending is True  # type: ignore[union-attr]
    site.fail_details.clear()
    res = run()
    assert res.status == "baseline" and res.diffs == []  # still no false "new promise"
    assert session.get(Meter, METER_ID).baseline_pending is False  # type: ignore[union-attr]


def test_schema_error_skips_meter(run: Runner, session: Session, site: FakeSite) -> None:
    site.page_error = SchemaError("bad", path="x")
    res = run()
    assert res.status == "skipped"
    assert snapshot_count(session) == 0


def test_rate_limit_propagates(run: Runner, site: FakeSite) -> None:
    site.page_error = RateLimitedError("slow down", path="x", status=429)
    with pytest.raises(RateLimitedError):
        run()


def test_unpublished_promise_is_ignored(run: Runner, site: FakeSite) -> None:
    run()
    pid = site.ids()[1]
    site.set_status(pid, "broken")
    site.item(pid)["published_status"] = "draft"
    res = run()
    assert res.diffs == []
    assert res.removed == []  # still listed, just not public


def test_force_full_fetches_everything_without_false_diffs(run: Runner, site: FakeSite) -> None:
    run()
    pid = site.ids()[9]
    site.details[pid]["title"] = "Edited title not visible in list fingerprint"
    site.item(pid)["title"] = site.details[pid]["title"]
    assert run().diffs == []  # cheap pass can't see title-only edits
    res = run(force_full=True)
    assert res.details_fetched == 53
    assert [d.fields_changed for d in res.diffs] == [("title",)]


def test_snapshots_are_append_only(run: Runner, session: Session, site: FakeSite) -> None:
    run()
    pid = site.ids()[4]
    first = session.scalars(
        select(PromiseSnapshot.snapshot_id).where(PromiseSnapshot.promise_id == uuid.UUID(pid))
    ).all()
    site.set_status(pid, "kept")
    run()
    after = session.scalars(
        select(PromiseSnapshot.snapshot_id).where(PromiseSnapshot.promise_id == uuid.UUID(pid))
    ).all()
    assert len(after) == 2 and first[0] in after
