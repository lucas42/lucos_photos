import os
import uuid

import pytest

# Set loganne env vars before any imports that would trigger loganne module-level code.
# The loganne library calls sys.exit() at import time if these aren't set.
os.environ.setdefault("LOGANNE_ENDPOINT", "http://loganne.test/events")
os.environ.setdefault("SYSTEM", "lucos_photos")
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import lucos_photos_common.models  # noqa: F401 - registers all models with Base.metadata
from lucos_photos_common.database import Base
from lucos_photos_common.models import Photo, ProcessingState, ProcessingStatus


@pytest.fixture
def db_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)


@pytest.fixture
def db_session(db_engine, monkeypatch):
    """Patch SessionLocal to return a session backed by the in-memory SQLite engine."""
    Session = sessionmaker(bind=db_engine)
    session = Session()

    def patched_session_local():
        return Session()

    monkeypatch.setattr("lucos_photos_common.database.SessionLocal", patched_session_local)
    monkeypatch.setattr("lucos_photos_common.jobs.SessionLocal", patched_session_local)

    yield session
    session.close()


@pytest.fixture
def pending_photo(db_session):
    """Create a photo record with a pending processing status."""
    photo = Photo(sha256_hash="a" * 64, file_extension="jpg")
    db_session.add(photo)
    db_session.flush()
    status = ProcessingStatus(photo_id=photo.id, state=ProcessingState.pending)
    db_session.add(status)
    db_session.commit()
    db_session.refresh(photo)
    return photo
