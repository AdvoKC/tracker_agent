from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from promise_monitor.agents.discovery import (
    CollectResult,
    SiteDownError,
    apply_discovery,
    collect,
)
from promise_monitor.api.client import PromiseTrackerClient
from promise_monitor.config import AppConfig, load_app_config
from promise_monitor.db.base import Base
from promise_monitor.db.models import Meter
from tests.conftest import REPO_ROOT
from tests.fakes import fixture_handler

PRESIDENT = uuid.UUID("e447b2f3-867c-4f12-a598-521572567fe7")
SENATE = uuid.UUID("aa43ac7e-9299-42a4-9d3b-fe8522de3f71")
EDO_TRACKER = uuid.UUID("b22f773a-ce6e-4ca1-9006-82f9d30c71c6")
EDO_RING = uuid.UUID("d59bd0b3-95c8-4fbd-acf8-9bf9a22bb778")
EKITI_RING_OLD = uuid.UUID("a688f742-8aca-455d-ae96-b1ed959258b3")
EKITI_RING_NEW = uuid.UUID("d5ad9614-12b5-4dc2-9e89-c5af3ae295b2")
OSUN_TRACKER = uuid.UUID("963c830a-6781-406c-8f32-a46344bdfc5e")
ABIA_RING = uuid.UUID("174df9ca-3a88-43c1-9eb1-0dd29b1de748")

T0 = datetime(2026, 11, 17, 7, 0, tzinfo=UTC)
T1 = datetime(2026, 11, 18, 7, 0, tzinfo=UTC)

Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture
def cfg() -> AppConfig:
    return load_app_config(REPO_ROOT / "config")


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def run_collect(cfg: AppConfig, handler: Handler | None = None) -> CollectResult:
    client = PromiseTrackerClient(
        cfg.tunables.api,
        transport=httpx.MockTransport(handler or fixture_handler()),
        sleep=lambda _s: None,
        jitter=lambda: 0.0,
    )
    with client:
        return collect(client, cfg.states)


def fail(status: int = 500) -> Handler:
    return lambda _r: httpx.Response(status)


def test_collect_finds_every_known_meter(cfg: AppConfig) -> None:
    res = run_collect(cfg)
    assert not res.failed_sources
    m = res.meters
    assert m[PRESIDENT].scope == "presidential"
    assert m[PRESIDENT].slug == "president-bola-ahmed-tinubu-2023-2027"
    assert m[SENATE].scope == "senate"
    # Edo's all_meters mixes families; tenures disambiguate them
    assert m[EDO_TRACKER].meter_type == "promise_tracker"
    assert m[EDO_RING].meter_type == "promise_ring"
    assert m[EDO_TRACKER].promise_count == 29
    # Ekiti has no state page meters at all: found only via tenures / ring list
    assert m[EKITI_RING_OLD].is_archived
    assert not m[EKITI_RING_NEW].is_archived
    assert m[OSUN_TRACKER].state_name == "Osun"
    assert m[ABIA_RING].meter_type == "promise_ring"
    assert m[ABIA_RING].promise_count == 19
    house = [x for x in m.values() if x.scope == "house"]
    assert len(house) == 1
    assert all(x.meter_type for x in m.values())


def test_apply_inserts_with_baseline_and_priority(cfg: AppConfig, session: Session) -> None:
    res = apply_discovery(session, run_collect(cfg), cfg, T0)
    assert res.status == "ok"
    assert PRESIDENT in res.new
    assert res.active[:3] == [PRESIDENT, SENATE, res.active[2]]
    president = session.get(Meter, PRESIDENT)
    assert president is not None
    assert president.baseline_pending is True
    assert president.priority_rank == 1
    assert president.list_endpoint == f"meter/{PRESIDENT}/presidential"
    assert (
        president.public_url
        == "https://promisetracker.ng/meter/president-bola-ahmed-tinubu-2023-2027"
    )
    ring = session.get(Meter, ABIA_RING)
    assert ring is not None
    assert ring.list_endpoint == f"meter/{ABIA_RING}/state/Abia"
    assert ring.public_url == "https://promisetracker.ng/promiseRing/Abia"
    old = session.get(Meter, EKITI_RING_OLD)
    assert old is not None
    assert old.registry_status == "archived"
    assert EKITI_RING_OLD not in res.active


def test_second_run_is_idempotent(cfg: AppConfig, session: Session) -> None:
    apply_discovery(session, run_collect(cfg), cfg, T0)
    session.commit()
    res = apply_discovery(session, run_collect(cfg), cfg, T1)
    assert res.new == [] and res.updated == [] and res.missing == []
    president = session.get(Meter, PRESIDENT)
    assert president is not None
    assert president.first_seen_at.replace(tzinfo=UTC) == T0
    assert president.last_seen_at.replace(tzinfo=UTC) == T1


def test_vanished_meter_marked_missing_never_deleted(cfg: AppConfig, session: Session) -> None:
    apply_discovery(session, run_collect(cfg), cfg, T0)
    empty = {"success": True, "data": {"promises": [], "all_meters": []}, "message": ""}
    handler = fixture_handler({"presidential": lambda _r: httpx.Response(200, json=empty)})
    res = apply_discovery(session, run_collect(cfg, handler), cfg, T1)
    assert res.missing == [PRESIDENT]
    row = session.get(Meter, PRESIDENT)
    assert row is not None and row.registry_status == "missing"
    # and it comes back
    res2 = apply_discovery(session, run_collect(cfg), cfg, T1)
    assert res2.reappeared == [PRESIDENT]
    assert session.get(Meter, PRESIDENT).registry_status == "active"  # type: ignore[union-attr]


def test_failed_source_does_not_mark_missing(cfg: AppConfig, session: Session) -> None:
    apply_discovery(session, run_collect(cfg), cfg, T0)
    handler = fixture_handler({"state/Edo/tenures?kind=promise_ring": fail(500)})
    collected = run_collect(cfg, handler)
    assert "state:Edo" in collected.failed_sources
    res = apply_discovery(session, collected, cfg, T1)
    assert res.status == "degraded"
    assert res.missing == []


def test_all_sources_down_raises(cfg: AppConfig, session: Session) -> None:
    with pytest.raises(SiteDownError):
        apply_discovery(session, run_collect(cfg, fail(503)), cfg, T0)


def test_rate_limit_stops_further_requests(cfg: AppConfig) -> None:
    calls: list[str] = []
    limited = lambda _r: httpx.Response(429, headers={"Retry-After": "9999"})  # noqa: E731
    handler = fixture_handler({"presidential": limited}, log=calls)
    res = run_collect(cfg, handler)
    assert res.rate_limited
    assert calls == ["presidential"]
    assert not res.ok_sources


def test_schema_change_in_one_state_is_isolated(cfg: AppConfig, session: Session) -> None:
    bad = {"success": True, "data": [{"no_id": True}], "message": ""}
    handler = fixture_handler(
        {"state/Osun/tenures?kind=promise_tracker": lambda _r: httpx.Response(200, json=bad)}
    )
    collected = run_collect(cfg, handler)
    assert list(collected.failed_sources) == ["state:Osun"]
    res = apply_discovery(session, collected, cfg, T0)
    assert res.status == "degraded"
    assert session.scalars(select(Meter).where(Meter.id == PRESIDENT)).one()
