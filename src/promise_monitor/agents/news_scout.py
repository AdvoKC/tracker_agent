"""News Scout Agent (PRD §3.9): Claude web search for concrete developments on promises.

Usage control: promises are searched in batches (``news.batch_size`` per Claude call), at
most ``news.max_calls_per_run`` calls per run, least-recently-searched first, then meter
priority. A batch whose call fails is not marked as searched, so it goes first next time.

Nothing the model returns is trusted. Each result is checked IN CODE:
- the promise reference must be one we sent;
- GROUNDING: the URL must appear in the RAW WebSearch tool results of that same call
  (``LLMResponse.search_hits``, produced by the search tool, not the model). This stops
  invented or altered URLs; found live on 2026-11-17 (a non-existent Channels TV link);
- the URL must be well-formed and on the outlet allowlist (config/news_sources.yaml);
- ``published_date`` must be a full date, after the promise's cutoff, and not in the future;
- it must be a concrete development with relevance ≥ ``news.min_relevance``;
- a URL already stored for that promise is a duplicate;
- at most ``max_results_per_promise`` are accepted per promise per run;
- one URL is accepted for ONE promise only (highest relevance; earlier runs win), so a story
  that touches several promises yields one article, not several near-duplicates.
Every result is stored in ``news_items`` with ``accepted``/``rejected`` and a reason.
Accepted items still go through source verification (P4) before any article cites them.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from promise_monitor.agents.classifier import STATUS_LABELS, NewsFinding
from promise_monitor.config import NewsConfig, NewsSourcesConfig
from promise_monitor.db.models import (
    Meter,
    NewsItem,
    NewsSearchState,
    PromiseIndex,
    PromiseSnapshot,
)
from promise_monitor.domain.urls import allowed_domains_text, allowed_outlet, normalize_url
from promise_monitor.llm.base import LLMAuthError, LLMClient, LLMError, LLMRequest
from promise_monitor.llm.prompts import load_prompt
from promise_monitor.logging_setup import get_logger

log = get_logger(__name__)

RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "search_queries": {"type": "array", "items": {"type": "string"}},
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "promise_ref": {"type": "string"},
                    "url": {"type": "string"},
                    "outlet": {"type": "string"},
                    "headline": {"type": "string"},
                    "published_date": {"type": "string"},
                    "summary": {"type": "string"},
                    "development_type": {"type": "string"},
                    "is_concrete_development": {"type": "boolean"},
                    "relevance": {"type": "number"},
                },
                "required": [
                    "promise_ref",
                    "url",
                    "outlet",
                    "headline",
                    "published_date",
                    "summary",
                    "development_type",
                    "is_concrete_development",
                    "relevance",
                ],
            },
        },
    },
    "required": ["search_queries", "results"],
}


@dataclass(frozen=True)
class Candidate:
    promise_id: uuid.UUID
    meter_id: uuid.UUID
    meter_name: str
    title: str
    status: str
    cutoff: date  # only news published after this date counts


@dataclass
class ScoutResult:
    findings: dict[uuid.UUID, list[NewsFinding]] = field(default_factory=dict)  # by meter
    calls: int = 0
    failed_calls: int = 0
    searched: int = 0
    accepted: int = 0
    rejected: dict[str, int] = field(default_factory=dict)
    auth_failed: bool = False


def last_tracker_update(content: dict[str, Any]) -> date | None:
    dates = [str(u.get("created_at", ""))[:10] for u in content.get("updates") or []]
    parsed = [date.fromisoformat(d) for d in dates if len(d) == 10]
    return max(parsed) if parsed else None


def select_candidates(
    session: Session, limit: int, today: date, cfg: NewsConfig
) -> list[Candidate]:
    """Present promises on active, baselined meters; least-recently-searched first."""
    rows = session.execute(
        select(PromiseIndex, Meter, NewsSearchState.last_searched_at)
        .join(Meter, Meter.id == PromiseIndex.meter_id)
        .outerjoin(NewsSearchState, NewsSearchState.promise_id == PromiseIndex.promise_id)
        .where(
            PromiseIndex.present.is_(True),
            Meter.registry_status == "active",
            Meter.baseline_pending.is_(False),
        )
        .order_by(
            NewsSearchState.last_searched_at.asc().nulls_first(),
            Meter.priority_rank,
            PromiseIndex.promise_id,
        )
        .limit(limit)
    ).all()
    if not rows:
        return []
    snap_ids = [pi.latest_snapshot_id for pi, _m, _t in rows]
    contents: dict[int, dict[str, Any]] = dict(
        session.execute(
            select(PromiseSnapshot.snapshot_id, PromiseSnapshot.content).where(
                PromiseSnapshot.snapshot_id.in_(snap_ids)
            )
        ).all()
    )
    floor = today - timedelta(days=cfg.lookback_days)
    out = []
    for pi, meter, _t in rows:
        content = contents.get(pi.latest_snapshot_id, {})
        last = last_tracker_update(content)
        out.append(
            Candidate(
                promise_id=pi.promise_id,
                meter_id=meter.id,
                meter_name=meter.name,
                title=str(content.get("title", "")),
                status=str(content.get("status", "")),
                cutoff=max(floor, last) if last else floor,
            )
        )
    return out


def _promise_block(refs: dict[str, Candidate]) -> str:
    return "\n".join(
        f"{ref}: [{c.meter_name}] {c.title} | current Promise Tracker status: "
        f"{STATUS_LABELS.get(c.status, c.status)} | cutoff: news published after {c.cutoff}"
        for ref, c in refs.items()
    )


def _reject_reason(
    r: dict[str, Any],
    c: Candidate,
    url: str,
    published: date | None,
    sources: NewsSourcesConfig,
    cfg: NewsConfig,
    today: date,
) -> str | None:
    if allowed_outlet(url, sources) is None:
        return "outlet_not_allowed"
    if published is None:
        return "date_imprecise"
    if published <= c.cutoff:
        return "too_old"
    if published > today:
        return "future_date"
    if not r.get("is_concrete_development"):
        return "not_concrete"
    if float(r.get("relevance", 0)) < cfg.min_relevance:
        return "low_relevance"
    return None


def _parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()[:10]
    if len(text) != 10:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def run_news_scout(
    session: Session,
    llm: LLMClient,
    *,
    run_id: uuid.UUID,
    now: datetime,
    cfg: NewsConfig,
    sources: NewsSourcesConfig,
    max_calls: int | None = None,
) -> ScoutResult:
    """Search news for the next batch of promises. Caller owns the transaction."""
    result = ScoutResult()
    calls = cfg.max_calls_per_run if max_calls is None else max_calls
    today = now.date()
    candidates = select_candidates(session, calls * cfg.batch_size, today, cfg)
    if not candidates:
        return result
    prompt = load_prompt("news_scout")
    domains = allowed_domains_text(sources)

    for start in range(0, len(candidates), cfg.batch_size):
        batch = candidates[start : start + cfg.batch_size]
        refs = {f"P{i + 1}": c for i, c in enumerate(batch)}
        system, user = prompt.render(
            allowed_domains=domains,
            max_per_promise=str(cfg.max_results_per_promise),
            today=today.isoformat(),
            promises=_promise_block(refs),
        )
        result.calls += 1
        try:
            resp = llm.run(
                LLMRequest(
                    task="news_scout",
                    prompt_name=prompt.name,
                    system=system,
                    user=user,
                    schema=RESULT_SCHEMA,
                    tools=("WebSearch",),
                    subject_id=f"batch:{start // cfg.batch_size}",
                )
            )
        except LLMAuthError as exc:
            result.failed_calls += 1
            result.auth_failed = True
            log.error("news_scout_auth_failed", error=str(exc))
            break
        except LLMError as exc:
            result.failed_calls += 1
            log.warning("news_scout_call_failed", error=str(exc))
            continue

        grounding = {
            u: h.title for h in resp.search_hits if (u := normalize_url(h.url)) is not None
        }
        _store_results(
            session, resp.output, refs, run_id, now, cfg, sources, today, result, grounding
        )
        for c in batch:
            state = session.get(NewsSearchState, c.promise_id)
            if state is None:
                session.add(
                    NewsSearchState(
                        promise_id=c.promise_id, last_searched_at=now, last_run_id=run_id
                    )
                )
            else:
                state.last_searched_at, state.last_run_id = now, run_id
        result.searched += len(batch)
        session.flush()

    log.info(
        "news_scout_done",
        calls=result.calls,
        failed=result.failed_calls,
        searched=result.searched,
        accepted=result.accepted,
        rejected=result.rejected,
    )
    return result


def _store_results(
    session: Session,
    output: dict[str, Any],
    refs: dict[str, Candidate],
    run_id: uuid.UUID,
    now: datetime,
    cfg: NewsConfig,
    sources: NewsSourcesConfig,
    today: date,
    result: ScoutResult,
    grounding: dict[str, str],
) -> None:
    raw_results = output.get("results")
    if not isinstance(raw_results, list):
        return
    known = {
        (pid, url)
        for pid, url in session.execute(
            select(NewsItem.promise_id, NewsItem.url).where(
                NewsItem.promise_id.in_([c.promise_id for c in refs.values()])
            )
        )
    }
    accepted_per_promise: dict[uuid.UUID, int] = defaultdict(int)
    # One story → one promise → one article. Highest relevance wins within the batch;
    # a URL already accepted for any promise on an earlier run is never accepted again.
    candidate_urls = {
        normalize_url(str(r.get("url", ""))) for r in raw_results if isinstance(r, dict)
    }
    claimed_urls = set(
        session.scalars(
            select(NewsItem.url).where(
                NewsItem.state == "accepted", NewsItem.url.in_([u for u in candidate_urls if u])
            )
        )
    )

    def _relevance(r: object) -> float:
        try:
            return float(r.get("relevance", 0) or 0) if isinstance(r, dict) else 0.0
        except (TypeError, ValueError):
            return 0.0

    for r in sorted(raw_results, key=_relevance, reverse=True):
        if not isinstance(r, dict):
            continue
        c = refs.get(str(r.get("promise_ref", "")).strip())
        if c is None:
            result.rejected["unknown_promise_ref"] = (
                result.rejected.get("unknown_promise_ref", 0) + 1
            )
            continue
        url = normalize_url(str(r.get("url", "")))
        if url is None:
            result.rejected["bad_url"] = result.rejected.get("bad_url", 0) + 1
            continue
        if (c.promise_id, url) in known:
            continue  # stored on an earlier run (accepted or rejected): already audited
        known.add((c.promise_id, url))
        published = _parse_date(r.get("published_date"))
        reason = (
            "not_in_search_results"
            if url not in grounding
            else _reject_reason(r, c, url, published, sources, cfg, today)
        )
        if reason is None and url in claimed_urls:
            reason = "same_story_other_promise"
        if reason is None and accepted_per_promise[c.promise_id] >= cfg.max_results_per_promise:
            reason = "over_limit"

        outlet = allowed_outlet(url, sources) or str(r.get("outlet", ""))[:128]
        item = NewsItem(
            run_id=run_id,
            promise_id=c.promise_id,
            meter_id=c.meter_id,
            url=url,
            outlet=outlet,
            headline=str(r.get("headline", ""))[:500],
            published_date=published,
            summary=str(r.get("summary", ""))[:1000],
            development_type=str(r.get("development_type", ""))[:64] or None,
            is_concrete=bool(r.get("is_concrete_development")),
            relevance=float(r.get("relevance", 0) or 0),
            search_grounded=url in grounding,
            search_title=grounding.get(url),
            state="rejected" if reason else "accepted",
            reject_reason=reason,
            found_at=now,
        )
        session.add(item)
        session.flush()
        if reason:
            result.rejected[reason] = result.rejected.get(reason, 0) + 1
            continue
        assert published is not None
        claimed_urls.add(url)
        accepted_per_promise[c.promise_id] += 1
        result.accepted += 1
        result.findings.setdefault(c.meter_id, []).append(
            NewsFinding(
                news_item_id=item.id,
                promise_id=c.promise_id,
                meter_id=c.meter_id,
                url=url,
                outlet=outlet,
                headline=item.headline,
                published_date=published,
                summary=item.summary,
                development_type=item.development_type or "",
                relevance=item.relevance,
            )
        )
