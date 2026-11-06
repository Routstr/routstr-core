"""add analytics v2 outbox

Revision ID: d9a6e2f4c7b1
Revises: c8e4a1f2b3d5
Create Date: 2026-08-31 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d9a6e2f4c7b1"
down_revision = "c8e4a1f2b3d5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "analytics_v2_delivery_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("sharing_enabled", sa.Boolean(), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("active_epoch_floor", sa.BigInteger(), nullable=True),
        sa.Column("identity_pubkey", sa.String(), nullable=True),
        sa.Column("provider_d", sa.String(), nullable=True),
        sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "id = 1",
            name="ck_analytics_v2_delivery_state_singleton",
        ),
        sa.CheckConstraint(
            "generation >= 0 AND updated_at_ms >= 0 "
            "AND (active_epoch_floor IS NULL OR active_epoch_floor >= 0)",
            name="ck_analytics_v2_delivery_state_nonnegative",
        ),
        sa.CheckConstraint(
            "(sharing_enabled AND active_epoch_floor IS NOT NULL) OR "
            "(NOT sharing_enabled AND active_epoch_floor IS NULL)",
            name="ck_analytics_v2_delivery_state_epoch_floor",
        ),
        sa.CheckConstraint(
            "(identity_pubkey IS NULL AND provider_d IS NULL) OR "
            "(identity_pubkey IS NOT NULL AND provider_d IS NOT NULL)",
            name="ck_analytics_v2_delivery_state_identity_pair",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "analytics_v2_outbox",
        sa.Column("event_id", sa.String(), nullable=False),
        sa.Column("pubkey", sa.String(), nullable=False),
        sa.Column("d_tag", sa.String(), nullable=False),
        sa.Column("kind", sa.Integer(), nullable=False),
        sa.Column("week", sa.Date(), nullable=False),
        sa.Column("epoch", sa.BigInteger(), nullable=False),
        sa.Column("through_day", sa.Date(), nullable=False),
        sa.Column("semantic_slot", sa.String(), nullable=False),
        sa.Column("delivery_generation", sa.BigInteger(), nullable=False),
        sa.Column("frame", sa.LargeBinary(), nullable=False),
        sa.Column("finalized", sa.Boolean(), nullable=False),
        sa.Column("corrected", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("stored_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("next_attempt_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("attempt_count", sa.BigInteger(), nullable=False),
        sa.Column("first_send_attempt_at_ms", sa.BigInteger(), nullable=True),
        sa.Column("delivered_at_ms", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            "kind = 38422",
            name="ck_analytics_v2_outbox_kind",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'delivered', 'superseded', 'cancelled')",
            name="ck_analytics_v2_outbox_status",
        ),
        sa.CheckConstraint(
            "epoch >= 0 AND delivery_generation >= 0 AND created_at >= 0 "
            "AND stored_at_ms >= 0 AND next_attempt_at_ms >= 0 "
            "AND attempt_count >= 0 "
            "AND (first_send_attempt_at_ms IS NULL "
            "OR first_send_attempt_at_ms >= 0) "
            "AND (delivered_at_ms IS NULL OR delivered_at_ms >= 0)",
            name="ck_analytics_v2_outbox_nonnegative",
        ),
        sa.CheckConstraint(
            "length(frame) > 0",
            name="ck_analytics_v2_outbox_frame_nonempty",
        ),
        sa.CheckConstraint(
            "length(semantic_slot) > 0",
            name="ck_analytics_v2_outbox_semantic_slot_nonempty",
        ),
        sa.CheckConstraint(
            "(status = 'delivered' AND delivered_at_ms IS NOT NULL) OR "
            "(status != 'delivered' AND delivered_at_ms IS NULL)",
            name="ck_analytics_v2_outbox_delivery_state",
        ),
        sa.PrimaryKeyConstraint("event_id"),
        sa.UniqueConstraint(
            "pubkey",
            "d_tag",
            "semantic_slot",
            name="uq_analytics_v2_outbox_semantic_slot",
        ),
    )
    op.create_index(
        "ix_analytics_v2_outbox_pending",
        "analytics_v2_outbox",
        ["status", "delivery_generation", "next_attempt_at_ms"],
        unique=False,
    )
    op.create_index(
        "ix_analytics_v2_outbox_coordinate",
        "analytics_v2_outbox",
        ["pubkey", "d_tag", "epoch", "created_at"],
        unique=False,
    )
    op.create_table(
        "analytics_v2_relay_receipts",
        sa.Column("event_id", sa.String(), nullable=False),
        sa.Column("relay_url", sa.String(), nullable=False),
        sa.Column("accepted_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("read_back_at_ms", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "accepted_at_ms >= 0 AND read_back_at_ms >= 0",
            name="ck_analytics_v2_relay_receipts_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["analytics_v2_outbox.event_id"],
        ),
        sa.PrimaryKeyConstraint("event_id", "relay_url"),
    )


def downgrade() -> None:
    op.drop_table("analytics_v2_relay_receipts")
    op.drop_index(
        "ix_analytics_v2_outbox_coordinate",
        table_name="analytics_v2_outbox",
    )
    op.drop_index(
        "ix_analytics_v2_outbox_pending",
        table_name="analytics_v2_outbox",
    )
    op.drop_table("analytics_v2_outbox")
    op.drop_table("analytics_v2_delivery_state")
