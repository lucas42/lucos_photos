"""add_prename_demotion

Revision ID: e1f3a5b7c9d0
Revises: d0e2f4a6b8c9
Create Date: 2026-11-09 09:00:00.000000

Provenance for the one-off repair that unconfirms suspicious migrated Google tags
(lucas42/lucos_photos#664); it is what makes --revert possible.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'e1f3a5b7c9d0'
down_revision: Union[str, None] = 'd0e2f4a6b8c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'prename_demotion',
        sa.Column('face_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('face.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('person_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('person.id', ondelete='CASCADE'), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('demoted_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('prename_demotion')
