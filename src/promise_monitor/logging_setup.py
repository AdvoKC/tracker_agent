"""structlog setup: JSON lines to ``<log_dir>/YYYY-MM-DD.jsonl`` plus readable console output.

Bind ``run_id``, ``meter_id``, ``promise_id`` and ``agent`` with ``structlog.contextvars``
so every line carries them (PRD §5.5).
"""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

import structlog

_SECRET_KEYS = frozenset(
    {"password", "token", "secret", "api_key", "authorization", "database_url"}
)


def _redact_secrets(
    _logger: object, _name: str, event_dict: structlog.typing.EventDict
) -> structlog.typing.EventDict:
    for key in list(event_dict):
        if key.lower() in _SECRET_KEYS:
            event_dict[key] = "***"
    return event_dict


def log_file_for(log_dir: Path, now: datetime | None = None) -> Path:
    day = (now or datetime.now(UTC)).strftime("%Y-%m-%d")
    return log_dir / f"{day}.jsonl"


def configure_logging(log_dir: Path, level: str = "INFO", *, console: bool = True) -> Path:
    """Configure stdlib logging + structlog. Returns the JSONL file path in use."""
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_file_for(log_dir)

    shared: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _redact_secrets,
    ]

    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.format_exc_info,
                structlog.processors.JSONRenderer(),
            ],
            foreign_pre_chain=shared,
        )
    )
    handlers: list[logging.Handler] = [file_handler]

    if console:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                processors=[
                    structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                    structlog.dev.ConsoleRenderer(colors=False),
                ],
                foreign_pre_chain=shared,
            )
        )
        handlers.append(console_handler)

    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    for h in handlers:
        root.addHandler(h)
    root.setLevel(level)

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )
    return path


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.stdlib.get_logger(name)
    return logger
