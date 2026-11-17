from __future__ import annotations

import json
from pathlib import Path

import structlog

from promise_monitor.logging_setup import configure_logging, get_logger


def test_json_lines_with_bound_context_and_redaction(tmp_path: Path) -> None:
    path = configure_logging(tmp_path / "logs", "INFO", console=False)
    structlog.contextvars.bind_contextvars(run_id="r1", meter_id="m1")
    try:
        get_logger("test").info("hello", promise_id="p1", password="hunter2")  # noqa: S106
    finally:
        structlog.contextvars.clear_contextvars()

    lines = path.read_text().strip().splitlines()
    record = json.loads(lines[-1])
    assert record["event"] == "hello"
    assert record["run_id"] == "r1"
    assert record["meter_id"] == "m1"
    assert record["promise_id"] == "p1"
    assert record["password"] == "***"  # noqa: S105
    assert record["level"] == "info"
    assert path.name.endswith(".jsonl")
