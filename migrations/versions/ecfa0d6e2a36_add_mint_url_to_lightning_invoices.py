"""add mint url to lightning invoices

Revision ID: ecfa0d6e2a36
Revises: 64ed5594df1f
Create Date: 2026-08-02 23:53:00.037456
"""

import json
import os

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "ecfa0d6e2a36"
down_revision = "64ed5594df1f"
branch_labels = None
depends_on = None


def _resolve_backfill_mint_url(bind: sa.engine.Connection) -> str | None:
    """Best-effort resolution of the mint that issued pre-existing invoices.

    Order: persisted settings JSON -> PRIMARY_MINT_URL env -> first CASHU_MINTS entry.
    """
    try:
        row = bind.execute(
            sa.text("SELECT data FROM settings ORDER BY id LIMIT 1")
        ).fetchone()
        if row and row[0]:
            data = json.loads(row[0])
            mint = data.get("primary_mint") or next(
                iter(data.get("cashu_mints") or []), None
            )
            if mint:
                return str(mint)
    except Exception:
        pass

    env_mint = os.environ.get("PRIMARY_MINT_URL", "").strip()
    if env_mint:
        return env_mint

    cashu_mints = os.environ.get("CASHU_MINTS", "").strip()
    if cashu_mints:
        return cashu_mints.split(",")[0].strip() or None
    return None


def upgrade() -> None:
    op.add_column(
        "lightning_invoices", sa.Column("mint_url", sa.String(), nullable=True)
    )

    bind = op.get_bind()
    backfill_mint = _resolve_backfill_mint_url(bind)
    if backfill_mint:
        bind.execute(
            sa.text(
                "UPDATE lightning_invoices SET mint_url = :mint WHERE mint_url IS NULL"
            ),
            {"mint": backfill_mint},
        )


def downgrade() -> None:
    op.drop_column("lightning_invoices", "mint_url")
