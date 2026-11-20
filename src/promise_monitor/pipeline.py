"""The daily run (PRD §5.2): one function, stage by stage, each stage isolated.

Order:
  heartbeat → review_pull → submissions_poll → publish → discover → monitor (+classify)
  → news (+classify)
  → verify → write → review_push → notify
A stage that raises is recorded on the RunReport and the run continues where it is safe
(e.g. if the tracker API is down, monitoring is skipped but publishing, review sync and
notifications still happen). Every stage commits its own work.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from promise_monitor.agents.classifier import classify
from promise_monitor.agents.discovery import SiteDownError, apply_discovery, collect
from promise_monitor.agents.monitor import monitor_meter
from promise_monitor.agents.news_scout import run_news_scout
from promise_monitor.agents.publisher import publish_approved, render_site
from promise_monitor.agents.review_queue import ReviewQueue, SyncResult
from promise_monitor.agents.verifier import Fetcher, Verifier
from promise_monitor.agents.writer import Drafter
from promise_monitor.api.client import PromiseTrackerClient, RateLimitedError
from promise_monitor.config import AppConfig
from promise_monitor.db.models import Article, Meter, Run
from promise_monitor.llm.base import LLMClient
from promise_monitor.llm.recording import RecordingLLMClient
from promise_monitor.logging_setup import get_logger, prune_logs
from promise_monitor.notify import Mailer, Notifier, RunReport, heartbeat_issues
from promise_monitor.review import announce_new, mark_stale
from promise_monitor.sheets.backend import SheetsBackend
from promise_monitor.submissions import ClientFactory, TokenBox, poll

log = get_logger(__name__)


@dataclass
class PipelineDeps:
    api: PromiseTrackerClient
    fetcher: Fetcher
    llm: LLMClient
    sheets: SheetsBackend | None  # None = Sheets not configured (review stages skipped)
    mailer: Mailer
    admin_email: str | None
    site_dir: Path | None = None
    review_sheet_url: str | None = None
    log_dir: Path | None = None
    token_box: TokenBox | None = None  # PM_TOKEN_KEY (P12 submissions)
    submission_client: ClientFactory | None = None  # token → SubmissionClient


class DailyRun:
    def __init__(
        self,
        session: Session,
        cfg: AppConfig,
        deps: PipelineDeps,
        *,
        now: datetime | None = None,
        force_full: bool = False,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.s = session
        self.cfg = cfg
        self.deps = deps
        self.now = now or clock()
        self.clock = clock
        self.force_full = force_full
        run = Run(started_at=self.now, trigger="cli:run")
        session.add(run)
        session.commit()
        self.run_row = run
        review_url = (
            cfg.tunables.review.dashboard_url
            if cfg.tunables.review.channel == "dashboard"
            else deps.review_sheet_url
        )
        self.report = RunReport(run_id=run.run_id, started_at=self.now, review_sheet_url=review_url)
        self.llm = RecordingLLMClient(deps.llm, session, run.run_id)
        self._api_down = False

    # ------------------------------------------------------------------ helpers

    def _stage(self, name: str, fn: Callable[[], None]) -> None:
        log.info("stage_start", stage=name)
        try:
            fn()
            self.s.commit()
        except Exception as exc:
            self.s.rollback()
            self.report.fail_stage(name, f"{type(exc).__name__}: {str(exc)[:300]}")
            log.exception("stage_failed", stage=name)

    def _absorb_sync(self, res: SyncResult, push: bool) -> None:
        for meter_id, rows in res.pushed_by_meter.items():
            self.report.new_rows.setdefault(meter_id, []).extend(rows)
        for meter_id, rows in res.stale_by_meter.items():
            self.report.stale.setdefault(meter_id, []).extend(rows)
        if push and res.unassigned_meters:
            self.report.note(
                f"{len(res.unassigned_meters)} active meter(s) have no coordinator email "
                "(fill the 'coordinators' tab)"
            )
        if res.unknown_decisions:
            self.report.note(f"{res.unknown_decisions} unrecognised decision value(s) in the Sheet")

    def _llm_auth_check(self) -> None:
        if self.llm.auth_failed:
            msg = "Claude login failed: run `claude` interactively and /login on the server"
            if msg not in self.report.urgent:
                self.report.urgent.append(msg)

    # ------------------------------------------------------------------ stages

    def housekeeping(self) -> None:
        removed = prune_logs(self.deps.log_dir, self.cfg.tunables.logs.retention_days, self.now)
        if removed:
            self.report.stats["logs_pruned"] = removed

    def heartbeat(self) -> None:
        self.report.urgent.extend(
            heartbeat_issues(self.s, self.cfg.tunables.notify, self.now, self.run_row.run_id)
        )

    def review(self, push: bool) -> None:
        if self.cfg.tunables.review.channel == "dashboard":
            if push:  # dashboard review: no sync needed; mark stale + announce new for digests
                for meter_id, rows in mark_stale(self.s, self.cfg, self.clock()).items():
                    self.report.stale.setdefault(meter_id, []).extend(rows)
                for meter_id, rows in announce_new(self.s, self.clock()).items():
                    self.report.new_rows.setdefault(meter_id, []).extend(rows)
                self.report.stats["review"] = {
                    "new": sum(len(v) for v in self.report.new_rows.values()),
                    "stale": sum(len(v) for v in self.report.stale.values()),
                }
            return
        if self.deps.sheets is None:
            if push:
                self.report.note(
                    "review sheet not configured: drafts were not sent to coordinators"
                )
            return
        res = ReviewQueue(
            self.s, self.deps.sheets, self.cfg, now=self.clock(), admin_email=self.deps.admin_email
        ).sync()
        self._absorb_sync(res, push)
        self.report.stats[f"review_{'push' if push else 'pull'}"] = {
            "pushed": res.pushed_articles + res.pushed_leads,
            "approved": len(res.approved),
            "rejected": len(res.rejected),
            "stale": len(res.stale),
        }

    def submissions_poll(self) -> None:
        """What Promise Tracker decided about the articles coordinators sent (PRD §8d)."""
        if not self.cfg.tunables.submissions.enabled or self.deps.submission_client is None:
            return
        res = poll(
            self.s,
            self.cfg,
            box=self.deps.token_box,
            client_for=self.deps.submission_client,
            now=self.clock(),
        )
        for meter_id, lines in res.changed.items():
            self.report.submissions.setdefault(meter_id, []).extend(lines)
        for err in res.errors[:5]:
            self.report.note(err)
        if len(res.errors) > 5:
            self.report.note(f"... and {len(res.errors) - 5} more submission status error(s)")
        self.report.stats["submissions"] = {
            "checked": res.checked,
            "changed": sum(len(v) for v in res.changed.values()),
            "errors": len(res.errors),
        }

    def publish(self) -> None:
        if not self.cfg.tunables.publish.enabled:
            held = self.s.scalars(
                select(Article.article_id).where(Article.state == "approved")
            ).all()
            self.report.note(
                f"shadow mode: publishing is off; {len(held)} approved article(s) held"
            )
            self.report.stats["published"] = 0
            self.report.stats["held_approved"] = len(held)
            return
        ids = publish_approved(self.s, self.clock())
        out = render_site(self.s, self.cfg, self.clock(), self.deps.site_dir)
        self.report.published.extend(ids)
        self.report.site_dir = str(out.out_dir)
        self.report.stats["published"] = len(ids)

    def discover(self) -> None:
        collected = collect(self.deps.api, self.cfg.states)
        try:
            res = apply_discovery(self.s, collected, self.cfg, self.clock())
        except SiteDownError as exc:
            self._api_down = True
            self.report.status = "failed"
            self.report.stage_errors["discover"] = f"promisetracker.ng API unreachable: {exc}"
            return
        for source, err in res.failed_sources.items():
            self.report.degrade(f"discovery source {source} failed: {err[:160]}")
        names = {m.id: m.name for m in self.s.scalars(select(Meter))}
        for mid in res.missing:
            self.report.degrade(f"meter disappeared from the site: {names.get(mid, mid)}")
        for mid in res.reappeared:
            self.report.note(f"meter reappeared: {names.get(mid, mid)}")
        for mid in res.new:
            self.report.note(
                f"new meter discovered (baseline only, no articles yet): {names.get(mid, mid)}"
            )
        self.report.stats["meters_active"] = len(res.active)

    def monitor(self) -> None:
        if self._api_down:
            return
        meters = list(
            self.s.scalars(
                select(Meter)
                .where(Meter.registry_status == "active")
                .order_by(Meter.priority_rank, Meter.name)
            )
        )
        totals = {"diffs": 0, "events": 0, "queued_groups": 0}
        for m in meters:
            try:
                with self.s.begin_nested():
                    res = monitor_meter(
                        self.s,
                        self.deps.api,
                        m,
                        self.run_row.run_id,
                        self.clock(),
                        self.cfg.tunables.monitor,
                        force_full=self.force_full,
                    )
                    cls = classify(
                        self.s,
                        run_id=self.run_row.run_id,
                        meter=m,
                        diffs=res.diffs,
                        removed=res.removed,
                        news=[],
                        cfg=self.cfg.tunables.classifier,
                        llm=self.llm,
                        now=self.clock(),
                    )
            except RateLimitedError as exc:
                self.report.degrade(
                    f"rate limited by the tracker API; monitoring stopped early: {exc}"
                )
                break
            except Exception as exc:
                self.report.degrade(
                    f"monitor crashed on {m.name}: {type(exc).__name__}: {str(exc)[:160]}"
                )
                log.exception("meter_crashed", meter_id=str(m.id))
                continue
            self.s.commit()
            if res.status == "skipped":
                self.report.degrade(f"{m.name}: skipped ({'; '.join(res.errors)[:200]})")
            elif res.status == "anomaly":
                ratio = f"{res.change_ratio:.0%}" if res.change_ratio is not None else "?"
                self.report.degrade(
                    f"{m.name}: anomaly guard tripped ({ratio} of promises changed); "
                    "nothing stored. Check the site, then re-run."
                )
            elif res.errors:
                self.report.degrade(f"{m.name}: {len(res.errors)} promise detail fetch(es) failed")
            if res.removed:
                self.report.note(f"{m.name}: {len(res.removed)} promise(s) no longer listed")
            totals["diffs"] += len(res.diffs)
            totals["events"] += len(cls.events)
            totals["queued_groups"] += len(cls.groups)
        self._llm_auth_check()
        self.report.stats["monitor"] = totals

    def news(self) -> None:
        if not self.cfg.tunables.news.enabled:
            return
        scout = run_news_scout(
            self.s,
            self.llm,
            run_id=self.run_row.run_id,
            now=self.clock(),
            cfg=self.cfg.tunables.news,
            sources=self.cfg.news_sources,
        )
        groups = 0
        for meter_id, findings in scout.findings.items():
            meter = self.s.get(Meter, meter_id)
            assert meter is not None
            cls = classify(
                self.s,
                run_id=self.run_row.run_id,
                meter=meter,
                diffs=[],
                removed=[],
                news=findings,
                cfg=self.cfg.tunables.classifier,
                llm=self.llm,
                now=self.clock(),
            )
            groups += len(cls.groups)
        if scout.failed_calls:
            self.report.degrade(
                f"news scout: {scout.failed_calls} of {scout.calls} search call(s) failed"
            )
        self._llm_auth_check()
        self.report.stats["news"] = {
            "searched": scout.searched,
            "accepted": scout.accepted,
            "queued_groups": groups,
        }

    def verify(self) -> None:
        res = Verifier(
            self.s,
            self.deps.fetcher,
            self.llm,
            cfg=self.cfg.tunables.verifier,
            public_promise_url=self.cfg.tunables.api.public_promise_url,
            now=self.clock(),
        ).run()
        if res.deferred:
            self.report.degrade(f"verifier: {res.deferred} group(s) deferred (LLM unavailable)")
        self._llm_auth_check()
        self.report.stats["verify"] = {"verified": res.verified, "needs_sources": res.needs_sources}

    def write(self) -> None:
        res = Drafter(
            self.s,
            self.llm,
            cfg=self.cfg.tunables.article,
            banned_terms=self.cfg.banned_terms,
            public_promise_url=self.cfg.tunables.api.public_promise_url,
            now=self.clock(),
        ).run()
        if res.deferred:
            self.report.degrade(f"writer: {res.deferred} group(s) deferred (LLM unavailable)")
        self._llm_auth_check()
        self.report.stats["write"] = {
            "in_review": res.in_review,
            "needs_attention": res.needs_attention,
        }

    def notify(self) -> None:
        res = Notifier(self.s, self.deps.mailer, admin_email=self.deps.admin_email).notify(
            self.report
        )
        self.report.stats["email"] = {
            "digests": res.digests,
            "admin_alerts": res.admin_alerts,
            "failures": res.failures,
        }

    # ------------------------------------------------------------------ run

    def run(self) -> RunReport:
        structlog.contextvars.bind_contextvars(run_id=str(self.run_row.run_id))
        try:
            self._stage("housekeeping", self.housekeeping)
            self._stage("heartbeat", self.heartbeat)
            self._stage("review_pull", lambda: self.review(push=False))
            self._stage("submissions_poll", self.submissions_poll)
            self._stage("publish", self.publish)
            self._stage("discover", self.discover)
            self._stage("monitor", self.monitor)
            self._stage("news", self.news)
            self._stage("verify", self.verify)
            self._stage("write", self.write)
            self._stage("review_push", lambda: self.review(push=True))
            self._stage("notify", self.notify)
            self.run_row.status = self.report.status
            self.run_row.finished_at = self.clock()
            self.run_row.stats = _jsonable(
                {
                    **self.report.stats,
                    "stage_errors": self.report.stage_errors,
                    "warnings": self.report.warnings,
                }
            )
            self.s.commit()
        finally:
            structlog.contextvars.clear_contextvars()
        return self.report


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, uuid.UUID | datetime | Path):
        return str(value)
    return value
