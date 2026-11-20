"""AdvoKC house style for update articles (studied 2026-11-18 from 462 AdvoKC evidence updates).

Measured conventions:
- headline: a Title Case news headline naming the actor and action (bill updates lead with the
  bill number, e.g. "SB 903: …"); median ≈ 10 words;
- body: actor-first lead with concrete specifics (amounts like N1.1bn, numbers, places, dates),
  then attribution ("A statement by the spokesperson for the governor, … disclosed …");
- closer, every time: "This promise is rated, 'IN THE WORKS.'" (curly quotes in the real text)
  with the rating in capitals (73% of updates); a changed rating simply appears as the new rating;
- never names the platform ("Promise Tracker": 0 of 462); "this promise" in 81% of updates;
- length: median 74 words (p25 57, p75 95).

``house_examples`` feeds the Writer REAL recent AdvoKC updates (same meter first) so the
style keeps following AdvoKC's latest writing; ``closer_issue`` enforces the closing line.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from promise_monitor.db.models import Meter, PromiseIndex, PromiseSnapshot

LQ, RQ = "\u2018", "\u2019"  # the curly single quotes AdvoKC uses around the rating
CLOSER = re.compile(
    r"this promise is (?P<now>now )?rated,?\s*[\u2018'\"\u201c]\s*(?P<label>[a-z ]+?)"
    r"\s*\.?\s*[\u2019'\"\u201d]\s*\.?\s*$",
    re.IGNORECASE,
)
_HAS_CLOSER = re.compile(r"this promise(?:'s)? (?:is )?rat", re.IGNORECASE)


def house_closer(label: str, *, now: bool = False) -> str:
    return f"This promise is {'now ' if now else ''}rated, {LQ}{label.upper()}.{RQ}"


def closer_issue(body_plain: str, current_label: str, status_changed: bool) -> str | None:
    """None if the body ends with the house closer naming the current rating, else the problem."""
    m = CLOSER.search(body_plain.strip())
    expected = house_closer(current_label)
    if m is None:
        return f"house style: the body must end with the closing line: {expected}"
    if " ".join(m.group("label").lower().split()) != current_label.lower():
        return f"house style: the closing line must give the current rating: {expected}"
    if m.group("now") and not status_changed:
        return (
            "house style: the rating did not change; write 'This promise is rated', not 'now rated'"
        )
    return None


def _clean(text: str) -> str:
    return " ".join(text.split())


def eligible_example(text: str) -> bool:
    """A well-formed AdvoKC update worth imitating (length, house closer, no platform name)."""
    words = len(text.split())
    return (
        50 <= words <= 110
        and bool(_HAS_CLOSER.search(text))
        and "promise tracker" not in text.lower()
    )


def house_examples(
    session: Session,
    meter_id: uuid.UUID | None,
    n: int,
    *,
    exclude_promise: uuid.UUID | None = None,
) -> list[str]:
    """Up to ``n`` real AdvoKC updates: same meter first, then same meter family, recent first."""
    if n <= 0:
        return []
    rows = session.execute(
        select(
            PromiseIndex.meter_id,
            PromiseIndex.promise_id,
            PromiseSnapshot.content,
            Meter.meter_type,
        )
        .join(PromiseSnapshot, PromiseSnapshot.snapshot_id == PromiseIndex.latest_snapshot_id)
        .join(Meter, Meter.id == PromiseIndex.meter_id)
    ).all()
    family = next((t for mid, _p, _c, t in rows if mid == meter_id), None)
    pool: list[tuple[int, str, str]] = []
    for mid, pid, content, mtype in rows:
        if pid == exclude_promise:
            continue
        for u in (content or {}).get("updates") or []:
            text = _clean(str(u.get("text") or ""))
            words = len(text.split())
            if (
                not (50 <= words <= 110)
                or not _HAS_CLOSER.search(text)
                or "promise tracker" in text.lower()
            ):
                continue
            rank = 0 if mid == meter_id else 1 if mtype == family else 2
            pool.append((rank, str(u.get("created_at") or ""), text))
    pool.sort(key=lambda t: (t[0], _neg(t[1])))
    out: list[str] = []
    for _rank, _when, text in pool:
        if text not in out:
            out.append(text)
        if len(out) >= n:
            break
    return out


def _neg(iso: str) -> Any:
    """Sort key: most recent first (ISO strings compare lexicographically)."""
    return tuple(-ord(ch) for ch in iso)
