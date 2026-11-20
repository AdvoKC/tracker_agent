"""Change Classifier Agent (PRD §3.3): label diffs + news findings, score them, dedupe, group.

Per promise per run:
- tracker diffs → status_change / new_evidence (one per update) / new_promise / cosmetic /
  substantive_edit; removed promises → promise_removed;
- News Scout findings → news_update (evidence lead for coordinators);
- a new tracker update that IS one of our own submissions (PRD §8d: its id is the
  ``remote_update_id`` Promise Tracker reported, or its text is within
  ``own_submission_max_edit_ratio`` of the body we sent) → own_submission, logged only;
- article-type events get a rule-based score; the LLM may adjust it (±max) ONLY when the
  adjustment could flip the article decision (saves subscription usage);
- status_change always qualifies; other article types qualify at score ≥ threshold;
- qualifying events of one promise share one ``article_group_id`` (one article per promise
  per run); everything else is stored as ``logged``;
- ``dedupe_key`` is UNIQUE: an event already stored is never stored or reported again.

Dedupe keys: status/new_promise/edits use the new snapshot id (each detection is unique,
re-running is safe); new_evidence uses the update id; news_update uses the normalized URL.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from difflib import SequenceMatcher
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from promise_monitor.agents.monitor import RawDiff
from promise_monitor.config import ClassifierConfig
from promise_monitor.db.models import ChangeEvent, Meter, PromiseSnapshot, PtSubmission
from promise_monitor.domain.hashing import sha256_hex
from promise_monitor.llm.base import LLMClient, LLMError, LLMRequest
from promise_monitor.llm.prompts import load_prompt
from promise_monitor.logging_setup import get_logger

log = get_logger(__name__)

ARTICLE_TYPES = frozenset({"status_change", "new_evidence", "new_promise", "news_update"})
FINAL_STATUSES = frozenset({"kept", "broken", "compromised"})
TEXT_FIELDS = (
    "title",
    "description",
    "category",
    "priority",
    "source_text",
    "promise_start_date",
    "promise_end_date",
    "blog_text",
    "blog_links",
)
STATUS_LABELS = {
    "kept": "Kept",
    "broken": "Broken",
    "stalled": "Stalled",
    "in_the_works": "In the Works",
    "compromised": "Compromised",
    "not_yet_rated": "Not Yet Rated",
}
SCORE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "score_delta": {"type": "integer"},
        "reason": {"type": "string"},
    },
    "required": ["score_delta", "reason"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class NewsFinding:
    """An accepted News Scout result, ready to classify."""

    news_item_id: int
    promise_id: uuid.UUID
    meter_id: uuid.UUID
    url: str
    outlet: str
    headline: str
    published_date: date
    summary: str
    development_type: str
    relevance: float


@dataclass
class _Draft:
    promise_id: uuid.UUID
    change_type: str
    dedupe_key: str
    describe: str  # one line for the LLM / logs
    prev_snapshot_id: int | None = None
    new_snapshot_id: int | None = None
    news_item_id: int | None = None
    old_status: str | None = None
    new_status: str | None = None
    added_update_ids: list[str] = field(default_factory=list)
    fields_changed: list[str] = field(default_factory=list)
    edit_distance: float | None = None
    score_base: int = 0
    score_delta: int = 0
    score_reason: str = "rule-based"
    evidence_lead: bool = False
    update_text: str = ""  # new_evidence only: the update's full text (own-submission match)

    @property
    def is_article_type(self) -> bool:
        return self.change_type in ARTICLE_TYPES

    @property
    def score(self) -> int:
        return max(0, min(100, self.score_base + self.score_delta))


@dataclass
class ClassifyResult:
    events: list[ChangeEvent] = field(default_factory=list)
    groups: dict[uuid.UUID, list[uuid.UUID]] = field(default_factory=dict)  # group → events
    duplicates_skipped: int = 0
    llm_calls: int = 0
    llm_failures: int = 0


def key(*parts: object) -> str:
    return sha256_hex("|".join(str(p) for p in parts))


def edit_ratio(old: str, new: str) -> float:
    """0 = identical, 1 = completely different (case/whitespace-insensitive)."""
    a, b = " ".join(old.lower().split()), " ".join(new.lower().split())
    if a == b:
        return 0.0
    return 1.0 - SequenceMatcher(None, a, b, autojunk=False).ratio()


def _text_blob(content: dict[str, Any], fields: list[str]) -> str:
    parts = []
    for f in fields:
        v = content.get(f)
        if f == "updates":
            v = " ".join(str(u.get("text", "")) for u in (v or []))
        parts.append(" ".join(v) if isinstance(v, list) else str(v or ""))
    return "\n".join(parts)


def _drafts_for_diff(
    d: RawDiff, prev: dict[str, Any], new: dict[str, Any], cfg: ClassifierConfig
) -> list[_Draft]:
    pid = d.promise_id

    def draft(**kw: Any) -> _Draft:
        return _Draft(
            promise_id=pid,
            prev_snapshot_id=d.prev_snapshot_id,
            new_snapshot_id=d.new_snapshot_id,
            **kw,
        )

    if d.is_new_promise:
        return [
            draft(
                change_type="new_promise",
                dedupe_key=key(pid, "new_promise", d.new_snapshot_id),
                describe=f"New promise added to the meter: {new.get('title')}",
                new_status=new.get("status"),
                fields_changed=list(d.fields_changed),
                score_base=cfg.base_scores.new_promise,
            )
        ]

    drafts: list[_Draft] = []
    if "status" in d.fields_changed:
        old_s, new_s = prev.get("status"), new.get("status")
        drafts.append(
            draft(
                change_type="status_change",
                dedupe_key=key(pid, "status_change", d.new_snapshot_id),
                describe=f"Status moved from {STATUS_LABELS.get(str(old_s), old_s)} "
                f"to {STATUS_LABELS.get(str(new_s), new_s)}",
                old_status=old_s,
                new_status=new_s,
                fields_changed=["status"],
                score_base=cfg.base_scores.status_change
                + (cfg.bonus_final_status if new_s in FINAL_STATUSES else 0),
            )
        )

    new_updates = {u["update_id"]: u for u in new.get("updates") or []}
    for uid in d.added_update_ids:
        full = str(new_updates.get(uid, {}).get("text", ""))
        drafts.append(
            draft(
                change_type="new_evidence",
                dedupe_key=key(pid, "new_evidence", uid),
                describe=f"New evidence update: {full[:400]}",
                update_text=full,
                new_status=new.get("status"),
                added_update_ids=[uid],
                fields_changed=["updates"],
                score_base=cfg.base_scores.new_evidence,
            )
        )

    text_fields = [f for f in d.fields_changed if f in TEXT_FIELDS]
    edited_updates = (
        "updates" in d.fields_changed and not d.added_update_ids and not d.removed_update_ids
    )
    if edited_updates:
        text_fields.append("updates")
    if text_fields:
        ratio = edit_ratio(_text_blob(prev, text_fields), _text_blob(new, text_fields))
        kind = "cosmetic" if ratio < cfg.cosmetic_max_edit_ratio else "substantive_edit"
        drafts.append(
            draft(
                change_type=kind,
                dedupe_key=key(pid, kind, d.new_snapshot_id),
                describe=f"Text edit in {', '.join(text_fields)} ({ratio:.0%} changed)",
                fields_changed=text_fields,
                edit_distance=round(ratio, 4),
                score_reason="logged only (v1: edits never produce articles)",
            )
        )
    if d.removed_update_ids:
        drafts.append(
            draft(
                change_type="substantive_edit",
                dedupe_key=key(pid, "evidence_removed", d.new_snapshot_id),
                describe=f"{len(d.removed_update_ids)} evidence update(s) removed or unpublished",
                fields_changed=["updates"],
                score_reason="logged only: evidence removed",
            )
        )
    return drafts


def _draft_for_news(f: NewsFinding, cfg: ClassifierConfig) -> _Draft:
    return _Draft(
        promise_id=f.promise_id,
        change_type="news_update",
        dedupe_key=key(f.promise_id, "news_update", f.url),
        describe=f"News ({f.outlet}, {f.published_date}): {f.headline}. {f.summary}",
        news_item_id=f.news_item_id,
        evidence_lead=True,
        score_base=cfg.base_scores.news_update
        + (cfg.bonus_news_high_confidence if f.relevance >= 0.9 else 0),
    )


def classify(
    session: Session,
    *,
    run_id: uuid.UUID,
    meter: Meter,
    diffs: list[RawDiff],
    removed: list[uuid.UUID],
    news: list[NewsFinding],
    cfg: ClassifierConfig,
    llm: LLMClient | None,
    now: datetime,
) -> ClassifyResult:
    """Classify one meter's changes for this run. Caller owns the transaction."""
    bound = log.bind(meter_id=str(meter.id), agent="classifier")
    result = ClassifyResult()

    snap_ids = {i for d in diffs for i in (d.prev_snapshot_id, d.new_snapshot_id) if i}
    contents: dict[int, dict[str, Any]] = (
        dict(
            session.execute(
                select(PromiseSnapshot.snapshot_id, PromiseSnapshot.content).where(
                    PromiseSnapshot.snapshot_id.in_(snap_ids)
                )
            ).all()
        )
        if snap_ids
        else {}
    )

    by_promise: dict[uuid.UUID, list[_Draft]] = defaultdict(list)
    latest: dict[uuid.UUID, dict[str, Any]] = {}
    for d in diffs:
        new = contents.get(d.new_snapshot_id, {})
        prev = contents.get(d.prev_snapshot_id, {}) if d.prev_snapshot_id else {}
        latest[d.promise_id] = new
        by_promise[d.promise_id].extend(_drafts_for_diff(d, prev, new, cfg))
    _mark_own_submissions(session, by_promise, cfg)
    for f in news:
        by_promise[f.promise_id].append(_draft_for_news(f, cfg))
    for pid in removed:
        by_promise[pid].append(
            _Draft(
                promise_id=pid,
                change_type="promise_removed",
                dedupe_key=key(pid, "promise_removed", run_id),
                describe="Promise no longer listed on the meter",
                score_reason="logged only: admin alert",
            )
        )

    all_keys = [dr.dedupe_key for drafts in by_promise.values() for dr in drafts]
    existing = (
        set(
            session.scalars(
                select(ChangeEvent.dedupe_key).where(ChangeEvent.dedupe_key.in_(all_keys))
            )
        )
        if all_keys
        else set()
    )

    prompt = load_prompt("classifier") if llm is not None else None
    lo = cfg.news_threshold - cfg.llm_max_adjustment
    hi = cfg.news_threshold + cfg.llm_max_adjustment

    for pid, drafts in by_promise.items():
        fresh = []
        for dr in drafts:
            if dr.dedupe_key in existing:
                result.duplicates_skipped += 1
                bound.info("event_duplicate_skipped", promise_id=str(pid), type=dr.change_type)
                continue
            existing.add(dr.dedupe_key)
            fresh.append(dr)
        if not fresh:
            continue

        content = latest.get(pid) or _latest_content(session, pid)
        for dr in fresh:
            if dr.is_article_type:
                if meter.priority_rank <= cfg.priority_meter_max_rank:
                    dr.score_base += cfg.bonus_priority_meter
                if (content or {}).get("priority") == "high":
                    dr.score_base += cfg.bonus_high_priority_promise
                dr.score_base = min(dr.score_base, 100)

        # LLM only where its adjustment could change the outcome.
        borderline = [
            dr
            for dr in fresh
            if dr.is_article_type and dr.change_type != "status_change" and lo <= dr.score_base < hi
        ]
        if borderline and llm is not None and prompt is not None:
            result.llm_calls += 1
            try:
                system, user = prompt.render(
                    max_delta=str(cfg.llm_max_adjustment),
                    promise_title=str((content or {}).get("title", "")),
                    meter_name=meter.name,
                    status=STATUS_LABELS.get(str((content or {}).get("status")), "unknown"),
                    base_score=str(max(dr.score_base for dr in borderline)),
                    threshold=str(cfg.news_threshold),
                    changes="\n".join(f"- {dr.describe}" for dr in fresh if dr.is_article_type),
                )
                resp = llm.run(
                    LLMRequest(
                        task="classifier",
                        prompt_name=prompt.name,
                        system=system,
                        user=user,
                        schema=SCORE_SCHEMA,
                        subject_id=str(pid),
                    )
                )
                delta = int(resp.output["score_delta"])
                delta = max(-cfg.llm_max_adjustment, min(cfg.llm_max_adjustment, delta))
                reason = str(resp.output.get("reason", ""))[:300]
                for dr in fresh:
                    if dr.is_article_type and dr.change_type != "status_change":
                        dr.score_delta, dr.score_reason = delta, reason
            except (LLMError, KeyError, ValueError, TypeError) as exc:
                result.llm_failures += 1
                bound.warning("classifier_llm_fallback", promise_id=str(pid), error=str(exc))
                for dr in borderline:
                    dr.score_reason = "llm_unavailable: rule-based score"

        qualifying = [
            dr
            for dr in fresh
            if dr.is_article_type
            and (dr.change_type == "status_change" or dr.score >= cfg.news_threshold)
        ]
        qualifying_ids = {id(dr) for dr in qualifying}
        group_id = uuid.uuid4() if qualifying else None
        for dr in fresh:
            ev = ChangeEvent(
                event_id=uuid.uuid4(),
                run_id=run_id,
                meter_id=meter.id,
                promise_id=pid,
                change_type=dr.change_type,
                prev_snapshot_id=dr.prev_snapshot_id,
                new_snapshot_id=dr.new_snapshot_id,
                news_item_id=dr.news_item_id,
                old_status=dr.old_status,
                new_status=dr.new_status,
                added_update_ids=dr.added_update_ids,
                fields_changed=dr.fields_changed,
                edit_distance=dr.edit_distance,
                score=dr.score if dr.is_article_type else 0,
                score_base=dr.score_base,
                score_llm_delta=dr.score_delta,
                score_reason=dr.score_reason,
                evidence_lead=dr.evidence_lead,
                article_group_id=group_id if id(dr) in qualifying_ids else None,
                dedupe_key=dr.dedupe_key,
                detected_at=now,
                state="queued" if id(dr) in qualifying_ids else "logged",
            )
            session.add(ev)
            result.events.append(ev)
            if group_id is not None and id(dr) in qualifying_ids:
                result.groups.setdefault(group_id, []).append(ev.event_id)

    session.flush()
    bound.info(
        "classified",
        events=len(result.events),
        groups=len(result.groups),
        duplicates=result.duplicates_skipped,
        llm_calls=result.llm_calls,
    )
    return result


def _mark_own_submissions(
    session: Session, by_promise: dict[uuid.UUID, list[_Draft]], cfg: ClassifierConfig
) -> None:
    """Relabel new_evidence that is our own published submission (never reported again).

    The dedupe key stays the new_evidence key, so the update can't come back as evidence later.
    """
    pids = {
        pid
        for pid, drafts in by_promise.items()
        if any(d.change_type == "new_evidence" for d in drafts)
    }
    if not pids:
        return
    ids: dict[uuid.UUID, set[str]] = defaultdict(set)
    texts: dict[uuid.UUID, list[str]] = defaultdict(list)
    for pid, upd, text in session.execute(
        select(
            PtSubmission.promise_id, PtSubmission.remote_update_id, PtSubmission.body_text
        ).where(PtSubmission.promise_id.in_(pids))
    ):
        if upd:
            ids[pid].add(upd)
        texts[pid].append(text)
    for pid in pids:
        for dr in by_promise[pid]:
            if dr.change_type != "new_evidence":
                continue
            by_id = bool(set(dr.added_update_ids) & ids[pid])
            by_text = any(
                edit_ratio(dr.update_text, t) <= cfg.own_submission_max_edit_ratio
                for t in texts[pid]
                if t and dr.update_text
            )
            if by_id or by_text:
                dr.change_type = "own_submission"
                dr.score_base = 0
                dr.score_reason = (
                    "logged only: our own submission, published by Promise Tracker "
                    f"(matched by {'update id' if by_id else 'text'})"
                )


def _latest_content(session: Session, promise_id: uuid.UUID) -> dict[str, Any] | None:
    row = session.scalars(
        select(PromiseSnapshot.content)
        .where(PromiseSnapshot.promise_id == promise_id)
        .order_by(PromiseSnapshot.snapshot_id.desc())
        .limit(1)
    ).first()
    return row
