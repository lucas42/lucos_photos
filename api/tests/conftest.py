import os

import pytest
from fastapi.testclient import TestClient

# Must be set before app.main is imported, as database.py constructs the engine at module load time.
# The actual Postgres engine is never used in tests — get_db is overridden with a SQLite session.
os.environ.setdefault("POSTGRES_USER", "test")
os.environ.setdefault("POSTGRES_PASSWORD", "test")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.main as main_module
from app.main import app, get_db
import lucos_photos_common.models  # noqa: F401 - registers all models with Base.metadata
from lucos_photos_common.database import Base


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()
    Base.metadata.drop_all(engine)


@pytest.fixture
def client(db_session, tmp_path, monkeypatch):
    monkeypatch.setenv("CLIENT_KEYS", "test:development=validkey")
    monkeypatch.setattr(main_module, "UPLOADS_DIR", tmp_path)

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
