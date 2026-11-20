"""Coordinator confirmation of sources that blocked the agent's reader.

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-19

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | Sequence[str] | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("article_group_sources") as t:
        t.add_column(sa.Column("confirmed_by", sa.Uuid(), nullable=True))
        t.add_column(sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True))
        t.add_column(sa.Column("confirmed_title", sa.Text(), nullable=True))
        t.create_foreign_key(
            "fk_article_group_sources_confirmed_by_users", "users", ["confirmed_by"], ["user_id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("article_group_sources") as t:
        t.drop_constraint("fk_article_group_sources_confirmed_by_users", type_="foreignkey")
        t.drop_column("confirmed_title")
        t.drop_column("confirmed_at")
        t.drop_column("confirmed_by")
