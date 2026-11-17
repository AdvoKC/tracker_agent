from __future__ import annotations

import pytest
from typer.testing import CliRunner

from promise_monitor.cli import app

runner = CliRunner()


def test_help_lists_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("run", "discover", "publish", "check-config", "db-check", "version"):
        assert cmd in result.output


def test_check_config_ok() -> None:
    result = runner.invoke(app, ["check-config"])
    assert result.exit_code == 0, result.output
    assert "states=37" in result.output


@pytest.mark.parametrize("cmd", [["run", "--dry-run"], ["publish"]])
def test_unimplemented_commands_fail_loudly(cmd: list[str]) -> None:
    result = runner.invoke(app, cmd)
    assert result.exit_code == 2  # cron must never see a silent success


def test_db_check_without_url_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PM_DATABASE_URL", raising=False)
    result = runner.invoke(app, ["db-check"])
    assert result.exit_code == 1
    assert "PM_DATABASE_URL" in result.output
