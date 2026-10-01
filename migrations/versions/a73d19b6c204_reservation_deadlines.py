"""Immutable reservation start and absolute recovery deadline.

Revision ID: a73d19b6c204
Revises: e4c7a1b9d520
"""

import time

import sqlalchemy as sa
from alembic import op

revision = "a73d19b6c204"
down_revision = "e4c7a1b9d520"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "reservation_releases", sa.Column("started_at", sa.Integer(), nullable=True)
    )
    op.add_column(
        "reservation_releases", sa.Column("expires_at", sa.Integer(), nullable=True)
    )
    op.create_index(
        "ix_reservation_releases_expires_at", "reservation_releases", ["expires_at"]
    )
    # Original ages are unknowable for renewed legacy rows. Give them a finite
    # migration grace period; deploy only after draining old workers.
    op.execute(
        sa.text(
            "UPDATE reservation_releases SET expires_at = :expiry WHERE status = 'active'"
        ).bindparams(expiry=int(time.time()) + 1830)
    )


def downgrade() -> None:
    op.drop_index(
        "ix_reservation_releases_expires_at", table_name="reservation_releases"
    )
    op.drop_column("reservation_releases", "expires_at")
    op.drop_column("reservation_releases", "started_at")
