"""Writer + Editor/QA Agents (PRD §3.5, §3.6).

``run_draft_loop`` is pure (LLM + QA only, no DB) so the eval can run it on fixture cases:
    writer → deterministic QA (domain/qa.py) → [only if QA passes] LLM editor review
    → on any failure, the issues go back to the writer as ``editor_feedback``
    → at most ``max_rewrites`` rewrites; then the best draft is kept with ``qa_flags``.

``Drafter`` wraps it for verified article groups: builds the writer input from the DB
(only citable sources), stores an ``articles`` row (``in_review`` or ``needs_attention``)
and moves the group/events to ``drafted``. An LLM outage leaves the group ``verified``
for the next run; nothing is auto-published (coordinators approve in P6).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from promise_monitor.config import ArticleConfig
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
from promise_monitor.domain.qa import STATUS_LABELS, QAContext, QAReport, check_article
from promise_monitor.domain.urls import normalize_url
from promise_monitor.house_style import house_examples
from promise_monitor.llm.base import LLMAuthError, LLMClient, LLMError, LLMRequest
from promise_monitor.llm.prompts import Prompt, load_prompt
from promise_monitor.logging_setup import get_logger

log = get_logger(__name__)

WRITER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "body_markdown": {"type": "string"},
        "sources_used": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["headline", "body_markdown", "sources_used"],
}
EDITOR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "neutral": {"type": "boolean"},
        "factually_consistent": {"type": "boolean"},
        "issues": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["neutral", "factually_consistent", "issues"],
}


class DeferredError(RuntimeError):
    """LLM unavailable; leave the group for the next run."""


@dataclass
class LoopResult:
    headline: str = ""
    body: str = ""
    passed: bool = False
    attempts: int = 0
    issues: list[str] = field(default_factory=list)
    report: QAReport | None = None
    history: list[list[str]] = field(default_factory=list)  # issues per attempt


def format_examples(examples: list[str]) -> str:
    if not examples:
        return "(no examples available: follow the house rules above)"
    return "\n\n".join(f"Example {i + 1}:\n{t}" for i, t in enumerate(examples))


def run_draft_loop(
    llm: LLMClient,
    writer_input: dict[str, Any],
    qa: QAContext,
    cfg: ArticleConfig,
    *,
    subject_id: str | None = None,
    writer_prompt: Prompt | None = None,
    editor_prompt: Prompt | None = None,
    examples: list[str] | None = None,
) -> LoopResult:
    """Draft until QA + editor pass or rewrites run out. Raises LLMError on outages."""
    wp = writer_prompt or load_prompt("writer")
    ep = editor_prompt or load_prompt("editor")
    out = LoopResult()
    feedback: list[str] = []
    best: tuple[int, str, str, QAReport | None, list[str]] | None = None

    for attempt in range(cfg.max_rewrites + 1):
        out.attempts = attempt + 1
        payload = dict(writer_input)
        if feedback:
            payload["editor_feedback"] = feedback
        system, user = wp.render(
            min_words=str(cfg.min_words),
            max_words=str(cfg.max_words),
            target_words=str(cfg.target_words),
            house_examples=format_examples(examples or []),
            input_json=json.dumps(payload, ensure_ascii=False, indent=1),
        )
        resp = llm.run(
            LLMRequest("writer", wp.name, system, user, WRITER_SCHEMA, subject_id=subject_id)
        )
        headline = str(resp.output.get("headline") or "").strip()
        body = str(resp.output.get("body_markdown") or "").strip()
        report = check_article(headline, body, qa)
        issues = list(report.issues)

        if not issues:
            system, user = ep.render(
                input_json=json.dumps(writer_input, ensure_ascii=False, indent=1),
                headline=headline,
                body=body,
            )
            review = llm.run(
                LLMRequest("editor", ep.name, system, user, EDITOR_SCHEMA, subject_id=subject_id)
            ).output
            if not (review.get("neutral") and review.get("factually_consistent")):
                editor_issues = [f"editor: {i}" for i in review.get("issues") or []]
                issues = editor_issues or ["editor: draft failed the neutrality/fact review"]

        out.history.append(issues)
        if best is None or len(issues) < best[0]:
            best = (len(issues), headline, body, report, issues)
        if not issues:
            out.passed = True
            break
        feedback = issues

    assert best is not None
    _, out.headline, out.body, out.report, out.issues = best
    if out.passed:
        out.issues = []
    return out


# --------------------------------------------------------------------------- DB wrapper


@dataclass
class WriteResult:
    groups: int = 0
    in_review: int = 0
    needs_attention: int = 0
    deferred: int = 0
    auth_failed: bool = False
    article_ids: list[uuid.UUID] = field(default_factory=list)


@dataclass(frozen=True)
class PreparedGroup:
    writer_input: dict[str, Any]
    qa: QAContext
    url_to_source: dict[str, int]
    events: list[ChangeEvent]
    examples: list[str]


def _date_text(value: object) -> str | None:
    return str(value) if value else None


def prepare_group(
    session: Session,
    group: ArticleGroup,
    cfg: ArticleConfig,
    banned_terms: tuple[str, ...],
    promise_url_tpl: str,
    now: datetime,
) -> PreparedGroup:
    events = list(
        session.scalars(
            select(ChangeEvent).where(
                ChangeEvent.article_group_id == group.group_id, ChangeEvent.state == "queued"
            )
        )
    )
    meter = session.get(Meter, group.meter_id)
    assert meter is not None
    content: dict[str, Any] = (
        session.scalars(
            select(PromiseSnapshot.content)
            .where(PromiseSnapshot.promise_id == group.promise_id)
            .order_by(PromiseSnapshot.snapshot_id.desc())
            .limit(1)
        ).first()
        or {}
    )
    promise_url = normalize_url(promise_url_tpl.format(promise_id=group.promise_id)) or ""
    current = str(content.get("status") or "not_yet_rated")
    updates = {u["update_id"]: u for u in content.get("updates") or []}
    news = {
        n.id: n
        for n in session.scalars(
            select(NewsItem).where(
                NewsItem.id.in_([e.news_item_id for e in events if e.news_item_id])
            )
        )
    }

    changes: list[dict[str, Any]] = []
    old_status = None
    for ev in events:
        if ev.change_type == "status_change":
            old_status = ev.old_status
            changes.append(
                {
                    "type": "status_change",
                    "old_status": STATUS_LABELS.get(ev.old_status or "", ev.old_status),
                    "new_status": STATUS_LABELS.get(ev.new_status or "", ev.new_status),
                }
            )
        elif ev.change_type == "new_evidence":
            for uid in ev.added_update_ids or []:
                changes.append(
                    {
                        "type": "new_evidence",
                        "update_text": str(updates.get(uid, {}).get("text", ""))[:1500],
                    }
                )
        elif ev.change_type == "new_promise":
            changes.append({"type": "new_promise"})
        elif ev.change_type == "coordinator_update":
            rating = ev.new_status or current
            if rating != current and old_status is None:
                # The coordinator re-rates the promise: report it as a rating change.
                old_status = current
                changes.append(
                    {
                        "type": "status_change",
                        "old_status": STATUS_LABELS.get(current, current),
                        "new_status": STATUS_LABELS.get(rating, rating),
                    }
                )
            current = rating
            changes.append({"type": "coordinator_update", "description": ev.brief or ""})
        elif ev.change_type == "news_update" and ev.news_item_id in news:
            n = news[ev.news_item_id]
            changes.append(
                {
                    "type": "news_update",
                    "outlet": n.outlet,
                    "headline": n.headline,
                    "published_date": _date_text(n.published_date),
                    "summary": n.summary,
                }
            )

    rows = session.execute(
        select(ArticleGroupSource, Source)
        .join(Source, Source.source_id == ArticleGroupSource.source_id)
        .where(ArticleGroupSource.group_id == group.group_id, ArticleGroupSource.citable.is_(True))
        .order_by(ArticleGroupSource.role, Source.source_id)
    ).all()
    sources = []
    url_to_source: dict[str, int] = {}
    for ags, src in rows:
        partial = ags.verdict == "verified_partial"
        sources.append(
            {
                "id": src.source_id,
                "url": src.url,
                "title": src.title or src.search_title or ags.confirmed_title,
                "publisher": src.publisher,
                "date": _date_text(src.published_date),
                "snippet": None if partial else (src.snippet or "")[:800],
                "verdict": ags.verdict,
            }
        )
        url_to_source[src.url] = src.source_id

    recent = tuple(
        session.scalars(
            select(Article.body_markdown).where(
                Article.promise_id == group.promise_id,
                Article.created_at >= now - timedelta(days=cfg.dedupe_window_days),
            )
        )
    )
    writer_input = {
        "meter": {"name": meter.name, "office": meter.scope},
        "promise": {
            "title": content.get("title"),
            "category": content.get("category"),
            "promise_url": promise_url,
            "current_status": STATUS_LABELS.get(current, current),
        },
        "changes": changes,
        "sources": sources,
    }
    qa = QAContext(
        citable_urls=frozenset(url_to_source),
        promise_url=promise_url,
        current_status=current,
        old_status=old_status,
        has_status_change=old_status is not None,
        banned_terms=banned_terms,
        min_words=cfg.min_words,
        max_words=cfg.max_words,
        recent_bodies=recent,
        dedupe_similarity=cfg.dedupe_similarity,
        min_links=cfg.min_links,
        max_headline_words=cfg.max_headline_words,
        max_quote_words=cfg.max_quote_words,
    )
    examples = house_examples(
        session, group.meter_id, cfg.house_examples, exclude_promise=group.promise_id
    )
    return PreparedGroup(writer_input, qa, url_to_source, events, examples)


_LABEL_TO_STATUS = {label: value for value, label in STATUS_LABELS.items()}


def qa_context_from_input(
    writer_input: dict[str, Any],
    cfg: ArticleConfig,
    banned_terms: tuple[str, ...],
    recent_bodies: tuple[str, ...] = (),
) -> QAContext:
    """Rebuild the QA context from a stored ``writer_input`` (used to re-check human edits)."""
    promise = writer_input.get("promise") or {}
    current = _LABEL_TO_STATUS.get(str(promise.get("current_status")), "not_yet_rated")
    old = next(
        (
            _LABEL_TO_STATUS.get(str(c.get("old_status")))
            for c in writer_input.get("changes") or []
            if c.get("type") == "status_change"
        ),
        None,
    )
    citable = frozenset(
        u for s in writer_input.get("sources") or [] if (u := normalize_url(str(s.get("url", ""))))
    )
    return QAContext(
        citable_urls=citable,
        promise_url=normalize_url(str(promise.get("promise_url", ""))) or "",
        current_status=current,
        old_status=old,
        has_status_change=old is not None,
        banned_terms=banned_terms,
        min_words=cfg.min_words,
        max_words=cfg.max_words,
        recent_bodies=recent_bodies,
        dedupe_similarity=cfg.dedupe_similarity,
        min_links=cfg.min_links,
        max_headline_words=cfg.max_headline_words,
        max_quote_words=cfg.max_quote_words,
    )


class Drafter:
    def __init__(
        self,
        session: Session,
        llm: LLMClient,
        *,
        cfg: ArticleConfig,
        banned_terms: tuple[str, ...],
        public_promise_url: str,
        now: datetime,
    ) -> None:
        self.s = session
        self.llm = llm
        self.cfg = cfg
        self.banned = banned_terms
        self.tpl = public_promise_url
        self.now = now
        self.result = WriteResult()

    def run(self, limit: int | None = None, only: uuid.UUID | None = None) -> WriteResult:
        q = select(ArticleGroup).where(ArticleGroup.state == "verified")
        if only is not None:
            q = q.where(ArticleGroup.group_id == only)
        groups = list(
            self.s.scalars(
                q.order_by(ArticleGroup.verified_at, ArticleGroup.group_id).limit(
                    limit or self.cfg.max_groups_per_run
                )
            )
        )
        for group in groups:
            self.result.groups += 1
            try:
                with self.s.begin_nested():
                    self.draft(group)
            except LLMAuthError as exc:
                self.result.auth_failed = True
                self.result.deferred += 1
                log.error("writer_auth_failed", error=str(exc))
                break
            except (LLMError, DeferredError) as exc:
                self.result.deferred += 1
                log.warning("writer_deferred", group_id=str(group.group_id), error=str(exc))
        return self.result

    def draft(self, group: ArticleGroup) -> Article:
        prep = prepare_group(self.s, group, self.cfg, self.banned, self.tpl, self.now)
        if not prep.events:
            raise DeferredError("no queued events left in group")
        loop = run_draft_loop(
            self.llm,
            prep.writer_input,
            prep.qa,
            self.cfg,
            subject_id=str(group.group_id),
            examples=prep.examples,
        )
        linked = [u for _a, u in (loop.report.links if loop.report else [])]
        source_ids = list(
            dict.fromkeys(prep.url_to_source[u] for u in linked if u in prep.url_to_source)
        )
        state = "in_review" if loop.passed else "needs_attention"
        article = Article(
            article_id=uuid.uuid4(),
            article_group_id=group.group_id,
            meter_id=group.meter_id,
            promise_id=group.promise_id,
            event_ids=[str(e.event_id) for e in prep.events],
            headline=loop.headline,
            body_markdown=loop.body,
            word_count=loop.report.word_count if loop.report else 0,
            source_ids=source_ids,
            qa_flags=loop.issues,
            rewrite_count=loop.attempts - 1,
            writer_input=prep.writer_input,
            state=state,
            created_at=self.now,
        )
        self.s.add(article)
        group.state = "drafted"
        for ev in prep.events:
            ev.state = "drafted"
        self.s.flush()
        self.result.article_ids.append(article.article_id)
        if loop.passed:
            self.result.in_review += 1
        else:
            self.result.needs_attention += 1
        log.info(
            "article_drafted",
            group_id=str(group.group_id),
            state=state,
            attempts=loop.attempts,
            words=article.word_count,
            flags=len(loop.issues),
        )
        return article
