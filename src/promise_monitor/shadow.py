"""Shadow-period report (PRD §8 P8 exit: 14 consecutive daily runs, 0 duplicate events,
0 unhandled exceptions) plus the numbers editors need before switching publishing on."""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from itertools import combinations
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from promise_monitor.db.models import Article, ArticleGroup, ChangeEvent, LLMCall, NewsItem, Run
from promise_monitor.domain.qa import cosine

_RANK = {"ok": 3, "degraded": 2, "failed": 1, "running": 0}


@dataclass
class ShadowReport:
    days: int
    run_days: dict[date, str] = field(default_factory=dict)  # day → best status that day
    crashed_runs: list[str] = field(default_factory=list)  # runs with stage errors
    consecutive_ok_days: int = 0
    duplicates: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.consecutive_ok_days >= 14 and not self.duplicates and not self.crashed_runs


def _grouped(session: Session, col: Any, since_col: Any, since: datetime) -> dict[str, int]:
    rows = session.execute(select(col, func.count()).where(since_col >= since).group_by(col)).all()
    return {str(k): int(v) for k, v in rows}


def _runs(session: Session, rep: ShadowReport, since: datetime, today: date) -> None:
    runs = session.scalars(
        select(Run)
        .where(Run.trigger == "cli:run", Run.started_at >= since)
        .order_by(Run.started_at)
    ).all()
    for r in runs:
        d = r.started_at.date()
        if _RANK.get(r.status, 0) > _RANK.get(rep.run_days.get(d, "running"), 0):
            rep.run_days[d] = r.status
        errors = (r.stats or {}).get("stage_errors") or {}
        if errors:
            rep.crashed_runs.append(f"{r.started_at:%Y-%m-%d} {r.run_id}: {', '.join(errors)}")
    day = today if today in rep.run_days else today - timedelta(days=1)
    while rep.run_days.get(day) in ("ok", "degraded"):
        rep.consecutive_ok_days += 1
        day -= timedelta(days=1)


def _duplicates(session: Session, rep: ShadowReport, since: datetime) -> list[Article]:
    arts = list(session.scalars(select(Article).where(Article.created_at >= since)))
    by_promise: dict[uuid.UUID, list[Article]] = {}
    for a in arts:
        by_promise.setdefault(a.promise_id, []).append(a)
    for group in by_promise.values():
        for a, b in combinations(group, 2):
            if cosine(a.body_markdown, b.body_markdown) >= 0.9:
                rep.duplicates.append(f"near-identical articles {a.article_id} ~ {b.article_id}")
    # the same news story behind more than one ARTICLE (leads that were never drafted don't count)
    rows = session.execute(
        select(NewsItem.url, Article.article_id)
        .join(ChangeEvent, ChangeEvent.news_item_id == NewsItem.id)
        .join(Article, Article.article_group_id == ChangeEvent.article_group_id)
        .where(Article.created_at >= since)
    ).all()
    per_url = Counter(url for url, _aid in {(u, a) for u, a in rows})
    rep.duplicates += [
        f"one news story behind {n} articles: {u}" for u, n in per_url.items() if n > 1
    ]
    return arts


def build_report(session: Session, now: datetime, days: int = 14) -> ShadowReport:
    since = now - timedelta(days=days)
    rep = ShadowReport(days=days)
    _runs(session, rep, since, now.date())
    arts = _duplicates(session, rep, since)
    out = rep.lines

    out.append(f"Shadow report: last {days} days (since {since:%Y-%m-%d})")
    out.append("")
    out.append(
        f"Daily runs: {len(rep.run_days)} day(s) with a run; "
        f"consecutive good days: {rep.consecutive_ok_days}/14"
    )
    out += [f"  {d}  {rep.run_days[d]}" for d in sorted(rep.run_days)]
    out.append(f"Runs with stage errors (unhandled exceptions): {len(rep.crashed_runs)}")
    out += [f"  {c}" for c in rep.crashed_runs]
    out.append(f"Duplicates: {len(rep.duplicates)}")
    out += [f"  {p}" for p in rep.duplicates]
    out.append("")

    events = _grouped(session, ChangeEvent.change_type, ChangeEvent.detected_at, since)
    groups = _grouped(session, ArticleGroup.state, ArticleGroup.created_at, since)
    states = _grouped(session, Article.state, Article.created_at, since)
    news = _grouped(session, NewsItem.reject_reason, NewsItem.found_at, since)
    out.append(f"Change events by type: {events}")
    out.append(f"Article groups by state: {groups}")
    out.append(f"Articles by state: {states}")
    decided = sum(states.get(k, 0) for k in ("approved", "published", "rejected"))
    if decided:
        ok = states.get("approved", 0) + states.get("published", 0)
        out.append(f"Coordinator approval rate: {ok}/{decided} = {ok / decided:.0%}")
    if arts:
        rewrites = sum(a.rewrite_count for a in arts) / len(arts)
        flagged = sum(1 for a in arts if a.qa_flags)
        out.append(f"Writer: avg rewrites {rewrites:.2f}; needed attention {flagged}/{len(arts)}")
    out.append(f"News items by reject reason (None = accepted): {news}")

    llm = session.execute(
        select(LLMCall.agent, func.count(), func.sum(LLMCall.cost_usd))
        .where(LLMCall.created_at >= since)
        .group_by(LLMCall.agent)
    ).all()
    total = sum(float(c or 0) for _a, _n, c in llm)
    per_agent = ", ".join(f"{a}={n} (${float(c or 0):.2f})" for a, n, c in llm)
    out.append(f"LLM calls: {per_agent}; total ≈ ${total:.2f} equivalent")
    out.append("")
    out.append(
        "VERDICT: PASS (ready to set publish.enabled: true)"
        if rep.passed
        else "VERDICT: NOT YET (need 14 consecutive good days, 0 duplicates, 0 crashed runs)"
    )
    return rep
