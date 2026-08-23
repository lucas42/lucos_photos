"""Regression test for lucas42/lucos_photos#500.

The worker runs under RQ's fork-per-job model (plus a scheduler fork at
startup). A forked child inherits the parent's pooled DBAPI connections —
without `os.register_at_fork`, the child would go on reusing the exact same
socket the parent's background sweep thread is using, causing the two
processes to interleave frames on the wire.

This test forces the interleaving deterministically (both processes reuse
the identical pooled connection object) rather than relying on real timing,
so it reproduces reliably without the fix and is not flaky.
"""

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import QueuePool

import lucos_photos_common.database as database


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork() not available on this platform")
class TestEngineDisposedAfterFork:
    def _make_engine(self, monkeypatch, tmp_path):
        """Point get_engine() at a throwaway file-backed SQLite DB, using the
        same pool class production uses (QueuePool), so pooled-connection
        inheritance across fork behaves the same way it does for Postgres.
        """
        db_path = tmp_path / "fork_test.db"
        monkeypatch.setattr(database, "_engine", None)
        monkeypatch.setattr(database, "_SessionLocal", None)
        monkeypatch.setattr(
            database,
            "create_engine",
            lambda *a, **kw: create_engine(f"sqlite:///{db_path}", poolclass=QueuePool),
        )
        monkeypatch.setenv("POSTGRES_USER", "photos")
        monkeypatch.setenv("POSTGRES_PASSWORD", "test")

    def test_child_gets_a_fresh_connection_not_the_parents(self, monkeypatch, tmp_path):
        self._make_engine(monkeypatch, tmp_path)

        # Engine created (and register_at_fork registered) here, in the "parent".
        engine = database.get_engine()

        # Check out a connection, then return it to the pool so it's the pool's
        # one live, inherited connection at fork time — mirroring the sweep
        # thread's pattern of opening a session, using it, and closing it.
        conn = engine.connect()
        conn.execute(text("SELECT 1"))
        parent_dbapi_id = id(conn.connection.dbapi_connection)
        conn.close()

        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:  # child
            os.close(read_fd)
            try:
                child_conn = database.get_engine().connect()
                child_conn.execute(text("SELECT 1"))
                child_dbapi_id = id(child_conn.connection.dbapi_connection)
                child_conn.close()
                os.write(write_fd, str(child_dbapi_id).encode())
            except Exception:
                os.write(write_fd, b"error")
            finally:
                os.close(write_fd)
                os._exit(0)

        os.close(write_fd)
        os.waitpid(pid, 0)
        raw = os.read(read_fd, 128).decode()
        os.close(read_fd)

        assert raw != "error"
        child_dbapi_id = int(raw)
        # Without the register_at_fork(after_in_child=...) fix, the child's
        # pool would still hold the parent's checked-in connection, so this
        # would come back equal — reproducing the shared-socket bug.
        assert child_dbapi_id != parent_dbapi_id
