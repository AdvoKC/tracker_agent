"""Safe, polite fetching of third-party source URLs (Verifier, PRD §3.4).

URLs come from the tracker's evidence HTML and from LLM output, so they are untrusted:
- only http(s); every hop (including redirects) must resolve to a PUBLIC address — no
  localhost, private, link-local or reserved ranges (SSRF guard);
- at most ``max_redirects`` hops, ``timeout`` per request, ``max_bytes`` read per response;
- at most one request per ``per_host_interval`` seconds per host;
- honest User-Agent (AGENTS.md rule 7) — we never pretend to be a browser, so some
  outlets answer 401/403 ("blocked"); the Verifier handles that with a search check.
"""

from __future__ import annotations

import ipaddress
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urljoin, urlsplit

import httpx

from promise_monitor.config import VerifierConfig
from promise_monitor.logging_setup import get_logger

log = get_logger(__name__)

FetchStatus = Literal["ok", "blocked", "broken", "unsupported", "unsafe", "error"]
BLOCKED_CODES = frozenset({401, 402, 403, 429, 451})
SUPPORTED_TYPES = ("text/html", "application/xhtml+xml", "application/pdf")

Resolver = Callable[[str], list[str]]


def system_resolver(host: str) -> list[str]:
    return list({str(info[4][0]) for info in socket.getaddrinfo(host, None)})


def is_public_host(host: str, resolver: Resolver) -> bool:
    try:
        addrs = resolver(host)
    except OSError:
        return False
    if not addrs:
        return False
    for a in addrs:
        ip = ipaddress.ip_address(a.split("%")[0])
        if not ip.is_global or ip.is_multicast:
            return False
    return True


@dataclass(frozen=True)
class FetchResult:
    url: str
    final_url: str | None
    status: FetchStatus
    http_status: int | None
    content_type: str | None
    body: bytes
    error: str | None = None


class SafeFetcher:
    def __init__(
        self,
        cfg: VerifierConfig,
        user_agent: str,
        *,
        transport: httpx.BaseTransport | None = None,
        resolver: Resolver = system_resolver,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cfg = cfg
        self._resolver = resolver
        self._sleep = sleep
        self._clock = clock
        self._last_hit: dict[str, float] = {}
        self._http = httpx.Client(
            headers={
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.5",
            },
            timeout=cfg.timeout_seconds,
            follow_redirects=False,  # we follow manually so every hop is checked
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> SafeFetcher:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _throttle(self, host: str) -> None:
        last = self._last_hit.get(host)
        if last is not None:
            wait = self._cfg.per_host_interval_seconds - (self._clock() - last)
            if wait > 0:
                self._sleep(wait)
        self._last_hit[host] = self._clock()

    def fetch(self, url: str) -> FetchResult:
        current = url
        for _hop in range(self._cfg.max_redirects + 1):
            parts = urlsplit(current)
            host = (parts.hostname or "").lower()
            if parts.scheme not in ("http", "https") or not host:
                return FetchResult(url, current, "unsafe", None, None, b"", "not http(s)")
            if not is_public_host(host, self._resolver):
                return FetchResult(url, current, "unsafe", None, None, b"", "non-public address")
            result = self._get_with_retries(url, current, host)
            if isinstance(result, FetchResult):
                return result
            current = result  # redirect target
        return FetchResult(url, current, "broken", None, None, b"", "too many redirects")

    def _get_with_retries(self, url: str, current: str, host: str) -> FetchResult | str:
        attempts = self._cfg.max_retries + 1
        for attempt in range(attempts):
            self._throttle(host)
            try:
                with self._http.stream("GET", current) as resp:
                    if resp.is_redirect:
                        loc = resp.headers.get("location")
                        if not loc:
                            return FetchResult(url, current, "broken", resp.status_code, None, b"")
                        return str(urljoin(current, loc))
                    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                    code = resp.status_code
                    if code >= 500 and attempt + 1 < attempts:
                        self._sleep(2.0 * (attempt + 1))
                        continue
                    if code in BLOCKED_CODES:
                        return FetchResult(url, current, "blocked", code, ctype, b"")
                    if code >= 400:
                        return FetchResult(url, current, "broken", code, ctype, b"")
                    if not ctype.startswith(SUPPORTED_TYPES):
                        return FetchResult(url, current, "unsupported", code, ctype, b"")
                    body = bytearray()
                    for chunk in resp.iter_bytes():
                        body.extend(chunk)
                        if len(body) >= self._cfg.max_bytes:
                            break
                    body_bytes = bytes(body[: self._cfg.max_bytes])
                    return FetchResult(url, current, "ok", code, ctype, body_bytes)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt + 1 < attempts:
                    self._sleep(2.0 * (attempt + 1))
                    continue
                return FetchResult(url, current, "broken", None, None, b"", type(exc).__name__)
        return FetchResult(
            url, current, "error", None, None, b"", "unreachable"
        )  # pragma: no cover
