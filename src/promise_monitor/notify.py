"""Notifier (PRD §3.8): coordinator digests + admin alerts by email (Gmail SMTP, free).

Email failures are logged and reported but never fail the run. Addresses are never
logged at info level (AGENTS.md rule 8): logs carry counts only.
"""

from __future__ import annotations

import smtplib
import ssl
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Literal, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from promise_monitor import access
from promise_monitor.config import NotifyConfig, Settings
from promise_monitor.db.models import Article, Meter, Run
from promise_monitor.logging_setup import get_logger

log = get_logger(__name__)

HOW_TO_REVIEW = (
    "Open each item, check it, then approve, edit or reject (leads: dismiss). "
    "Nothing is published without approval."
)


# --------------------------------------------------------------------------- run report


@dataclass
class RunReport:
    """Everything a run wants to tell people. Filled stage by stage by the pipeline."""

    run_id: uuid.UUID
    started_at: datetime
    status: Literal["ok", "degraded", "failed"] = "ok"
    stage_errors: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    urgent: list[str] = field(default_factory=list)
    stats: dict[str, object] = field(default_factory=dict)
    # coordinator-facing, by meter id
    new_rows: dict[uuid.UUID, list[str]] = field(default_factory=dict)
    stale: dict[uuid.UUID, list[str]] = field(default_factory=dict)
    submissions: dict[uuid.UUID, list[str]] = field(
        default_factory=dict
    )  # Promise Tracker decisions
    published: list[uuid.UUID] = field(default_factory=list)
    site_dir: str | None = None
    review_sheet_url: str | None = None

    def note(self, warning: str) -> None:
        """Admin should know, but the run itself is fine (e.g. unassigned coordinators)."""
        self.warnings.append(warning)

    def degrade(self, warning: str) -> None:
        self.warnings.append(warning)
        if self.status == "ok":
            self.status = "degraded"

    def fail_stage(self, stage: str, error: str) -> None:
        self.stage_errors[stage] = error
        if self.status == "ok":
            self.status = "degraded"


# --------------------------------------------------------------------------- mailers


class Mailer(Protocol):
    def send(self, to: list[str], subject: str, body: str) -> None: ...


class MemoryMailer:
    def __init__(self) -> None:
        self.sent: list[tuple[list[str], str, str]] = []

    def send(self, to: list[str], subject: str, body: str) -> None:
        self.sent.append((list(to), subject, body))


class NullMailer:
    """Email not configured: record what would have been sent, send nothing."""

    def __init__(self) -> None:
        self.skipped = 0

    def send(self, to: list[str], subject: str, body: str) -> None:
        self.skipped += 1
        log.warning("email_not_configured", subject=subject)


class SMTPMailer:
    """Any SMTP server: Gmail (587), Mailtrap sandbox (2525/587, or 465 for implicit TLS).
    TLS is always required; plain-text SMTP is never used."""

    def __init__(self, host: str, port: int, user: str, password: str, sender: str) -> None:
        self.host, self.port, self.user, self.password, self.sender = (
            host,
            port,
            user,
            password,
            sender,
        )

    def send(self, to: list[str], subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["From"] = self.sender
        msg["To"] = ", ".join(to)
        msg["Subject"] = subject
        msg.set_content(body)
        ctx = ssl.create_default_context()
        if self.port == 465:  # implicit TLS (SMTPS)
            with smtplib.SMTP_SSL(self.host, self.port, timeout=30, context=ctx) as smtp:
                smtp.login(self.user, self.password)
                smtp.send_message(msg)
            return
        with smtplib.SMTP(self.host, self.port, timeout=30) as smtp:  # 587 / 2525 / 25: STARTTLS
            smtp.starttls(context=ctx)
            smtp.login(self.user, self.password)
            smtp.send_message(msg)


def make_mailer(settings: Settings) -> Mailer:
    if settings.smtp_user and settings.smtp_password:
        return SMTPMailer(
            settings.smtp_host,
            settings.smtp_port,
            settings.smtp_user,
            settings.smtp_password,
            settings.smtp_from
            or (settings.smtp_user if "@" in settings.smtp_user else settings.admin_email or ""),
        )
    return NullMailer()


# --------------------------------------------------------------------------- heartbeat


def heartbeat_issues(
    session: Session, cfg: NotifyConfig, now: datetime, current_run: uuid.UUID
) -> list[str]:
    """Urgent problems visible from run history (PRD risk R-T6)."""
    issues = []
    recent = list(
        session.scalars(
            select(Run)
            .where(
                Run.trigger.like("cli:run%"),
                Run.run_id != current_run,
                Run.finished_at.is_not(None),
            )
            .order_by(Run.started_at.desc())
            .limit(cfg.consecutive_failures_urgent)
        )
    )
    if len(recent) == cfg.consecutive_failures_urgent and all(r.status == "failed" for r in recent):
        issues.append(f"the last {len(recent)} daily runs FAILED")
    last_ok = session.scalars(
        select(Run.started_at)
        .where(
            Run.trigger.like("cli:run%"),
            Run.status.in_(("ok", "degraded")),
            Run.run_id != current_run,
        )
        .order_by(Run.started_at.desc())
        .limit(1)
    ).first()
    any_prior = session.scalars(
        select(Run.run_id).where(Run.trigger.like("cli:run%"), Run.run_id != current_run).limit(1)
    ).first()
    if any_prior is not None:
        if last_ok is None:
            issues.append("no daily run has succeeded yet")
        else:
            aware = last_ok if last_ok.tzinfo else last_ok.replace(tzinfo=now.tzinfo)
            if now - aware > timedelta(hours=cfg.missed_run_hours):
                hours = int((now - aware).total_seconds() // 3600)
                issues.append(f"the last successful daily run was {hours} hours ago (cron missed?)")
    return issues


# --------------------------------------------------------------------------- notifier


@dataclass
class NotifyResult:
    digests: int = 0
    admin_alerts: int = 0
    failures: int = 0


class Notifier:
    def __init__(self, session: Session, mailer: Mailer, *, admin_email: str | None) -> None:
        self.s = session
        self.mailer = mailer
        self.admin = admin_email
        self.result = NotifyResult()

    def _send(self, to: list[str], subject: str, body: str) -> bool:
        try:
            self.mailer.send(to, subject, body)
            return True
        except Exception as exc:  # never fail the run because of email
            self.result.failures += 1
            log.error("email_failed", subject=subject, error=type(exc).__name__)
            return False

    def notify(self, report: RunReport) -> NotifyResult:
        self.coordinator_digests(report)
        self.admin_alert(report)
        log.info(
            "notified",
            digests=self.result.digests,
            admin_alerts=self.result.admin_alerts,
            failures=self.result.failures,
        )
        return self.result

    def coordinator_digests(self, report: RunReport) -> None:
        meter_ids = set(report.new_rows) | set(report.stale) | set(report.submissions)
        if not meter_ids:
            return
        by_recipient: dict[str, list[Meter]] = {}
        for meter in self.s.scalars(select(Meter).where(Meter.id.in_(meter_ids))):
            # dashboard assignments (primary + active temporary cover) first, then the
            # Sheet's coordinators tab, then the admin
            recipients = [
                u.email for u in access.coordinators_for_meter(self.s, meter.id, report.started_at)
            ]
            if not recipients:
                fallback = meter.coordinator_email or self.admin
                recipients = [fallback] if fallback else []
            for to in recipients:
                by_recipient.setdefault(to, []).append(meter)
        day = report.started_at.date().isoformat()
        for to, meters in by_recipient.items():
            lines = ["Promise Tracker Monitor: items waiting for your review.", ""]
            for meter in sorted(meters, key=lambda m: (m.priority_rank, m.name)):
                new, stale = report.new_rows.get(meter.id, []), report.stale.get(meter.id, [])
                open_count = (
                    self.s.query(Article)
                    .filter(
                        Article.meter_id == meter.id,
                        Article.state.in_(("in_review", "needs_attention", "stale")),
                    )
                    .count()
                )
                lines.append(f"## {meter.name}  ({open_count} open)")
                if to == self.admin and meter.coordinator_email is None:
                    lines.append("   (no coordinator assigned: sent to the admin)")
                lines += [f"   NEW    {x}" for x in new]
                lines += [f"   STALE  {x}" for x in stale]
                lines += [f"   SENT   {x}" for x in report.submissions.get(meter.id, [])]
                lines.append("")
            if report.review_sheet_url:
                lines.append(f"Review: {report.review_sheet_url}")
            lines.append(HOW_TO_REVIEW)
            total = sum(
                len(report.new_rows.get(m.id, []))
                + len(report.stale.get(m.id, []))
                + len(report.submissions.get(m.id, []))
                for m in meters
            )
            if self._send(
                [to], f"[Promise Tracker] {total} item(s) to review: {day}", "\n".join(lines)
            ):
                self.result.digests += 1

    def admin_alert(self, report: RunReport) -> None:
        if not self.admin:
            return
        if report.status == "ok" and not report.warnings and not report.urgent:
            return
        level = "URGENT" if report.urgent else report.status.upper()
        day = report.started_at.date().isoformat()
        lines = [f"Run {report.run_id} ({day}) finished: {report.status.upper()}.", ""]
        if report.urgent:
            lines += ["URGENT:", *[f" - {u}" for u in report.urgent], ""]
        if report.stage_errors:
            lines += [
                "Stage errors:",
                *[f" - {k}: {v}" for k, v in report.stage_errors.items()],
                "",
            ]
        if report.warnings:
            lines += ["Warnings:", *[f" - {w}" for w in report.warnings], ""]
        if report.stats:
            lines += ["Stats:", *[f" - {k}: {v}" for k, v in report.stats.items()], ""]
        lines.append("Logs: logs/<date>.jsonl (filter by run_id); DB table runs.")
        if self._send([self.admin], f"[promise-monitor] {level} run {day}", "\n".join(lines)):
            self.result.admin_alerts += 1
