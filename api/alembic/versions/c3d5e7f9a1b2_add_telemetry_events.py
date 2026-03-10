"""add telemetry_event table

Revision ID: c3d5e7f9a1b2
Revises: b2c4d6e8f0a1
Create Date: 2026-03-10 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID
from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'c3d5e7f9a1b2'
down_revision: Union[str, None] = 'b2c4d6e8f0a1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'telemetry_event',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('event_type', sa.String(100), nullable=False),
        sa.Column('app_version', sa.String(50), nullable=True),
        sa.Column('timestamp', sa.DateTime(timezone=True), nullable=True),
        sa.Column('received_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('data', JSONB, nullable=True),
    )
    op.create_index('ix_telemetry_event_event_type', 'telemetry_event', ['event_type'])
    op.create_index('ix_telemetry_event_received_at', 'telemetry_event', ['received_at'])


def downgrade() -> None:
    op.drop_index('ix_telemetry_event_received_at', table_name='telemetry_event')
    op.drop_index('ix_telemetry_event_event_type', table_name='telemetry_event')
    op.drop_table('telemetry_event')
