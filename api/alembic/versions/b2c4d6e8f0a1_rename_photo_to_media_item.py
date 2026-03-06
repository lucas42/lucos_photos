"""rename photo to media_item and add video columns

Revision ID: b2c4d6e8f0a1
Revises: a1befdcf4948
Create Date: 2026-03-06 11:30:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'b2c4d6e8f0a1'
down_revision: Union[str, None] = 'a1befdcf4948'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop indexes that reference the old table name before renaming
    op.drop_index('ix_photo_uploaded_at', table_name='photo')
    op.drop_index('ix_photo_sha256_hash', table_name='photo')

    # Rename the table
    op.rename_table('photo', 'media_item')

    # Add discriminator column
    op.add_column('media_item', sa.Column(
        'media_type', sa.String(10), nullable=False, server_default='photo'
    ))

    # Add video-specific nullable columns
    op.add_column('media_item', sa.Column('duration', sa.Float, nullable=True))
    op.add_column('media_item', sa.Column('codec', sa.String(50), nullable=True))
    op.add_column('media_item', sa.Column('video_width', sa.Integer, nullable=True))
    op.add_column('media_item', sa.Column('video_height', sa.Integer, nullable=True))
    op.add_column('media_item', sa.Column('fps', sa.Float, nullable=True))

    # Recreate indexes with new names on the renamed table
    op.create_index('ix_media_item_uploaded_at', 'media_item', ['uploaded_at'], unique=False)
    op.create_index('ix_media_item_sha256_hash', 'media_item', ['sha256_hash'], unique=True)


def downgrade() -> None:
    # Drop new indexes
    op.drop_index('ix_media_item_sha256_hash', table_name='media_item')
    op.drop_index('ix_media_item_uploaded_at', table_name='media_item')

    # Remove video-specific columns
    op.drop_column('media_item', 'fps')
    op.drop_column('media_item', 'video_height')
    op.drop_column('media_item', 'video_width')
    op.drop_column('media_item', 'codec')
    op.drop_column('media_item', 'duration')

    # Remove discriminator column
    op.drop_column('media_item', 'media_type')

    # Rename table back
    op.rename_table('media_item', 'photo')

    # Recreate original indexes
    op.create_index('ix_photo_sha256_hash', 'photo', ['sha256_hash'], unique=True)
    op.create_index('ix_photo_uploaded_at', 'photo', ['uploaded_at'], unique=False)
