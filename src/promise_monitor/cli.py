"""Command-line entry point: ``promise-monitor``.

Pipeline commands are stubs until their PRD §8 phase lands; they exit with code 2
and say which phase implements them, so cron never silently "succeeds".
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated
from zoneinfo import ZoneInfo

import structlog
import typer
from sqlalchemy import select

from promise_monitor import __version__
from promise_monitor.agents.classifier import classify
from promise_monitor.agents.discovery import SiteDownError, apply_discovery, collect, priority_key
from promise_monitor.agents.monitor import monitor_meter
from promise_monitor.agents.news_scout import run_news_scout
from promise_monitor.api.client import PromiseTrackerClient, RateLimitedError
from promise_monitor.config import ConfigError, get_settings, load_app_config
from promise_monitor.db.base import (
    DatabaseNotConfiguredError,
    make_engine,
    make_session_factory,
    ping,
)
from promise_monitor.db.models import ChangeEvent, Meter, Run
from promise_monitor.llm.factory import make_llm
from promise_monitor.llm.recording import RecordingLLMClient
from promise_monitor.logging_setup import configure_logging, get_logger

app = typer.Typer(
    name="promise-monitor",
    help="AdvoKC Promise Tracker Monitor — see docs/PRD.md.",
    no_args_is_help=True,
    add_completion=False,
)


def _not_yet(command: str, phase: str) -> None:
    typer.echo(f"'{command}' is not implemented yet (arrives in phase {phase}).", err=True)
    raise typer.Exit(code=2)


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)


@app.command("check-config")
def check_config() -> None:
    """Validate .env and config/*.yaml; exit non-zero on any error."""
    settings = get_settings()
    try:
        cfg = load_app_config(settings.config_dir)
    except (ConfigError, ValueError) as exc:
        typer.echo(f"config invalid: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        f"ok: env={settings.env} states={len(cfg.states)} "
        f"banned_terms={len(cfg.banned_terms)} "
        f"database_url={'set' if settings.database_url else 'NOT SET'} "
        f"admin_email={'set' if settings.admin_email else 'NOT SET'}"
    )


@app.command("db-check")
def db_check() -> None:
    """Connect to PostgreSQL and print the server version."""
    settings = get_settings()
    try:
        version_str = ping(make_engine(settings.database_url))
    except DatabaseNotConfiguredError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    except Exception as exc:
        typer.echo(f"database unreachable: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"ok: {version_str}")


@app.command()
def run(
    meter: Annotated[str | None, typer.Option(help="Run a single meter by id.")] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="No Sheet/DB writes, no emails.")
    ] = False,
) -> None:
    """Full daily pipeline (Publisher → Discovery → per-meter subgraph → Notifier)."""
    settings = get_settings()
    configure_logging(settings.log_dir, settings.log_level)
    get_logger("cli").info("run_requested", meter=meter, dry_run=dry_run)
    _not_yet("run", "P1-P7")


@app.command()
def discover(
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Fetch and print meters; no DB writes.")
    ] = False,
) -> None:
    """Refresh the meter registry (PRD §3.1)."""
    settings = get_settings()
    configure_logging(settings.log_dir, settings.log_level, console=False)
    cfg = load_app_config(settings.config_dir)

    with PromiseTrackerClient(cfg.tunables.api, contact=settings.admin_email) as client:
        collected = collect(client, cfg.states)
        requests = client.request_count

    for source, err in sorted(collected.failed_sources.items()):
        typer.echo(f"FAILED {source}: {err}", err=True)

    if dry_run:
        if not collected.ok_sources:
            typer.echo("all discovery sources failed", err=True)
            raise typer.Exit(code=1)
        rows = sorted(
            collected.meters.values(),
            key=lambda m: (cfg.priority.rank_for(priority_key(m)), m.source_key, m.name),
        )
        for m in rows:
            status = "archived" if m.is_archived else "active"
            typer.echo(
                f"{priority_key(m):<22} {m.meter_type:<16} {status:<9} "
                f"{m.promise_count or 0:>4}  {m.name}  [{m.id}]"
            )
        typer.echo(
            f"{len(rows)} meters ({sum(not m.is_archived for m in rows)} active), "
            f"{requests} requests, {len(collected.failed_sources)} failed sources"
        )
        return

    try:
        engine = make_engine(settings.database_url)
        with make_session_factory(engine).begin() as session:
            result = apply_discovery(session, collected, cfg, datetime.now(UTC))
    except SiteDownError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    except DatabaseNotConfiguredError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        f"{result.status}: active={len(result.active)} new={len(result.new)} "
        f"updated={len(result.updated)} missing={len(result.missing)} "
        f"reappeared={len(result.reappeared)} requests={requests}"
    )


@app.command()
def monitor(
    meter: Annotated[str | None, typer.Option(help="Monitor a single meter by id.")] = None,
    full: Annotated[
        bool | None,
        typer.Option("--full/--no-full", help="Fetch every promise's detail (default: weekly)."),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Do everything, then roll back all DB writes.")
    ] = False,
    no_llm: Annotated[
        bool, typer.Option("--no-llm", help="Rule-based scores only (no Claude calls).")
    ] = False,
) -> None:
    """Snapshot active meters, classify changes into events (PRD §3.2-3.3)."""
    settings = get_settings()
    configure_logging(settings.log_dir, settings.log_level, console=False)
    cfg = load_app_config(settings.config_dir)
    tz = ZoneInfo(cfg.tunables.timezone_display)
    now = datetime.now(UTC)
    force_full = (
        full
        if full is not None
        else now.astimezone(tz).weekday() == cfg.tunables.monitor.full_sweep_weekday
    )
    try:
        engine = make_engine(settings.database_url)
    except DatabaseNotConfiguredError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    with make_session_factory(engine)() as session:
        run_row = Run(started_at=now, trigger="cli:monitor" + (":dry-run" if dry_run else ""))
        session.add(run_row)
        session.flush()
        if not dry_run:
            session.commit()
        structlog.contextvars.bind_contextvars(run_id=str(run_row.run_id))

        query = select(Meter).order_by(Meter.priority_rank, Meter.name, Meter.id)
        if meter:
            query = query.where(Meter.id == uuid.UUID(meter))
        else:
            query = query.where(Meter.registry_status == "active")
        meters = list(session.scalars(query))
        if not meters:
            typer.echo("no meters to monitor (run `promise-monitor discover` first)", err=True)
            raise typer.Exit(code=1)

        totals: dict[str, int] = {
            "diffs": 0,
            "snapshots": 0,
            "details": 0,
            "removed": 0,
            "events": 0,
            "queued_groups": 0,
        }
        llm = (
            None
            if no_llm
            else RecordingLLMClient(make_llm(cfg.tunables.llm), session, run_row.run_id)
        )
        statuses: dict[str, str] = {}
        rate_limited = False
        with PromiseTrackerClient(cfg.tunables.api, contact=settings.admin_email) as client:
            for m in meters:
                try:
                    with session.begin_nested():
                        res = monitor_meter(
                            session,
                            client,
                            m,
                            run_row.run_id,
                            datetime.now(UTC),
                            cfg.tunables.monitor,
                            force_full=force_full,
                        )
                        cls = classify(
                            session,
                            run_id=run_row.run_id,
                            meter=m,
                            diffs=res.diffs,
                            removed=res.removed,
                            news=[],
                            cfg=cfg.tunables.classifier,
                            llm=llm,
                            now=datetime.now(UTC),
                        )
                except RateLimitedError as exc:
                    typer.echo(f"rate limited, stopping: {exc}", err=True)
                    rate_limited = True
                    break
                except Exception as exc:
                    get_logger("cli").exception("meter_crashed", meter_id=str(m.id))
                    statuses[str(m.id)] = "error"
                    typer.echo(f"ERROR {m.name}: {type(exc).__name__}: {exc}", err=True)
                    continue
                if not dry_run:
                    session.commit()
                statuses[str(m.id)] = res.status if not res.errors else f"{res.status}+errors"
                totals["diffs"] += len(res.diffs)
                totals["snapshots"] += res.snapshots_written
                totals["details"] += res.details_fetched
                totals["removed"] += len(res.removed)
                totals["events"] += len(cls.events)
                totals["queued_groups"] += len(cls.groups)
                typer.echo(
                    f"{res.status:<9} {m.name[:40]:<40} listed={res.listed:<3} "
                    f"details={res.details_fetched:<3} snapshots={res.snapshots_written:<3} "
                    f"diffs={len(res.diffs)} removed={len(res.removed)}"
                    + (f" ERRORS={len(res.errors)}" if res.errors else "")
                )
                _echo_events(cls.events)
            requests = client.request_count

        bad = rate_limited or any(
            v in ("error", "skipped", "anomaly", "degraded") or v.endswith("+errors")
            for v in statuses.values()
        )
        run_row.status = "degraded" if bad else "ok"
        run_row.finished_at = datetime.now(UTC)
        run_row.stats = {
            "monitor": {
                "meters": statuses,
                "requests": requests,
                "force_full": force_full,
                **totals,
            }
        }
        if dry_run:
            session.rollback()
            typer.echo("dry run: all DB writes rolled back")
        else:
            session.commit()
        structlog.contextvars.clear_contextvars()
    typer.echo(
        f"{run_row.status}: meters={len(statuses)} requests={requests} "
        + " ".join(f"{k}={v}" for k, v in totals.items())
    )


def _echo_events(events: list[ChangeEvent]) -> None:
    for ev in events:
        typer.echo(
            f"    {ev.state:<7} {ev.change_type:<16} score={ev.score:<3} "
            f"promise={ev.promise_id} {ev.score_reason[:60]}"
        )


@app.command("news-scan")
def news_scan(
    max_calls: Annotated[
        int | None, typer.Option(help="Override news.max_calls_per_run for this run.")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Search and classify, then roll back DB writes.")
    ] = False,
) -> None:
    """Search news for promises with Claude web search; classify findings (PRD §3.9)."""
    settings = get_settings()
    configure_logging(settings.log_dir, settings.log_level, console=False)
    cfg = load_app_config(settings.config_dir)
    if not cfg.tunables.news.enabled:
        typer.echo("news scouting is disabled (news.enabled=false)")
        return
    try:
        engine = make_engine(settings.database_url)
    except DatabaseNotConfiguredError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    now = datetime.now(UTC)
    with make_session_factory(engine)() as session:
        run_row = Run(started_at=now, trigger="cli:news-scan" + (":dry-run" if dry_run else ""))
        session.add(run_row)
        session.flush()
        if not dry_run:
            session.commit()
        structlog.contextvars.bind_contextvars(run_id=str(run_row.run_id))
        llm = RecordingLLMClient(make_llm(cfg.tunables.llm), session, run_row.run_id)

        scout = run_news_scout(
            session,
            llm,
            run_id=run_row.run_id,
            now=now,
            cfg=cfg.tunables.news,
            sources=cfg.news_sources,
            max_calls=max_calls,
        )
        typer.echo(
            f"searched={scout.searched} calls={scout.calls} failed_calls={scout.failed_calls} "
            f"accepted={scout.accepted} rejected={dict(sorted(scout.rejected.items()))}"
        )
        events = 0
        groups = 0
        for meter_id, findings in scout.findings.items():
            meter = session.get(Meter, meter_id)
            assert meter is not None
            cls = classify(
                session,
                run_id=run_row.run_id,
                meter=meter,
                diffs=[],
                removed=[],
                news=findings,
                cfg=cfg.tunables.classifier,
                llm=llm,
                now=datetime.now(UTC),
            )
            typer.echo(f"  {meter.name}")
            _echo_events(cls.events)
            events += len(cls.events)
            groups += len(cls.groups)

        bad = scout.auth_failed or scout.failed_calls > 0
        run_row.status = "degraded" if bad else "ok"
        run_row.finished_at = datetime.now(UTC)
        run_row.stats = {
            "news_scan": {
                "searched": scout.searched,
                "calls": scout.calls,
                "failed_calls": scout.failed_calls,
                "accepted": scout.accepted,
                "rejected": scout.rejected,
                "events": events,
                "queued_groups": groups,
            }
        }
        if dry_run:
            session.rollback()
            typer.echo("dry run: all DB writes rolled back")
        else:
            session.commit()
        structlog.contextvars.clear_contextvars()
    if scout.auth_failed:
        typer.echo("Claude login failed: run `claude` once interactively and /login", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"{run_row.status}: events={events} queued_groups={groups}")


@app.command()
def publish() -> None:
    """Publish coordinator-approved articles to site/."""
    _not_yet("publish", "P7")


if __name__ == "__main__":
    app()
