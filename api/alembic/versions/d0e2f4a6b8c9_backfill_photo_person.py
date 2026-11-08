"""backfill_photo_person

Revision ID: d0e2f4a6b8c9
Revises: c9d1e3f5a7b8
Create Date: 2026-11-08 18:00:00.000000

Faces auto-assigned to an existing person at detection time never got a photo_person row,
so those photos were missing from the person page and counts (lucas42/lucos_photos#659).
Inserts every missing (photo, person) pair; idempotent. Downgrade is a no-op.
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

BACKFILL_SQL = """
    INSERT INTO photo_person (photo_id, person_id)
    SELECT DISTINCT photo_id, person_id FROM face WHERE person_id IS NOT NULL
    ON CONFLICT DO NOTHING
"""


def upgrade() -> None:
    result = op.get_bind().execute(sa.text(BACKFILL_SQL))
    logger.info("backfill_photo_person: inserted %d missing photo_person row(s)", result.rowcount)


def downgrade() -> None:
    pass
