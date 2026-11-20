"""Coordinator review loop in Google Sheets (PRD §3.7 steps 1-4, §4.6). Nothing auto-publishes.

One sync, in order:
1. Coordinators tab: every active meter gets a row (unassigned ones listed for the admin);
   ``meters.coordinator_email``/``coordinator_name`` mirror the tab.
2. For every meter tab, read coordinator decisions (rows matched by ``article_id``,
   never by row number, so sorting/filtering is safe):
   - ``approve``: re-run deterministic QA on the Sheet's headline/body (coordinators may
     edit both). Pass → ``approved`` (P7 publishes it). Fail → issues in ``qa_result``,
     decision cleared, article stays in review;
   - ``edit``: re-check edited text only; result in ``qa_result``;
   - ``reject``: ``rejected`` — the change is consumed and never redrafted;
   - lead rows (``needs_sources`` groups): ``dismiss``/``reject`` close the lead.
3. Undecided articles older than ``stale_after_days`` → ``stale`` (reminder in P7). A stale
   article can still be approved or rejected later.
4. Append new rows: drafted articles (``in_review``/``needs_attention``) and new leads.
The pipeline writes only ``state``, ``qa_result``, ``word_count`` and clears ``decision``;
coordinators write ``headline``, ``body_markdown``, ``decision``, ``decision_note``.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from promise_monitor import access
from promise_monitor.agents.writer import qa_context_from_input
from promise_monitor.config import AppConfig
from promise_monitor.db.models import (
    Article,
    ArticleGroup,
    ArticleGroupSource,
    ChangeEvent,
    Meter,
    NewsItem,
    PromiseSnapshot,
    Source,
)
from promise_monitor.domain.qa import check_article
from promise_monitor.logging_setup import get_logger
from promise_monitor.sheets.backend import Cell, SheetsBackend

log = get_logger(__name__)

COORD_HEADER = ["meter_id", "meter_name", "coordinator_name", "email", "active"]
HEADER = [
    "article_id",
    "kind",
    "created_at",
    "promise_title",
    "change_summary",
    "score",
    "evidence_lead",
    "headline",
    "body_markdown",
    "word_count",
    "sources",
    "qa_flags",
    "decision",
    "decision_note",
    "qa_result",
    "state",
]
_BAD_TITLE_CHARS = re.compile(r"[\[\]:*?/\\']")
OPEN_STATES = ("in_review", "needs_attention", "stale")


def tab_title(meter: Meter) -> str:
    name = _BAD_TITLE_CHARS.sub("", meter.name).strip()[:70] or "meter"
    return f"{name} · {str(meter.id)[:8]}"


def _find_tab(titles: list[str], meter: Meter) -> str | None:
    suffix = str(meter.id)[:8]
    return next((t for t in titles if t.endswith(suffix)), None)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


@dataclass
class SyncResult:
    pushed_articles: int = 0
    pushed_leads: int = 0
    approved: list[uuid.UUID] = field(default_factory=list)
    rejected: list[uuid.UUID] = field(default_factory=list)
    qa_failed: list[uuid.UUID] = field(default_factory=list)
    edited_ok: list[uuid.UUID] = field(default_factory=list)
    dismissed_leads: list[uuid.UUID] = field(default_factory=list)
    stale: list[uuid.UUID] = field(default_factory=list)
    unassigned_meters: list[str] = field(default_factory=list)
    unknown_decisions: int = 0
    # one-line descriptions for the coordinator digest, by meter id
    pushed_by_meter: dict[uuid.UUID, list[str]] = field(default_factory=dict)
    stale_by_meter: dict[uuid.UUID, list[str]] = field(default_factory=dict)


class ReviewQueue:
    def __init__(
        self,
        session: Session,
        sheets: SheetsBackend,
        cfg: AppConfig,
        *,
        now: datetime,
        admin_email: str | None,
    ) -> None:
        self.s = session
        self.sheets = sheets
        self.cfg = cfg
        self.now = now
        self.admin = admin_email
        self.result = SyncResult()

    # ------------------------------------------------------------------ entry point

    def sync(self) -> SyncResult:
        self.sync_coordinators()
        meters = list(self.s.scalars(select(Meter).order_by(Meter.priority_rank, Meter.name)))
        titles = self.sheets.tab_titles()
        for meter in meters:
            tab = _find_tab(titles, meter)
            if tab is not None:
                self.pull(meter, tab)
        self.push(meters)
        self.s.flush()
        log.info(
            "review_synced",
            pushed=self.result.pushed_articles,
            leads=self.result.pushed_leads,
            approved=len(self.result.approved),
            rejected=len(self.result.rejected),
            stale=len(self.result.stale),
        )
        return self.result

    # ------------------------------------------------------------------ coordinators

    def sync_coordinators(self) -> None:
        tab = self.cfg.tunables.review.coordinators_tab
        if tab not in self.sheets.tab_titles():
            self.sheets.create_tab(tab, COORD_HEADER)
        rows = self.sheets.read(tab)
        col = {h: i for i, h in enumerate(rows[0])} if rows else {}
        by_id: dict[str, dict[str, str]] = {}
        for r in rows[1:]:
            rec = {h: (r[i].strip() if i < len(r) else "") for h, i in col.items()}
            if rec.get("meter_id"):
                by_id[rec["meter_id"]] = rec

        active = list(self.s.scalars(select(Meter).where(Meter.registry_status == "active")))
        missing = [m for m in active if str(m.id) not in by_id]
        if missing:
            self.sheets.append(tab, [[str(m.id), m.name, "", "", "yes"] for m in missing])
        row_of = {r[0].strip(): i for i, r in enumerate(rows[1:], start=2) if r and r[0].strip()}
        mirror: list[tuple[int, int, str]] = []
        for m in self.s.scalars(select(Meter)):
            primary = access.primary_for_meter(self.s, m.id, self.now)
            covering = [
                u for u in access.coordinators_for_meter(self.s, m.id, self.now) if u is not primary
            ]
            if primary is not None or covering:
                # Dashboard is the source of truth for this meter; the tab mirrors it.
                lead = primary or covering[0]
                m.coordinator_email, m.coordinator_name = lead.email, lead.name
                shown = lead.name + (
                    f" (+ temporary: {', '.join(u.name for u in covering)})"
                    if primary and covering
                    else ""
                )
                row_no = row_of.get(str(m.id))
                if row_no is not None and col:
                    mirror.append((row_no, col["coordinator_name"] + 1, shown))
                    mirror.append((row_no, col["email"] + 1, lead.email))
                continue
            rec = by_id.get(str(m.id), {})
            on = rec.get("active", "yes").lower() not in ("no", "false", "0", "n")
            email = rec.get("email") or None
            m.coordinator_email = email if on and email else None
            m.coordinator_name = (rec.get("coordinator_name") or None) if on else None
            if m.registry_status == "active" and m.coordinator_email is None:
                self.result.unassigned_meters.append(m.name)
        if mirror:
            self.sheets.write_cells(tab, mirror)

    # ------------------------------------------------------------------ pull decisions

    def pull(self, meter: Meter, tab: str) -> None:
        rows = self.sheets.read(tab)
        if not rows:
            return
        col = {h: i for i, h in enumerate(rows[0])}
        missing_cols = [h for h in HEADER if h not in col]
        if missing_cols:
            log.error("review_tab_columns_missing", tab=tab, missing=missing_cols)
            return
        cells: list[Cell] = []

        def get(r: list[str], h: str) -> str:
            i = col[h]
            return r[i] if i < len(r) else ""

        def put(row_no: int, h: str, value: str) -> None:
            cells.append((row_no, col[h] + 1, value))

        for idx, r in enumerate(rows[1:], start=2):
            raw_id = get(r, "article_id").strip()
            try:
                rid = uuid.UUID(raw_id)
            except ValueError:
                continue
            decision = get(r, "decision").strip().lower()
            note = get(r, "decision_note").strip() or None
            if get(r, "kind").strip() == "lead":
                self._pull_lead(rid, decision, note, idx, put)
                continue
            art = self.s.get(Article, rid)
            if art is None or art.meter_id != meter.id:
                continue
            if art.state not in OPEN_STATES:
                if get(r, "state") != art.state:
                    put(idx, "state", art.state)
                if decision:
                    put(idx, "decision", "")
                continue
            if decision:
                self._apply_decision(
                    art,
                    meter,
                    decision,
                    note,
                    get(r, "headline"),
                    get(r, "body_markdown"),
                    idx,
                    put,
                )
            elif art.state != "stale" and self._is_stale(art):
                art.state = "stale"
                art.stale_at = self.now
                self._set_events(art, "stale")
                self.result.stale.append(art.article_id)
                self.result.stale_by_meter.setdefault(meter.id, []).append(art.headline)
                put(idx, "state", "stale")
                put(
                    idx,
                    "qa_result",
                    f"stale: no decision after {self.cfg.tunables.review.stale_after_days} days",
                )
        if cells:
            self.sheets.write_cells(tab, cells)

    def _is_stale(self, art: Article) -> bool:
        days = self.cfg.tunables.review.stale_after_days
        return _aware(art.created_at) <= self.now - timedelta(days=days)

    def _apply_decision(
        self,
        art: Article,
        meter: Meter,
        decision: str,
        note: str | None,
        headline: str,
        body: str,
        row: int,
        put: Any,
    ) -> None:
        put(row, "decision", "")  # always consume the decision
        if decision == "reject":
            art.state = "rejected"
            art.decision_note = note
            self._set_events(art, "rejected")
            group = self.s.get(ArticleGroup, art.article_group_id)
            if group is not None:
                group.state = "rejected"
            self.result.rejected.append(art.article_id)
            put(row, "state", "rejected")
            put(row, "qa_result", "rejected: this change will not be reported")
            return
        if decision not in ("approve", "edit"):
            self.result.unknown_decisions += 1
            put(row, "qa_result", f"unknown decision '{decision}': use approve, edit or reject")
            return

        headline, body = headline.strip(), body.strip()
        recent = tuple(
            self.s.scalars(
                select(Article.body_markdown).where(
                    Article.promise_id == art.promise_id,
                    Article.article_id != art.article_id,
                    Article.state.in_(("approved", "published")),
                    Article.created_at
                    >= self.now - timedelta(days=self.cfg.tunables.article.dedupe_window_days),
                )
            )
        )
        qa = qa_context_from_input(
            art.writer_input, self.cfg.tunables.article, self.cfg.banned_terms, recent
        )
        rep = check_article(headline, body, qa)
        if (headline, body) != (art.headline, art.body_markdown):
            art.headline, art.body_markdown, art.word_count = headline, body, rep.word_count
            put(row, "word_count", str(rep.word_count))
        if not rep.passed:
            art.state = "needs_attention"
            art.qa_flags = rep.issues
            self.result.qa_failed.append(art.article_id)
            put(row, "state", art.state)
            put(
                row,
                "qa_result",
                "QA failed (fix and set decision again): " + " | ".join(rep.issues),
            )
            return
        art.qa_flags = []
        if decision == "edit":
            art.state = "in_review"
            self.result.edited_ok.append(art.article_id)
            put(row, "state", art.state)
            put(row, "qa_result", "OK: edited text passes all checks; set decision to approve")
            return
        art.state = "approved"
        art.decision_note = note
        art.approved_by = meter.coordinator_email or (
            f"admin:{self.admin}" if self.admin else "unassigned"
        )
        art.approved_at = self.now
        self._set_events(art, "approved")
        self.result.approved.append(art.article_id)
        put(row, "state", "approved")
        put(
            row,
            "qa_result",
            "OK: approved; will be published on the next run"
            if self.cfg.tunables.publish.enabled
            else "OK: approved (shadow mode: held, not published yet)",
        )

    def _pull_lead(
        self, gid: uuid.UUID, decision: str, note: str | None, row: int, put: Any
    ) -> None:
        group = self.s.get(ArticleGroup, gid)
        if group is None or not decision:
            return
        put(row, "decision", "")
        if group.state != "needs_sources":
            put(row, "state", group.state)
            return
        if decision in ("dismiss", "reject"):
            group.state = "rejected"
            for ev in self.s.scalars(
                select(ChangeEvent).where(ChangeEvent.article_group_id == gid)
            ):
                ev.state = "rejected"
            self.result.dismissed_leads.append(gid)
            put(row, "state", "dismissed")
            put(row, "qa_result", f"dismissed{': ' + note if note else ''}")
        else:
            self.result.unknown_decisions += 1
            put(row, "qa_result", f"unknown decision '{decision}': leads accept dismiss")

    def _set_events(self, art: Article, state: str) -> None:
        for ev in self.s.scalars(
            select(ChangeEvent).where(
                ChangeEvent.event_id.in_([uuid.UUID(e) for e in art.event_ids])
            )
        ):
            ev.state = state

    # ------------------------------------------------------------------ push new rows

    def push(self, meters: list[Meter]) -> None:
        by_id = {m.id: m for m in meters}
        new_articles = list(
            self.s.scalars(
                select(Article)
                .where(
                    Article.pushed_at.is_(None), Article.state.in_(("in_review", "needs_attention"))
                )
                .order_by(Article.created_at)
            )
        )
        new_leads = list(
            self.s.scalars(
                select(ArticleGroup)
                .where(ArticleGroup.pushed_at.is_(None), ArticleGroup.state == "needs_sources")
                .order_by(ArticleGroup.created_at)
            )
        )
        rows_by_meter: dict[uuid.UUID, list[list[str]]] = {}
        for art in new_articles:
            rows_by_meter.setdefault(art.meter_id, []).append(self._article_row(art))
            label = "draft" if art.state == "in_review" else "draft needing attention"
            self.result.pushed_by_meter.setdefault(art.meter_id, []).append(
                f"{label}: {art.headline}"
            )
            art.pushed_at = self.now
            group = self.s.get(ArticleGroup, art.article_group_id)
            if group is not None and group.state == "drafted":
                group.state = "in_review"
            self._set_events(art, "in_review")
            self.result.pushed_articles += 1
        for group in new_leads:
            row = self._lead_row(group)
            rows_by_meter.setdefault(group.meter_id, []).append(row)
            self.result.pushed_by_meter.setdefault(group.meter_id, []).append(
                f"evidence lead (sources to check): {row[3][:90]}"
            )
            group.pushed_at = self.now
            self.result.pushed_leads += 1

        titles = self.sheets.tab_titles()
        for meter_id, rows in rows_by_meter.items():
            meter = by_id[meter_id]
            tab = _find_tab(titles, meter)
            if tab is None:
                tab = tab_title(meter)
                self.sheets.create_tab(tab, HEADER)
                titles.append(tab)
            self.sheets.append(tab, rows)

    def _article_row(self, art: Article) -> list[str]:
        wi = art.writer_input or {}
        changes = wi.get("changes") or []
        summary = "; ".join(_change_line(c) for c in changes)
        sources = "\n".join(
            f"[{s.get('verdict')}] {s.get('title') or ''} — {s.get('url')}"
            for s in wi.get("sources") or []
        )
        score = max(
            (
                e.score
                for e in self.s.scalars(
                    select(ChangeEvent).where(
                        ChangeEvent.event_id.in_([uuid.UUID(x) for x in art.event_ids])
                    )
                )
            ),
            default=0,
        )
        lead = any(c.get("type") == "news_update" for c in changes)
        return [
            str(art.article_id),
            "article",
            art.created_at.date().isoformat(),
            str((wi.get("promise") or {}).get("title") or ""),
            summary,
            str(score),
            "yes" if lead else "",
            art.headline,
            art.body_markdown,
            str(art.word_count),
            sources,
            "\n".join(art.qa_flags or []),
            "",
            "",
            "",
            art.state,
        ]

    def _lead_row(self, group: ArticleGroup) -> list[str]:
        events = list(
            self.s.scalars(
                select(ChangeEvent).where(ChangeEvent.article_group_id == group.group_id)
            )
        )
        news = {
            n.id: n
            for n in self.s.scalars(
                select(NewsItem).where(
                    NewsItem.id.in_([e.news_item_id for e in events if e.news_item_id])
                )
            )
        }
        lines = []
        for e in events:
            n = news.get(e.news_item_id or -1)
            lines.append(
                f"news: {n.outlet}, {n.published_date}: {n.headline}" if n else e.change_type
            )
        content = (
            self.s.scalars(
                select(PromiseSnapshot.content)
                .where(PromiseSnapshot.promise_id == group.promise_id)
                .order_by(PromiseSnapshot.snapshot_id.desc())
                .limit(1)
            ).first()
            or {}
        )
        srcs = self.s.execute(
            select(ArticleGroupSource.verdict, Source.url)
            .join(Source, Source.source_id == ArticleGroupSource.source_id)
            .where(ArticleGroupSource.group_id == group.group_id)
        ).all()
        return [
            str(group.group_id),
            "lead",
            _aware(group.created_at).date().isoformat(),
            str(content.get("title") or ""),
            "; ".join(lines),
            str(max((e.score for e in events), default=0)),
            "yes",
            "",
            "",
            "",
            "\n".join(f"[{v}] {u}" for v, u in srcs),
            "\n".join(group.notes or []),
            "",
            "",
            "sources could not be verified: check them, update Promise Tracker if warranted, "
            "or set decision to dismiss",
            "needs_sources",
        ]


def _change_line(c: dict[str, Any]) -> str:
    t = c.get("type")
    if t == "status_change":
        return f"status: {c.get('old_status')} → {c.get('new_status')}"
    if t == "news_update":
        return f"news: {c.get('outlet')} ({c.get('published_date')}): {c.get('headline')}"
    if t == "new_evidence":
        return "new evidence on Promise Tracker"
    if t == "coordinator_update":
        return f"coordinator's update: {str(c.get('description', ''))[:120]}"
    return str(t)
