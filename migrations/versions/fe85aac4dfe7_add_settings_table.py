"""add_settings_table

Revision ID: fe85aac4dfe7
Revises: 7bc4e8b02b9d
Create Date: 2025-08-10 17:58:36.562926
"""

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision = 'fe85aac4dfe7'
down_revision = '7bc4e8b02b9d'
branch_labels = None
depends_on = None

def upgrade() -> None:
    # Create settings table
    op.create_table(
        'settings',
        sa.Column('key', sqlmodel.sql.sqltypes.AutoString(), primary_key=True),
        sa.Column('value', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('description', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('value_type', sqlmodel.sql.sqltypes.AutoString(), server_default='str', nullable=False),
        sa.Column('is_manually_changed', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    # Drop settings table
    op.drop_table('settings')
