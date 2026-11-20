"""Coordinator briefs: change_type coordinator_update + brief columns on change_events.

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-18

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD = (
    "change_type IN ('status_change', 'new_evidence', 'new_promise', 'news_update', "
    "'cosmetic', 'substantive_edit', 'promise_removed')"
)
NEW = (
    "change_type IN ('status_change', 'new_evidence', 'new_promise', 'news_update', "
    "'cosmetic', 'substantive_edit', 'promise_removed', 'coordinator_update')"
)
JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    with op.batch_alter_table("change_events") as b:
        b.add_column(sa.Column("brief", sa.Text(), nullable=True))
        b.add_column(sa.Column("brief_urls", JSON, nullable=False, server_default="[]"))
        b.add_column(sa.Column("requested_by", sa.Uuid(), nullable=True))
        b.create_foreign_key(
            op.f("fk_change_events_requested_by_users"), "users", ["requested_by"], ["user_id"]
        )
        b.drop_constraint(op.f("ck_change_events_change_type"), type_="check")
        b.create_check_constraint(op.f("ck_change_events_change_type"), NEW)


def downgrade() -> None:
    op.execute("DELETE FROM change_events WHERE change_type = 'coordinator_update'")
    with op.batch_alter_table("change_events") as b:
        b.drop_constraint(op.f("ck_change_events_change_type"), type_="check")
        b.create_check_constraint(op.f("ck_change_events_change_type"), OLD)
        b.drop_constraint(op.f("fk_change_events_requested_by_users"), type_="foreignkey")
        b.drop_column("requested_by")
        b.drop_column("brief_urls")
        b.drop_column("brief")
