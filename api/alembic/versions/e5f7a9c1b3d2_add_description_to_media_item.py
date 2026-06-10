"""add description to media_item

Revision ID: e5f7a9c1b3d2
Revises: a64f76b7fe87
Create Date: 2026-06-10 15:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'e5f7a9c1b3d2'
down_revision: Union[str, None] = 'a64f76b7fe87'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('media_item', sa.Column('description', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('media_item', 'description')
