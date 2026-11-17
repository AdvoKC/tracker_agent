from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from promise_monitor.config import get_settings

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Tests never read the developer's .env or write to the real logs/ dir."""
    monkeypatch.chdir(tmp_path)  # Settings reads .env relative to cwd
    monkeypatch.setenv("PM_ENV", "test")
    monkeypatch.setenv("PM_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("PM_CONFIG_DIR", str(REPO_ROOT / "config"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
