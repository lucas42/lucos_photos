"""move_face_embedding_to_side_table

Revision ID: c9d1e3f5a7b8
Revises: b8c0d2e4f6a7
Create Date: 2026-11-08 16:00:00.000000

"""
import logging
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'c9d1e3f5a7b8'
down_revision: Union[str, None] = 'b8c0d2e4f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

log = logging.getLogger("alembic.runtime.migration")


def _tune_index_build() -> None:
    # The default 64MB can't hold the HNSW graph for ~98k 512-d vectors; the build must be single-threaded
    # because parallel builds need /dev/shm, which is Docker's default 64MB.
    op.execute("SET LOCAL maintenance_work_mem = '512MB'")
    op.execute("SET LOCAL max_parallel_maintenance_workers = 0")


def upgrade() -> None:
    _tune_index_build()
    op.create_table(
        'face_embedding',
        sa.Column('face_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('face.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('embedding', Vector(dim=512), nullable=False),
    )
    op.execute("INSERT INTO face_embedding (face_id, embedding) SELECT id, embedding FROM face WHERE embedding IS NOT NULL")
    source = op.get_bind().execute(sa.text("SELECT count(*) FROM face WHERE embedding IS NOT NULL")).scalar()
    copied = op.get_bind().execute(sa.text("SELECT count(*) FROM face_embedding")).scalar()
    log.info("face_embedding migration: %d face(s) had an embedding, %d row(s) copied", source, copied)
    if source != copied:
        raise RuntimeError(f"face_embedding row count {copied} does not match {source}")
    op.execute('CREATE INDEX ix_face_embedding_embedding ON face_embedding USING hnsw (embedding vector_cosine_ops)')
    op.execute('DROP INDEX ix_face_embedding')
    op.drop_column('face', 'embedding')


def downgrade() -> None:
    _tune_index_build()
    op.add_column('face', sa.Column('embedding', Vector(dim=512), nullable=True))
    op.execute("UPDATE face SET embedding = fe.embedding FROM face_embedding fe WHERE fe.face_id = face.id")
    op.execute('CREATE INDEX ix_face_embedding ON face USING hnsw (embedding vector_cosine_ops)')
    op.drop_table('face_embedding')
