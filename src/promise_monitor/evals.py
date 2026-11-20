"""Writer eval (PRD §8 P5 exit: ≥90% of fixture groups pass QA within 3 rewrites).

Cases are built from the recorded API fixtures (real promise titles, update texts and
evidence links), alternating new-evidence and status-change framings, plus two
synthetic news_update cases (one verified, one verified_partial) on illustrative
example.org URLs. Nothing is written to the database; nothing here is publishable.
"""

from __future__ import annotations

import glob
import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from promise_monitor.api.models import ApiPromiseDetail
from promise_monitor.config import PROJECT_ROOT
from promise_monitor.domain.normalize import normalize_promise
from promise_monitor.domain.qa import STATUS_LABELS, QAContext
from promise_monitor.domain.urls import normalize_url
from promise_monitor.house_style import eligible_example

FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "api"


@dataclass(frozen=True)
class EvalCase:
    name: str
    writer_input: dict[str, Any]
    current_status: str
    old_status: str | None
    examples: list[str] = field(default_factory=list)  # real AdvoKC updates (style only)


def _first_line(text: str) -> str:
    return text.split(". ")[0][:140]


def _fixture_examples(fixtures: Path) -> list[tuple[str, str]]:
    """(promise_id, text) of real AdvoKC updates: the style corpus + the recorded fixtures."""
    out = []
    corpus = fixtures.parent / "house_style_examples.json"
    if corpus.exists():
        for e in json.loads(corpus.read_text(encoding="utf-8")).get("examples", []):
            out.append((str(e["promise_id"]), str(e["text"])))
    for f in sorted(glob.glob(str(fixtures / "*.json"))):
        try:
            data = json.loads(Path(f).read_text(encoding="utf-8")).get("data")
        except (ValueError, AttributeError):
            continue
        if not isinstance(data, dict) or "updates" not in data:
            continue
        n = normalize_promise(ApiPromiseDetail.model_validate(data))
        for u in n.updates:
            text = " ".join(u.text.split())
            if eligible_example(text):
                out.append((n.promise_id, text))
    return out


def build_cases(fixtures: Path = FIXTURES) -> list[EvalCase]:
    cases = _build_cases(fixtures)
    pool = _fixture_examples(fixtures)
    with_examples = []
    for c in cases:
        pid = c.writer_input["promise"]["promise_url"].rsplit("/", 1)[-1]
        examples = [t for p, t in pool if p != pid][:4]
        with_examples.append(replace(c, examples=examples))
    return with_examples


def _build_cases(fixtures: Path = FIXTURES) -> list[EvalCase]:
    files = [*sorted(glob.glob(str(fixtures / "p_*.json"))), str(fixtures / "promise.json")]
    cases: list[EvalCase] = []
    for i, f in enumerate(files):
        raw = json.loads(Path(f).read_text(encoding="utf-8"))["data"]
        n = normalize_promise(ApiPromiseDetail.model_validate(raw))
        if not n.updates:
            continue
        u = n.updates[-1]
        externals = list(u.evidence_links) + list(n.source_urls)
        if not externals:
            continue
        promise_url = f"https://promisetracker.ng/blog/{n.promise_id}"
        sources: list[dict[str, Any]] = [
            {
                "id": 1,
                "url": promise_url,
                "title": n.title,
                "publisher": "Promise Tracker",
                "date": None,
                "snippet": n.title,
                "verdict": "verified",
            }
        ]
        for j, link in enumerate(externals[:2], start=2):
            sources.append(
                {
                    "id": j,
                    "url": link,
                    "title": _first_line(u.text) if j == 2 else "Source document for the promise",
                    "publisher": None,
                    "date": u.created_at[:10] if j == 2 else None,
                    "snippet": u.text[:700] if j == 2 else n.title,
                    "verdict": "verified",
                }
            )
        tracker_type = (raw.get("tracker") or {}).get("type")
        meter_name = (
            "10th House of Representatives"
            if tracker_type == "legislative"
            else "President Bola Ahmed Tinubu"
        )
        status_change = i % 2 == 1 and n.status != "not_yet_rated"
        changes: list[dict[str, Any]] = []
        old = None
        if status_change:
            old = "not_yet_rated"
            changes.append(
                {
                    "type": "status_change",
                    "old_status": STATUS_LABELS[old],
                    "new_status": STATUS_LABELS[n.status],
                }
            )
        changes.append({"type": "new_evidence", "update_text": u.text[:1500]})
        cases.append(
            EvalCase(
                name=f"{os.path.basename(f)[:14]}:{'status' if status_change else 'evidence'}",
                writer_input={
                    "meter": {"name": meter_name, "office": tracker_type or "presidential"},
                    "promise": {
                        "title": n.title,
                        "category": n.category,
                        "promise_url": promise_url,
                        "current_status": STATUS_LABELS[n.status],
                    },
                    "changes": changes,
                    "sources": sources,
                },
                current_status=n.status,
                old_status=old,
            )
        )

    rail_promise = "https://promisetracker.ng/blog/e8c27bf8-199a-44bd-86ab-95aaa5297e49"
    for verdict in ("verified_partial", "verified"):
        cases.append(
            EvalCase(
                name=f"news:{verdict}",
                writer_input={
                    "meter": {"name": "President Bola Ahmed Tinubu", "office": "presidential"},
                    "promise": {
                        "title": "Complete the Kaduna-Kano railway line",
                        "category": "infrastructure",
                        "promise_url": rail_promise,
                        "current_status": "In the Works",
                    },
                    "changes": [
                        {
                            "type": "news_update",
                            "outlet": "Example News (illustrative)",
                            "headline": "Kaduna-Kano rail set for December 3 inauguration",
                            "published_date": "2026-11-08",
                            "summary": "The Federal Government said the Kaduna-Kano standard gauge "
                            "line will be inaugurated on 3 December 2026.",
                        }
                    ],
                    "sources": [
                        {
                            "id": 1,
                            "url": rail_promise,
                            "title": "Complete the Kaduna-Kano railway line",
                            "publisher": "Promise Tracker",
                            "date": None,
                            "snippet": "Complete the Kaduna-Kano railway line",
                            "verdict": "verified",
                        },
                        {
                            "id": 2,
                            "url": "https://example.org/illustrative/kaduna-kano-rail-december-3",
                            "title": "Kaduna-Kano rail set for December 3 inauguration",
                            "publisher": "Example News (illustrative)",
                            "date": "2026-11-08",
                            "snippet": None
                            if verdict == "verified_partial"
                            else "The Federal Government said the 203-kilometre Kaduna-Kano "
                            "standard gauge line will be inaugurated on 3 December 2026, after "
                            "test runs were completed in October. The Minister of Transportation "
                            "said passenger services would begin the same week.",
                            "verdict": verdict,
                        },
                    ],
                },
                current_status="in_the_works",
                old_status=None,
            )
        )
    return cases


def qa_context(case: EvalCase, banned: tuple[str, ...], min_w: int, max_w: int) -> QAContext:
    urls = frozenset(u for s in case.writer_input["sources"] if (u := normalize_url(s["url"])))
    return QAContext(
        citable_urls=urls,
        promise_url=normalize_url(case.writer_input["promise"]["promise_url"]) or "",
        current_status=case.current_status,
        old_status=case.old_status,
        has_status_change=case.old_status is not None,
        banned_terms=banned,
        min_words=min_w,
        max_words=max_w,
    )
