"""add_face_negative_labels

Revision ID: b8c0d2e4f6a7
Revises: a7b9c1d3e5f6
Create Date: 2026-11-08 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b8c0d2e4f6a7'
down_revision: Union[str, None] = 'a7b9c1d3e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('face', sa.Column('not_a_face_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('person', sa.Column('emptied_at', sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        'face_person_rejection',
        sa.Column('face_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('face.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('person_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('person.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_face_person_rejection_person_id', 'face_person_rejection', ['person_id'])


def downgrade() -> None:
    op.drop_index('ix_face_person_rejection_person_id', table_name='face_person_rejection')
    op.drop_table('face_person_rejection')
    op.drop_column('person', 'emptied_at')
    op.drop_column('face', 'not_a_face_at')
