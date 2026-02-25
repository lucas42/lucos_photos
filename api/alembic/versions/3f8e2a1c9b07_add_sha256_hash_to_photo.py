"""add sha256_hash to photo

Revision ID: 3f8e2a1c9b07
Revises: 0d7180b6439a
Create Date: 2026-02-25 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = '3f8e2a1c9b07'
down_revision: Union[str, None] = '0d7180b6439a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('photo', sa.Column('sha256_hash', sa.String(64), nullable=False))
    op.create_index('ix_photo_sha256_hash', 'photo', ['sha256_hash'], unique=True)


def downgrade() -> None:
    op.drop_index('ix_photo_sha256_hash', table_name='photo')
    op.drop_column('photo', 'sha256_hash')
