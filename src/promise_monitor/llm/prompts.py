"""Load prompts from ``prompts/*.md``. Prompt text never lives in Python (AGENTS.md rule 10).

A prompt file has a system part and a user template, split by a line ``---USER---``.
The user template uses ``string.Template`` ``$placeholders`` so JSON braces need no escaping.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from string import Template

from promise_monitor.config import PROJECT_ROOT

PROMPTS_DIR = PROJECT_ROOT / "prompts"
SPLIT = "---USER---"


@dataclass(frozen=True)
class Prompt:
    name: str
    system: str
    user_template: str
    sha256: str

    def render(self, **values: str) -> tuple[str, str]:
        """Return (system, user) with ``$placeholders`` filled in both parts."""
        return (
            Template(self.system).substitute(**values),
            Template(self.user_template).substitute(**values),
        )


def load_prompt(name: str, prompts_dir: Path = PROMPTS_DIR) -> Prompt:
    path = prompts_dir / f"{name}.md"
    text = path.read_text(encoding="utf-8")
    if SPLIT not in text:
        raise ValueError(f"{path} must contain a '{SPLIT}' line")
    system, user = text.split(SPLIT, 1)
    return Prompt(
        name=name,
        system=system.strip(),
        user_template=user.strip(),
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )
