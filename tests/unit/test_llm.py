from __future__ import annotations

import json
import subprocess
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from promise_monitor.config import LLMConfig, load_app_config
from promise_monitor.db.base import Base
from promise_monitor.db.models import LLMCall, Run
from promise_monitor.llm.base import LLMAuthError, LLMError, LLMRequest
from promise_monitor.llm.claude_cli import ClaudeCLIClient, parse_cli_output
from promise_monitor.llm.prompts import load_prompt
from promise_monitor.llm.recording import RecordingLLMClient
from tests.conftest import REPO_ROOT
from tests.fakes import FakeLLM

REQ = LLMRequest(
    task="news_scout",
    prompt_name="news_scout",
    system="SYS",
    user="USER",
    schema={"type": "object"},
    tools=("WebSearch",),
)


@pytest.fixture
def llm_cfg() -> LLMConfig:
    return load_app_config(REPO_ROOT / "config").tunables.llm


def ok_stdout(output: dict[str, Any]) -> str:
    return json.dumps(
        {
            "is_error": False,
            "structured_output": output,
            "total_cost_usd": 0.01,
            "num_turns": 2,
            "modelUsage": {"claude-sonnet-5-5": {}},
        }
    )


class FakeRunner:
    def __init__(self, *results: subprocess.CompletedProcess[str] | Exception) -> None:
        self.results = list(results)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append({"argv": argv, **kw})
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def proc(stdout: str, rc: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], rc, stdout, stderr)


def test_argv_is_trimmed_and_tool_scoped(llm_cfg: LLMConfig, tmp_path: Path) -> None:
    c = ClaudeCLIClient(llm_cfg, workdir=tmp_path)
    argv = c.argv(REQ)
    assert argv[:2] == ["claude", "-p"]
    assert argv[argv.index("--model") + 1] == llm_cfg.models.news_scout
    assert argv[argv.index("--system-prompt") + 1] == "SYS"
    assert argv[argv.index("--tools") + 1] == "WebSearch"
    assert argv[argv.index("--allowedTools") + 1] == "WebSearch"
    assert "--bare" not in argv  # bare mode ignores OAuth logins
    for flag in ("--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"):
        assert flag in argv
    assert argv[-2:] == ["--", "USER"]  # prompt can't be swallowed by a variadic flag
    no_tools = c.argv(LLMRequest("classifier", "classifier", "S", "U", {}))
    assert no_tools[no_tools.index("--tools") + 1] == ""
    assert "--allowedTools" not in no_tools


def test_run_success_uses_empty_workdir_and_no_stdin(llm_cfg: LLMConfig, tmp_path: Path) -> None:
    runner = FakeRunner(proc(ok_stdout({"results": []})))
    resp = ClaudeCLIClient(llm_cfg, runner=runner, workdir=tmp_path).run(REQ)
    assert resp.output == {"results": []}
    assert resp.model == "claude-sonnet-5-5"
    assert resp.cost_usd == 0.01
    assert runner.calls[0]["cwd"] == tmp_path
    assert runner.calls[0]["stdin"] == subprocess.DEVNULL


def test_retry_then_success(llm_cfg: LLMConfig, tmp_path: Path) -> None:
    runner = FakeRunner(subprocess.TimeoutExpired("claude", 1), proc(ok_stdout({"a": 1})))
    c = ClaudeCLIClient(llm_cfg, runner=runner, workdir=tmp_path, sleep=lambda _s: None)
    assert c.run(REQ).output == {"a": 1}
    assert len(runner.calls) == 2


def test_auth_error_not_retried(llm_cfg: LLMConfig, tmp_path: Path) -> None:
    out = json.dumps({"is_error": True, "result": "Not logged in · Please run /login"})
    runner = FakeRunner(proc(out, rc=1))
    c = ClaudeCLIClient(llm_cfg, runner=runner, workdir=tmp_path, sleep=lambda _s: None)
    with pytest.raises(LLMAuthError):
        c.run(REQ)
    assert len(runner.calls) == 1


@pytest.mark.parametrize(
    ("stdout", "exc"),
    [
        ("garbage", LLMError),
        (json.dumps({"is_error": False, "result": "text only"}), LLMError),
        (json.dumps({"is_error": True, "result": "overloaded"}), LLMError),
    ],
)
def test_parse_failures(stdout: str, exc: type[Exception]) -> None:
    with pytest.raises(exc):
        parse_cli_output(stdout, "", 1, 5)


def test_missing_binary_is_auth_class_error(llm_cfg: LLMConfig, tmp_path: Path) -> None:
    runner = FakeRunner(FileNotFoundError("claude"))
    with pytest.raises(LLMAuthError):
        ClaudeCLIClient(llm_cfg, runner=runner, workdir=tmp_path).run(REQ)


@pytest.fixture
def db() -> Iterator[Session]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def test_recording_success_failure_and_auth_lockout(db: Session) -> None:
    run = Run(started_at=datetime.now(UTC), trigger="test")
    db.add(run)
    db.flush()
    inner = FakeLLM({"ok": True}, LLMError("boom"), LLMAuthError("Not logged in"))
    rec = RecordingLLMClient(inner, db, run.run_id)
    assert rec.run(REQ).output == {"ok": True}
    with pytest.raises(LLMError):
        rec.run(REQ)
    with pytest.raises(LLMAuthError):
        rec.run(REQ)
    with pytest.raises(LLMError, match="disabled"):
        rec.run(REQ)  # never reaches the inner client
    rows = db.scalars(select(LLMCall).order_by(LLMCall.id)).all()
    assert [r.ok for r in rows] == [True, False, False]
    assert rows[0].output == {"ok": True} and rows[0].model == "fake-model"
    assert all(len(r.prompt_sha256) == 64 for r in rows)
    assert len(inner.requests) == 3
    assert isinstance(rows[0].run_id, uuid.UUID)


@pytest.mark.parametrize(
    ("name", "values"),
    [
        (
            "classifier",
            {
                "max_delta": "15",
                "promise_title": "t",
                "meter_name": "m",
                "status": "s",
                "base_score": "50",
                "threshold": "60",
                "changes": "- c",
            },
        ),
        (
            "news_scout",
            {"allowed_domains": "a.ng", "max_per_promise": "3", "today": "d", "promises": "P1"},
        ),
    ],
)
def test_prompts_render_with_exact_placeholders(name: str, values: dict[str, str]) -> None:
    p = load_prompt(name)
    system, user = p.render(**values)
    assert "$" not in system + user
    assert len(p.sha256) == 64
