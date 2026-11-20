"""Publisher Agent (PRD §3.7 steps 5-6): approved articles → static HTML site.

- ``publish_approved``: ``approved`` articles become ``published`` (and their group/events).
  Only coordinator-approved articles ever reach the site (AGENTS.md rule 3).
- ``render_site``: renders every published article into ``index.html`` + one page per
  meter, newest first. Markdown is rendered with raw HTML DISABLED (article text can't
  inject markup); every link gets ``rel="noopener noreferrer"``. The site is built in a
  temp dir and swapped in atomically, so a failed render leaves the previous site intact.
"""

from __future__ import annotations

import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markdown_it import MarkdownIt
from markupsafe import Markup
from sqlalchemy import select
from sqlalchemy.orm import Session

from promise_monitor.config import AppConfig
from promise_monitor.db.models import Article, ArticleGroup, ChangeEvent, Meter
from promise_monitor.domain.qa import STATUS_LABELS
from promise_monitor.logging_setup import get_logger

log = get_logger(__name__)

TEMPLATES = Path(__file__).resolve().parents[1] / "site" / "templates"
_LABEL_TO_STATUS = {label: value for value, label in STATUS_LABELS.items()}


MD = MarkdownIt("commonmark", {"html": False, "linkify": False})


def render_markdown(text: str) -> str:
    """Markdown → HTML with raw HTML disabled; every link gets rel="noopener noreferrer"."""
    env: dict[str, Any] = {}
    tokens = MD.parse(text, env)
    for tok in tokens:
        for child in tok.children or []:
            if child.type == "link_open":
                child.attrSet("rel", "noopener noreferrer")
    return str(MD.renderer.render(tokens, MD.options, env))


def slug_for(meter: Meter) -> str:
    base = meter.slug or meter.name
    s = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")[:60] or "meter"
    return f"{s}-{str(meter.id)[:8]}"


@dataclass
class PublishResult:
    published: list[uuid.UUID] = field(default_factory=list)
    pages: int = 0
    out_dir: Path | None = None


def publish_approved(session: Session, now: datetime) -> list[uuid.UUID]:
    out = []
    for art in session.scalars(
        select(Article).where(Article.state == "approved").order_by(Article.approved_at)
    ):
        art.state = "published"
        art.published_at = now
        art.body_html = render_markdown(art.body_markdown)
        group = session.get(ArticleGroup, art.article_group_id)
        if group is not None:
            group.state = "published"
        for ev in session.scalars(
            select(ChangeEvent).where(
                ChangeEvent.event_id.in_([uuid.UUID(e) for e in art.event_ids])
            )
        ):
            ev.state = "published"
        out.append(art.article_id)
        log.info("article_published", article_id=str(art.article_id))
    session.flush()
    return out


def _article_view(art: Article, tz: ZoneInfo) -> dict[str, Any]:
    wi = art.writer_input or {}
    current_label = str((wi.get("promise") or {}).get("current_status") or "Not Yet Rated")
    old_label = next(
        (
            str(c.get("old_status"))
            for c in wi.get("changes") or []
            if c.get("type") == "status_change"
        ),
        None,
    )
    linked = set(re.findall(r"\]\(\s*<?([^)\s>]+)", art.body_markdown))
    sources = [
        {
            "url": s.get("url"),
            "title": s.get("title"),
            "partial": s.get("verdict") == "verified_partial",
        }
        for s in wi.get("sources") or []
        if s.get("url") and (s.get("url") in linked or "promisetracker.ng" not in str(s.get("url")))
    ]
    when = (art.published_at or art.created_at).astimezone(tz)
    return {
        "id": str(art.article_id),
        "headline": art.headline,
        "body_html": Markup(art.body_html or render_markdown(art.body_markdown)),  # noqa: S704 (markdown-it, html disabled)
        "status": _LABEL_TO_STATUS.get(current_label, "not_yet_rated"),
        "status_label": current_label,
        "old_label": old_label,
        "is_news": any(c.get("type") == "news_update" for c in wi.get("changes") or []),
        "sources": sources,
        "date": f"{when.day} {when:%B %Y}",
        "iso": when.isoformat(),
    }


def render_site(
    session: Session, cfg: AppConfig, now: datetime, out_dir: Path | None = None
) -> PublishResult:
    tz = ZoneInfo(cfg.tunables.timezone_display)
    site_cfg = cfg.tunables.site
    target = Path(out_dir or site_cfg.out_dir).resolve()
    env = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    generated = f"{now.astimezone(tz):%d %B %Y, %H:%M}"
    common = {"site": site_cfg, "generated": generated}

    published = list(
        session.scalars(
            select(Article)
            .where(Article.state == "published")
            .order_by(Article.published_at.desc())
        )
    )
    by_meter: dict[uuid.UUID, list[Article]] = {}
    for art in published:
        by_meter.setdefault(art.meter_id, []).append(art)
    meters = {m.id: m for m in session.scalars(select(Meter).where(Meter.id.in_(list(by_meter))))}

    result = PublishResult(out_dir=target)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".{target.name}-", dir=target.parent))
    tmp.chmod(0o755)  # mkdtemp is 0700; the site must be readable by a web server
    try:
        (tmp / "meters").mkdir()
        shutil.copyfile(TEMPLATES / "style.css", tmp / "style.css")
        index_rows = []
        for meter_id, arts in sorted(
            by_meter.items(), key=lambda kv: (meters[kv[0]].priority_rank, meters[kv[0]].name)
        ):
            meter = meters[meter_id]
            views = [_article_view(a, tz) for a in arts]
            slug = slug_for(meter)
            page = env.get_template("meter.html.j2").render(
                **common, root="../", meter=meter, articles=views
            )
            (tmp / "meters" / f"{slug}.html").write_text(page, encoding="utf-8")
            index_rows.append(
                {"name": meter.name, "slug": slug, "count": len(arts), "latest": views[0]["date"]}
            )
            result.pages += 1
        (tmp / "index.html").write_text(
            env.get_template("index.html.j2").render(**common, root="", meters=index_rows),
            encoding="utf-8",
        )
        result.pages += 1
        old = target.with_name(f".{target.name}-old-{uuid.uuid4().hex[:6]}")
        if target.exists():
            target.rename(old)
        tmp.rename(target)
        if old.exists():
            shutil.rmtree(old)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    log.info("site_rendered", pages=result.pages, articles=len(published), out=str(target))
    return result
