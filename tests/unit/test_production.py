from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from promise_monitor.db.base import Base
from promise_monitor.db.lock import run_lock
from promise_monitor.db.models import Run
from promise_monitor.logging_setup import prune_logs
from promise_monitor.shadow import build_report
from tests.unit.test_verifier import NOW


def test_lock_is_a_noop_on_sqlite() -> None:
    with run_lock(create_engine("sqlite://")) as got:
        assert got is True


def test_prune_logs(tmp_path: Path) -> None:
    for name in (
        "2026-01-01.jsonl",
        "2026-11-10.jsonl",
        "eval-writer-20260101T000000.json",
        "notes.txt",
    ):
        (tmp_path / name).write_text("x")
    assert prune_logs(tmp_path, 90, NOW) == 2
    assert sorted(p.name for p in tmp_path.iterdir()) == ["2026-11-10.jsonl", "notes.txt"]
    assert prune_logs(None, 90, NOW) == 0


def _session() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return Session(engine)


def test_shadow_report_verdict() -> None:
    s = _session()
    for d in range(14):
        s.add(
            Run(
                started_at=NOW - timedelta(days=d),
                finished_at=NOW,
                trigger="cli:run",
                status="ok",
                stats={},
            )
        )
    s.commit()
    rep = build_report(s, NOW, 14)
    assert rep.consecutive_ok_days == 14 and rep.passed
    assert rep.lines[-1].startswith("VERDICT: PASS")

    s.add(
        Run(
            started_at=NOW - timedelta(days=3),
            finished_at=NOW,
            trigger="cli:run",
            status="degraded",
            stats={"stage_errors": {"verify": "RuntimeError: x"}},
        )
    )
    s.commit()
    rep = build_report(s, NOW, 14)
    assert not rep.passed and rep.crashed_runs


def test_shadow_streak_breaks_on_a_missing_day() -> None:
    s = _session()
    for d in (0, 1, 2, 4, 5):
        s.add(
            Run(
                started_at=NOW - timedelta(days=d),
                finished_at=NOW,
                trigger="cli:run",
                status="ok",
                stats={},
            )
        )
    s.add(
        Run(
            run_id=uuid.uuid4(),
            started_at=NOW - timedelta(days=6),
            trigger="cli:monitor",
            status="ok",
        )
    )
    s.commit()
    assert build_report(s, NOW, 14).consecutive_ok_days == 3
