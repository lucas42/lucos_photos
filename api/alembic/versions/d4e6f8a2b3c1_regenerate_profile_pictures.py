"""Regenerate profile pictures with new sizing

Revision ID: d4e6f8a2b3c1
Revises: 18c545a1701b
Create Date: 2026-03-14 00:00:00.000000

The profile picture crop formula and output size cap have changed.
All auto-generated profile pictures must be regenerated with the new parameters.

This migration resets profile_auto_generated to NULL for all auto-generated entries
(signalling they need regeneration), then enqueues generate_profile_picture jobs for
those persons via Redis. Redis unavailability is non-fatal — if Redis is down, the
pictures can be re-enqueued manually later by running the queue command again.
"""
from typing import Sequence, Union
import logging
import os

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

    # Find all persons with auto-generated profile pictures
    result = conn.execute(
        sa.text("SELECT id FROM person WHERE profile_auto_generated = TRUE")
    )
    person_ids = [str(row[0]) for row in result.fetchall()]

    # Reset their auto-generated flag so generate_profile_picture will re-run for them
    if person_ids:
        conn.execute(
            sa.text(
                "UPDATE person SET profile_auto_generated = NULL WHERE profile_auto_generated = TRUE"
            )
        )
        logger.info(
            "regenerate_profile_pictures migration: reset %d auto-generated profile pictures",
            len(person_ids),
        )

    # Enqueue regeneration jobs via Redis
    if person_ids:
        try:
            from redis import Redis
            from rq import Queue
            from rq.job import Retry
            from lucos_photos_common.jobs import generate_profile_picture

            redis_url = os.environ.get("REDIS_URL", "redis://redis:6379")
            redis_conn = Redis.from_url(redis_url)
            queue = Queue("photos", connection=redis_conn)
            for pid in person_ids:
                queue.enqueue(
                    generate_profile_picture,
                    pid,
                    retry=Retry(max=3, interval=[10, 30, 60]),
                )
            logger.info(
                "regenerate_profile_pictures migration: enqueued %d profile picture jobs",
                len(person_ids),
            )
        except Exception:
            logger.warning(
                "regenerate_profile_pictures migration: could not enqueue profile picture jobs "
                "(Redis may be unavailable). Run generate_profile_picture manually for each person.",
                exc_info=True,
            )


def downgrade() -> None:
    # No downgrade: we can't undo a job enqueue, and resetting flags back to TRUE
    # would be incorrect (the pictures haven't been regenerated yet in a downgrade scenario).
    pass
