from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from promise_monitor.agents.publisher import publish_approved, render_markdown, render_site
from promise_monitor.config import AppConfig, load_app_config
from promise_monitor.db.models import Article, ArticleGroup, ChangeEvent, Meter, Run
from promise_monitor.notify import MemoryMailer, Notifier, RunReport, heartbeat_issues
from tests.conftest import REPO_ROOT
from tests.unit.test_monitor import METER_ID, FakeSite
from tests.unit.test_monitor import session as session
from tests.unit.test_monitor import site as site
from tests.unit.test_review_queue import drafted as drafted
from tests.unit.test_verifier import NOW


@pytest.fixture
def app_cfg() -> AppConfig:
    return load_app_config(REPO_ROOT / "config")


def approve(session: Session, art: Article) -> None:
    art.state = "approved"
    art.approved_at = NOW
    session.commit()


# ----------------------------------------------------------------------------- publisher


def test_markdown_is_safe_and_links_are_noopener() -> None:
    html = render_markdown('Hi <script>alert(1)</script> [the report](https://a.ng/x "t")')
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert 'rel="noopener noreferrer"' in html and 'href="https://a.ng/x"' in html


def test_only_approved_articles_are_published_and_rendered(
    drafted: Article,
    session: Session,
    app_cfg: AppConfig,
    tmp_path: Path,
) -> None:
    out = tmp_path / "site"
    assert publish_approved(session, NOW) == []  # in_review is never published
    res = render_site(session, app_cfg, NOW, out)
    assert "No updates have been published yet" in (out / "index.html").read_text()
    assert res.pages == 1

    approve(session, drafted)
    assert publish_approved(session, NOW) == [drafted.article_id]
    render_site(session, app_cfg, NOW, out)
    art = session.get(Article, drafted.article_id)
    assert art is not None and art.state == "published" and art.published_at is not None
    group = session.get(ArticleGroup, art.article_group_id)
    assert group is not None and group.state == "published"
    assert set(
        session.scalars(
            select(ChangeEvent.state).where(ChangeEvent.article_group_id == group.group_id)
        )
    ) == {"published"}
    pages = list((out / "meters").glob("*.html"))
    assert len(pages) == 1
    page = pages[0].read_text()
    assert art.headline in page
    assert 'class="badge kept"' in page and "(was " in page
    assert 'rel="noopener noreferrer"' in page
    assert (out / "style.css").exists()
    assert oct(out.stat().st_mode & 0o777) == "0o755"
    index = (out / "index.html").read_text()
    assert "1 update ·" in index and pages[0].name in index


def test_headline_is_escaped(
    drafted: Article, session: Session, app_cfg: AppConfig, tmp_path: Path
) -> None:
    drafted.headline = "Budget <b>bold</b> & more"
    approve(session, drafted)
    publish_approved(session, NOW)
    render_site(session, app_cfg, NOW, tmp_path / "site")
    page = next((tmp_path / "site" / "meters").glob("*.html")).read_text()
    assert "Budget &lt;b&gt;bold&lt;/b&gt; &amp; more" in page


def test_failed_render_keeps_previous_site(
    session: Session,
    app_cfg: AppConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = tmp_path / "site"
    render_site(session, app_cfg, NOW, out)
    before = (out / "index.html").read_text()

    def boom(*a: object, **k: object) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr("promise_monitor.agents.publisher.shutil.copyfile", boom)
    with pytest.raises(RuntimeError):
        render_site(session, app_cfg, NOW + timedelta(hours=1), out)
    assert (out / "index.html").read_text() == before
    assert [p.name for p in tmp_path.iterdir()] == ["site"]  # temp dir cleaned up


# ----------------------------------------------------------------------------- notifier


def report(**kw: object) -> RunReport:
    r = RunReport(run_id=uuid.uuid4(), started_at=NOW)
    for k, v in kw.items():
        setattr(r, k, v)
    return r


def test_digest_goes_to_coordinator_or_admin(
    session: Session, site: FakeSite, app_cfg: AppConfig
) -> None:
    from tests.unit.test_monitor import Runner

    Runner(session, site, app_cfg.tunables.monitor)()  # creates nothing new, but meter exists
    mailer = MemoryMailer()
    rep = report(
        new_rows={METER_ID: ["draft: Senate passes bill"]}, stale={METER_ID: ["Old draft"]}
    )
    Notifier(session, mailer, admin_email="admin@x.ng").notify(rep)
    [(to, subject, body)] = mailer.sent  # status ok, no warnings → no admin alert
    assert to == ["admin@x.ng"] and "2 item(s) to review" in subject
    assert "NEW    draft: Senate passes bill" in body and "STALE  Old draft" in body
    assert "no coordinator assigned" in body

    meter = session.get(Meter, METER_ID)
    assert meter is not None
    meter.coordinator_email = "coord@x.ng"
    mailer2 = MemoryMailer()
    Notifier(session, mailer2, admin_email="admin@x.ng").notify(rep)
    assert mailer2.sent[0][0] == ["coord@x.ng"]


def test_admin_alert_only_when_something_is_wrong(session: Session) -> None:
    mailer = MemoryMailer()
    Notifier(session, mailer, admin_email="admin@x.ng").notify(report())
    assert mailer.sent == []
    bad = report()
    bad.fail_stage("verify", "RuntimeError: boom")
    bad.urgent.append("the last 2 daily runs FAILED")
    Notifier(session, mailer, admin_email="admin@x.ng").notify(bad)
    [(_to, subject, body)] = mailer.sent
    assert subject.startswith("[promise-monitor] URGENT")
    assert "verify: RuntimeError: boom" in body and "last 2 daily runs FAILED" in body


def test_email_failure_never_raises(session: Session) -> None:
    class Broken:
        def send(self, to: list[str], subject: str, body: str) -> None:
            raise OSError("smtp down")

    bad = report()
    bad.degrade("x")
    res = Notifier(session, Broken(), admin_email="admin@x.ng").notify(bad)
    assert res.failures == 1 and res.admin_alerts == 0


def test_heartbeat_detects_consecutive_failures_and_missed_runs(
    session: Session, app_cfg: AppConfig
) -> None:
    ncfg = app_cfg.tunables.notify
    current = uuid.uuid4()
    assert heartbeat_issues(session, ncfg, NOW, current) == []  # first ever run: nothing to say
    for days in (3, 2, 1):
        session.add(
            Run(
                started_at=NOW - timedelta(days=days),
                finished_at=NOW,
                trigger="cli:run",
                status="failed",
            )
        )
    session.commit()
    issues = heartbeat_issues(session, ncfg, NOW, current)
    assert any("FAILED" in i for i in issues)
    assert any("no daily run has succeeded" in i for i in issues)
    session.add(
        Run(started_at=NOW - timedelta(hours=50), finished_at=NOW, trigger="cli:run", status="ok")
    )
    session.commit()
    assert any("50 hours ago" in i for i in heartbeat_issues(session, ncfg, NOW, current))


def test_digest_goes_to_temporary_cover_too(
    session: Session, site: FakeSite, app_cfg: AppConfig
) -> None:
    from promise_monitor import access
    from tests.unit.test_monitor import Runner

    Runner(session, site, app_cfg.tunables.monitor)()
    pw = "correct horse battery staple"
    admin = access.create_user(
        session, email="boss@x.ng", name="B", role="superadmin", password=pw, actor=None, now=NOW
    )
    amina = access.create_user(
        session, email="amina@x.ng", name="A", role="coordinator", password=pw, actor=admin, now=NOW
    )
    bayo = access.create_user(
        session, email="bayo@x.ng", name="Bo", role="coordinator", password=pw, actor=admin, now=NOW
    )
    access.set_primary(
        session, meter_id=METER_ID, user=amina, actor=admin, now=NOW - timedelta(days=1)
    )
    access.grant_temporary(
        session,
        meter_id=METER_ID,
        user=bayo,
        starts_at=NOW - timedelta(hours=1),
        ends_at=NOW + timedelta(days=3),
        reason="leave cover",
        actor=admin,
        now=NOW - timedelta(hours=1),
    )
    session.commit()
    mailer = MemoryMailer()
    Notifier(session, mailer, admin_email="admin@x.ng").notify(
        report(new_rows={METER_ID: ["draft: X"]})
    )
    assert sorted(to[0] for to, _s, _b in mailer.sent) == ["amina@x.ng", "bayo@x.ng"]


@pytest.mark.parametrize(("port", "ssl_mode"), [(2525, False), (587, False), (465, True)])
def test_smtp_mailer_always_uses_tls(
    monkeypatch: pytest.MonkeyPatch, port: int, ssl_mode: bool
) -> None:
    import promise_monitor.notify as notify

    calls: list[str] = []

    class FakeSMTP:
        def __init__(self, host: str, p: int, **kw: object) -> None:
            calls.append(f"{'ssl' if ssl_mode and p == 465 else 'plain'}:{host}:{p}")

        def __enter__(self) -> FakeSMTP:
            return self

        def __exit__(self, *a: object) -> None:
            pass

        def starttls(self, **kw: object) -> None:
            calls.append("starttls")

        def login(self, u: str, pw: str) -> None:
            calls.append(f"login:{u}")

        def send_message(self, msg: object) -> None:
            calls.append("sent")

    monkeypatch.setattr("promise_monitor.notify.smtplib.SMTP", FakeSMTP)
    monkeypatch.setattr("promise_monitor.notify.smtplib.SMTP_SSL", FakeSMTP)
    notify.SMTPMailer("sandbox.smtp.mailtrap.io", port, "u", "p", "pm@example.org").send(
        ["a@x.ng"], "s", "b"
    )
    if ssl_mode:
        assert calls == ["ssl:sandbox.smtp.mailtrap.io:465", "login:u", "sent"]
    else:
        assert calls == [f"plain:sandbox.smtp.mailtrap.io:{port}", "starttls", "login:u", "sent"]


def test_from_address_falls_back_when_smtp_user_is_not_an_email() -> None:
    from promise_monitor.config import Settings
    from promise_monitor.notify import SMTPMailer, make_mailer

    m = make_mailer(Settings(smtp_user="a1b2c3d4e5", smtp_password="x", admin_email="admin@x.ng"))  # noqa: S106
    assert isinstance(m, SMTPMailer) and m.sender == "admin@x.ng"
