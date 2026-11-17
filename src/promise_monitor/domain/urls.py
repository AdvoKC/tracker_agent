"""URL normalization and outlet allowlist checks (used by News Scout, later the Verifier)."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from promise_monitor.config import NewsSourcesConfig

_TRACKING_PREFIXES = ("utm_", "fbclid", "gclid", "mc_", "ref", "amp")


def normalize_url(url: str) -> str | None:
    """Lowercase scheme/host, drop fragments, tracking params, default ports, trailing '/'."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname.lower()
    if host.startswith("www."):
        host = host[4:]
    port = f":{parts.port}" if parts.port and parts.port not in (80, 443) else ""
    query = urlencode(
        [(k, v) for k, v in parse_qsl(parts.query) if not k.lower().startswith(_TRACKING_PREFIXES)]
    )
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", host + port, path, query, ""))


def host_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _matches(host: str, domain: str) -> bool:
    domain = domain.lower().lstrip(".")
    return host == domain or host.endswith("." + domain)


def allowed_outlet(url: str, sources: NewsSourcesConfig) -> str | None:
    """Return the outlet name if ``url`` is on the allowlist, else None."""
    host = host_of(url)
    for outlet in sources.outlets:
        if _matches(host, outlet.domain):
            return outlet.name
    for suffix in sources.official_suffixes:
        if _matches(host, suffix):
            return f"Official ({host})"
    return None


def allowed_domains_text(sources: NewsSourcesConfig) -> str:
    domains = [o.domain for o in sources.outlets] + [f"*.{s}" for s in sources.official_suffixes]
    return ", ".join(domains)
