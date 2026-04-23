import os
import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, MagicMock, patch

# Set loganne env vars before any imports that would trigger loganne module-level code.
# The loganne library calls sys.exit() at import time if these aren't set.
os.environ.setdefault("LOGANNE_ENDPOINT", "http://loganne.test/events")
os.environ.setdefault("SYSTEM", "lucos_photos")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.main as main_module
import app.routers.photos as photos_module
from app.main import app
from app.database import get_db
from app.auth import verify_session_or_key
import lucos_photos_common.models  # noqa: F401 - registers all models with Base.metadata
from lucos_photos_common.database import Base


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
def db_session(db_engine):
    Session = sessionmaker(bind=db_engine)
    session = Session()
    yield session
    session.close()


@pytest.fixture
def client(db_session, db_engine, tmp_path, monkeypatch):
    monkeypatch.setenv("CLIENT_KEYS", "test:development=validkey")
    monkeypatch.setattr(photos_module, "UPLOADS_DIR", tmp_path)

    # Patch SessionLocal in both app.database (used by get_db) and app.main (used by
    # check_db / get_metrics) so that all database access uses the same in-memory SQLite.
    TestSession = sessionmaker(bind=db_engine)
    import app.database as database_module
    import app.main as main_module_inner
    monkeypatch.setattr(database_module, "SessionLocal", TestSession)
    monkeypatch.setattr(main_module_inner, "SessionLocal", TestSession)

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def authenticated_client(client):
    """A test client that bypasses session auth (verify_session always succeeds).

    Use this fixture for tests of user-facing endpoints where the focus is on
    endpoint behaviour rather than authentication itself.

    Overrides both verify_session and verify_session_or_key so that endpoints
    using either dependency are unblocked.
    """
    async def _noop_verify_session():
        return None

    app.dependency_overrides[verify_session_or_key] = _noop_verify_session
    yield client
    # Restore: remove the auth overrides (other overrides like get_db remain)
    app.dependency_overrides.pop(verify_session_or_key, None)
