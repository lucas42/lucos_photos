"""photo_person_view

Revision ID: d0e2f4a6b8c9
Revises: c9d1e3f5a7b8
Create Date: 2026-11-08 18:00:00.000000

Replaces the photo_person table (a stored copy of a derivation that fell out of step with
face, lucas42/lucos_photos#620 and #659) with a view of the same name and columns. The view
is correct the moment it exists, so there is nothing to backfill. Downgrade rebuilds the table.
"""
import logging
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

logger = logging.getLogger(__name__)

revision: str = 'd0e2f4a6b8c9'
down_revision: Union[str, None] = 'c9d1e3f5a7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

VIEW_QUERY = "SELECT DISTINCT photo_id, person_id FROM face WHERE person_id IS NOT NULL"


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT count(*) FROM photo_person")).scalar()
    logger.info("photo_person_view: dropping table with %d row(s)", rows)
    op.execute("DROP TABLE photo_person")
    op.execute(f"CREATE VIEW photo_person AS {VIEW_QUERY}")
    pairs = conn.execute(sa.text("SELECT count(*) FROM photo_person")).scalar()
    logger.info("photo_person_view: view reports %d photo/person pair(s)", pairs)


def downgrade() -> None:
    op.execute("DROP VIEW photo_person")
    op.create_table(
        'photo_person',
        sa.Column('photo_id', sa.UUID(), sa.ForeignKey('media_item.id'), primary_key=True),
        sa.Column('person_id', sa.UUID(), sa.ForeignKey('person.id'), primary_key=True),
    )
    op.execute(f"INSERT INTO photo_person (photo_id, person_id) {VIEW_QUERY}")
