"""One test (at least) per deterministic QA rule (PRD §8 P5 exit criterion)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from promise_monitor.config import load_app_config
from promise_monitor.domain.qa import QAContext, check_article, cosine, word_count
from tests.conftest import REPO_ROOT

PROMISE = "https://promisetracker.ng/blog/031e8e3b-f198-439d-9038-c7a2874eb19f"
PDF = (
    "https://placng.org/i/wp-content/uploads/2023/11/"
    "Legislative-Agenda-of-the-10th-House-of-Representatives.pdf"
)
VOTES = "https://example.org/illustrative/votes-and-proceedings"

HEADLINE = "CAMA amendment bill on civil society barriers passes second reading in House"
BODY = (
    "The House of Representatives' promise to review the Companies and Allied Matters Act "
    "(CAMA) to remove barriers facing civil society organisations has gained new evidence on "
    "Promise Tracker. Its status remains In the Works. According to the "
    f"[House Votes and Proceedings of 12 November 2026]({VOTES}), the CAMA (Amendment) Bill, "
    "HB 659, passed second reading and was referred to the Committee on Commerce for further "
    "legislative action. The commitment comes from the "
    f"[Legislative Agenda of the 10th House]({PDF}). The committee has not yet submitted its "
    "report to plenary. The full evidence timeline is on the "
    f"[Promise Tracker page for this promise]({PROMISE})."
)


@pytest.fixture
def ctx() -> QAContext:
    cfg = load_app_config(REPO_ROOT / "config")
    return QAContext(
        citable_urls=frozenset({PROMISE, PDF, VOTES}),
        promise_url=PROMISE,
        current_status="in_the_works",
        old_status=None,
        has_status_change=False,
        banned_terms=cfg.banned_terms,
        min_words=95,
        max_words=105,
    )


def issues(ctx: QAContext, body: str = BODY, headline: str = HEADLINE) -> list[str]:
    return check_article(headline, body, ctx).issues


def test_prd_sample_article_passes(ctx: QAContext) -> None:
    rep = check_article(HEADLINE, BODY, ctx)
    assert rep.issues == []
    assert rep.word_count == 100
    assert len(rep.links) == 3


def test_word_count_rules() -> None:
    assert word_count("[two words](https://a.ng/x) and — three") == 4  # dash is not a word
    assert word_count("see https://a.ng/x") == 2  # bare URLs would count, but are flagged elsewhere


@pytest.mark.parametrize("extra", [" Extra words added here to push it over the maximum."])
def test_too_long(ctx: QAContext, extra: str) -> None:
    assert any(i.startswith("word_count") for i in issues(ctx, BODY + extra))


def test_too_short(ctx: QAContext) -> None:
    assert any(
        i.startswith("word_count") for i in issues(ctx, BODY[:300] + f" [Promise page]({PROMISE})")
    )


@pytest.mark.parametrize(
    ("headline", "needle"),
    [
        ("", "missing"),
        ("Is the CAMA bill finally moving in the House of Representatives now?", "question"),
        (" ".join(["word"] * 15), "at most"),
        (f"Read [this]({PROMISE})", "no links"),
    ],
)
def test_headline_rules(ctx: QAContext, headline: str, needle: str) -> None:
    assert any(i.startswith("headline") and needle in i for i in issues(ctx, headline=headline))


def test_needs_two_links(ctx: QAContext) -> None:
    body = BODY.replace(
        f"[Legislative Agenda of the 10th House]({PDF})", "Legislative Agenda"
    ).replace(f"[House Votes and Proceedings of 12 November 2026]({VOTES})", "House record")
    assert any("at least 2" in i for i in issues(ctx, body))


def test_unverified_url_rejected(ctx: QAContext) -> None:
    body = BODY.replace(VOTES, "https://randomblog.ng/made-up")
    assert any("not in the verified source list" in i for i in issues(ctx, body))


def test_url_normalization_tolerated(ctx: QAContext) -> None:
    body = BODY.replace(PROMISE, PROMISE.replace("https://", "HTTPS://www.") + "/")
    assert issues(ctx, body) == []


def test_vague_anchor_rejected(ctx: QAContext) -> None:
    body = BODY.replace("[Legislative Agenda of the 10th House]", "[here]")
    assert any("descriptive" in i for i in issues(ctx, body))


def test_promise_page_required(ctx: QAContext) -> None:
    body = BODY.replace(
        f"[Promise Tracker page for this promise]({PROMISE})", "Promise Tracker page"
    )
    assert any("promise page" in i for i in issues(ctx, body))


def test_external_source_required(ctx: QAContext) -> None:
    body = BODY.replace(VOTES, PROMISE).replace(PDF, PROMISE)
    assert any("external" in i for i in issues(ctx, body))


def test_bare_url_rejected(ctx: QAContext) -> None:
    body = BODY.replace("report to plenary.", f"report to plenary, see {VOTES}.")
    assert any("bare URL" in i for i in issues(ctx, body))


def test_wrong_status_label(ctx: QAContext) -> None:
    body = BODY.replace("Its status remains In the Works.", "Its status remains Stalled.")
    found = issues(ctx, body)
    assert any("Stalled" in i for i in found)
    assert any("current Promise Tracker rating" in i for i in found)


def test_status_change_labels_allowed(ctx: QAContext) -> None:
    c = replace(ctx, current_status="kept", old_status="in_the_works", has_status_change=True)
    body = BODY.replace(
        "Its status remains In the Works.", "Its status moved from In the Works to Kept."
    )
    assert issues(c, body) == []


def test_news_update_must_not_imply_rating_change(ctx: QAContext) -> None:
    body = BODY.replace("Its status remains In the Works.", "It is now rated In the Works.")
    assert any("rating did not change" in i for i in issues(ctx, body))


def test_banned_term(ctx: QAContext) -> None:
    body = BODY.replace("The committee has not yet", "Sadly the committee has not yet")
    assert any("sadly" in i for i in issues(ctx, body))


def test_banned_term_is_whole_word(ctx: QAContext) -> None:
    c = replace(ctx, banned_terms=("only",))
    assert issues(c, BODY.replace("further", "commonly")) == []  # 'commonly' ≠ 'only'


def test_long_quote_rejected(ctx: QAContext) -> None:
    quote = '"' + " ".join(["word"] * 16) + '"'
    body = BODY.replace("for further legislative action.", f"saying {quote}.")
    assert any(i.startswith("quotes") for i in issues(ctx, body))


def test_near_duplicate_rejected(ctx: QAContext) -> None:
    c = replace(ctx, recent_bodies=(BODY.replace("12 November", "13 November"),))
    assert any(i.startswith("dedupe") for i in issues(c))
    assert cosine(BODY, "Something entirely different about roads in Kano.") < 0.3
