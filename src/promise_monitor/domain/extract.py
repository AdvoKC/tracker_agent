"""Extract title / published date / publisher / readable text from HTML and PDF sources."""

from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from selectolax.lexbor import LexborHTMLParser

from promise_monitor.domain.normalize import clean_text

_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_PDF_DATE_RE = re.compile(r"D:(\d{4})(\d{2})(\d{2})")
_PAYWALL_MARKERS = (
    "subscribe to continue",
    "subscribe to read",
    "subscribers only",
    "for subscribers",
    "premium content",
    "sign in to continue reading",
    "create a free account to continue",
)
_META_DATE_KEYS = (
    ("property", "article:published_time"),
    ("property", "og:article:published_time"),
    ("name", "article:published_time"),
    ("itemprop", "datePublished"),
    ("name", "pubdate"),
    ("name", "publishdate"),
    ("name", "publish-date"),
    ("name", "date"),
    ("name", "DC.date.issued"),
    ("name", "dcterms.date"),
    ("name", "parsely-pub-date"),
    ("name", "sailthru.date"),
)


@dataclass(frozen=True)
class Extracted:
    title: str | None
    published_date: date | None
    publisher: str | None
    text: str
    paywall_marker: bool


def parse_iso_date(raw: str | None) -> date | None:
    if not raw:
        return None
    m = _DATE_RE.search(raw)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _meta(tree: LexborHTMLParser, attr: str, key: str) -> str | None:
    for node in tree.css(f"meta[{attr}]"):
        if (node.attributes.get(attr) or "").lower() == key.lower():
            content = node.attributes.get("content")
            if content and content.strip():
                return content.strip()
    return None


def _jsonld_dates(tree: LexborHTMLParser) -> list[str]:
    found: list[str] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == "datePublished" and isinstance(v, str):
                    found.append(v)
                else:
                    walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    for node in tree.css('script[type="application/ld+json"]'):
        try:
            walk(json.loads(node.text() or ""))
        except ValueError:
            continue
    return found


def _node_text(tree: LexborHTMLParser, selector: str) -> str | None:
    node = tree.css_first(selector)
    return clean_text(node.text()) if node is not None else None


def extract_html(body: bytes) -> Extracted:
    tree = LexborHTMLParser(body.decode("utf-8", errors="replace"))
    title = (
        _meta(tree, "property", "og:title")
        or _meta(tree, "name", "twitter:title")
        or _node_text(tree, "title")
        or _node_text(tree, "h1")
    )
    published = None
    for attr, key in _META_DATE_KEYS:
        published = parse_iso_date(_meta(tree, attr, key))
        if published:
            break
    if published is None:
        for raw in _jsonld_dates(tree):
            published = parse_iso_date(raw)
            if published:
                break
    if published is None:
        t = tree.css_first("time[datetime]")
        published = parse_iso_date(t.attributes.get("datetime") if t else None)

    publisher = _meta(tree, "property", "og:site_name")
    for sel in ("script", "style", "nav", "header", "footer", "aside", "noscript", "form"):
        for node in tree.css(sel):
            node.decompose()
    container = tree.css_first("article") or tree.body
    paragraphs = [clean_text(p.text()) for p in container.css("p")] if container else []
    text = (
        " ".join(p for p in paragraphs if p)
        or clean_text(container.text() if container else "")
        or ""
    )
    low = (tree.html or "").lower()
    marker = any(m in low for m in _PAYWALL_MARKERS) or 'class="paywall' in low
    return Extracted(clean_text(title), published, clean_text(publisher), text[:5000], marker)


def extract_pdf(body: bytes) -> Extracted:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(body))
        meta: dict[str, Any] = dict(reader.metadata or {})
        title = clean_text(str(meta.get("/Title") or ""))
        raw_date = str(meta.get("/CreationDate") or "")
        m = _PDF_DATE_RE.search(raw_date)
        published = None
        if m:
            try:
                published = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                published = None
        text_parts = []
        for page in reader.pages[:2]:
            text_parts.append(page.extract_text() or "")
        text = clean_text(" ".join(text_parts)) or ""
    except (PdfReadError, ValueError, KeyError, OSError):
        return Extracted(None, None, None, "", False)
    if not title and text:
        title = text[:120]
    return Extracted(title, published, None, text[:5000], False)


def extract(body: bytes, content_type: str | None) -> Extracted:
    if (content_type or "").startswith("application/pdf") or body[:5] == b"%PDF-":
        return extract_pdf(body)
    return extract_html(body)
