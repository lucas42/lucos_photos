"""Database session dependency for FastAPI routes."""

from lucos_photos_common.database import SessionLocal


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
