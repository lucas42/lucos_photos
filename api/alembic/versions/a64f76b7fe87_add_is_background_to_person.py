"""add_is_background_to_person

Revision ID: a64f76b7fe87
Revises: d4e6f8a2b3c1
Create Date: 2026-03-16 17:37:35.009656

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a64f76b7fe87'
down_revision: Union[str, None] = 'd4e6f8a2b3c1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('person', sa.Column('is_background', sa.Boolean(), server_default='false', nullable=False))


def downgrade() -> None:
    op.drop_column('person', 'is_background')
