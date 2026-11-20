"""SubmissionClient: the only code that writes to admin.promisetracker.ng (PRD §8d)."""

from __future__ import annotations

import uuid

import pytest

from promise_monitor.api.submissions import (
    Pacer,
    ScopeDeniedError,
    SubmissionClient,
    SubmissionError,
    SubmissionPayload,
    TokenRejectedError,
)
from promise_monitor.config import AppConfig, load_app_config
from tests.conftest import REPO_ROOT
from tests.fake_submissions import FakeSubmissionBackend, FakeToken, with_submissions

METER = str(uuid.uuid4())
PROMISE = str(uuid.uuid4())
TOKEN = "ptsub_" + "a" * 40


@pytest.fixture
def cfg() -> AppConfig:
    return with_submissions(load_app_config(REPO_ROOT / "config"))


@pytest.fixture
def backend() -> FakeSubmissionBackend:
    b = FakeSubmissionBackend(promise_meters={PROMISE: METER})
    b.tokens[TOKEN] = FakeToken(meter_ids=[METER])
    return b


def client(
    cfg: AppConfig,
    backend: FakeSubmissionBackend,
    token: str = TOKEN,
    sleeps: list[float] | None = None,
) -> SubmissionClient:
    slept = sleeps if sleeps is not None else []
    pacer = Pacer(cfg.tunables.api.min_interval_seconds, sleep=slept.append, clock=lambda: 0.0)
    return SubmissionClient(
        cfg.tunables.submissions,
        cfg.tunables.api,
        token,
        transport=backend.transport(),
        pacer=pacer,
        jitter=lambda: 0.0,
    )


def payload(**kw: object) -> SubmissionPayload:
    base: dict[str, object] = {
        "meter_id": METER,
        "promise_id": PROMISE,
        "headline": "Ports reform bill passes",
        "body_markdown": "Body with [a link](https://punchng.com/a).",
        "body_html": '<p>Body with <a href="https://punchng.com/a">a link</a>.</p>',
        "sources": [{"url": "https://punchng.com/a", "title": "A"}],
        "external_ref": "tracker-agents:article:x",
        "approved_at": "2026-11-20T09:00:00+00:00",
    }
    base.update(kw)
    return SubmissionPayload.model_validate(base)


def test_whoami_returns_scope_and_sends_bearer_and_user_agent(
    cfg: AppConfig, backend: FakeSubmissionBackend
) -> None:
    info = client(cfg, backend).whoami()
    assert info.meter_ids == [METER]
    assert "submissions:write" in info.scopes
    req = backend.requests[-1]
    assert req.headers["authorization"] == f"Bearer {TOKEN}"
    assert req.headers["user-agent"].startswith(cfg.tunables.api.user_agent)


def test_unknown_token_is_rejected(cfg: AppConfig, backend: FakeSubmissionBackend) -> None:
    with pytest.raises(TokenRejectedError):
        client(cfg, backend, token="nope").whoami()  # noqa: S106


def test_create_is_idempotent_on_the_article_id(
    cfg: AppConfig, backend: FakeSubmissionBackend
) -> None:
    c = client(cfg, backend)
    key = str(uuid.uuid4())
    first = c.create(payload(), idempotency_key=key)
    again = c.create(payload(), idempotency_key=key)
    assert first.submission_id == again.submission_id
    assert len(backend.submissions) == 1
    assert backend.requests[-1].headers["idempotency-key"] == key


def test_create_outside_token_scope_is_denied(
    cfg: AppConfig, backend: FakeSubmissionBackend
) -> None:
    with pytest.raises(ScopeDeniedError):
        client(cfg, backend).create(payload(meter_id=str(uuid.uuid4())), idempotency_key="k")


def test_validation_errors_are_reported(cfg: AppConfig, backend: FakeSubmissionBackend) -> None:
    with pytest.raises(SubmissionError) as exc:
        client(cfg, backend).create(payload(promise_id=str(uuid.uuid4())), idempotency_key="k")
    assert exc.value.status == 422
    assert "promise_id" in str(exc.value)


def test_server_errors_are_retried_with_backoff_then_succeed(
    cfg: AppConfig, backend: FakeSubmissionBackend
) -> None:
    backend.fail_next = [503, 502]
    sleeps: list[float] = []
    receipt = client(cfg, backend, sleeps=sleeps).create(payload(), idempotency_key="k")
    assert receipt.state == "pending_review"
    assert len(backend.submissions) == 1
    assert [s for s in sleeps if s > 1] == [2.0, 4.0]  # backoff_base * 2**attempt


def test_status_lookup(cfg: AppConfig, backend: FakeSubmissionBackend) -> None:
    c = client(cfg, backend)
    sid = c.create(payload(), idempotency_key="k").submission_id
    backend.publish(sid, update_id="u-1", note="thanks")
    st = c.get(sid)
    assert (st.state, st.update_id, st.reviewer_note) == ("published", "u-1", "thanks")


def test_pacer_spaces_requests(cfg: AppConfig, backend: FakeSubmissionBackend) -> None:
    sleeps: list[float] = []
    c = client(cfg, backend, sleeps=sleeps)
    c.whoami()
    c.whoami()
    assert sleeps == [cfg.tunables.api.min_interval_seconds]


def test_token_never_appears_in_error_text(cfg: AppConfig, backend: FakeSubmissionBackend) -> None:
    with pytest.raises(SubmissionError) as exc:
        client(cfg, backend).create(payload(meter_id=str(uuid.uuid4())), idempotency_key="k")
    assert TOKEN not in str(exc.value) and TOKEN not in repr(exc.value)


def test_http_base_url_is_refused(cfg: AppConfig) -> None:
    with pytest.raises(ValueError, match="https"):
        cfg.tunables.submissions.model_validate(
            {**cfg.tunables.submissions.model_dump(), "base_url": "http://pt.test/x"}
        )
