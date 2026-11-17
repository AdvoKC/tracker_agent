"""Deterministic article QA (PRD §3.6). Every rule here is enforced in code, never only in a prompt.

Word counting rule (also stated in prompts/writer.md): markdown links count as their anchor
text; URLs never count; a "word" is a whitespace-separated token containing at least one
letter or digit (so a lone dash or bullet is not a word). The headline is not counted.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field

from promise_monitor.domain.urls import host_of, normalize_url

LINK_RE = re.compile(r"\[([^\]]+)\]\(\s*<?([^)\s>]+)>?\s*\)")
BARE_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
WORDISH = re.compile(r"[A-Za-z0-9]")
QUOTE_RE = re.compile(r"[\"“”]([^\"“”]+)[\"“”]")
BAD_ANCHORS = frozenset(
    {"here", "this", "link", "this link", "click", "click here", "source", "read more", "more"}
)
STATUS_LABELS = {
    "kept": "Kept",
    "broken": "Broken",
    "stalled": "Stalled",
    "in_the_works": "In the Works",
    "compromised": "Compromised",
    "not_yet_rated": "Not Yet Rated",
}
RATING_CHANGE_PHRASES = (
    "moved from",
    "now rated",
    "changed to",
    "changed its rating",
    "rating changed",
    "upgraded",
    "downgraded",
    "re-rated",
    "reclassified",
)
PROMISE_TRACKER_HOST = "promisetracker.ng"


@dataclass(frozen=True)
class QAContext:
    citable_urls: frozenset[str]  # normalized
    promise_url: str  # normalized
    current_status: str  # API value
    old_status: str | None  # set when the group has a status_change
    has_status_change: bool
    banned_terms: tuple[str, ...]
    min_words: int
    max_words: int
    recent_bodies: tuple[str, ...] = ()  # same promise, last N days
    dedupe_similarity: float = 0.9
    min_links: int = 2
    max_headline_words: int = 14
    max_quote_words: int = 15


@dataclass
class QAReport:
    word_count: int = 0
    links: list[tuple[str, str]] = field(default_factory=list)  # (anchor, normalized url)
    issues: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.issues


def strip_links(markdown: str) -> str:
    return LINK_RE.sub(lambda m: m.group(1), markdown)


def word_count(markdown: str) -> int:
    return sum(1 for tok in strip_links(markdown).split() if WORDISH.search(tok))


def _words(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9']+", text.lower())]


def cosine(a: str, b: str) -> float:
    ca, cb = Counter(_words(strip_links(a))), Counter(_words(strip_links(b)))
    if not ca or not cb:
        return 0.0
    dot = sum(ca[w] * cb[w] for w in ca.keys() & cb.keys())
    return dot / (
        math.sqrt(sum(v * v for v in ca.values())) * math.sqrt(sum(v * v for v in cb.values()))
    )


def _contains_term(text: str, term: str) -> bool:
    return (
        re.search(rf"(?<![A-Za-z]){re.escape(term)}(?![A-Za-z])", text, re.IGNORECASE) is not None
    )


def mentioned_statuses(text: str) -> set[str]:
    found = set()
    low = text.lower()
    for value, label in STATUS_LABELS.items():
        if re.search(rf"(?<![a-z]){re.escape(label.lower())}(?![a-z])", low):
            found.add(value)
    # "In the Works" contains no other label; "Not Yet Rated" likewise — no overlap handling needed
    return found


def check_article(headline: str, body: str, ctx: QAContext) -> QAReport:
    rep = QAReport()
    issues = rep.issues

    # 1. length --------------------------------------------------------------------------
    rep.word_count = word_count(body)
    if not ctx.min_words <= rep.word_count <= ctx.max_words:
        issues.append(
            f"word_count: body has {rep.word_count} words; "
            f"it must be {ctx.min_words}-{ctx.max_words} "
            f"(anchor text counts, URLs do not, headline excluded)"
        )

    # 2. headline --------------------------------------------------------------------------
    h_words = len([t for t in headline.split() if WORDISH.search(t)])
    if not headline.strip():
        issues.append("headline: missing")
    elif h_words > ctx.max_headline_words or len(headline) > 120:
        issues.append(
            f"headline: {h_words} words; at most {ctx.max_headline_words} and 120 characters"
        )
    if any(ch in headline for ch in "?!"):
        issues.append("headline: no question or exclamation marks")
    if LINK_RE.search(headline) or BARE_URL_RE.search(headline):
        issues.append("headline: no links or URLs in the headline")

    # 3. links -------------------------------------------------------------------------------
    for anchor, raw in LINK_RE.findall(body):
        rep.links.append((anchor.strip(), normalize_url(raw) or raw))
    if len(rep.links) < ctx.min_links:
        issues.append(f"links: {len(rep.links)} link(s); at least {ctx.min_links} required")
    for anchor, url in rep.links:
        if url not in ctx.citable_urls:
            issues.append(
                f"links: {url} is not in the verified source list; use only provided URLs"
            )
        a = anchor.lower().strip(" .")
        if a in BAD_ANCHORS or len([t for t in anchor.split() if WORDISH.search(t)]) < 2:
            issues.append(
                f"links: anchor text '{anchor}' must be descriptive (2+ words, not 'here')"
            )
        if BARE_URL_RE.fullmatch(anchor.strip()):
            issues.append(f"links: anchor text '{anchor}' is a bare URL")
    urls = {u for _a, u in rep.links}
    if ctx.promise_url not in urls:
        issues.append("links: must link the Promise Tracker promise page")
    if not any(
        host_of(u) != PROMISE_TRACKER_HOST and not host_of(u).endswith("." + PROMISE_TRACKER_HOST)
        for u in urls
    ):
        issues.append("links: must link at least one external verified source")
    if BARE_URL_RE.search(strip_links(body)):
        issues.append("links: bare URL in text; every URL must be inside a markdown link")

    # 4. status wording ---------------------------------------------------------------------
    text = strip_links(body) + " " + headline
    allowed = {ctx.current_status} | ({ctx.old_status} if ctx.old_status else set())
    stray = mentioned_statuses(text) - allowed
    if stray:
        labels = ", ".join(STATUS_LABELS[s] for s in sorted(stray))
        issues.append(f"status: mentions rating(s) {labels} that do not apply to this promise")
    if STATUS_LABELS[ctx.current_status].lower() not in text.lower():
        issues.append(
            "status: must state the current Promise Tracker rating, "
            f"'{STATUS_LABELS[ctx.current_status]}'"
        )
    if not ctx.has_status_change:
        low = text.lower()
        hit = next((p for p in RATING_CHANGE_PHRASES if p in low), None)
        if hit:
            issues.append(f"status: the rating did not change; remove '{hit}'")

    # 5. banned terms -------------------------------------------------------------------------
    for term in ctx.banned_terms:
        if _contains_term(text, term):
            issues.append(f"neutrality: remove loaded term '{term}'")

    # 6. quotes ----------------------------------------------------------------------------------
    for q in QUOTE_RE.findall(strip_links(body)):
        if word_count(q) > ctx.max_quote_words:
            issues.append(
                f"quotes: a quotation of {word_count(q)} words; "
                f"paraphrase or keep it to {ctx.max_quote_words}"
            )

    # 7. near-duplicates -------------------------------------------------------------------------
    for prev in ctx.recent_bodies:
        sim = cosine(body, prev)
        if sim >= ctx.dedupe_similarity:
            issues.append(
                f"dedupe: {sim:.0%} similar to a recent article on this promise; report what is new"
            )
            break
    return rep


def citable_set(urls: Iterable[str]) -> frozenset[str]:
    return frozenset(u for raw in urls if (u := normalize_url(raw)))
