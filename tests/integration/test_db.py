"""Needs a reachable PostgreSQL in PM_DATABASE_URL; skipped otherwise."""

from __future__ import annotations

import os

import pytest

from promise_monitor.db.base import make_engine, ping

pytestmark = pytest.mark.integration

DATABASE_URL = os.environ.get("PM_DATABASE_URL")


@pytest.mark.skipif(not DATABASE_URL, reason="PM_DATABASE_URL not set")
def test_database_reachable() -> None:
    assert "PostgreSQL" in ping(make_engine(DATABASE_URL))
