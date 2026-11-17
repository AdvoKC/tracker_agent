"""Headless Claude Code backend: ``claude -p`` with the user's subscription login.

Each call is trimmed hard to keep subscription usage low (measured 2026-11-17: ~1k input
tokens instead of ~33k with defaults):
- ``--system-prompt`` replaces Claude Code's own large system prompt;
- ``--tools`` exposes only the tools a task needs (none, or WebSearch);
- ``--setting-sources ""``, ``--strict-mcp-config``, ``--disable-slash-commands``: no
  project settings, MCP servers or skills;
- runs in an empty working directory so no CLAUDE.md is picked up;
- ``--json-schema`` makes the CLI validate output and return it as ``structured_output``;
- ``--output-format stream-json --verbose`` so we also get the RAW WebSearch tool results
  (``tool_use_result`` events). They are produced by the search tool, not written by the
  model, so code can check that a URL really appeared in search results (grounding).
``--bare`` is NOT used: it ignores OAuth logins and requires an API key.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from promise_monitor.config import LLMConfig
from promise_monitor.llm.base import (
    LLMAuthError,
    LLMError,
    LLMRequest,
    LLMResponse,
    SearchHit,
)
from promise_monitor.logging_setup import get_logger

log = get_logger(__name__)

Runner = Callable[..., subprocess.CompletedProcess[str]]
_AUTH_MARKERS = ("not logged in", "/login", "invalid api key", "oauth token", "authentication")


class ClaudeCLIClient:
    def __init__(
        self,
        cfg: LLMConfig,
        *,
        runner: Runner = subprocess.run,
        workdir: Path | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._cfg = cfg
        self._runner = runner
        self._sleep = sleep
        self._workdir = workdir or Path(tempfile.mkdtemp(prefix="promise-monitor-llm-"))

    def model_for(self, request: LLMRequest) -> str:
        return str(getattr(self._cfg.models, request.task))

    def argv(self, request: LLMRequest) -> list[str]:
        tools = ",".join(request.tools)
        args = [
            self._cfg.claude_bin,
            "-p",
            "--model",
            self.model_for(request),
            "--system-prompt",
            request.system,
            "--tools",
            tools,
            "--setting-sources",
            "",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--output-format",
            "stream-json",
            "--verbose",
            "--json-schema",
            json.dumps(request.schema, separators=(",", ":")),
        ]
        if tools:
            args += ["--allowedTools", tools]
        # "--" ends option parsing: variadic flags like --allowedTools would otherwise
        # swallow the prompt (found live 2026-11-17).
        return [*args, "--", request.user]

    def run(self, request: LLMRequest) -> LLMResponse:
        last: LLMError | None = None
        for attempt in range(self._cfg.max_retries + 1):
            try:
                return self._run_once(request)
            except LLMAuthError:
                raise
            except LLMError as exc:
                last = exc
                log.warning("llm_retry", task=request.task, attempt=attempt, error=str(exc))
                if attempt < self._cfg.max_retries:
                    self._sleep(5.0 * (attempt + 1))
        assert last is not None
        raise last

    def _run_once(self, request: LLMRequest) -> LLMResponse:
        started = time.monotonic()
        try:
            proc = self._runner(
                self.argv(request),
                cwd=self._workdir,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=self._cfg.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise LLMError(f"claude timed out after {self._cfg.timeout_seconds}s") from exc
        except FileNotFoundError as exc:
            raise LLMAuthError(f"claude binary not found: {self._cfg.claude_bin}") from exc
        latency = int((time.monotonic() - started) * 1000)
        return parse_cli_output(proc.stdout, proc.stderr, proc.returncode, latency)


def parse_cli_output(stdout: str, stderr: str, returncode: int, latency_ms: int) -> LLMResponse:
    """Parse ``stream-json`` (one event per line) or a single ``json`` result object."""
    events: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict):
            events.append(ev)

    payload = next((e for e in reversed(events) if e.get("type") == "result"), None)
    if payload is None and len(events) == 1:
        payload = events[0]  # plain --output-format json
    if payload is None:
        detail = (stderr or stdout).strip()[:500]
        _raise_for_text(detail, f"exit {returncode}, no result event")
        raise LLMError(f"exit {returncode}, no result event: {detail}")

    if payload.get("is_error"):
        _raise_for_text(str(payload.get("result", "")), "claude reported an error")
        raise LLMError(f"claude reported an error: {str(payload.get('result'))[:300]}")

    output = payload.get("structured_output")
    if not isinstance(output, dict):
        raise LLMError("no structured_output in claude response")

    models: Sequence[str] = list((payload.get("modelUsage") or {}).keys())
    cost = payload.get("total_cost_usd")
    return LLMResponse(
        output=output,
        model=models[0] if models else "unknown",
        latency_ms=latency_ms,
        cost_usd=float(cost) if isinstance(cost, int | float) else None,
        meta={
            "num_turns": payload.get("num_turns"),
            "usage": payload.get("usage"),
            "session_id": payload.get("session_id"),
        },
        search_hits=tuple(_search_hits(events)),
    )


def _search_hits(events: list[dict[str, Any]]) -> list[SearchHit]:
    hits: list[SearchHit] = []
    for ev in events:
        result = ev.get("tool_use_result") if ev.get("type") == "user" else None
        if not isinstance(result, dict) or not isinstance(result.get("results"), list):
            continue
        query = str(result.get("query", ""))
        for block in result["results"]:
            items = block.get("content") if isinstance(block, dict) else None
            for item in items if isinstance(items, list) else []:
                if isinstance(item, dict) and isinstance(item.get("url"), str):
                    hits.append(SearchHit(item["url"], str(item.get("title", "")), query))
    return hits


def _raise_for_text(text: str, context: str) -> None:
    low = text.lower()
    if any(m in low for m in _AUTH_MARKERS):
        raise LLMAuthError(f"{context}: {text[:200]}")
