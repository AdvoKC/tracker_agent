"""Build the configured LLM backend."""

from __future__ import annotations

from promise_monitor.config import LLMConfig
from promise_monitor.llm.base import LLMClient
from promise_monitor.llm.claude_cli import ClaudeCLIClient


def make_llm(cfg: LLMConfig) -> LLMClient:
    if cfg.backend == "claude_cli":
        return ClaudeCLIClient(cfg)
    raise ValueError(f"unknown LLM backend: {cfg.backend}")  # pragma: no cover
