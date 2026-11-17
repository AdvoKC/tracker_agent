"""Headless Claude Code backend: ``claude -p`` with the user's subscription login.

Each call is trimmed hard to keep subscription usage low (measured 2026-11-17: ~1k input
tokens instead of ~33k with defaults):
- ``--system-prompt`` replaces Claude Code's own large system prompt;
- ``--tools`` exposes only the tools a task needs (none, or WebSearch);
- ``--setting-sources ""``, ``--strict-mcp-config``, ``--disable-slash-commands``: no
  project settings, MCP servers or skills;
- runs in an empty working directory so no CLAUDE.md is picked up;
- ``--json-schema`` makes the CLI validate output and return it as ``structured_output``.
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
from promise_monitor.llm.base import LLMAuthError, LLMError, LLMRequest, LLMResponse
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
            "json",
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
    try:
        payload: dict[str, Any] = json.loads(stdout)
    except json.JSONDecodeError as exc:
        detail = (stderr or stdout).strip()[:500]
        _raise_for_text(detail, f"exit {returncode}, non-JSON output")
        raise LLMError(f"exit {returncode}, non-JSON output: {detail}") from exc

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
    )


def _raise_for_text(text: str, context: str) -> None:
    low = text.lower()
    if any(m in low for m in _AUTH_MARKERS):
        raise LLMAuthError(f"{context}: {text[:200]}")
