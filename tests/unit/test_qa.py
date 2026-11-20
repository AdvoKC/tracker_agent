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

HEADLINE = "CAMA Amendment Bill on Civil Society Barriers Passes Second Reading"
CLOSER = "is rated, \u2018IN THE WORKS.\u2019"
BODY = (
    "The House of Representatives has advanced its promise to review the Companies and Allied "
    "Matters Act (CAMA) to remove barriers facing civil society organisations. According to the "
    f"[House Votes and Proceedings of 12 November 2026]({VOTES}), the CAMA (Amendment) Bill, "
    "HB 659, passed second reading and was referred to the Committee on Commerce for further "
    "legislative action. The commitment comes from the "
    f"[Legislative Agenda of the 10th House]({PDF}). The committee has not yet submitted its "
    f"report to plenary. [This promise]({PROMISE}) {CLOSER}"
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
        min_words=60,
        max_words=100,
    )


def issues(ctx: QAContext, body: str = BODY, headline: str = HEADLINE) -> list[str]:
    return check_article(headline, body, ctx).issues


def test_house_style_sample_passes(ctx: QAContext) -> None:
    rep = check_article(HEADLINE, BODY, ctx)
    assert rep.issues == []
    assert rep.word_count == 84
    assert len(rep.links) == 3


def test_word_count_rules() -> None:
    assert word_count("[two words](https://a.ng/x) and — three") == 4  # dash is not a word
    assert word_count("see https://a.ng/x") == 2  # bare URLs would count, but are flagged elsewhere


@pytest.mark.parametrize("extra", [" " + " ".join(["More"] * 20) + "."])
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
    body = BODY.replace(f"[This promise]({PROMISE})", "This promise")
    assert any("promise page" in i for i in issues(ctx, body))


def test_external_source_required(ctx: QAContext) -> None:
    body = BODY.replace(VOTES, PROMISE).replace(PDF, PROMISE)
    assert any("external" in i for i in issues(ctx, body))


def test_bare_url_rejected(ctx: QAContext) -> None:
    body = BODY.replace("report to plenary.", f"report to plenary, see {VOTES}.")
    assert any("bare URL" in i for i in issues(ctx, body))


def test_wrong_status_label(ctx: QAContext) -> None:
    body = BODY.replace(CLOSER, "is rated, \u2018STALLED.\u2019")
    found = issues(ctx, body)
    assert any("Stalled" in i for i in found)
    assert any("current Promise Tracker rating" in i for i in found)


def test_status_change_labels_allowed(ctx: QAContext) -> None:
    c = replace(ctx, current_status="kept", old_status="in_the_works", has_status_change=True)
    body = BODY.replace(CLOSER, "is now rated, \u2018KEPT.\u2019")
    assert issues(c, body) == []


def test_news_update_must_not_imply_rating_change(ctx: QAContext) -> None:
    body = BODY.replace(CLOSER, "is now rated, \u2018IN THE WORKS.\u2019")
    found = issues(ctx, body)
    assert any("rating did not change" in i for i in found)


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


# ----------------------------------------------------------------------------- house style


def test_house_closer_required(ctx: QAContext) -> None:
    body = BODY.replace(
        f"[This promise]({PROMISE}) {CLOSER}", f"See the [promise page here]({PROMISE})."
    )
    assert any(i.startswith("house style: the body must end") for i in issues(ctx, body))


def test_house_closer_accepts_quote_variants(ctx: QAContext) -> None:
    for closer in (
        'is rated "IN THE WORKS."',
        "is rated, 'In the Works.'",
        "is rated \u201cIN THE WORKS\u201d.",
    ):
        assert issues(ctx, BODY.replace(CLOSER, closer)) == [], closer


def test_platform_name_not_used(ctx: QAContext) -> None:
    body = BODY.replace(
        "The committee has not yet", "On Promise Tracker, the committee has not yet"
    )
    assert any("Promise Tracker" in i for i in issues(ctx, body))


def test_never_more_than_100_words(ctx: QAContext) -> None:
    padding = " ".join(["Additional"] * 17) + "."
    body = BODY.replace("[This promise]", f"{padding} [This promise]")
    rep = check_article(HEADLINE, body, ctx)
    assert rep.word_count == 101
    assert any(i.startswith("word_count") for i in rep.issues)
