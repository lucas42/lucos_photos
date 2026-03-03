"""add performance indexes

Revision ID: a1befdcf4948
Revises: 4f9e3b2d1c18
Create Date: 2026-03-02 23:48:06.868429

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1befdcf4948'
down_revision: Union[str, None] = '4f9e3b2d1c18'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index('ix_photo_uploaded_at', 'photo', ['uploaded_at'], unique=False)
    op.create_index('ix_face_photo_id', 'face', ['photo_id'], unique=False)
    op.create_index('ix_face_person_id', 'face', ['person_id'], unique=False)
    op.create_index('ix_processing_status_state', 'processing_status', ['state'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_processing_status_state', table_name='processing_status')
    op.drop_index('ix_face_person_id', table_name='face')
    op.drop_index('ix_face_photo_id', table_name='face')
    op.drop_index('ix_photo_uploaded_at', table_name='photo')
