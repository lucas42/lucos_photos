"""add_flagged_at_to_person

Revision ID: f6a8b0c2d3e4
Revises: e5f7a9c1b3d2
Create Date: 2026-07-24 01:35:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'f6a8b0c2d3e4'
down_revision: Union[str, None] = 'e5f7a9c1b3d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('person', sa.Column('flagged_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('person', 'flagged_at')
