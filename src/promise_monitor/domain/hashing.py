"""Canonical JSON, fingerprints and content hashes (PRD §3.2 step 4).

- ``fingerprint``: cheap-pass signature from the meter list. A change means "fetch detail".
- ``content_hash``: hash of normalized content. A change means "something really changed".
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from promise_monitor.api.models import ApiPromise

FINGERPRINT_FIELDS = (
    "status",
    "last_published_status",
    "updated_at",
    "last_updated_at",
    "update_count",
    "published_status",
)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fingerprint(item: ApiPromise) -> str:
    return sha256_hex(canonical_json({f: getattr(item, f) for f in FINGERPRINT_FIELDS}))


def content_hash(content: dict[str, Any]) -> str:
    return sha256_hex(canonical_json(content))
