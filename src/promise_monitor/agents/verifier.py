"""Source Verification Agent (PRD §3.4).

For each queued article group (one promise, one run's qualifying events):
1. Collect sources:
   - the promise page (always citable while the promise is present on the tracker);
   - REQUIRED: links backing the change itself — new updates' evidence links; for a status
     change without a new update, the latest update's links; for a new promise, its own
     links; for a news_update, the news URL; for a coordinator_update, the coordinator's links;
   - CONTEXT: the promise's source/write-up links (at most ``max_context_sources``).
2. Fetch each (cached ``cache_days``) with SafeFetcher; extract title/date/publisher/text.
3. Blocked or paywalled pages: confirm existence from RAW search results — reusing the News
   Scout's grounding for news URLs, otherwise one WebSearch call per URL (cached).
4. One LLM call per group judges relevance of every source that has something to judge.
5. Verdicts: verified / verified_partial / unverifiable / broken. For news, a page date older
   than the claimed date by more than ``date_tolerance_days`` → unverifiable (date_mismatch).
6. Sufficiency: promise page + ≥1 citable REQUIRED external source. A news_update whose own
   URL is not citable → that event becomes ``needs_sources``. No usable events → the group
   becomes ``needs_sources`` (shown to the coordinator, never auto-drafted).
LLM unavailable → the group stays ``queued`` and is retried next run (nothing is written).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from promise_monitor.agents.classifier import STATUS_LABELS
from promise_monitor.config import VerifierConfig
from promise_monitor.db.models import (
    ArticleGroup,
    ArticleGroupSource,
    ChangeEvent,
    Meter,
    NewsItem,
    PromiseIndex,
    PromiseSnapshot,
    Source,
)
from promise_monitor.domain.extract import extract
from promise_monitor.domain.fetch import FetchResult
from promise_monitor.domain.urls import host_of, normalize_url
from promise_monitor.llm.base import LLMAuthError, LLMClient, LLMError, LLMRequest
from promise_monitor.llm.prompts import load_prompt
from promise_monitor.logging_setup import get_logger

log = get_logger(__name__)

RELEVANCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "sources": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source_ref": {"type": "string"},
                    "relevant": {"type": "boolean"},
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["source_ref", "relevant", "confidence", "reason"],
            },
        }
    },
    "required": ["sources"],
}
SEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "searched": {"type": "boolean"},
        "queries": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["searched", "queries"],
}


class Fetcher(Protocol):
    def fetch(self, url: str) -> FetchResult: ...


class DeferredError(RuntimeError):
    """Group can't be finished this run (LLM unavailable); leave it queued."""


@dataclass
class _Src:
    url: str
    role: str  # promise_page | required | context
    event_id: uuid.UUID | None
    row: Source | None = None
    claimed_date: date | None = None  # News Scout's claim, for news URLs
    relevant: bool | None = None
    confidence: float | None = None
    reason: str | None = None
    verdict: str = "unverifiable"
    # a coordinator's earlier confirmation of a page that blocked us: (user_id, at, title)
    confirmed: tuple[uuid.UUID, datetime, str | None] | None = None


@dataclass
class VerifyResult:
    groups: int = 0
    verified: int = 0
    needs_sources: int = 0
    deferred: int = 0
    fetched: int = 0
    cache_hits: int = 0
    llm_calls: int = 0
    auth_failed: bool = False
    group_ids: list[uuid.UUID] = field(default_factory=list)


def pending_groups(session: Session, limit: int, now: datetime) -> list[uuid.UUID]:
    """Queued events' groups that have not been verified yet, oldest first."""
    rows = session.execute(
        select(ChangeEvent.article_group_id, ChangeEvent.meter_id, ChangeEvent.promise_id)
        .outerjoin(ArticleGroup, ArticleGroup.group_id == ChangeEvent.article_group_id)
        .where(
            ChangeEvent.state == "queued",
            ChangeEvent.article_group_id.is_not(None),
            (ArticleGroup.group_id.is_(None)) | (ArticleGroup.state == "queued"),
        )
        .order_by(ChangeEvent.detected_at, ChangeEvent.article_group_id)
    ).all()
    out: list[uuid.UUID] = []
    for gid, meter_id, promise_id in rows:
        assert gid is not None
        if gid in out:
            continue
        if session.get(ArticleGroup, gid) is None:
            session.add(
                ArticleGroup(
                    group_id=gid,
                    meter_id=meter_id,
                    promise_id=promise_id,
                    state="queued",
                    notes=[],
                    created_at=now,
                )
            )
        out.append(gid)
        if len(out) >= limit:
            break
    session.flush()
    return out


def _latest_content(session: Session, promise_id: uuid.UUID) -> dict[str, Any]:
    row = session.scalars(
        select(PromiseSnapshot.content)
        .where(PromiseSnapshot.promise_id == promise_id)
        .order_by(PromiseSnapshot.snapshot_id.desc())
        .limit(1)
    ).first()
    return row or {}


def _update_texts(updates: list[dict[str, Any]]) -> str:
    """Published Promise Tracker update text: it says why the cited links matter."""
    texts = [str(u.get("text") or "")[:UPDATE_TEXT_CHARS] for u in updates]
    return " | ".join(t for t in texts if t)


def _describe(
    ev: ChangeEvent, news: NewsItem | None, updates: dict[str, dict[str, Any]] | None = None
) -> str:
    updates = updates or {}
    if ev.change_type == "status_change":
        line = (
            f"Status moved from {STATUS_LABELS.get(ev.old_status or '', ev.old_status)} "
            f"to {STATUS_LABELS.get(ev.new_status or '', ev.new_status)} on Promise Tracker"
        )
        if updates:
            latest = max(updates.values(), key=lambda u: str(u.get("created_at", "")))
            if text := _update_texts([latest]):
                line += f". Latest published update: {text}"
        return line
    if ev.change_type == "new_evidence":
        text = _update_texts([updates[u] for u in ev.added_update_ids or [] if u in updates])
        line = "New evidence update published on Promise Tracker"
        return f"{line}: {text}" if text else line
    if ev.change_type == "new_promise":
        return "Promise newly added to the meter on Promise Tracker"
    if ev.change_type == "news_update" and news is not None:
        return f"News report ({news.outlet}, {news.published_date}): {news.headline}"
    if ev.change_type == "coordinator_update":
        rating = STATUS_LABELS.get(ev.new_status or "", ev.new_status)
        return f"AdvoKC coordinator's update (rating: {rating}): {(ev.brief or '')[:1200]}"
    return ev.change_type


UPDATE_TEXT_CHARS = 800
COORDINATOR_CONFIRMED = "Page blocked the agent's reader; a coordinator opened it and confirmed it."
_MIN_SLUG = 20  # shorter last path segments ("index", "news") are too generic to match on


def _same_article(hit_url: str, url: str) -> bool:
    """A search hit is the same article: same URL, or same host + same long slug (AMP etc.)."""
    hit = normalize_url(hit_url)
    if hit is None:
        return False
    if hit == url:
        return True
    if host_of(hit) != host_of(url):
        return False

    def slug(u: str) -> str:
        parts = [p for p in u.split("?", 1)[0].split("/")[3:] if p and p != "amp"]
        return parts[-1] if parts else ""

    s = slug(url)
    return len(s) >= _MIN_SLUG and slug(hit) == s


class Verifier:
    def __init__(
        self,
        session: Session,
        fetcher: Fetcher,
        llm: LLMClient,
        *,
        cfg: VerifierConfig,
        public_promise_url: str,
        now: datetime,
    ) -> None:
        self.s = session
        self.fetcher = fetcher
        self.llm = llm
        self.cfg = cfg
        self.promise_url_tpl = public_promise_url
        self.now = now
        self.result = VerifyResult()
        self._relevance = load_prompt("relevance")
        self._search = load_prompt("search_confirm")

    # ------------------------------------------------------------------ public

    def requeue(self, group_id: uuid.UUID, *, refresh: bool) -> None:
        """Send one group back through verification (dashboard 'Re-verify sources')."""
        group = self.s.get(ArticleGroup, group_id)
        if group is None:
            raise ValueError("group not found")
        if group.state not in ("queued", "verified", "needs_sources"):
            raise ValueError(f"group is {group.state}; redraft the article instead")
        group.state = "queued"
        for ev in self.s.scalars(
            select(ChangeEvent).where(ChangeEvent.article_group_id == group_id)
        ):
            if ev.state in ("queued", "needs_sources"):
                ev.state = "queued"
        if refresh:
            src_ids = self.s.scalars(
                select(ArticleGroupSource.source_id).where(ArticleGroupSource.group_id == group_id)
            ).all()
            for src in self.s.scalars(select(Source).where(Source.source_id.in_(src_ids))):
                src.checked_at = datetime(2000, 1, 1, tzinfo=UTC)
                src.search_checked_at = None
        self.s.flush()

    def run(self, limit: int | None = None, only: uuid.UUID | None = None) -> VerifyResult:
        if only is not None:  # a single group must not be cut off by the per-run limit
            groups = [g for g in pending_groups(self.s, 1_000_000, self.now) if g == only]
        else:
            groups = pending_groups(self.s, limit or self.cfg.max_groups_per_run, self.now)
        for gid in groups:
            self.result.groups += 1
            self.result.group_ids.append(gid)
            try:
                with self.s.begin_nested():
                    self.verify_group(gid)
            except LLMAuthError as exc:
                self.result.auth_failed = True
                self.result.deferred += 1
                log.error("verifier_auth_failed", error=str(exc))
                break
            except DeferredError as exc:
                self.result.deferred += 1
                log.warning("verifier_deferred", group_id=str(gid), error=str(exc))
        return self.result

    def verify_group(self, gid: uuid.UUID) -> None:
        group = self.s.get(ArticleGroup, gid)
        assert group is not None
        events = list(
            self.s.scalars(
                select(ChangeEvent).where(
                    ChangeEvent.article_group_id == gid, ChangeEvent.state == "queued"
                )
            )
        )
        meter = self.s.get(Meter, group.meter_id)
        assert meter is not None
        content = _latest_content(self.s, group.promise_id)
        news_by_id = {
            n.id: n
            for n in self.s.scalars(
                select(NewsItem).where(
                    NewsItem.id.in_([e.news_item_id for e in events if e.news_item_id])
                )
            )
        }
        srcs = self._collect(group, events, content, news_by_id)
        notes: list[str] = []
        previous = {
            a.source_id: (a.confirmed_by, a.confirmed_at, a.confirmed_title)
            for a in self.s.scalars(
                select(ArticleGroupSource).where(
                    ArticleGroupSource.group_id == gid, ArticleGroupSource.confirmed_by.isnot(None)
                )
            )
        }

        for src in srcs:
            if src.role == "promise_page":
                src.row = self._promise_page_source(src.url, content)
            else:
                src.row = self._get_or_fetch(src.url)
            prev = previous.get(src.row.source_id)
            if prev is not None and prev[0] is not None and prev[1] is not None:
                src.confirmed = (prev[0], prev[1], prev[2])

        for src in srcs:
            row = src.row
            assert row is not None
            # A miss is retried sooner than a hit expires: new articles reach search late.
            recently_searched = row.search_checked_at is not None and _aware(
                row.search_checked_at
            ) >= self.now - timedelta(days=self.cfg.search_retry_days)
            if (
                row.fetch_status in ("blocked", "paywalled")
                and not row.search_confirmed
                and not recently_searched
            ):
                news = next(
                    (n for n in news_by_id.values() if n.url == src.url and n.search_grounded),
                    None,
                )
                if news is not None:
                    row.search_confirmed, row.search_title = True, news.search_title
                else:
                    self._search_confirm(row, title_hint=row.title or "")

        self._judge_relevance(meter, content, events, news_by_id, srcs)
        self._verdicts(srcs, notes)
        self._decide(group, events, srcs, notes)

    # ------------------------------------------------------------------ collection

    def _collect(
        self,
        group: ArticleGroup,
        events: list[ChangeEvent],
        content: dict[str, Any],
        news_by_id: dict[int, NewsItem],
    ) -> list[_Src]:
        found: dict[str, _Src] = {}

        def add(
            raw: str, role: str, event_id: uuid.UUID | None, claimed: date | None = None
        ) -> None:
            url = normalize_url(raw)
            if url is None:
                return
            existing = found.get(url)
            if existing is None or (existing.role == "context" and role == "required"):
                found[url] = _Src(url, role, event_id, claimed_date=claimed)

        add(self.promise_url_tpl.format(promise_id=group.promise_id), "promise_page", None)
        updates = {u["update_id"]: u for u in content.get("updates") or []}
        has_evidence = any(e.change_type == "new_evidence" for e in events)
        for ev in events:
            if ev.change_type == "new_evidence":
                for uid in ev.added_update_ids or []:
                    for link in updates.get(uid, {}).get("evidence_links", []):
                        add(link, "required", ev.event_id)
            elif ev.change_type == "status_change" and not has_evidence and updates:
                latest = max(updates.values(), key=lambda u: str(u.get("created_at", "")))
                for link in latest.get("evidence_links", []):
                    add(link, "required", ev.event_id)
            elif ev.change_type == "new_promise":
                for link in [*content.get("source_urls", []), *content.get("blog_links", [])]:
                    add(link, "required", ev.event_id)
            elif ev.change_type == "news_update" and ev.news_item_id in news_by_id:
                n = news_by_id[ev.news_item_id]
                add(n.url, "required", ev.event_id, n.published_date)
            elif ev.change_type == "coordinator_update":
                for link in ev.brief_urls or []:
                    add(link, "required", ev.event_id)

        context = [*content.get("source_urls", []), *content.get("blog_links", [])]
        for link in context[: self.cfg.max_context_sources]:
            add(link, "context", None)
        return list(found.values())

    # ------------------------------------------------------------------ fetching

    def _promise_page_source(self, url: str, content: dict[str, Any]) -> Source:
        row = self.s.scalars(select(Source).where(Source.url == url)).first()
        if row is None:
            row = Source(url=url, fetch_status="ok", checked_at=self.now)
            self.s.add(row)
        row.title = str(content.get("title") or "") or None
        row.publisher = "Promise Tracker"
        row.final_url, row.http_status, row.content_type = url, None, "application/json"
        row.checked_at = self.now
        self.s.flush()
        return row

    def _get_or_fetch(self, url: str) -> Source:
        row = self.s.scalars(select(Source).where(Source.url == url)).first()
        fresh_after = self.now - timedelta(days=self.cfg.cache_days)
        if row is not None and _aware(row.checked_at) >= fresh_after:
            self.result.cache_hits += 1
            return row
        res = self.fetcher.fetch(url)
        self.result.fetched += 1
        status: str = res.status
        title = published = publisher = snippet = None
        if res.status == "ok":
            ex = extract(res.body, res.content_type)
            title, published, publisher = ex.title, ex.published_date, ex.publisher
            snippet = ex.text[:2000] or None
            is_html = not (res.content_type or "").startswith("application/pdf")
            if is_html and (ex.paywall_marker or len(ex.text) < self.cfg.min_text_chars):
                status = "paywalled"
        if row is None:
            row = Source(url=url, fetch_status=status, checked_at=self.now)
            self.s.add(row)
        row.final_url, row.http_status, row.content_type = (
            res.final_url,
            res.http_status,
            res.content_type,
        )
        row.fetch_status, row.error = status, res.error
        row.title, row.published_date, row.publisher, row.snippet = (
            title,
            published,
            publisher,
            snippet,
        )
        row.search_confirmed, row.search_title, row.search_checked_at = False, None, None
        row.checked_at = self.now
        self.s.flush()
        log.info("source_fetched", url=url, status=status, http=res.http_status)
        return row

    def _search_confirm(self, row: Source, title_hint: str) -> None:
        domain = host_of(row.url)
        slug = row.url.rstrip("/").rsplit("/", 1)[-1].replace("-", " ")
        system, user = self._search.render(
            domain=domain, title_hint=title_hint or slug, url=row.url
        )
        self.result.llm_calls += 1
        try:
            resp = self.llm.run(
                LLMRequest(
                    task="verifier",
                    prompt_name=self._search.name,
                    system=system,
                    user=user,
                    schema=SEARCH_SCHEMA,
                    tools=("WebSearch",),
                    subject_id=f"source:{row.source_id}",
                )
            )
        except LLMAuthError:
            raise
        except LLMError as exc:
            raise DeferredError(f"search confirm failed: {exc}") from exc
        row.search_checked_at = self.now  # cache negative results too
        for hit in resp.search_hits:
            if _same_article(hit.url, row.url):
                row.search_confirmed, row.search_title = True, hit.title or None
                break
        self.s.flush()

    # ------------------------------------------------------------------ judging

    def _judge_relevance(
        self,
        meter: Meter,
        content: dict[str, Any],
        events: list[ChangeEvent],
        news_by_id: dict[int, NewsItem],
        srcs: list[_Src],
    ) -> None:
        judgeable: dict[str, _Src] = {}
        lines = []
        for src in srcs:
            row = src.row
            assert row is not None
            if src.role == "promise_page":
                continue
            ok = row.fetch_status == "ok"
            confirmed = row.fetch_status in ("blocked", "paywalled") and row.search_confirmed
            if not (ok or confirmed):
                continue
            ref = f"S{len(judgeable) + 1}"
            judgeable[ref] = src
            title = row.title or row.search_title or "(no title)"
            text = (row.snippet or "")[:700] if ok else "(page blocked our reader; title only)"
            lines.append(
                f"{ref}: {title} | publisher: {row.publisher or host_of(row.url)} | "
                f"date: {row.published_date or 'unknown'}\n    {text}"
            )
        if not judgeable:
            return
        updates = {u["update_id"]: u for u in content.get("updates") or [] if "update_id" in u}
        changes = "\n".join(
            f"- {_describe(e, news_by_id.get(e.news_item_id or -1), updates)}" for e in events
        )
        system, user = self._relevance.render(
            promise_title=str(content.get("title", "")),
            meter_name=meter.name,
            changes=changes,
            sources="\n".join(lines),
        )
        self.result.llm_calls += 1
        try:
            resp = self.llm.run(
                LLMRequest(
                    task="verifier",
                    prompt_name=self._relevance.name,
                    system=system,
                    user=user,
                    schema=RELEVANCE_SCHEMA,
                    subject_id=str(events[0].article_group_id) if events else None,
                )
            )
        except LLMAuthError:
            raise
        except LLMError as exc:
            raise DeferredError(f"relevance check failed: {exc}") from exc
        for item in resp.output.get("sources") or []:
            if not isinstance(item, dict):
                continue
            judged = judgeable.get(str(item.get("source_ref", "")))
            if judged is None:
                continue
            src = judged
            try:
                src.confidence = max(0.0, min(1.0, float(item.get("confidence", 0))))
            except (TypeError, ValueError):
                src.confidence = 0.0
            src.relevant = bool(item.get("relevant"))
            src.reason = str(item.get("reason", ""))[:300]

    def _verdicts(self, srcs: list[_Src], notes: list[str]) -> None:
        for src in srcs:
            row = src.row
            assert row is not None
            if src.role == "promise_page":
                src.verdict = "verified"
                continue
            if row.fetch_status in ("broken", "unsafe", "error"):
                src.verdict = "broken"
                continue
            relevant = (
                bool(src.relevant) and (src.confidence or 0) >= self.cfg.relevance_min_confidence
            )
            if row.fetch_status == "ok":
                src.verdict = (
                    "verified" if relevant and (row.title or row.published_date) else "unverifiable"
                )
                if (
                    src.verdict == "verified"
                    and src.claimed_date
                    and row.published_date
                    and (src.claimed_date - row.published_date).days > self.cfg.date_tolerance_days
                ):
                    src.verdict = "unverifiable"
                    notes.append(
                        f"date_mismatch: {row.url} published {row.published_date}, "
                        f"News Scout claimed {src.claimed_date}"
                    )
            elif row.fetch_status in ("blocked", "paywalled") and src.confirmed is not None:
                # A person opened the page and confirmed it; that outranks a title-only guess.
                src.verdict, src.relevant, src.confidence = "verified_partial", True, 1.0
                src.reason = COORDINATOR_CONFIRMED
            elif row.fetch_status in ("blocked", "paywalled"):
                src.verdict = (
                    "verified_partial" if row.search_confirmed and relevant else "unverifiable"
                )
                if not row.search_confirmed:
                    notes.append(
                        f"blocked: {row.url} blocked the agent's reader and search didn't find "
                        "it. Open it and confirm it if it's genuine."
                    )
            else:
                src.verdict = "unverifiable"

    def _decide(
        self, group: ArticleGroup, events: list[ChangeEvent], srcs: list[_Src], notes: list[str]
    ) -> None:
        citable = {id(s) for s in srcs if s.verdict in ("verified", "verified_partial")}
        promise_ok = self._promise_present(group.promise_id)
        if not promise_ok:
            notes.append("promise no longer present on the tracker")

        remaining = []
        for ev in events:
            own = [s for s in srcs if s.event_id == ev.event_id]
            if ev.change_type == "news_update" and not any(id(s) in citable for s in own):
                ev.state = "needs_sources"
                notes.append(f"news_update {ev.event_id}: news URL not verifiable")
                continue
            remaining.append(ev)

        remaining_ids = {e.event_id for e in remaining}
        sufficient = promise_ok and any(
            id(s) in citable and s.role == "required" and s.event_id in remaining_ids for s in srcs
        )
        for s in srcs:
            assert s.row is not None
            existing = self.s.get(ArticleGroupSource, (group.group_id, s.row.source_id))
            if existing is not None:
                self.s.delete(existing)
                self.s.flush()
            self.s.add(
                ArticleGroupSource(
                    group_id=group.group_id,
                    source_id=s.row.source_id,
                    role=s.role,
                    event_id=s.event_id,
                    relevant=s.relevant,
                    relevance_confidence=s.confidence,
                    reason=s.reason,
                    verdict=s.verdict,
                    citable=id(s) in citable,
                    confirmed_by=s.confirmed[0] if s.confirmed else None,
                    confirmed_at=s.confirmed[1] if s.confirmed else None,
                    confirmed_title=s.confirmed[2] if s.confirmed else None,
                )
            )
        group.sufficient = sufficient
        group.notes = notes
        group.verified_at = self.now
        if sufficient:
            group.state = "verified"
            self.result.verified += 1
        else:
            group.state = "needs_sources"
            for ev in remaining:
                ev.state = "needs_sources"
            self.result.needs_sources += 1
        self.s.flush()
        log.info(
            "group_verified",
            group_id=str(group.group_id),
            state=group.state,
            sources={s.url: s.verdict for s in srcs},
        )

    def _promise_present(self, promise_id: uuid.UUID) -> bool:
        idx = self.s.get(PromiseIndex, promise_id)
        return bool(idx and idx.present)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
