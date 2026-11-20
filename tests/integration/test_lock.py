"""Advisory lock against real Postgres (skipped without PM_DATABASE_URL)."""

from __future__ import annotations

import os

import pytest

from promise_monitor.db.base import make_engine
from promise_monitor.db.lock import run_lock

pytestmark = pytest.mark.integration
URL = os.environ.get("PM_DATABASE_URL")


@pytest.mark.skipif(not URL, reason="PM_DATABASE_URL not set")
def test_second_holder_is_refused_and_lock_is_released() -> None:
    engine = make_engine(URL)
    key = 0x7E57  # test-only key, never the production one
    with run_lock(engine, key) as first:
        assert first is True
        with run_lock(make_engine(URL), key) as second:
            assert second is False
    with run_lock(engine, key) as again:
        assert again is True
