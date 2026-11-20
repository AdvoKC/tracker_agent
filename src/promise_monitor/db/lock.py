"""Single-writer lock (PRD §5.1): a Postgres advisory lock shared by every writing command.

Held on a dedicated connection for the duration of the command, released automatically if
the process dies (Postgres drops session locks when the connection closes). Non-Postgres
engines (SQLite in tests) always "acquire".
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, text

LOCK_KEY = 0x50_4D_4F_4E  # "PMON"


@contextmanager
def run_lock(engine: Engine, key: int = LOCK_KEY) -> Iterator[bool]:
    if engine.dialect.name != "postgresql":
        yield True
        return
    with engine.connect() as conn:
        got = bool(conn.execute(text("select pg_try_advisory_lock(:k)"), {"k": key}).scalar())
        conn.commit()
        try:
            yield got
        finally:
            if got:
                conn.execute(text("select pg_advisory_unlock(:k)"), {"k": key})
                conn.commit()
