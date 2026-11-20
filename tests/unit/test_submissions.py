"""P12 domain: tokens, who may send, sending, polling, and self-update suppression (PRD §8d)."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, cast

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from promise_monitor import access, review, submissions
from promise_monitor.access import AccessError
from promise_monitor.api.submissions import Pacer, SubmissionClient
from promise_monitor.config import AppConfig
from promise_monitor.db.models import Article, AuditLog, PtSubmission, PtToken, User
from promise_monitor.notify import MemoryMailer
from promise_monitor.pipeline import DailyRun, PipelineDeps
from promise_monitor.submissions import TokenBox
from tests.fake_submissions import FakeSubmissionBackend, FakeToken, with_submissions
from tests.fakes import FakeLLM
from tests.unit.test_dashboard_full import NOW, cfg, factory, world  # noqa: F401 (fixtures)
from tests.unit.test_monitor import METER_ID

TOKEN = "ptsub_" + "k" * 40
OTHER = "ptsub_" + "o" * 40


@pytest.fixture
def on(cfg: AppConfig) -> AppConfig:  # noqa: F811
    return with_submissions(cfg)


@pytest.fixture
def box() -> TokenBox:
    return TokenBox(Fernet.generate_key().decode())


@pytest.fixture
def backend(world: dict[str, Any]) -> FakeSubmissionBackend:  # noqa: F811
    b = FakeSubmissionBackend(promise_meters={str(world["promise"]): str(METER_ID)})
    b.tokens[TOKEN] = FakeToken(meter_ids=[str(METER_ID)])
    b.tokens[OTHER] = FakeToken(meter_ids=[str(world["kano"])])
    return b


def factory_for(backend: FakeSubmissionBackend, conf: AppConfig) -> submissions.ClientFactory:
    pacer = Pacer(1.0, sleep=lambda _s: None, clock=lambda: 0.0)

    def make(token: str) -> SubmissionClient:
        return SubmissionClient(
            conf.tunables.submissions,
            conf.tunables.api,
            token,
            transport=backend.transport(),
            pacer=pacer,
            jitter=lambda: 0.0,
        )

    return make


def user(s: Session, email: str) -> User:
    return s.scalars(select(User).where(User.email == email)).one()


def approved(s: Session, world: dict[str, Any], cfg: AppConfig) -> Article:  # noqa: F811
    art = s.get(Article, world["article"])
    assert art is not None
    rep = review.approve(s, art, actor=user(s, "amina@x.ng"), cfg=cfg, now=NOW)
    assert rep.passed, rep.issues
    return art


def with_token(
    s: Session, email: str, raw: str, box: TokenBox, backend: FakeSubmissionBackend, conf: AppConfig
) -> PtToken:
    return submissions.save_token(
        s, user(s, email), raw, box=box, client_for=factory_for(backend, conf), now=NOW
    )


# ----------------------------------------------------------------------------- tokens


def test_token_is_stored_encrypted_and_never_in_the_audit_log(
    factory: sessionmaker[Session],  # noqa: F811
    on: AppConfig,
    box: TokenBox,
    backend: FakeSubmissionBackend,
) -> None:
    with factory() as s:
        tok = with_token(s, "amina@x.ng", TOKEN, box, backend, on)
        s.commit()
        assert tok.token_ciphertext != TOKEN and TOKEN not in tok.token_ciphertext
        assert box.decrypt(tok.token_ciphertext) == TOKEN
        assert tok.last4 == TOKEN[-4:] and tok.meter_ids == [str(METER_ID)]
        dumped = json.dumps([[a.before, a.after] for a in s.scalars(select(AuditLog))])
        assert TOKEN not in dumped


def test_new_token_replaces_the_old_one(
    factory: sessionmaker[Session],  # noqa: F811
    on: AppConfig,
    box: TokenBox,
    backend: FakeSubmissionBackend,
) -> None:
    with factory() as s:
        first = with_token(s, "amina@x.ng", TOKEN, box, backend, on)
        second = with_token(s, "amina@x.ng", OTHER, box, backend, on)
        assert first.revoked_at == NOW and second.revoked_at is None
        assert submissions.active_token(s, user(s, "amina@x.ng").user_id, NOW) is second
        assert submissions.remove_token(s, user(s, "amina@x.ng"), NOW)
        assert submissions.active_token(s, user(s, "amina@x.ng").user_id, NOW) is None


@pytest.mark.parametrize(
    ("raw", "setup", "msg"),
    [
        ("short", None, "doesn't look like"),
        ("ptsub_" + "z" * 40, None, "token check failed"),
        (TOKEN, {"scopes": ["read"]}, "missing scope"),
        (TOKEN, {"expires_at": "2026-01-01T00:00:00+00:00"}, "expired"),
    ],
)
def test_bad_tokens_are_refused(
    factory: sessionmaker[Session],  # noqa: F811
    on: AppConfig,
    box: TokenBox,
    backend: FakeSubmissionBackend,
    raw: str,
    setup: dict[str, Any] | None,
    msg: str,
) -> None:
    for k, v in (setup or {}).items():
        setattr(backend.tokens[TOKEN], k, v)
    with factory() as s, pytest.raises(AccessError, match=msg):
        with_token(s, "amina@x.ng", raw, box, backend, on)


def test_no_token_key_means_no_tokens(
    factory: sessionmaker[Session],  # noqa: F811
    on: AppConfig,
    backend: FakeSubmissionBackend,
) -> None:
    with factory() as s, pytest.raises(AccessError, match="PM_TOKEN_KEY"):
        submissions.save_token(
            s,
            user(s, "amina@x.ng"),
            TOKEN,
            box=None,
            client_for=factory_for(backend, on),
            now=NOW,
        )


# ----------------------------------------------------------------------------- sending


def test_assigned_coordinator_sends_once(
    factory: sessionmaker[Session],  # noqa: F811
    world: dict[str, Any],  # noqa: F811
    on: AppConfig,
    box: TokenBox,
    backend: FakeSubmissionBackend,
) -> None:
    with factory() as s:
        art = approved(s, world, on)
        with_token(s, "amina@x.ng", TOKEN, box, backend, on)
        amina = user(s, "amina@x.ng")
        assert submissions.blockers(s, art, amina, on, NOW, box=box) == []
        sub = submissions.send(
            s, art, actor=amina, cfg=on, box=box, client_for=factory_for(backend, on), now=NOW
        )
        s.commit()
        assert sub.remote_state == "pending_review" and sub.submitted_by == amina.user_id
        req = backend.requests[-1]
        assert req.headers["idempotency-key"] == str(art.article_id)
        body = json.loads(req.content)
        assert body["meter_id"] == str(METER_ID) and body["promise_id"] == str(art.promise_id)
        assert body["body_markdown"] == art.body_markdown
        assert "<script" not in body["body_html"] and "<a href=" in body["body_html"]
        assert body["sources"] and body["external_ref"].endswith(str(art.article_id))
        assert "Promise Tracker" not in sub.body_text  # plain text, links → anchor text
        # a second send is refused and makes no request
        n = len(backend.requests)
        with pytest.raises(AccessError, match="already sent"):
            submissions.send(
                s, art, actor=amina, cfg=on, box=box, client_for=factory_for(backend, on), now=NOW
            )
        assert len(backend.requests) == n and len(backend.submissions) == 1
        actions = [a.action for a in s.scalars(select(AuditLog))]
        assert "pt_submission.send" in actions and "pt_submission.refused" in actions


def test_retry_after_lost_commit_reuses_the_backend_submission(
    factory: sessionmaker[Session],  # noqa: F811
    world: dict[str, Any],  # noqa: F811
    on: AppConfig,
    box: TokenBox,
    backend: FakeSubmissionBackend,
) -> None:
    """The POST succeeded but our commit didn't: sending again must not create a duplicate."""
    with factory() as s:
        art = approved(s, world, on)
        with_token(s, "amina@x.ng", TOKEN, box, backend, on)
        amina = user(s, "amina@x.ng")
        with factory_for(backend, on)(TOKEN) as c:
            first = c.create(submissions.build_payload(s, art), idempotency_key=str(art.article_id))
        sub = submissions.send(
            s, art, actor=amina, cfg=on, box=box, client_for=factory_for(backend, on), now=NOW
        )
        assert sub.remote_id == first.submission_id and len(backend.submissions) == 1


def _refusal(
    s: Session, art: Article, email: str, conf: AppConfig, box: TokenBox | None
) -> list[str]:
    return submissions.blockers(s, art, user(s, email), conf, NOW, box=box)


def test_who_may_send(
    factory: sessionmaker[Session],  # noqa: F811
    world: dict[str, Any],  # noqa: F811
    cfg: AppConfig,  # noqa: F811
    on: AppConfig,
    box: TokenBox,
    backend: FakeSubmissionBackend,
) -> None:
    with factory() as s:
        art = approved(s, world, on)
        backend.tokens[TOKEN].meter_ids.append(str(world["kano"]))
        with_token(s, "amina@x.ng", TOKEN, box, backend, on)
        with_token(s, "bayo@x.ng", OTHER, box, backend, on)
        admin = user(s, "admin@x.ng")
        # superadmins aren't exempt: no assignment → no send (even with a covering token)
        backend.tokens["ptsub_" + "s" * 40] = FakeToken(meter_ids=[str(METER_ID)])
        with_token(s, "admin@x.ng", "ptsub_" + "s" * 40, box, backend, on)
        assert any("assigned" in r for r in _refusal(s, art, "admin@x.ng", on, box))
        # another meter's coordinator: not assigned, and the token doesn't cover it
        bayo = _refusal(s, art, "bayo@x.ng", on, box)
        assert any("assigned" in r for r in bayo) and any("doesn't cover" in r for r in bayo)
        # shadow mode / switched off
        assert any("shadow mode" in r for r in _refusal(s, art, "amina@x.ng", cfg, box))
        off = with_submissions(cfg, enabled=False)
        assert any("switched off" in r for r in _refusal(s, art, "amina@x.ng", off, box))
        assert any("PM_TOKEN_KEY" in r for r in _refusal(s, art, "amina@x.ng", on, None))
        # a temporary cover whose window has ended can't send
        access.revoke(
            s,
            assignment_id=access.active_assignments(
                s, NOW, meter_id=METER_ID, user_id=user(s, "amina@x.ng").user_id
            )[0].assignment_id,
            actor=admin,
            now=NOW,
        )
        bayo_user = user(s, "bayo@x.ng")
        access.grant_temporary(
            s,
            meter_id=METER_ID,
            user=bayo_user,
            starts_at=NOW - timedelta(days=3),
            ends_at=NOW - timedelta(hours=1),
            reason="leave",
            actor=admin,
            now=NOW - timedelta(days=3),
        )
        assert any("assigned" in r for r in _refusal(s, art, "bayo@x.ng", on, box))
        assert any("assigned" in r for r in _refusal(s, art, "amina@x.ng", on, box))


def test_unapproved_or_failing_qa_is_not_sent(
    factory: sessionmaker[Session],  # noqa: F811
    world: dict[str, Any],  # noqa: F811
    on: AppConfig,
    box: TokenBox,
    backend: FakeSubmissionBackend,
) -> None:
    with factory() as s:
        art = s.get(Article, world["article"])
        assert art is not None
        with_token(s, "amina@x.ng", TOKEN, box, backend, on)
        assert _refusal(s, art, "amina@x.ng", on, box) == ["only approved articles can be sent"]
        art = approved(s, world, on)
        art.body_markdown += " Sadly."  # tampered after approval
        assert any("fails QA" in r for r in _refusal(s, art, "amina@x.ng", on, box))


def test_backend_refusal_is_audited_and_nothing_stored(
    factory: sessionmaker[Session],  # noqa: F811
    world: dict[str, Any],  # noqa: F811
    on: AppConfig,
    box: TokenBox,
    backend: FakeSubmissionBackend,
) -> None:
    with factory() as s:
        art = approved(s, world, on)
        with_token(s, "amina@x.ng", TOKEN, box, backend, on)
        backend.tokens[TOKEN].meter_ids.clear()  # backend narrowed the scope since
        with pytest.raises(AccessError, match="doesn't cover"):
            submissions.send(
                s,
                art,
                actor=user(s, "amina@x.ng"),
                cfg=on,
                box=box,
                client_for=factory_for(backend, on),
                now=NOW,
            )
        assert s.scalars(select(PtSubmission)).first() is None
        failed = s.scalars(select(AuditLog).where(AuditLog.action == "pt_submission.failed")).one()
        assert failed.after is not None and failed.after["status"] == 403


# ----------------------------------------------------------------------------- polling


def test_poll_reports_decisions(
    factory: sessionmaker[Session],  # noqa: F811
    world: dict[str, Any],  # noqa: F811
    on: AppConfig,
    box: TokenBox,
    backend: FakeSubmissionBackend,
) -> None:
    make = factory_for(backend, on)
    with factory() as s:
        art = approved(s, world, on)
        with_token(s, "amina@x.ng", TOKEN, box, backend, on)
        sub = submissions.send(
            s, art, actor=user(s, "amina@x.ng"), cfg=on, box=box, client_for=make, now=NOW
        )
        res = submissions.poll(s, on, box=box, client_for=make, now=NOW)
        assert res.checked == 1 and res.changed == {}
        backend.publish(sub.remote_id, update_id="upd-1", note="Looks good")
        res = submissions.poll(s, on, box=box, client_for=make, now=NOW)
        assert sub.remote_state == "published" and sub.remote_update_id == "upd-1"
        assert res.changed[METER_ID][0].startswith("Promise Tracker published:")
        # nothing open any more
        assert submissions.poll(s, on, box=box, client_for=make, now=NOW).checked == 0


def test_poll_without_a_live_token_records_why(
    factory: sessionmaker[Session],  # noqa: F811
    world: dict[str, Any],  # noqa: F811
    on: AppConfig,
    box: TokenBox,
    backend: FakeSubmissionBackend,
) -> None:
    make = factory_for(backend, on)
    with factory() as s:
        art = approved(s, world, on)
        with_token(s, "amina@x.ng", TOKEN, box, backend, on)
        sub = submissions.send(
            s, art, actor=user(s, "amina@x.ng"), cfg=on, box=box, client_for=make, now=NOW
        )
        submissions.remove_token(s, user(s, "amina@x.ng"), NOW)
        res = submissions.poll(s, on, box=box, client_for=make, now=NOW)
        assert res.checked == 0 and res.errors and sub.error
        assert (
            submissions.poll(
                s, with_submissions(on, enabled=False), box=box, client_for=make, now=NOW
            ).errors
            == []
        )


# ----------------------------------------------------------------------------- daily run


def test_daily_run_polls_and_tells_the_coordinator(
    factory: sessionmaker[Session],  # noqa: F811
    world: dict[str, Any],  # noqa: F811
    on: AppConfig,
    box: TokenBox,
    backend: FakeSubmissionBackend,
) -> None:
    make = factory_for(backend, on)
    with factory() as s:
        art = approved(s, world, on)
        with_token(s, "amina@x.ng", TOKEN, box, backend, on)
        sub = submissions.send(
            s, art, actor=user(s, "amina@x.ng"), cfg=on, box=box, client_for=make, now=NOW
        )
        s.commit()
        backend.reject(sub.remote_id, note="Needs a second source")
        mailer = MemoryMailer()
        deps = PipelineDeps(
            api=cast(Any, None),
            fetcher=cast(Any, None),
            llm=FakeLLM(),
            sheets=None,
            mailer=mailer,
            admin_email="admin@x.ng",
            token_box=box,
            submission_client=make,
        )
        daily = DailyRun(s, on, deps, now=NOW, clock=lambda: NOW)
        daily.submissions_poll()
        daily.notify()
        assert daily.report.stats["submissions"] == {"checked": 1, "changed": 1, "errors": 0}
        [(to, _subject, body)] = [m for m in mailer.sent if "to review" in m[1]]
        assert to == ["amina@x.ng"]
        assert "SENT   Promise Tracker rejected:" in body and "Needs a second source" in body
