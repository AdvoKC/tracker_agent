"""Sending approved articles to Promise Tracker's review queue (PRD §8d, P12).

- ``save_token`` / ``remove_token``: a coordinator's backend-issued token, checked with
  ``whoami`` and stored ENCRYPTED (Fernet, ``PM_TOKEN_KEY``). Never shown again or logged.
- ``blockers`` / ``send``: one approved article → one submission. Allowed only when ALL hold:
  approved; not sent before; ``submissions.enabled`` and ``publish.enabled``; the actor has an
  ACTIVE assignment to the meter (superadmins are not exempt); the actor's own token is live
  and covers the meter; the current text passes QA. The backend checks scope again.
  The article id is the idempotency key, so a retry can never create a second submission.
- ``poll``: the daily run checks open submissions and reports what Promise Tracker decided.
- The Classifier recognises our own update once published (``remote_update_id`` or a close
  text match on ``body_text``) and logs it as ``own_submission``, never as new evidence.
Every send, refusal and failure is written to the audit log. Nothing here publishes anything.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.orm import Session

from promise_monitor import access
from promise_monitor.access import AccessError
from promise_monitor.agents.publisher import render_markdown
from promise_monitor.api.submissions import (
    Pacer,
    SourceRef,
    SubmissionClient,
    SubmissionError,
    SubmissionPayload,
)
from promise_monitor.config import AppConfig, Settings
from promise_monitor.db.models import Article, PtSubmission, PtToken, Source, User
from promise_monitor.domain.normalize import html_to_text
from promise_monitor.logging_setup import get_logger
from promise_monitor.review import check_text

log = get_logger(__name__)

ClientFactory = Callable[[str], SubmissionClient]  # token → client
SENDABLE_STATES = ("approved", "published")
SCOPE = "submissions:write"
EXTERNAL_REF = "tracker-agents:article:{article_id}"


# --------------------------------------------------------------------------- token storage


class TokenBox:
    """Encrypts tokens at rest. The key lives in ``PM_TOKEN_KEY`` (outside the repo)."""

    def __init__(self, key: str) -> None:
        try:
            self._f = Fernet(key.encode())
        except (ValueError, TypeError) as exc:
            raise ValueError("PM_TOKEN_KEY is not a valid Fernet key") from exc

    def encrypt(self, token: str) -> str:
        return self._f.encrypt(token.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._f.decrypt(ciphertext.encode()).decode()
        except InvalidToken as exc:
            raise AccessError(
                "stored token can't be decrypted (was PM_TOKEN_KEY changed?)"
            ) from exc


def token_box(settings: Settings) -> TokenBox | None:
    return TokenBox(settings.token_key) if settings.token_key else None


def _aware(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=UTC) if dt is not None and dt.tzinfo is None else dt


def active_token(session: Session, user_id: uuid.UUID, now: datetime) -> PtToken | None:
    tok = session.scalars(
        select(PtToken)
        .where(PtToken.user_id == user_id, PtToken.revoked_at.is_(None))
        .order_by(PtToken.id.desc())
        .limit(1)
    ).first()
    if tok is None:
        return None
    expires = _aware(tok.expires_at)
    return None if expires is not None and expires <= now else tok


def save_token(
    session: Session,
    user: User,
    raw: str,
    *,
    box: TokenBox | None,
    client_for: ClientFactory,
    now: datetime,
) -> PtToken:
    """Check ``raw`` with the backend, then store it encrypted (replacing any previous one)."""
    if box is None:
        raise AccessError("PM_TOKEN_KEY isn't set on the server; ask the administrator")
    raw = raw.strip()
    if not 16 <= len(raw) <= 512 or any(c.isspace() for c in raw):
        raise AccessError("that doesn't look like a Promise Tracker token")
    try:
        with client_for(raw) as client:
            info = client.whoami()
    except SubmissionError as exc:
        raise AccessError(f"token check failed: {exc}") from None
    if SCOPE not in info.scopes:
        raise AccessError(f"this token can't submit updates (missing scope {SCOPE})")
    expires = _aware(info.expires_at)
    if expires is not None and expires <= now:
        raise AccessError("this token has already expired")
    for old in session.scalars(
        select(PtToken).where(PtToken.user_id == user.user_id, PtToken.revoked_at.is_(None))
    ):
        old.revoked_at = now
    tok = PtToken(
        user_id=user.user_id,
        token_ciphertext=box.encrypt(raw),
        remote_token_id=info.token_id,
        last4=raw[-4:],
        meter_ids=sorted({str(uuid.UUID(m)) for m in info.meter_ids}),
        holder_name=(info.holder_name or "")[:255] or None,
        expires_at=expires,
        created_at=now,
    )
    session.add(tok)
    session.flush()
    access.audit(
        session,
        user,
        "pt_token.save",
        "user",
        user.user_id,
        now,
        after={
            "remote_token_id": tok.remote_token_id,
            "last4": tok.last4,
            "meter_ids": tok.meter_ids,
            "expires_at": expires.isoformat() if expires else None,
        },
    )
    return tok


def remove_token(session: Session, user: User, now: datetime) -> bool:
    removed = False
    for tok in session.scalars(
        select(PtToken).where(PtToken.user_id == user.user_id, PtToken.revoked_at.is_(None))
    ):
        tok.revoked_at, removed = now, True
        access.audit(
            session, user, "pt_token.remove", "user", user.user_id, now, before={"last4": tok.last4}
        )
    session.flush()
    return removed


# --------------------------------------------------------------------------- sending


def submission_for(session: Session, article_id: uuid.UUID) -> PtSubmission | None:
    return session.scalars(
        select(PtSubmission).where(PtSubmission.article_id == article_id)
    ).first()


def blockers(
    session: Session,
    art: Article,
    user: User,
    cfg: AppConfig,
    now: datetime,
    *,
    box: TokenBox | None,
) -> list[str]:
    """Every reason ``user`` can't send ``art`` right now (empty = they can)."""
    if art.state not in SENDABLE_STATES:
        return ["only approved articles can be sent"]
    sent = submission_for(session, art.article_id)
    if sent is not None:
        return [f"already sent to Promise Tracker ({sent.remote_state.replace('_', ' ')})"]
    out: list[str] = []
    if not cfg.tunables.submissions.enabled:
        out.append("sending to Promise Tracker is switched off (submissions.enabled)")
    if not cfg.tunables.publish.enabled:
        out.append("shadow mode: publishing is off, so nothing is sent yet (publish.enabled)")
    if not access.active_assignments(session, now, meter_id=art.meter_id, user_id=user.user_id):
        out.append("only a coordinator currently assigned to this meter can send it")
    if box is None:
        out.append("PM_TOKEN_KEY isn't set on the server")
    tok = active_token(session, user.user_id, now)
    if tok is None:
        out.append("add your Promise Tracker token first (Account → Promise Tracker token)")
    elif str(art.meter_id) not in tok.meter_ids:
        out.append("your Promise Tracker token doesn't cover this meter")
    rep = check_text(session, art, art.headline, art.body_markdown, cfg, now)
    if not rep.passed:
        out.append("the text fails QA: " + "; ".join(rep.issues[:3]))
    return out


def plain_text(markdown: str) -> str:
    return html_to_text(render_markdown(markdown)) or ""


def build_payload(session: Session, art: Article) -> SubmissionPayload:
    sources = [
        SourceRef(
            url=src.final_url or src.url,
            title=src.title or src.search_title,
            publisher=src.publisher,
            published_date=src.published_date.isoformat() if src.published_date else None,
        )
        for src in session.scalars(
            select(Source)
            .where(Source.source_id.in_(art.source_ids or []))
            .order_by(Source.source_id)
        )
    ]
    approved = _aware(art.approved_at)
    return SubmissionPayload(
        meter_id=str(art.meter_id),
        promise_id=str(art.promise_id),
        headline=art.headline,
        body_markdown=art.body_markdown,
        body_html=render_markdown(art.body_markdown),
        sources=sources,
        external_ref=EXTERNAL_REF.format(article_id=art.article_id),
        approved_at=approved.isoformat() if approved else "",
    )


def send(
    session: Session,
    art: Article,
    *,
    actor: User,
    cfg: AppConfig,
    box: TokenBox | None,
    client_for: ClientFactory,
    now: datetime,
) -> PtSubmission:
    """Send ``art`` for review. Raises AccessError (already audited) when refused or failed.

    The caller should COMMIT after an AccessError too, so the audit row is kept.
    """
    why = blockers(session, art, actor, cfg, now, box=box)
    if why:
        _audit(session, actor, "pt_submission.refused", art, now, {"reasons": why})
        raise AccessError("Not sent: " + "; ".join(why))
    assert box is not None
    tok = active_token(session, actor.user_id, now)
    assert tok is not None
    payload = build_payload(session, art)
    digest = hashlib.sha256(
        json.dumps(payload.model_dump(mode="json"), sort_keys=True).encode()
    ).hexdigest()
    try:
        with client_for(box.decrypt(tok.token_ciphertext)) as client:
            receipt = client.create(payload, idempotency_key=str(art.article_id))
    except SubmissionError as exc:
        _audit(
            session,
            actor,
            "pt_submission.failed",
            art,
            now,
            {"status": exc.status, "error": str(exc)[:300]},
        )
        log.warning("submission_failed", article_id=str(art.article_id), status=exc.status)
        raise AccessError(f"Not sent: {exc}") from None
    sub = PtSubmission(
        article_id=art.article_id,
        meter_id=art.meter_id,
        promise_id=art.promise_id,
        submitted_by=actor.user_id,
        token_row_id=tok.id,
        payload_sha256=digest,
        body_text=plain_text(art.body_markdown),
        remote_id=receipt.submission_id,
        remote_state=receipt.state,
        submitted_at=now,
    )
    session.add(sub)
    session.flush()
    _audit(
        session,
        actor,
        "pt_submission.send",
        art,
        now,
        {"remote_id": sub.remote_id, "state": sub.remote_state, "payload_sha256": digest},
    )
    log.info("submission_sent", article_id=str(art.article_id), remote_id=sub.remote_id)
    return sub


def _audit(
    session: Session,
    actor: User,
    action: str,
    art: Article,
    now: datetime,
    after: dict[str, object],
) -> None:
    access.audit(session, actor, action, "article", art.article_id, now, after=after)
    session.flush()


# --------------------------------------------------------------------------- polling


@dataclass
class PollResult:
    checked: int = 0
    changed: dict[uuid.UUID, list[str]] = field(default_factory=dict)  # meter → digest lines
    errors: list[str] = field(default_factory=list)


def poll(
    session: Session,
    cfg: AppConfig,
    *,
    box: TokenBox | None,
    client_for: ClientFactory,
    now: datetime,
) -> PollResult:
    """Check open submissions (oldest-checked first, capped) with each submitter's token."""
    res = PollResult()
    limit = cfg.tunables.submissions.poll_max_per_run
    if not cfg.tunables.submissions.enabled or limit == 0:
        return res
    open_subs = list(
        session.scalars(
            select(PtSubmission)
            .where(PtSubmission.remote_state == "pending_review")
            .order_by(PtSubmission.last_checked_at.is_(None).desc(), PtSubmission.last_checked_at)
            .limit(limit)
        )
    )
    if open_subs and box is None:
        res.errors.append("PM_TOKEN_KEY isn't set: submission status not checked")
        return res
    assert box is not None or not open_subs
    for sub in open_subs:
        tok = session.get(PtToken, sub.token_row_id)
        expires = _aware(tok.expires_at) if tok else None
        if tok is None or tok.revoked_at is not None or (expires is not None and expires <= now):
            sub.error = "the sender's token was replaced, removed or expired: status unknown"
            sub.last_checked_at = now
            res.errors.append(f"submission {sub.remote_id}: {sub.error}")
            continue
        assert box is not None
        try:
            with client_for(box.decrypt(tok.token_ciphertext)) as client:
                st = client.get(sub.remote_id)
        except (SubmissionError, AccessError) as exc:
            sub.error, sub.last_checked_at = str(exc)[:300], now
            res.errors.append(f"submission {sub.remote_id}: {sub.error}")
            continue
        res.checked += 1
        sub.last_checked_at, sub.error = now, None
        if st.state == sub.remote_state:
            continue
        sub.remote_state = st.state
        sub.remote_update_id = st.update_id
        sub.reviewer_note = (st.reviewer_note or "")[:2000] or None
        art = session.get(Article, sub.article_id)
        headline = art.headline if art else str(sub.article_id)
        note = f" ({sub.reviewer_note[:160]})" if sub.reviewer_note else ""
        res.changed.setdefault(sub.meter_id, []).append(
            f"Promise Tracker {st.state.replace('_', ' ')}: {headline}{note}"
        )
    session.flush()
    return res


def default_client_factory(cfg: AppConfig, contact: str | None = None) -> ClientFactory:
    """Real clients sharing one Pacer, so several tokens still mean 1 request per second."""
    pacer = Pacer(cfg.tunables.api.min_interval_seconds)

    def make(token: str) -> SubmissionClient:
        return SubmissionClient(
            cfg.tunables.submissions, cfg.tunables.api, token, contact=contact, pacer=pacer
        )

    return make
