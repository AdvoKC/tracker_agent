from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from promise_monitor.api.models import ApiPromise, ApiPromiseDetail
from promise_monitor.domain.hashing import content_hash, fingerprint
from promise_monitor.domain.normalize import (
    html_links,
    html_to_text,
    normalize_promise,
    text_urls,
    to_utc_iso,
)
from tests.fakes import load_fixture


def _detail(**over: Any) -> ApiPromiseDetail:
    raw = load_fixture("promise.json")["data"]
    raw.update(over)
    return ApiPromiseDetail.model_validate(raw)


def test_fixture_promise_normalizes() -> None:
    n = normalize_promise(_detail())
    assert n.status == "in_the_works"
    assert n.category == "governance & political reform"
    assert n.source_urls == (
        "https://placng.org/i/wp-content/uploads/2023/11/"
        "Legislative-Agenda-of-the-10th-House-of-Representatives.pdf",
    )
    assert len(n.updates) == 1
    u = n.updates[0]
    assert u.text.startswith("HB 659: Companies and Allied Matters Act")
    assert "\xa0" not in u.text and "  " not in u.text
    assert u.created_at.endswith("+00:00")
    assert n.evidence_links[: len(u.evidence_links)] == u.evidence_links


def test_html_helpers() -> None:
    html = '<p>See <a href=" https://a.ng/x. ">A</a> and <a href="mailto:x@y">m</a> <a href="https://a.ng/x.">dup</a>&nbsp;ok</p>'
    assert html_links(html) == ("https://a.ng/x",)
    assert html_to_text(html) == "See A and m dup ok"
    assert html_to_text(None) is None


def test_urls_inside_source_text() -> None:
    assert text_urls("(source: https://punchng.com/story-1/).") == ("https://punchng.com/story-1/",)
    assert text_urls("manifesto") == ()


def test_timestamp_formats() -> None:
    assert to_utc_iso("2025-11-11 00:13:27") == "2025-11-11T00:13:27+00:00"
    assert to_utc_iso("2025-04-27T22:38:08.000000Z") == "2025-04-27T22:38:08+00:00"


def test_placeholders_and_nulls() -> None:
    n = normalize_promise(
        _detail(promise_start_date="1970-01-01", subject_area="null", source="manifesto")
    )
    assert n.promise_start_date is None
    assert n.category is None
    assert n.source_text == "manifesto"
    assert n.source_urls == ()


def test_unpublished_updates_are_dropped() -> None:
    raw = load_fixture("promise.json")["data"]
    raw["updates"][0]["is_published"] = "0"
    assert normalize_promise(ApiPromiseDetail.model_validate(raw)).updates == ()


def test_published_rating_wins_over_draft_status() -> None:
    n = normalize_promise(_detail(status="kept", last_published_status="in_the_works"))
    assert n.status == "in_the_works"


def test_content_hash_ignores_bookkeeping_fields() -> None:
    a = normalize_promise(_detail())
    b = normalize_promise(
        _detail(update_count=9, updated_at="2030-01-01T00:00:00Z", promise_img="x.png")
    )
    assert content_hash(a.content()) == content_hash(b.content())
    c = normalize_promise(_detail(title="A different title"))
    assert content_hash(a.content()) != content_hash(c.content())


def test_fingerprint_tracks_cheap_fields() -> None:
    raw = load_fixture("promise.json")["data"]
    a = ApiPromise.model_validate(raw)
    b = ApiPromise.model_validate({**raw, "update_count": 2})
    c = ApiPromise.model_validate({**raw, "title": "changed title only"})
    assert fingerprint(a) != fingerprint(b)
    assert fingerprint(a) == fingerprint(c)  # title edits are caught by the weekly sweep


def test_select_option_placeholder_update_status_tolerated() -> None:
    raw = load_fixture("p_7845cb92_select_option_update.json")["data"]
    n = normalize_promise(ApiPromiseDetail.model_validate(raw))
    assert [u.status for u in n.updates].count(None) == 1
    assert len(n.updates) == 3


def test_unknown_update_status_still_fails() -> None:
    raw = load_fixture("p_7845cb92_select_option_update.json")["data"]
    raw["updates"][0]["status"] = "teleported"
    with pytest.raises(ValidationError):
        ApiPromiseDetail.model_validate(raw)
