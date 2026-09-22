"""add terminal outcome ledger

Revision ID: c8e4a1f2b3d5
Revises: e4c7a1b9d520
Create Date: 2026-08-31 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c8e4a1f2b3d5"
down_revision = "e4c7a1b9d520"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "terminal_outcome_epochs",
        sa.Column("epoch", sa.BigInteger(), nullable=False),
        sa.Column("coverage_start_day", sa.Date(), nullable=False),
        sa.Column("coverage_end_day", sa.Date(), nullable=True),
        sa.Column("current_slot", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "epoch >= 0",
            name="ck_terminal_outcome_epochs_nonnegative",
        ),
        sa.CheckConstraint(
            "current_slot IS NULL OR current_slot = 1",
            name="ck_terminal_outcome_epochs_current_slot",
        ),
        sa.CheckConstraint(
            "(current_slot = 1 AND coverage_end_day IS NULL) OR "
            "(current_slot IS NULL AND coverage_end_day IS NOT NULL)",
            name="ck_terminal_outcome_epochs_state",
        ),
        sa.PrimaryKeyConstraint("epoch"),
        sa.UniqueConstraint("current_slot", name="uq_terminal_outcome_epochs_current"),
    )
    op.create_table(
        "terminal_outcome_writer_runs",
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("started_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("heartbeat_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("flushed_through_ms", sa.BigInteger(), nullable=True),
        sa.Column("closed_at_ms", sa.BigInteger(), nullable=True),
        sa.Column("loss_day", sa.Date(), nullable=True),
        sa.CheckConstraint(
            "status IN ('active', 'degraded', 'clean', 'lost', 'recovered')",
            name="ck_terminal_outcome_writer_runs_status",
        ),
        sa.CheckConstraint(
            "started_at_ms >= 0 AND heartbeat_at_ms >= 0 "
            "AND (closed_at_ms IS NULL OR closed_at_ms >= 0)",
            name="ck_terminal_outcome_writer_runs_nonnegative",
        ),
        sa.CheckConstraint(
            "(status IN ('active', 'degraded') AND closed_at_ms IS NULL) OR "
            "(status IN ('clean', 'lost', 'recovered') "
            "AND closed_at_ms IS NOT NULL)",
            name="ck_terminal_outcome_writer_runs_state",
        ),
        sa.CheckConstraint(
            "status NOT IN ('lost', 'recovered') OR loss_day IS NOT NULL",
            name="ck_terminal_outcome_writer_runs_lost_day",
        ),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_index(
        "ix_terminal_outcome_writer_runs_status_heartbeat",
        "terminal_outcome_writer_runs",
        ["status", "heartbeat_at_ms"],
        unique=False,
    )
    op.create_table(
        "terminal_outcomes",
        sa.Column("outcome_id", sa.String(), nullable=False),
        sa.Column("terminal_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("terminal_day", sa.Date(), nullable=False),
        sa.Column("model_identifier", sa.String(), nullable=True),
        sa.Column("served_model_identifier", sa.String(), nullable=True),
        sa.Column("pricing_source", sa.String(), nullable=True),
        sa.Column(
            "input_source", sa.String(), nullable=False, server_default="missing"
        ),
        sa.Column(
            "output_source", sa.String(), nullable=False, server_default="missing"
        ),
        sa.Column(
            "cache_read_source", sa.String(), nullable=False, server_default="missing"
        ),
        sa.Column(
            "cache_creation_source",
            sa.String(),
            nullable=False,
            server_default="missing",
        ),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cache_read_input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cache_creation_input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("revenue_msats", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "terminal_at_ms >= 0 AND input_tokens >= 0 "
            "AND output_tokens >= 0 AND cache_read_input_tokens >= 0 "
            "AND cache_creation_input_tokens >= 0 AND revenue_msats >= 0",
            name="ck_terminal_outcomes_nonnegative",
        ),
        sa.PrimaryKeyConstraint("outcome_id"),
    )
    op.create_index(
        "ix_terminal_outcomes_terminal_day_terminal_at_ms",
        "terminal_outcomes",
        ["terminal_day", "terminal_at_ms"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_terminal_outcomes_terminal_day_terminal_at_ms",
        table_name="terminal_outcomes",
    )
    op.drop_table("terminal_outcomes")
    op.drop_index(
        "ix_terminal_outcome_writer_runs_status_heartbeat",
        table_name="terminal_outcome_writer_runs",
    )
    op.drop_table("terminal_outcome_writer_runs")
    op.drop_table("terminal_outcome_epochs")
