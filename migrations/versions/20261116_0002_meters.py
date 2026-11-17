"""Meter registry (PRD §4.1).

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "meters",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("meter_type", sa.String(32), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("state_name", sa.String(64), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("meter_name", sa.String(255), nullable=True),
        sa.Column("slug", sa.String(255), nullable=True),
        sa.Column("official_name", sa.String(255), nullable=True),
        sa.Column("parent_meter_id", sa.Uuid(), nullable=True),
        sa.Column("tenure_start", sa.Date(), nullable=True),
        sa.Column("tenure_end", sa.Date(), nullable=True),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("registry_status", sa.String(16), nullable=False),
        sa.Column("baseline_pending", sa.Boolean(), nullable=False),
        sa.Column("priority_rank", sa.Integer(), nullable=False),
        sa.Column("source_key", sa.String(80), nullable=False),
        sa.Column("list_endpoint", sa.Text(), nullable=False),
        sa.Column("public_url", sa.Text(), nullable=False),
        sa.Column("coordinator_email", sa.String(255), nullable=True),
        sa.Column("promise_count", sa.Integer(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "meter_type IN ('promise_tracker', 'promise_ring')", name=op.f("ck_meters_meter_type")
        ),
        sa.CheckConstraint(
            "scope IN ('presidential', 'senate', 'house', 'state')", name=op.f("ck_meters_scope")
        ),
        sa.CheckConstraint(
            "registry_status IN ('active', 'archived', 'missing')",
            name=op.f("ck_meters_registry_status"),
        ),
        sa.CheckConstraint(
            "(scope = 'state') = (state_name IS NOT NULL)", name=op.f("ck_meters_state_name")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_meters")),
    )
    op.create_index(op.f("ix_meters_registry_status"), "meters", ["registry_status"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_meters_registry_status"), table_name="meters")
    op.drop_table("meters")
