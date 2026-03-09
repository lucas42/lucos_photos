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
from app.main import app, get_db, verify_session
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
    monkeypatch.setattr(main_module, "UPLOADS_DIR", tmp_path)

    # Patch SessionLocal so that any code calling SessionLocal() directly (e.g. check_db,
    # get_metrics) uses the same in-memory SQLite database as the test session.
    TestSession = sessionmaker(bind=db_engine)
    monkeypatch.setattr(main_module, "SessionLocal", TestSession)

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
    """
    async def _noop_verify_session():
        return None

    app.dependency_overrides[verify_session] = _noop_verify_session
    yield client
    # Restore: remove the verify_session override (other overrides like get_db remain)
    app.dependency_overrides.pop(verify_session, None)
