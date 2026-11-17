"""Raw API promise → normalized, hashable promise content (PRD §3.2 step 3).

Only published content survives. Volatile bookkeeping fields (update_count, timestamps
of the promise row, images) are excluded so they can't cause false diffs.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from promise_monitor.api.models import ApiPromiseDetail, Status

_WS = re.compile(r"\s+")
_URL_IN_TEXT = re.compile(r"https?://[^\s<>\"')\]]+")
_TRAILING_PUNCT = ".,;:!?"


class NormalizedUpdate(BaseModel):
    model_config = ConfigDict(frozen=True)

    update_id: str
    status: Status | None
    update_type: str | None
    created_at: str  # ISO-8601 UTC
    text: str
    evidence_links: tuple[str, ...]


class NormalizedPromise(BaseModel):
    model_config = ConfigDict(frozen=True)

    promise_id: str
    meter_id: str
    title: str
    description: str | None
    category: str | None
    raw_subject_area: str | None
    status: Status
    priority: str | None
    source_text: str | None
    source_urls: tuple[str, ...]
    promise_start_date: str | None
    promise_end_date: str | None
    blog_text: str | None
    blog_links: tuple[str, ...]
    updates: tuple[NormalizedUpdate, ...]

    @property
    def evidence_links(self) -> tuple[str, ...]:
        """Union of every link source, in order: updates, write-up, source field."""
        return _dedupe(
            [link for u in self.updates for link in u.evidence_links]
            + list(self.blog_links)
            + list(self.source_urls)
        )

    def content(self) -> dict[str, Any]:
        """JSON-safe dict used for hashing, storage and diffing."""
        return self.model_dump(mode="json")


def clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = _WS.sub(" ", value.replace("\xa0", " ")).strip()
    return text or None


def html_to_text(html: str | None) -> str | None:
    if not html:
        return None
    tree = HTMLParser(html)
    node = tree.body or tree.root
    return clean_text(node.text(separator=" ")) if node is not None else None


def _clean_url(url: str) -> str | None:
    url = url.strip().rstrip(_TRAILING_PUNCT)
    return url if url.lower().startswith(("http://", "https://")) else None


def _dedupe(items: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(items))


def html_links(html: str | None) -> tuple[str, ...]:
    if not html:
        return ()
    found = []
    for node in HTMLParser(html).css("a[href]"):
        href = node.attributes.get("href")
        url = _clean_url(href) if href else None
        if url:
            found.append(url)
    return _dedupe(found)


def text_urls(text: str | None) -> tuple[str, ...]:
    if not text:
        return ()
    return _dedupe([u for m in _URL_IN_TEXT.findall(text) if (u := _clean_url(m))])


def normalize_category(raw: str | None) -> str | None:
    """Light cleanup only; distinct site categories are not merged (see PRD §4.2)."""
    text = clean_text(raw)
    if text is None or text.lower() in {"null", "none"}:
        return None
    return text.lower()


def to_utc_iso(raw: str) -> str:
    """API timestamps: ISO with 'Z' or naive 'YYYY-MM-DD HH:MM:SS' (assumed UTC)."""
    value = raw.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def normalize_promise(raw: ApiPromiseDetail) -> NormalizedPromise:
    updates = sorted(
        (
            NormalizedUpdate(
                update_id=str(u.id),
                status=u.status,
                update_type=clean_text(u.update_type),
                created_at=to_utc_iso(u.created_at),
                text=html_to_text(u.blog) or "",
                evidence_links=_dedupe(
                    list(html_links(u.blog)) + list(text_urls(u.rating_source_url))
                ),
            )
            for u in raw.updates
            if u.is_published
        ),
        key=lambda u: (u.created_at, u.update_id),
    )
    return NormalizedPromise(
        promise_id=str(raw.id),
        meter_id=str(raw.meter_id),
        title=clean_text(raw.title) or "",
        description=html_to_text(raw.description),
        category=normalize_category(raw.subject_area),
        raw_subject_area=raw.subject_area,
        status=raw.published_rating,
        priority=clean_text(raw.priority.lower()) if raw.priority else None,
        source_text=clean_text(raw.source),
        source_urls=text_urls(raw.source),
        promise_start_date=raw.promise_start_date.isoformat() if raw.promise_start_date else None,
        promise_end_date=raw.promise_end_date.isoformat() if raw.promise_end_date else None,
        blog_text=html_to_text(raw.promise_blog),
        blog_links=html_links(raw.promise_blog),
        updates=tuple(updates),
    )


def promise_uuid(n: NormalizedPromise) -> UUID:
    return UUID(n.promise_id)
