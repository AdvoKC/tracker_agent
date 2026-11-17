from __future__ import annotations

import io
import json
from collections.abc import Callable
from datetime import date

import httpx
import pytest

from promise_monitor.config import VerifierConfig, load_app_config
from promise_monitor.domain.extract import extract, extract_html, parse_iso_date
from promise_monitor.domain.fetch import SafeFetcher, is_public_host
from promise_monitor.llm.claude_cli import parse_cli_output
from tests.conftest import REPO_ROOT

ARTICLE = (
    b"""<html><head>
<meta property="og:title" content="Senate passes CAMA amendment bill">
<meta property="og:site_name" content="Premium Times">
<script type="application/ld+json">
{"@graph":[{"@type":"NewsArticle","datePublished":"2026-11-14T09:12:00+01:00"}]}
</script>
<title>ignored | Premium Times</title></head>
<body><nav>Home News</nav><article><h1>Senate passes CAMA amendment bill</h1>
<p>The Senate on Thursday passed the Companies and Allied Matters Act (Amendment) Bill.</p>
<p>"""
    + b"Lawmakers said the change removes barriers for civil society groups. " * 6
    + b"""</p>
<script>var tracking = 1;</script></article><footer>Copyright</footer></body></html>"""
)


@pytest.fixture
def vcfg() -> VerifierConfig:
    return load_app_config(REPO_ROOT / "config").tunables.verifier


# ----------------------------------------------------------------------------- extract


def test_extract_html_article() -> None:
    ex = extract_html(ARTICLE)
    assert ex.title == "Senate passes CAMA amendment bill"
    assert ex.published_date == date(2026, 11, 14)
    assert ex.publisher == "Premium Times"
    assert ex.text.startswith("The Senate on Thursday passed")
    assert "tracking" not in ex.text and "Home News" not in ex.text
    assert ex.paywall_marker is False


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        (
            '<meta property="article:published_time" content="2026-10-01T10:00:00Z">',
            date(2026, 10, 1),
        ),
        ('<meta name="pubdate" content="2026-09-30">', date(2026, 9, 30)),
        ("", date(2026, 8, 7)),  # falls back to <time datetime>
    ],
)
def test_date_sources(head: str, expected: date) -> None:
    html = f"<html><head>{head}</head><body><time datetime='2026-08-07'>7 Aug</time></body></html>"
    assert extract_html(html.encode()).published_date == expected


def test_paywall_marker() -> None:
    html = b"<html><body><p>Subscribe to continue reading this story.</p></body></html>"
    assert extract_html(html).paywall_marker is True


def test_parse_iso_date_rejects_garbage() -> None:
    assert parse_iso_date("2026-13-45") is None
    assert parse_iso_date("yesterday") is None


def test_extract_pdf_metadata() -> None:
    from pypdf import PdfWriter

    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    w.add_metadata(
        {"/Title": "Legislative Agenda of the 10th House", "/CreationDate": "D:20231105120000"}
    )
    buf = io.BytesIO()
    w.write(buf)
    ex = extract(buf.getvalue(), "application/pdf")
    assert ex.title == "Legislative Agenda of the 10th House"
    assert ex.published_date == date(2023, 11, 5)


def test_corrupt_pdf_does_not_crash() -> None:
    ex = extract(b"%PDF-1.4 garbage", "application/pdf")
    assert ex.title is None and ex.text == ""


# ----------------------------------------------------------------------------- fetch

PUBLIC = {"news.example": ["93.184.216.34"], "cdn.example": ["93.184.216.35"]}


def resolver(host: str) -> list[str]:
    return PUBLIC.get(host, ["127.0.0.1"] if host == "evil.example" else ["10.0.0.5"])


def make(
    vcfg: VerifierConfig, handler: Callable[[httpx.Request], httpx.Response], sleeps: list[float]
) -> SafeFetcher:
    return SafeFetcher(
        vcfg,
        "AdvoKC-PromiseMonitor/1.0",
        transport=httpx.MockTransport(handler),
        resolver=resolver,
        sleep=sleeps.append,
        clock=lambda: 0.0,
    )


def html(body: bytes = ARTICLE, code: int = 200) -> httpx.Response:
    return httpx.Response(code, content=body, headers={"content-type": "text/html; charset=utf-8"})


def test_fetch_ok(vcfg: VerifierConfig) -> None:
    res = make(vcfg, lambda r: html(), []).fetch("https://news.example/a")
    assert res.status == "ok" and res.http_status == 200 and res.body.startswith(b"<html>")


@pytest.mark.parametrize(
    ("code", "status"), [(404, "broken"), (410, "broken"), (403, "blocked"), (401, "blocked")]
)
def test_fetch_status_mapping(vcfg: VerifierConfig, code: int, status: str) -> None:
    assert (
        make(vcfg, lambda r: html(b"x", code), []).fetch("https://news.example/a").status == status
    )


def test_redirect_to_private_address_is_unsafe(vcfg: VerifierConfig) -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://internal.example/admin"})

    res = make(vcfg, handler, []).fetch("https://news.example/a")
    assert res.status == "unsafe"


def test_redirect_followed_when_public(vcfg: VerifierConfig) -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        if r.url.host == "news.example":
            return httpx.Response(301, headers={"location": "https://cdn.example/a"})
        return html()

    res = make(vcfg, handler, []).fetch("https://news.example/a")
    assert res.status == "ok" and res.final_url == "https://cdn.example/a"


def test_too_many_redirects(vcfg: VerifierConfig) -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://news.example/loop"})

    assert make(vcfg, handler, []).fetch("https://news.example/a").status == "broken"


def test_localhost_and_non_http_rejected(vcfg: VerifierConfig) -> None:
    f = make(vcfg, lambda r: html(), [])
    assert f.fetch("https://evil.example/x").status == "unsafe"
    assert f.fetch("file:///etc/passwd").status == "unsafe"
    assert is_public_host("evil.example", resolver) is False


def test_unsupported_type_and_byte_cap(vcfg: VerifierConfig) -> None:
    img = httpx.Response(200, content=b"x", headers={"content-type": "image/png"})
    assert make(vcfg, lambda r: img, []).fetch("https://news.example/i").status == "unsupported"
    big = html(b"a" * (vcfg.max_bytes * 2))
    assert len(make(vcfg, lambda r: big, []).fetch("https://news.example/b").body) <= vcfg.max_bytes


def test_retry_on_5xx_then_ok(vcfg: VerifierConfig) -> None:
    calls = iter([html(b"", 503), html()])
    sleeps: list[float] = []
    res = make(vcfg, lambda r: next(calls), sleeps).fetch("https://news.example/a")
    assert res.status == "ok" and 2.0 in sleeps


def test_per_host_throttle(vcfg: VerifierConfig) -> None:
    sleeps: list[float] = []
    f = make(vcfg, lambda r: html(), sleeps)
    f.fetch("https://news.example/a")
    f.fetch("https://news.example/b")
    assert sleeps == [vcfg.per_host_interval_seconds]


# ----------------------------------------------------------------------------- stream-json


def test_stream_json_yields_raw_search_hits() -> None:
    lines = [
        {"type": "system", "subtype": "init"},
        {
            "type": "user",
            "tool_use_result": {
                "query": "ports deal",
                "results": [
                    {
                        "tool_use_id": "x",
                        "content": [{"title": "Deal", "url": "https://thisdaylive.com/a"}],
                    },
                    "some text block",
                ],
            },
        },
        {
            "type": "result",
            "is_error": False,
            "structured_output": {"searched": True},
            "total_cost_usd": 0.01,
            "modelUsage": {"claude-haiku-5-5": {}},
        },
    ]
    resp = parse_cli_output("\n".join(json.dumps(x) for x in lines), "", 0, 10)
    assert resp.output == {"searched": True}
    assert [(h.url, h.title, h.query) for h in resp.search_hits] == [
        ("https://thisdaylive.com/a", "Deal", "ports deal")
    ]
