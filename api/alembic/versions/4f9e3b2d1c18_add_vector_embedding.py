"""add vector embedding

Revision ID: 4f9e3b2d1c18
Revises: 3f8e2a1c9b07
Create Date: 2026-03-01 20:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector


# revision identifiers, used by Alembic.
revision: str = '4f9e3b2d1c18'
down_revision: Union[str, None] = '3f8e2a1c9b07'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Enable the vector extension
    op.execute('CREATE EXTENSION IF NOT EXISTS vector')
    
    # Add the embedding column (512 dimensions for InsightFace)
    op.add_column('face', sa.Column('embedding', Vector(dim=512), nullable=True))
    
    # Create an HNSW index for cosine distance
    op.execute('CREATE INDEX ix_face_embedding ON face USING hnsw (embedding vector_cosine_ops)')


def downgrade() -> None:
    op.execute('DROP INDEX ix_face_embedding')
    op.drop_column('face', 'embedding')
