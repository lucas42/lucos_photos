"""delete_orphaned_persons

Revision ID: a7b9c1d3e5f6
Revises: f6a8b0c2d3e4
Create Date: 2026-10-11 00:00:00.000000

Removes person rows with no faces, no photo_person links and no contact link — residue
of face moves made outside the merge path (lucas42/lucos_photos#530). Same conditions
as delete_person_if_emptied. Counts are logged before and after; the migration runs in
one transaction and aborts (rolling back) if any orphan survives the delete.
"""
import logging
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

logger = logging.getLogger(__name__)

revision: str = 'a7b9c1d3e5f6'
down_revision: Union[str, None] = 'f6a8b0c2d3e4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ORPHANS = """
    FROM person p
    WHERE p.contact_id IS NULL
      AND NOT EXISTS (SELECT 1 FROM face f WHERE f.person_id = p.id)
      AND NOT EXISTS (SELECT 1 FROM photo_person pp WHERE pp.person_id = p.id)
"""


def upgrade() -> None:
    conn = op.get_bind()
    total_before = conn.execute(sa.text("SELECT count(*) FROM person")).scalar()
    orphans = conn.execute(sa.text("SELECT count(*) " + _ORPHANS)).scalar()
    logger.info("delete_orphaned_persons: %d persons total, %d orphaned", total_before, orphans)

    conn.execute(sa.text("DELETE FROM person WHERE id IN (SELECT p.id " + _ORPHANS + ")"))

    total_after = conn.execute(sa.text("SELECT count(*) FROM person")).scalar()
    remaining = conn.execute(sa.text("SELECT count(*) " + _ORPHANS)).scalar()
    logger.info("delete_orphaned_persons: %d persons remain (%d deleted)", total_after, total_before - total_after)
    if remaining != 0 or total_before - total_after != orphans:
        raise RuntimeError(
            f"delete_orphaned_persons: expected to delete {orphans}, deleted {total_before - total_after}, "
            f"{remaining} orphans remain — rolling back"
        )


def downgrade() -> None:
    pass  # deleted rows held no data worth restoring
