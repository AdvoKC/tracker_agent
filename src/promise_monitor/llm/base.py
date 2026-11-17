"""LLM interface. Every model call in the project goes through ``LLMClient.run``.

Backends: ``ClaudeCLIClient`` (headless ``claude -p`` on the subscription) today; an
Anthropic API-key backend can implement the same protocol later (PRD risk R-L3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

Task = Literal["classifier", "news_scout", "verifier", "writer", "editor", "default"]
Tool = Literal["WebSearch", "WebFetch"]


@dataclass(frozen=True)
class LLMRequest:
    task: Task
    prompt_name: str  # file stem in prompts/, recorded for audit
    system: str
    user: str
    schema: dict[str, Any]  # JSON Schema the output must satisfy
    tools: tuple[Tool, ...] = ()
    subject_id: str | None = None  # article group / event / batch id, for llm_calls


@dataclass(frozen=True)
class SearchHit:
    """One raw WebSearch result as returned by the search TOOL (not written by the model).

    Code uses these to check that a URL the model reports really appeared in search results.
    """

    url: str
    title: str
    query: str


@dataclass(frozen=True)
class LLMResponse:
    output: dict[str, Any]
    model: str
    latency_ms: int
    cost_usd: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    search_hits: tuple[SearchHit, ...] = ()


class LLMError(RuntimeError):
    """The call failed or returned unusable output. Callers fall back or retry next run."""


class LLMAuthError(LLMError):
    """Not logged in / token expired. Stop calling the LLM for the rest of the run."""


class LLMClient(Protocol):
    def run(self, request: LLMRequest) -> LLMResponse: ...
