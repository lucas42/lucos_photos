"""Database session dependency for FastAPI routes."""

from lucos_photos_common.database import SessionLocal


def get_db():
    db = SessionLocal()
    try:
        yield db
    except Exception:
        # Roll back explicitly on any unhandled exception so the connection is
        # returned to the pool in a clean state.  SQLAlchemy 2.0's close()
        # should handle this, but being explicit removes any ambiguity.
        db.rollback()
        raise
    finally:
        db.close()
