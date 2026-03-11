"""add cashu_refunds table

Revision ID: a776ca70e5fe
Revises: 614c0a740e68
Create Date: 2026-03-11 22:00:01.554762
"""

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision = 'a776ca70e5fe'
down_revision = '614c0a740e68'
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_table(
        'cashu_refunds',
        sa.Column('payment_token_hash', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('refund_token', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('amount', sa.Integer(), nullable=False),
        sa.Column('unit', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('mint_url', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('created_at', sa.Integer(), nullable=False),
        sa.Column('collected', sa.Boolean(), nullable=False),
        sa.Column('swept', sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint('payment_token_hash'),
    )


def downgrade() -> None:
    op.drop_table('cashu_refunds')
