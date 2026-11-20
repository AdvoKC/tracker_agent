from __future__ import annotations

import pytest
from typer.testing import CliRunner

from promise_monitor.cli import app

runner = CliRunner()


def test_help_lists_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("run", "discover", "publish", "check-config", "db-check", "version", "notify-test"):
        assert cmd in result.output


def test_check_config_ok() -> None:
    result = runner.invoke(app, ["check-config"])
    assert result.exit_code == 0, result.output
    assert "states=37" in result.output


@pytest.mark.parametrize("cmd", [["run"], ["publish"], ["monitor"], ["verify"], ["write"]])
def test_db_commands_fail_cleanly_without_db(
    cmd: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PM_DATABASE_URL", raising=False)
    result = runner.invoke(app, cmd)
    assert result.exit_code == 1
    assert "PM_DATABASE_URL" in result.output


def test_notify_test_requires_smtp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PM_ADMIN_EMAIL", "admin@example.org")
    monkeypatch.delenv("PM_SMTP_USER", raising=False)
    monkeypatch.delenv("PM_SMTP_PASSWORD", raising=False)
    result = runner.invoke(app, ["notify-test"])
    assert result.exit_code == 1 and "SMTP not configured" in result.output


def test_db_check_without_url_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PM_DATABASE_URL", raising=False)
    result = runner.invoke(app, ["db-check"])
    assert result.exit_code == 1
    assert "PM_DATABASE_URL" in result.output
