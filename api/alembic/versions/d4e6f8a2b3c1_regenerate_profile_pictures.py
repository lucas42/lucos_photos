"""Regenerate profile pictures with new sizing

Revision ID: d4e6f8a2b3c1
Revises: 18c545a1701b
Create Date: 2026-03-14 00:00:00.000000

The profile picture crop formula and output size cap have changed.
All auto-generated profile pictures must be regenerated with the new parameters.

This migration resets profile_auto_generated to NULL for all persons that previously
had an auto-generated profile picture. This signals that their profile picture is
stale and needs to be regenerated.

After deploying, trigger regeneration by enqueueing generate_profile_picture for all
persons with profile_auto_generated IS NULL (e.g. via a management command or by
reprocessing all photos through the worker).
"""
from typing import Sequence, Union
import logging

from alembic import op
import sqlalchemy as sa

logger = logging.getLogger(__name__)

# revision identifiers, used by Alembic.
revision: str = 'd4e6f8a2b3c1'
down_revision: Union[str, None] = '18c545a1701b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    result = conn.execute(
        sa.text("UPDATE person SET profile_auto_generated = NULL WHERE profile_auto_generated = TRUE")
    )
    logger.info(
        "regenerate_profile_pictures migration: reset %d auto-generated profile pictures",
        result.rowcount,
    )


def downgrade() -> None:
    # No meaningful downgrade: we cannot tell which NULLs were set by this migration
    # vs which were always NULL. Leave them as-is.
    pass
