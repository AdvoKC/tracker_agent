"""Command-line entry point: ``promise-monitor``.

Pipeline commands are stubs until their PRD §8 phase lands; they exit with code 2
and say which phase implements them, so cron never silently "succeeds".
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

import structlog
import typer
from sqlalchemy import Engine, select

from promise_monitor import __version__
from promise_monitor.access import audit
from promise_monitor.agents.classifier import classify
from promise_monitor.agents.discovery import SiteDownError, apply_discovery, collect, priority_key
from promise_monitor.agents.monitor import monitor_meter
from promise_monitor.agents.news_scout import run_news_scout
from promise_monitor.agents.publisher import publish_approved, render_site
from promise_monitor.agents.review_queue import ReviewQueue
from promise_monitor.agents.verifier import Verifier
from promise_monitor.agents.writer import (
    DeferredError,
    Drafter,
    qa_context_from_input,
    run_draft_loop,
)
from promise_monitor.api.client import PromiseTrackerClient, RateLimitedError
from promise_monitor.config import AppConfig, ConfigError, get_settings, load_app_config
from promise_monitor.db.base import (
    DatabaseNotConfiguredError,
    make_engine,
    make_session_factory,
    ping,
)
from promise_monitor.db.lock import run_lock
from promise_monitor.db.models import (
    Article,
    ArticleGroup,
    ArticleGroupSource,
    ChangeEvent,
    Meter,
    Run,
    Source,
    User,
)
from promise_monitor.domain.fetch import SafeFetcher
from promise_monitor.evals import build_cases, qa_context
from promise_monitor.house_style import house_examples
from promise_monitor.llm.base import LLMError
from promise_monitor.llm.factory import make_llm
from promise_monitor.llm.recording import RecordingLLMClient
from promise_monitor.logging_setup import configure_logging, get_logger
from promise_monitor.notify import Mailer, MemoryMailer, NullMailer, RunReport, make_mailer
from promise_monitor.pipeline import DailyRun, PipelineDeps
from promise_monitor.settings_store import effective_config
from promise_monitor.shadow import build_report
from promise_monitor.sheets.backend import GSpreadBackend, MemorySheets, SheetsBackend
from promise_monitor.submissions import TokenBox, default_client_factory, token_box

app = typer.Typer(
    name="promise-monitor",
    help="AdvoKC Promise Tracker Monitor — see docs/PRD.md.",
    no_args_is_help=True,
    add_completion=False,
)


def _not_yet(command: str, phase: str) -> None:
    typer.echo(f"'{command}' is not implemented yet (arrives in phase {phase}).", err=True)
    raise typer.Exit(code=2)


def _effective(engine: Engine, cfg: AppConfig) -> AppConfig:
    """YAML config + the dashboard's audited overrides (settings_store)."""
    with make_session_factory(engine)() as session:
        return effective_config(session, cfg)


@contextmanager
def _exclusive(engine: Engine, *, quiet_skip: bool = False) -> Iterator[None]:
    """One writer at a time (cron run vs manual commands). Busy → exit 0 (run) or 3."""
    with run_lock(engine) as acquired:
        if not acquired:
            typer.echo("another promise-monitor command is running (lock held); skipping", err=True)
            raise typer.Exit(code=0 if quiet_skip else 3)
        yield


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
    try:
        token_box(settings)
    except ValueError as exc:
        typer.echo(f"config invalid: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        f"ok: env={settings.env} states={len(cfg.states)} "
        f"banned_terms={len(cfg.banned_terms)} "
        f"database_url={'set' if settings.database_url else 'NOT SET'} "
        f"admin_email={'set' if settings.admin_email else 'NOT SET'}"
    )
    if cfg.tunables.submissions.enabled and not settings.token_key:
        typer.echo("warning: submissions.enabled but PM_TOKEN_KEY is not set", err=True)


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
    full: Annotated[
        bool | None,
        typer.Option("--full/--no-full", help="Fetch every promise's detail (default: weekly)."),
    ] = None,
    no_email: Annotated[bool, typer.Option("--no-email", help="Don't send any email.")] = False,
) -> None:
    """The daily pipeline (cron). Stages: see promise_monitor/pipeline.py."""
    settings = get_settings()
    configure_logging(settings.log_dir, settings.log_level, console=False)
    cfg = load_app_config(settings.config_dir)
    try:
        engine = make_engine(settings.database_url)
    except DatabaseNotConfiguredError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    cfg = _effective(engine, cfg)
    now = datetime.now(UTC)
    tz = ZoneInfo(cfg.tunables.timezone_display)
    force_full = (
        full
        if full is not None
        else now.astimezone(tz).weekday() == cfg.tunables.monitor.full_sweep_weekday
    )
    sheets: SheetsBackend | None = None
    sheet_url: str | None = cfg.tunables.review.dashboard_url
    if (
        cfg.tunables.review.channel == "sheets"
        and settings.review_spreadsheet_id
        and settings.google_service_account_file
    ):
        sheet_url = f"https://docs.google.com/spreadsheets/d/{settings.review_spreadsheet_id}/edit"
        try:
            sheets = GSpreadBackend(
                settings.review_spreadsheet_id, settings.google_service_account_file
            )
        except Exception as exc:
            typer.echo(f"review sheet unavailable: {type(exc).__name__}: {exc}", err=True)
    mailer: Mailer = MemoryMailer() if no_email else make_mailer(settings)
    box: TokenBox | None = None
    try:
        box = token_box(settings)
    except ValueError as exc:
        typer.echo(f"submissions: {exc}; status checks skipped", err=True)
    ua_contact = f" (+contact: {settings.admin_email})" if settings.admin_email else ""
    with (
        PromiseTrackerClient(cfg.tunables.api, contact=settings.admin_email) as api,
        SafeFetcher(cfg.tunables.verifier, cfg.tunables.api.user_agent + ua_contact) as fetcher,
        _exclusive(engine, quiet_skip=True),
        make_session_factory(engine)() as session,
    ):
        deps = PipelineDeps(
            api=api,
            fetcher=fetcher,
            llm=make_llm(cfg.tunables.llm),
            sheets=sheets,
            mailer=mailer,
            admin_email=settings.admin_email,
            review_sheet_url=sheet_url,
            log_dir=settings.log_dir,
            token_box=box,
            submission_client=default_client_factory(cfg, settings.admin_email),
        )
        daily = DailyRun(session, cfg, deps, now=now, force_full=force_full)
        if (
            cfg.tunables.review.channel == "sheets"
            and sheets is None
            and settings.review_spreadsheet_id
        ):
            daily.report.degrade("review sheet configured but could not be opened")
        report = daily.run()
    typer.echo(f"{report.status}: run {report.run_id}")
    for k, v in report.stats.items():
        typer.echo(f"  {k}: {v}")
    for k, v in report.stage_errors.items():
        typer.echo(f"  STAGE ERROR {k}: {v}", err=True)
    for w in report.urgent:
        typer.echo(f"  URGENT: {w}", err=True)
    for w in report.warnings:
        typer.echo(f"  warning: {w}")
    if isinstance(mailer, NullMailer) and mailer.skipped:
        typer.echo(f"  ({mailer.skipped} email(s) not sent: SMTP not configured)")
    if report.status == "failed":
        # 2 = failed but already reported by email; systemd treats it as success so its
        # OnFailure alert only fires for crashes that never reached the Notifier.
        raise typer.Exit(code=2)


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
        with _exclusive(engine), make_session_factory(engine).begin() as session:
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
    cfg = _effective(engine, cfg)

    with _exclusive(engine), make_session_factory(engine)() as session:
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
    meter_filter: Annotated[
        str | None, typer.Option("--meter", help="Only search this meter's promises.")
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
    cfg = _effective(engine, cfg)

    now = datetime.now(UTC)
    with _exclusive(engine), make_session_factory(engine)() as session:
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
            meter_id=uuid.UUID(meter_filter) if meter_filter else None,
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
def verify(
    limit: Annotated[int | None, typer.Option(help="Max article groups this run.")] = None,
    group_id: Annotated[
        str | None, typer.Option("--group", help="Re-verify just this article group.")
    ] = None,
    refresh: Annotated[bool, typer.Option("--refresh", help="Ignore the source cache.")] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Verify, print, then roll back DB writes.")
    ] = False,
) -> None:
    """Fetch and judge the sources of queued article groups (PRD §3.4)."""
    settings = get_settings()
    configure_logging(settings.log_dir, settings.log_level, console=False)
    cfg = load_app_config(settings.config_dir)
    try:
        engine = make_engine(settings.database_url)
    except DatabaseNotConfiguredError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    cfg = _effective(engine, cfg)

    now = datetime.now(UTC)
    ua = cfg.tunables.api.user_agent + (
        f" (+source verification; contact: {settings.admin_email})" if settings.admin_email else ""
    )
    with _exclusive(engine), make_session_factory(engine)() as session:
        run_row = Run(started_at=now, trigger="cli:verify" + (":dry-run" if dry_run else ""))
        session.add(run_row)
        session.flush()
        if not dry_run:
            session.commit()
        structlog.contextvars.bind_contextvars(run_id=str(run_row.run_id))
        llm = RecordingLLMClient(make_llm(cfg.tunables.llm), session, run_row.run_id)
        with SafeFetcher(cfg.tunables.verifier, ua) as fetcher:
            verifier = Verifier(
                session,
                fetcher,
                llm,
                cfg=cfg.tunables.verifier,
                public_promise_url=cfg.tunables.api.public_promise_url,
                now=now,
            )
            only = None
            if group_id:
                only = uuid.UUID(group_id)
                try:
                    verifier.requeue(only, refresh=refresh)
                except ValueError as exc:
                    typer.echo(str(exc), err=True)
                    raise typer.Exit(code=1) from exc
            res = verifier.run(limit, only=only)

        for gid in res.group_ids:
            group = session.get(ArticleGroup, gid)
            if group is None:
                continue
            typer.echo(f"{group.state:<13} group={gid} promise={group.promise_id}")
            for ags, src in session.execute(
                select(ArticleGroupSource, Source)
                .join(Source, Source.source_id == ArticleGroupSource.source_id)
                .where(ArticleGroupSource.group_id == gid)
            ):
                conf = ags.relevance_confidence
                typer.echo(
                    f"    {ags.verdict:<16} {ags.role:<12} fetch={src.fetch_status:<9} "
                    f"conf={'-' if conf is None else round(conf, 2)} {src.url[:90]}"
                )
            for note in group.notes or []:
                typer.echo(f"    note: {note[:140]}")

        run_row.status = "degraded" if (res.deferred or res.auth_failed) else "ok"
        run_row.finished_at = datetime.now(UTC)
        run_row.stats = {"verify": {k: v for k, v in res.__dict__.items() if k != "group_ids"}}
        if dry_run:
            session.rollback()
            typer.echo("dry run: all DB writes rolled back")
        else:
            session.commit()
        structlog.contextvars.clear_contextvars()
    typer.echo(
        f"{run_row.status}: groups={res.groups} verified={res.verified} "
        f"needs_sources={res.needs_sources} deferred={res.deferred} fetched={res.fetched} "
        f"cache_hits={res.cache_hits} llm_calls={res.llm_calls}"
    )
    if res.auth_failed:
        typer.echo("Claude login failed: run `claude` once interactively and /login", err=True)
        raise typer.Exit(code=1)


@app.command()
def write(
    limit: Annotated[int | None, typer.Option(help="Max article groups this run.")] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Draft and print, then roll back DB writes.")
    ] = False,
    group: Annotated[
        str | None, typer.Option("--group", help="Draft only this verified article group.")
    ] = None,
) -> None:
    """Draft articles for verified groups: Writer → QA → Editor, ≤3 rewrites (PRD §3.5-3.6)."""
    try:
        only = uuid.UUID(group) if group else None
    except ValueError as exc:
        typer.echo("invalid group id", err=True)
        raise typer.Exit(code=1) from exc
    settings = get_settings()
    configure_logging(settings.log_dir, settings.log_level, console=False)
    cfg = load_app_config(settings.config_dir)
    try:
        engine = make_engine(settings.database_url)
    except DatabaseNotConfiguredError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    cfg = _effective(engine, cfg)
    now = datetime.now(UTC)
    with _exclusive(engine), make_session_factory(engine)() as session:
        run_row = Run(started_at=now, trigger="cli:write" + (":dry-run" if dry_run else ""))
        session.add(run_row)
        session.flush()
        if not dry_run:
            session.commit()
        structlog.contextvars.bind_contextvars(run_id=str(run_row.run_id))
        llm = RecordingLLMClient(make_llm(cfg.tunables.llm), session, run_row.run_id)
        drafter = Drafter(
            session,
            llm,
            cfg=cfg.tunables.article,
            banned_terms=cfg.banned_terms,
            public_promise_url=cfg.tunables.api.public_promise_url,
            now=now,
        )
        res = drafter.run(limit, only=only)
        for aid in res.article_ids:
            art = session.get(Article, aid)
            assert art is not None
            typer.echo(
                f"\n[{art.state}] {art.word_count} words, {art.rewrite_count} rewrite(s), "
                f"article={art.article_id}"
            )
            typer.echo(f"  HEADLINE: {art.headline}")
            typer.echo(f"  {art.body_markdown}")
            for flag in art.qa_flags:
                typer.echo(f"  FLAG: {flag}")
        run_row.status = "degraded" if (res.deferred or res.auth_failed) else "ok"
        run_row.finished_at = datetime.now(UTC)
        run_row.stats = {"write": {k: v for k, v in res.__dict__.items() if k != "article_ids"}}
        if dry_run:
            session.rollback()
            typer.echo("dry run: all DB writes rolled back")
        else:
            session.commit()
        structlog.contextvars.clear_contextvars()
    typer.echo(
        f"\n{run_row.status}: groups={res.groups} in_review={res.in_review} "
        f"needs_attention={res.needs_attention} deferred={res.deferred}"
    )
    if res.auth_failed:
        typer.echo("Claude login failed: run `claude` once interactively and /login", err=True)
        raise typer.Exit(code=1)


@app.command("write-brief")
def write_brief(
    group_id: Annotated[str, typer.Option("--group", help="Article group of the brief.")],
) -> None:
    """Verify the sources of one coordinator brief, then draft its article (dashboard job)."""
    settings = get_settings()
    configure_logging(settings.log_dir, settings.log_level, console=False)
    cfg = load_app_config(settings.config_dir)
    try:
        engine = make_engine(settings.database_url)
    except DatabaseNotConfiguredError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    cfg = _effective(engine, cfg)
    try:
        gid = uuid.UUID(group_id)
    except ValueError as exc:
        typer.echo("invalid group id", err=True)
        raise typer.Exit(code=1) from exc
    now = datetime.now(UTC)
    ua = cfg.tunables.api.user_agent + (
        f" (+source verification; contact: {settings.admin_email})" if settings.admin_email else ""
    )
    with _exclusive(engine), make_session_factory(engine)() as session:
        group = session.get(ArticleGroup, gid)
        if group is None or group.state != "queued":
            typer.echo("brief not found, or already processed", err=True)
            raise typer.Exit(code=1)
        run_row = Run(started_at=now, trigger="cli:write-brief")
        session.add(run_row)
        session.flush()
        session.commit()
        structlog.contextvars.bind_contextvars(run_id=str(run_row.run_id))
        llm = RecordingLLMClient(make_llm(cfg.tunables.llm), session, run_row.run_id)
        typer.echo("checking the source links…")
        with SafeFetcher(cfg.tunables.verifier, ua) as fetcher:
            vres = Verifier(
                session,
                fetcher,
                llm,
                cfg=cfg.tunables.verifier,
                public_promise_url=cfg.tunables.api.public_promise_url,
                now=now,
            ).run(only=gid)
        session.commit()
        session.refresh(group)
        for ags, src in session.execute(
            select(ArticleGroupSource, Source)
            .join(Source, Source.source_id == ArticleGroupSource.source_id)
            .where(ArticleGroupSource.group_id == gid)
        ):
            typer.echo(f"  {ags.verdict:<16} {ags.role:<12} {src.url[:100]}")
        for note in group.notes or []:
            typer.echo(f"  note: {note[:160]}")
        status, code = "ok", 0
        if group.state == "verified":
            typer.echo("sources verified; drafting the article…")
            drafter = Drafter(
                session,
                llm,
                cfg=cfg.tunables.article,
                banned_terms=cfg.banned_terms,
                public_promise_url=cfg.tunables.api.public_promise_url,
                now=now,
            )
            try:
                art = drafter.draft(group)
                typer.echo(f"[{art.state}] {art.word_count} words  article={art.article_id}")
                typer.echo(f"  HEADLINE: {art.headline}\n  {art.body_markdown}")
                for flag in art.qa_flags:
                    typer.echo(f"  FLAG: {flag}")
            except (LLMError, DeferredError) as exc:
                status, code = "degraded", 1
                typer.echo(f"drafting deferred to the next daily run: {exc}", err=True)
        elif group.state == "needs_sources":
            typer.echo("no source could be verified: saved as an evidence lead, not drafted")
        else:
            status, code = "degraded", 1
            typer.echo(f"verification deferred (state {group.state}); the daily run retries")
        run_row.status = status
        run_row.finished_at = datetime.now(UTC)
        run_row.stats = {"verify": {"deferred": vres.deferred, "llm_calls": vres.llm_calls}}
        session.commit()
        structlog.contextvars.clear_contextvars()
    if code:
        raise typer.Exit(code=code)


@app.command()
def redraft(
    article: Annotated[str, typer.Option(help="Article id to redraft (still under review).")],
) -> None:
    """Write a fresh draft for an article still under review (same sources, same QA loop)."""
    settings = get_settings()
    configure_logging(settings.log_dir, settings.log_level, console=False)
    cfg = load_app_config(settings.config_dir)
    try:
        engine = make_engine(settings.database_url)
    except DatabaseNotConfiguredError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    cfg = _effective(engine, cfg)
    now = datetime.now(UTC)
    with _exclusive(engine), make_session_factory(engine)() as session:
        art = session.get(Article, uuid.UUID(article))
        if art is None or art.state not in ("in_review", "needs_attention", "stale"):
            typer.echo("article not found, or no longer under review", err=True)
            raise typer.Exit(code=1)
        run_row = Run(started_at=now, trigger="cli:redraft")
        session.add(run_row)
        session.flush()
        llm = RecordingLLMClient(make_llm(cfg.tunables.llm), session, run_row.run_id)
        qa = qa_context_from_input(art.writer_input, cfg.tunables.article, cfg.banned_terms)
        examples = house_examples(
            session,
            art.meter_id,
            cfg.tunables.article.house_examples,
            exclude_promise=art.promise_id,
        )
        try:
            loop = run_draft_loop(
                llm,
                art.writer_input,
                qa,
                cfg.tunables.article,
                subject_id=str(art.article_group_id),
                examples=examples,
            )
        except LLMError as exc:
            session.commit()
            typer.echo(f"Claude unavailable: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        before = {"headline": art.headline, "body_markdown": art.body_markdown}
        art.headline, art.body_markdown = loop.headline, loop.body
        art.word_count = loop.report.word_count if loop.report else 0
        art.qa_flags, art.rewrite_count = loop.issues, loop.attempts - 1
        art.state = "in_review" if loop.passed else "needs_attention"
        audit(
            session,
            None,
            "article.redraft",
            "article",
            art.article_id,
            now,
            before=before,
            after={"headline": art.headline, "passed": loop.passed},
        )
        run_row.status, run_row.finished_at = "ok", datetime.now(UTC)
        session.commit()
    typer.echo(f"redrafted: {art.state} ({art.word_count} words, {loop.attempts} attempt(s))")


@app.command("eval-writer")
def eval_writer(
    cases: Annotated[int | None, typer.Option(help="Run only the first N cases.")] = None,
) -> None:
    """Run the real Writer+Editor loop on fixture cases; report the QA pass rate (P5 exit)."""
    settings = get_settings()
    configure_logging(settings.log_dir, settings.log_level, console=False)
    cfg = load_app_config(settings.config_dir)
    acfg = cfg.tunables.article
    llm = make_llm(cfg.tunables.llm)
    all_cases = build_cases()[: cases or None]
    results = []
    for case in all_cases:
        qa = qa_context(case, cfg.banned_terms, acfg.min_words, acfg.max_words)
        try:
            loop = run_draft_loop(
                llm, case.writer_input, qa, acfg, subject_id=case.name, examples=case.examples
            )
        except LLMError as exc:
            typer.echo(f"ERROR {case.name}: {exc}", err=True)
            results.append({"case": case.name, "passed": False, "error": str(exc)})
            continue
        words = loop.report.word_count if loop.report else 0
        typer.echo(
            f"{'PASS' if loop.passed else 'FAIL'} {case.name:<28} attempts={loop.attempts} "
            f"words={words}"
        )
        for i, issues in enumerate(loop.history):
            for issue in issues:
                typer.echo(f"     try{i + 1}: {issue[:120]}")
        results.append(
            {
                "case": case.name,
                "passed": loop.passed,
                "attempts": loop.attempts,
                "words": words,
                "history": loop.history,
                "headline": loop.headline,
                "body": loop.body,
            }
        )
    passed = sum(1 for r in results if r["passed"])
    rate = passed / len(results) if results else 0.0
    out = settings.log_dir / f"eval-writer-{datetime.now(UTC):%Y%m%dT%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=1, ensure_ascii=False), encoding="utf-8")
    typer.echo(f"\npass rate: {passed}/{len(results)} = {rate:.0%} (target >= 90%); details: {out}")
    if rate < 0.9:
        raise typer.Exit(code=1)


@app.command("review-sync")
def review_sync(
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Use an in-memory sheet and roll back DB writes."),
    ] = False,
) -> None:
    """Sync the coordinators' Google Sheet: read decisions, mark stale, push new rows."""
    settings = get_settings()
    configure_logging(settings.log_dir, settings.log_level, console=False)
    cfg = load_app_config(settings.config_dir)
    try:
        engine = make_engine(settings.database_url)
    except DatabaseNotConfiguredError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    cfg = _effective(engine, cfg)

    sheets: SheetsBackend
    if dry_run:
        sheets = MemorySheets()
    else:
        if not (settings.review_spreadsheet_id and settings.google_service_account_file):
            typer.echo(
                "PM_REVIEW_SPREADSHEET_ID and PM_GOOGLE_SERVICE_ACCOUNT_FILE must be set "
                "(see .env.example; run scripts/setup_google_sheets.sh)",
                err=True,
            )
            raise typer.Exit(code=1)
        try:
            sheets = GSpreadBackend(
                settings.review_spreadsheet_id, settings.google_service_account_file
            )
        except Exception as exc:
            typer.echo(f"cannot open the review spreadsheet: {type(exc).__name__}: {exc}", err=True)
            raise typer.Exit(code=1) from exc

    now = datetime.now(UTC)
    with _exclusive(engine), make_session_factory(engine)() as session:
        res = ReviewQueue(session, sheets, cfg, now=now, admin_email=settings.admin_email).sync()
        if dry_run:
            assert isinstance(sheets, MemorySheets)
            for title, grid in sheets.tabs.items():
                typer.echo(f"[tab] {title}: {len(grid) - 1} row(s)")
            session.rollback()
            typer.echo("dry run: in-memory sheet, all DB writes rolled back")
        else:
            session.commit()
    typer.echo(
        f"pushed_articles={res.pushed_articles} pushed_leads={res.pushed_leads} "
        f"approved={len(res.approved)} rejected={len(res.rejected)} "
        f"qa_failed={len(res.qa_failed)} edited_ok={len(res.edited_ok)} "
        f"dismissed_leads={len(res.dismissed_leads)} stale={len(res.stale)} "
        f"unknown_decisions={res.unknown_decisions}"
    )
    if res.unassigned_meters:
        typer.echo(
            f"{len(res.unassigned_meters)} active meter(s) have no coordinator email "
            "(fill the 'coordinators' tab): "
            + ", ".join(res.unassigned_meters[:5])
            + ("…" if len(res.unassigned_meters) > 5 else "")
        )


@app.command()
def publish(
    out: Annotated[
        Path | None, typer.Option(help="Output directory (default: site.out_dir).")
    ] = None,
) -> None:
    """Publish coordinator-approved articles and re-render the static site."""
    settings = get_settings()
    configure_logging(settings.log_dir, settings.log_level, console=False)
    cfg = load_app_config(settings.config_dir)
    try:
        engine = make_engine(settings.database_url)
    except DatabaseNotConfiguredError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    cfg = _effective(engine, cfg)
    now = datetime.now(UTC)
    if not cfg.tunables.publish.enabled:
        typer.echo(
            "shadow mode: publish.enabled is false in config/settings.yaml; nothing published"
        )
        return
    with _exclusive(engine), make_session_factory(engine)() as session:
        ids = publish_approved(session, now)
        res = render_site(session, cfg, now, out)
        session.commit()
    typer.echo(
        f"published {len(ids)} new article(s); rendered {res.pages} page(s) to {res.out_dir}"
    )


@app.command("notify-test")
def notify_test() -> None:
    """Send a test admin alert (a simulated failed run) to PM_ADMIN_EMAIL via SMTP."""
    settings = get_settings()
    if not settings.admin_email:
        typer.echo("PM_ADMIN_EMAIL is not set", err=True)
        raise typer.Exit(code=1)
    mailer = make_mailer(settings)
    if isinstance(mailer, NullMailer):
        typer.echo(
            "SMTP not configured (PM_SMTP_USER / PM_SMTP_PASSWORD); see .env.example", err=True
        )
        raise typer.Exit(code=1)
    report = RunReport(run_id=uuid.uuid4(), started_at=datetime.now(UTC), status="failed")
    report.stage_errors["discover"] = "TEST: injected failure (promise-monitor notify-test)"
    report.urgent.append("TEST alert: if you can read this, admin alerts work.")
    try:
        mailer.send(
            [settings.admin_email],
            f"[promise-monitor] TEST alert {report.started_at:%Y-%m-%d %H:%M}Z",
            "This is a test of promise-monitor admin alerts.\n\n"
            + "\n".join(f" - {k}: {v}" for k, v in report.stage_errors.items())
            + "\n - "
            + report.urgent[0],
        )
    except Exception as exc:
        typer.echo(f"sending failed: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo("sent: check the admin inbox (and spam folder)")


@app.command()
def alert(
    unit: Annotated[str, typer.Option(help="The systemd unit that failed.")] = "promise-monitor",
) -> None:
    """Email the admin that the run CRASHED before it could report (systemd OnFailure)."""
    settings = get_settings()
    lines = [
        f"{unit} exited unexpectedly at {datetime.now(UTC):%Y-%m-%d %H:%M}Z, before it could send",
        "its own report. Nothing was lost: the next run continues from the stored state.",
        "",
        f"Check: journalctl --user -u {unit} -n 100",
    ]
    today = settings.log_dir / f"{datetime.now(UTC):%Y-%m-%d}.jsonl"
    if today.exists():
        errors = [
            ln for ln in today.read_text(encoding="utf-8").splitlines() if '"level": "error"' in ln
        ]
        if errors:
            lines += ["", "Last errors in today's log:", *[e[:400] for e in errors[-10:]]]
    mailer = make_mailer(settings)
    if not settings.admin_email or isinstance(mailer, NullMailer):
        typer.echo("\n".join(lines))
        typer.echo("(email not configured: PM_ADMIN_EMAIL / PM_SMTP_*)", err=True)
        raise typer.Exit(code=1)
    mailer.send([settings.admin_email], f"[promise-monitor] CRASH: {unit}", "\n".join(lines))
    typer.echo("alert sent")


@app.command("shadow-report")
def shadow_report(
    days: Annotated[int, typer.Option(help="Window to report on.")] = 14,
) -> None:
    """Summarise the shadow period and say whether publishing can be switched on (P8 exit)."""
    settings = get_settings()
    try:
        engine = make_engine(settings.database_url)
    except DatabaseNotConfiguredError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    with make_session_factory(engine)() as session:
        rep = build_report(session, datetime.now(UTC), days)
    typer.echo("\n".join(rep.lines))
    if not rep.passed:
        raise typer.Exit(code=1)


@app.command("create-superadmin")
def create_superadmin(
    email: Annotated[str, typer.Option(prompt=True, help="Login email.")],
    name: Annotated[str, typer.Option(prompt=True, help="Display name.")],
) -> None:
    """Create a dashboard superadmin (password asked interactively, never on the command line)."""
    from promise_monitor import access

    settings = get_settings()
    try:
        engine = make_engine(settings.database_url)
    except DatabaseNotConfiguredError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    password = typer.prompt(
        f"Password (at least {access.MIN_PASSWORD_LEN} characters)",
        hide_input=True,
        confirmation_prompt=True,
    )
    with make_session_factory(engine)() as session:
        try:
            user = access.create_user(
                session,
                email=email,
                name=name,
                role="superadmin",
                password=password,
                actor=None,
                now=datetime.now(UTC),
            )
        except access.AccessError as exc:
            typer.echo(f"not created: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        session.commit()
    typer.echo(f"superadmin {user.email} created; sign in at the dashboard")


@app.command()
def dashboard(
    host: Annotated[
        str, typer.Option(help="Bind address. Keep 127.0.0.1 unless behind HTTPS.")
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port.")] = 8800,
) -> None:
    """Serve the admin dashboard (per-meter search context, promises, users and access)."""
    import uvicorn

    from promise_monitor.dashboard.app import create_app

    settings = get_settings()
    configure_logging(settings.log_dir, settings.log_level, console=True)
    cfg = load_app_config(settings.config_dir)
    try:
        engine = make_engine(settings.database_url)
    except DatabaseNotConfiguredError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    local = host in ("127.0.0.1", "localhost", "::1")
    if not local and not (settings.dashboard_https and settings.dashboard_secret):
        typer.echo(
            "refusing to listen on a non-local address without HTTPS: put the dashboard behind an "
            "HTTPS reverse proxy and set PM_DASHBOARD_HTTPS=true and PM_DASHBOARD_SECRET",
            err=True,
        )
        raise typer.Exit(code=1)
    if not settings.dashboard_secret:
        typer.echo(
            "note: PM_DASHBOARD_SECRET is not set; everyone is signed out when it restarts",
            err=True,
        )
    factory = make_session_factory(engine)
    with factory() as session:
        if session.scalars(select(User).limit(1)).first() is None:
            typer.echo(
                "no users yet: run `uv run promise-monitor create-superadmin` first", err=True
            )
            raise typer.Exit(code=1)
    typer.echo(f"dashboard on http://{host}:{port}  (Ctrl-C to stop)")
    uvicorn.run(
        create_app(factory, cfg, settings),
        host=host,
        port=port,
        log_level="warning",
        proxy_headers=settings.dashboard_https,
        server_header=False,
    )


if __name__ == "__main__":
    app()
