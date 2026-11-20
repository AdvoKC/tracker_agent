"""Promise Tracker submissions (P12): pt_tokens, pt_submissions, change_type own_submission.

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-19

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD = (
    "change_type IN ('status_change', 'new_evidence', 'new_promise', 'news_update', "
    "'cosmetic', 'substantive_edit', 'promise_removed', 'coordinator_update')"
)
NEW = (
    "change_type IN ('status_change', 'new_evidence', 'new_promise', 'news_update', "
    "'cosmetic', 'substantive_edit', 'promise_removed', 'coordinator_update', "
    "'own_submission')"
)
JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "pt_tokens",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_ciphertext", sa.Text(), nullable=False),
        sa.Column("remote_token_id", sa.String(128), nullable=False),
        sa.Column("last4", sa.String(4), nullable=False),
        sa.Column("meter_ids", JSON, nullable=False),
        sa.Column("holder_name", sa.String(255), nullable=True),
        sa.Column("expires_at", TS, nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("revoked_at", TS, nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.user_id"], name=op.f("fk_pt_tokens_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pt_tokens")),
    )
    op.create_index(op.f("ix_pt_tokens_user_id"), "pt_tokens", ["user_id"])
    op.create_table(
        "pt_submissions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("article_id", sa.Uuid(), nullable=False),
        sa.Column("meter_id", sa.Uuid(), nullable=False),
        sa.Column("promise_id", sa.Uuid(), nullable=False),
        sa.Column("submitted_by", sa.Uuid(), nullable=False),
        sa.Column("token_row_id", sa.Integer(), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column("remote_id", sa.String(128), nullable=False),
        sa.Column("remote_state", sa.String(20), nullable=False),
        sa.Column("remote_update_id", sa.String(64), nullable=True),
        sa.Column("reviewer_note", sa.Text(), nullable=True),
        sa.Column("submitted_at", TS, nullable=False),
        sa.Column("last_checked_at", TS, nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "remote_state IN ('pending_review', 'published', 'rejected', 'withdrawn')",
            name=op.f("ck_pt_submissions_remote_state"),
        ),
        sa.ForeignKeyConstraint(
            ["article_id"],
            ["articles.article_id"],
            name=op.f("fk_pt_submissions_article_id_articles"),
        ),
        sa.ForeignKeyConstraint(
            ["meter_id"], ["meters.id"], name=op.f("fk_pt_submissions_meter_id_meters")
        ),
        sa.ForeignKeyConstraint(
            ["submitted_by"], ["users.user_id"], name=op.f("fk_pt_submissions_submitted_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["token_row_id"],
            ["pt_tokens.id"],
            name=op.f("fk_pt_submissions_token_row_id_pt_tokens"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pt_submissions")),
        sa.UniqueConstraint("article_id", name=op.f("uq_pt_submissions_article_id")),
        sa.UniqueConstraint("remote_id", name=op.f("uq_pt_submissions_remote_id")),
    )
    op.create_index(op.f("ix_pt_submissions_meter_id"), "pt_submissions", ["meter_id"])
    op.create_index(op.f("ix_pt_submissions_promise_id"), "pt_submissions", ["promise_id"])
    op.create_index(op.f("ix_pt_submissions_remote_state"), "pt_submissions", ["remote_state"])
    op.create_index(
        op.f("ix_pt_submissions_remote_update_id"), "pt_submissions", ["remote_update_id"]
    )
    with op.batch_alter_table("change_events") as b:
        b.drop_constraint(op.f("ck_change_events_change_type"), type_="check")
        b.create_check_constraint(op.f("ck_change_events_change_type"), NEW)


def downgrade() -> None:
    op.execute("DELETE FROM change_events WHERE change_type = 'own_submission'")
    with op.batch_alter_table("change_events") as b:
        b.drop_constraint(op.f("ck_change_events_change_type"), type_="check")
        b.create_check_constraint(op.f("ck_change_events_change_type"), OLD)
    op.drop_table("pt_submissions")
    op.drop_table("pt_tokens")
