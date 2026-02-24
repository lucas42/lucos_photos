"""initial schema

Revision ID: 0d7180b6439a
Revises:
Create Date: 2026-02-24 22:49:59.716621

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID


# revision identifiers, used by Alembic.
revision: str = '0d7180b6439a'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'photo',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('file_extension', sa.String(10), nullable=False),
        sa.Column('taken_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('uploaded_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('width', sa.Integer, nullable=True),
        sa.Column('height', sa.Integer, nullable=True),
    )

    op.create_table(
        'processing_status',
        sa.Column('photo_id', UUID(as_uuid=True), sa.ForeignKey('photo.id'), primary_key=True),
        sa.Column('state', sa.Enum('pending', 'processing', 'complete', 'failed', name='processingstate'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('error_message', sa.Text, nullable=True),
    )

    op.create_table(
        'person',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('contact_id', sa.String, nullable=True, unique=True),
        sa.Column('display_name', sa.String, nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        'face',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('photo_id', UUID(as_uuid=True), sa.ForeignKey('photo.id'), nullable=False),
        sa.Column('person_id', UUID(as_uuid=True), sa.ForeignKey('person.id'), nullable=True),
        sa.Column('person_confirmed', sa.Boolean, nullable=False, server_default='false'),
        sa.Column('bbox_x', sa.Float, nullable=False),
        sa.Column('bbox_y', sa.Float, nullable=False),
        sa.Column('bbox_width', sa.Float, nullable=False),
        sa.Column('bbox_height', sa.Float, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        'photo_person',
        sa.Column('photo_id', UUID(as_uuid=True), sa.ForeignKey('photo.id'), primary_key=True),
        sa.Column('person_id', UUID(as_uuid=True), sa.ForeignKey('person.id'), primary_key=True),
    )


def downgrade() -> None:
    op.drop_table('photo_person')
    op.drop_table('face')
    op.drop_table('person')
    op.drop_table('processing_status')
    op.execute('DROP TYPE processingstate')
    op.drop_table('photo')
