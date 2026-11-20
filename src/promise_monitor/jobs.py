"""Background operations started from the dashboard (superadmin; coordinators: briefs only).

Only a fixed whitelist of commands can run; targets must be UUIDs of existing rows, so no
user text ever reaches a command line. Each job runs ``uv run promise-monitor …`` as a
separate process (same code path, same run lock as cron) and its state/exit code/output tail
are stored in ``jobs``. Exit code 3 = the run lock was busy → state ``busy``.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from promise_monitor import access
from promise_monitor.access import AccessError
from promise_monitor.config import PROJECT_ROOT
from promise_monitor.db.models import Article, ArticleGroup, Job, Meter, User
from promise_monitor.logging_setup import get_logger

log = get_logger(__name__)

KINDS: dict[str, tuple[str, Callable[[str], list[str]], type | None]] = {
    "run": ("Run the full pipeline now", lambda _t: ["run"], None),
    "news_scan_meter": (
        "Re-scan news for this meter",
        lambda t: ["news-scan", "--meter", t],
        Meter,
    ),
    "monitor_meter": (
        "Re-check this meter's promises",
        lambda t: ["monitor", "--meter", t, "--no-full"],
        Meter,
    ),
    "verify_group": (
        "Re-verify sources",
        lambda t: ["verify", "--group", t, "--refresh"],
        ArticleGroup,
    ),
    "redraft_article": ("Redraft this article", lambda t: ["redraft", "--article", t], Article),
    "write_brief": (
        "Write an update from a brief",
        lambda t: ["write-brief", "--group", t],
        ArticleGroup,
    ),
    "draft_group": (
        "Draft the update for this lead",
        lambda t: ["write", "--group", t],
        ArticleGroup,
    ),
}
# Kinds a coordinator may start, for a target on a meter they can currently see.
COORDINATOR_KINDS = frozenset({"write_brief", "draft_group"})
OUTPUT_TAIL = 8000

Launcher = Callable[[list[str], Path], tuple[int, str]]


def default_launcher(argv: list[str], cwd: Path) -> tuple[int, str]:
    uv = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
    proc = subprocess.run(  # noqa: S603 (fixed whitelist argv, no shell)
        [uv, "run", "--frozen", "promise-monitor", *argv],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=3 * 3600,
        check=False,
    )
    return proc.returncode, (proc.stdout + proc.stderr)[-OUTPUT_TAIL:]


def recover_lost(session: Session, now: datetime) -> int:
    """Jobs left queued/running by a previous dashboard process can't be tracked any more."""
    n = 0
    for job in session.scalars(select(Job).where(Job.state.in_(("queued", "running")))):
        job.state, job.finished_at = "lost", now
        job.output = (
            job.output or ""
        ) + "\n[dashboard restarted while this job ran; check the logs]"
        n += 1
    session.commit()
    return n


def start_job(
    factory: sessionmaker[Session],
    *,
    kind: str,
    target: str | None,
    actor: User,
    now: datetime,
    launcher: Launcher = default_launcher,
    cwd: Path = PROJECT_ROOT,
    wait: bool = False,
) -> uuid.UUID:
    if kind not in KINDS:
        raise AccessError("unknown operation")
    if kind not in COORDINATOR_KINDS:
        access.require_superadmin(actor)
    _label, build, model = KINDS[kind]
    with factory() as s:
        if model is not None:
            try:
                tid = uuid.UUID(str(target))
            except ValueError as exc:
                raise AccessError("invalid target") from exc
            row = s.get(model, tid)
            if row is None:
                raise AccessError("target not found")
            meter_id = getattr(row, "meter_id", None)
            if kind in COORDINATOR_KINDS and not (
                meter_id is not None and access.can_view(s, actor, meter_id, now)
            ):
                raise AccessError("target not found")
            target = str(tid)
        else:
            target = None
        active = s.scalars(
            select(Job).where(
                Job.kind == kind, Job.target == target, Job.state.in_(("queued", "running"))
            )
        ).first()
        if active is not None:
            raise AccessError("that operation is already running")
        job = Job(
            kind=kind,
            target=target,
            argv=build(target or ""),
            state="queued",
            requested_by=actor.user_id,
            created_at=now,
        )
        s.add(job)
        access.audit(s, actor, f"job.{kind}", "job", job.job_id, now, after={"target": target})
        s.commit()
        job_id, argv = job.job_id, list(job.argv)

    def work() -> None:
        with factory() as s:
            j = s.get(Job, job_id)
            assert j is not None
            j.state, j.started_at = "running", datetime.now(UTC)
            s.commit()
        try:
            code, output = launcher(argv, cwd)
        except Exception as exc:
            code, output = -1, f"{type(exc).__name__}: {exc}"
        with factory() as s:
            j = s.get(Job, job_id)
            assert j is not None
            j.exit_code, j.output, j.finished_at = code, output, datetime.now(UTC)
            j.state = "done" if code in (0, 2) else "busy" if code == 3 else "failed"
            s.commit()
        log.info("job_finished", kind=kind, state=j.state, exit_code=code)

    if wait:
        work()
    else:
        threading.Thread(target=work, name=f"job-{job_id}", daemon=True).start()
    return job_id
