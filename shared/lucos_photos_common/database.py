import os
from sqlalchemy import URL, create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

_engine = None
_SessionLocal = None


def get_engine():
    """Return (and lazily create) the SQLAlchemy engine.

    Engine creation is deferred until first call so that POSTGRES_USER and
    POSTGRES_PASSWORD do not need to be set at import time.  This makes it
    straightforward for tests to import application code without setting up
    dummy environment variables first.
    """
    global _engine
    if _engine is None:
        database_url = URL.create(
            drivername="postgresql",
            username=os.environ["POSTGRES_USER"],
            password=os.environ["POSTGRES_PASSWORD"],
            host="postgres",
            port=5432,
            database="photos",
        )
        _engine = create_engine(database_url)
    return _engine


def get_session_local():
    """Return (and lazily create) the SessionLocal factory."""
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine())
    return _SessionLocal


def SessionLocal():
    """Convenience wrapper: create and return a new database session.

    Kept as a module-level callable so existing call sites (e.g.
    ``db = SessionLocal()``) continue to work without change.
    """
    return get_session_local()()


class Base(DeclarativeBase):
    pass
