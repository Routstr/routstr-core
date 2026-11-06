"""add writer continuity loss reason

Revision ID: e1b7c3d9a204
Revises: a7c3e9f1b2d4
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e1b7c3d9a204"
down_revision = "a7c3e9f1b2d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Historical runs remain unknown rather than retroactively claiming loss.
    op.add_column(
        "terminal_outcome_writer_runs",
        sa.Column("loss_reason", sa.String(), nullable=True),
    )


def downgrade() -> None:
    with op.batch_alter_table("terminal_outcome_writer_runs") as batch_op:
        batch_op.drop_column("loss_reason")
