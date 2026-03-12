"""add_profile_picture_and_face_scoring

Revision ID: 18c545a1701b
Revises: c3d5e7f9a1b2
Create Date: 2026-03-12 12:56:08.248747

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '18c545a1701b'
down_revision: Union[str, None] = 'c3d5e7f9a1b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('face', sa.Column('det_score', sa.Float(), nullable=True))
    op.add_column('face', sa.Column('kps', sa.JSON(), nullable=True))
    op.add_column('person', sa.Column('profile_photo_id', sa.UUID(), nullable=True))
    op.add_column('person', sa.Column('profile_auto_generated', sa.Boolean(), nullable=True))
    op.create_foreign_key(None, 'person', 'media_item', ['profile_photo_id'], ['id'])


def downgrade() -> None:
    op.drop_constraint(None, 'person', type_='foreignkey')
    op.drop_column('person', 'profile_auto_generated')
    op.drop_column('person', 'profile_photo_id')
    op.drop_column('face', 'kps')
    op.drop_column('face', 'det_score')
