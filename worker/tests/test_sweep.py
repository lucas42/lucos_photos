"""Tests for the worker's pending/processing sweep logic."""

import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import lucos_photos_common.models  # noqa: F401 - registers all models with Base.metadata
from lucos_photos_common.database import Base
from lucos_photos_common.models import Face, MediaItem, Person, ProcessingState, ProcessingStatus

import app.main as app_main
from app.main import sweep_pending_photos


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
    monkeypatch.setattr("app.main.SessionLocal", patched_session_local)

    yield session
    session.close()


class _FakeRedis:
    """Minimal in-memory Redis double covering the hash/string ops the per-item backoff
    logic uses (hgetall, hincrby, hset, expire, get, set). A MagicMock won't do here —
    _apply_reenqueue_backoff needs hgetall() to actually return a dict it can read count/
    timestamps out of, not another MagicMock. Not a general Redis replacement: Queue
    itself is separately mocked in every test below, so this never needs to back a queue."""

    def __init__(self):
        self._hashes = {}
        self._strings = {}

    def hgetall(self, key):
        return dict(self._hashes.get(key, {}))

    def hincrby(self, key, field, amount=1):
        h = self._hashes.setdefault(key, {})
        h[field] = str(int(h.get(field, 0)) + amount)
        return int(h[field])

    def hset(self, key, mapping=None):
        h = self._hashes.setdefault(key, {})
        for k, v in (mapping or {}).items():
            h[k] = str(v)

    def expire(self, key, ttl):
        pass  # no-op; TTL expiry isn't exercised by these tests

    def set(self, key, value, ex=None):
        self._strings[key] = str(value)

    def get(self, key):
        return self._strings.get(key)


def _make_fake_redis():
    return _FakeRedis()


def _make_media_item(db_session, *, sha256_hash=None, media_type="photo", file_extension="jpg"):
    """Helper: create and persist a MediaItem."""
    item = MediaItem(
        sha256_hash=sha256_hash or ("a" * 64),
        file_extension=file_extension,
        media_type=media_type,
    )
    db_session.add(item)
    db_session.flush()
    return item


def _make_processing_status(db_session, photo_id, state, *, age_minutes=10):
    """Helper: create a ProcessingStatus with updated_at set to `age_minutes` ago."""
    status = ProcessingStatus(
        photo_id=photo_id,
        state=state,
        updated_at=datetime.now(timezone.utc) - timedelta(minutes=age_minutes),
    )
    db_session.add(status)
    db_session.commit()
    return status


def _make_mock_queue():
    """Return a MagicMock RQ queue. Queue depth is no longer consulted anywhere (the old
    global breaker is gone), so there's nothing to configure here."""
    return MagicMock()


def _make_person(db_session, *, is_background=False, profile_photo_id=None):
    """Helper: create and persist a Person."""
    person = Person(is_background=is_background, profile_photo_id=profile_photo_id)
    db_session.add(person)
    db_session.flush()
    return person


def _make_face(db_session, photo, person):
    """Helper: create and persist a Face linking a photo to a person."""
    face = Face(
        photo_id=photo.id,
        person_id=person.id,
        person_confirmed=False,
        bbox_x=0.1,
        bbox_y=0.1,
        bbox_width=0.4,
        bbox_height=0.4,
    )
    db_session.add(face)
    db_session.flush()
    return face


class TestSweepPendingPhotos:
    def test_enqueues_process_photo_for_stuck_pending_photo(self, db_session):
        """A photo stuck in pending should be re-enqueued via process_photo."""
        item = _make_media_item(db_session, sha256_hash="b" * 64, media_type="photo")
        _make_processing_status(db_session, item.id, ProcessingState.pending, age_minutes=10)

        fake_redis = _make_fake_redis()
        mock_queue = _make_mock_queue()

        with patch("app.main.Queue", return_value=mock_queue), \
             patch("lucos_photos_common.jobs.process_photo", __name__="process_photo") as mock_process_photo, \
             patch("lucos_photos_common.jobs.process_video", __name__="process_video") as mock_process_video:
            sweep_pending_photos(fake_redis)

        mock_queue.enqueue.assert_called_once()
        args, kwargs = mock_queue.enqueue.call_args
        assert args[0] is mock_process_photo
        assert args[1] == str(item.id)

    def test_enqueues_process_video_for_stuck_pending_video(self, db_session):
        """A video stuck in pending should be re-enqueued via process_video, not process_photo."""
        item = _make_media_item(db_session, sha256_hash="c" * 64, media_type="video", file_extension="mp4")
        _make_processing_status(db_session, item.id, ProcessingState.pending, age_minutes=10)

        fake_redis = _make_fake_redis()
        mock_queue = _make_mock_queue()

        with patch("app.main.Queue", return_value=mock_queue), \
             patch("lucos_photos_common.jobs.process_photo", __name__="process_photo") as mock_process_photo, \
             patch("lucos_photos_common.jobs.process_video", __name__="process_video") as mock_process_video:
            sweep_pending_photos(fake_redis)

        mock_queue.enqueue.assert_called_once()
        args, kwargs = mock_queue.enqueue.call_args
        assert args[0] is mock_process_video
        assert args[1] == str(item.id)

    def test_does_not_enqueue_recently_pending_item(self, db_session):
        """An item pending for less than the threshold should not be swept."""
        item = _make_media_item(db_session, sha256_hash="d" * 64, media_type="photo")
        _make_processing_status(db_session, item.id, ProcessingState.pending, age_minutes=1)

        fake_redis = _make_fake_redis()
        mock_queue = _make_mock_queue()

        with patch("app.main.Queue", return_value=mock_queue):
            sweep_pending_photos(fake_redis)

        mock_queue.enqueue.assert_not_called()

    def test_enqueues_photo_stuck_in_processing(self, db_session):
        """A photo stuck in processing (likely from a worker crash) should be re-enqueued."""
        item = _make_media_item(db_session, sha256_hash="e" * 64, media_type="photo")
        _make_processing_status(db_session, item.id, ProcessingState.processing, age_minutes=60)

        fake_redis = _make_fake_redis()
        mock_queue = _make_mock_queue()

        with patch("app.main.Queue", return_value=mock_queue), \
             patch("lucos_photos_common.jobs.process_photo", __name__="process_photo") as mock_process_photo, \
             patch("lucos_photos_common.jobs.process_video", __name__="process_video") as mock_process_video:
            sweep_pending_photos(fake_redis)

        mock_queue.enqueue.assert_called_once()
        args, kwargs = mock_queue.enqueue.call_args
        assert args[0] is mock_process_photo
        assert args[1] == str(item.id)

    def test_enqueues_video_stuck_in_processing(self, db_session):
        """A video stuck in processing should be re-enqueued via process_video."""
        item = _make_media_item(db_session, sha256_hash="f" * 64, media_type="video", file_extension="mp4")
        _make_processing_status(db_session, item.id, ProcessingState.processing, age_minutes=60)

        fake_redis = _make_fake_redis()
        mock_queue = _make_mock_queue()

        with patch("app.main.Queue", return_value=mock_queue), \
             patch("lucos_photos_common.jobs.process_photo", __name__="process_photo") as mock_process_photo, \
             patch("lucos_photos_common.jobs.process_video", __name__="process_video") as mock_process_video:
            sweep_pending_photos(fake_redis)

        mock_queue.enqueue.assert_called_once()
        args, kwargs = mock_queue.enqueue.call_args
        assert args[0] is mock_process_video
        assert args[1] == str(item.id)

    def test_does_not_enqueue_recently_processing_item(self, db_session):
        """An item in processing for less than the processing threshold is left alone."""
        item = _make_media_item(db_session, sha256_hash="0" * 64, media_type="photo")
        _make_processing_status(db_session, item.id, ProcessingState.processing, age_minutes=5)

        fake_redis = _make_fake_redis()
        mock_queue = _make_mock_queue()

        with patch("app.main.Queue", return_value=mock_queue):
            sweep_pending_photos(fake_redis)

        mock_queue.enqueue.assert_not_called()

    def test_does_not_enqueue_complete_items(self, db_session):
        """Complete items should never be re-enqueued."""
        item = _make_media_item(db_session, sha256_hash="1" * 64, media_type="photo")
        _make_processing_status(db_session, item.id, ProcessingState.complete, age_minutes=60)

        fake_redis = _make_fake_redis()
        mock_queue = _make_mock_queue()

        with patch("app.main.Queue", return_value=mock_queue):
            sweep_pending_photos(fake_redis)

        mock_queue.enqueue.assert_not_called()

    def test_does_not_enqueue_failed_items(self, db_session):
        """Failed items should not be swept — they need manual intervention."""
        item = _make_media_item(db_session, sha256_hash="2" * 64, media_type="photo")
        _make_processing_status(db_session, item.id, ProcessingState.failed, age_minutes=60)

        fake_redis = _make_fake_redis()
        mock_queue = _make_mock_queue()

        with patch("app.main.Queue", return_value=mock_queue):
            sweep_pending_photos(fake_redis)

        mock_queue.enqueue.assert_not_called()

    def test_enqueues_multiple_mixed_items(self, db_session):
        """Multiple stuck items of mixed types should each get the correct job."""
        photo = _make_media_item(db_session, sha256_hash="3" * 64, media_type="photo")
        video = _make_media_item(db_session, sha256_hash="4" * 64, media_type="video", file_extension="mp4")
        _make_processing_status(db_session, photo.id, ProcessingState.pending, age_minutes=10)
        _make_processing_status(db_session, video.id, ProcessingState.pending, age_minutes=10)

        fake_redis = _make_fake_redis()
        mock_queue = _make_mock_queue()

        with patch("app.main.Queue", return_value=mock_queue), \
             patch("lucos_photos_common.jobs.process_photo", __name__="process_photo") as mock_process_photo, \
             patch("lucos_photos_common.jobs.process_video", __name__="process_video") as mock_process_video:
            sweep_pending_photos(fake_redis)

        assert mock_queue.enqueue.call_count == 2

        # Collect all enqueued (job_fn, photo_id) pairs
        enqueued = [(c.args[0], c.args[1]) for c in mock_queue.enqueue.call_args_list]
        assert (mock_process_photo, str(photo.id)) in enqueued
        assert (mock_process_video, str(video.id)) in enqueued


class TestSweepSessionLifecycle:
    """Regression tests for lucas42/lucos_photos#480 — the DB session must be closed
    before the enqueue loop begins, not held open across it (each iteration does Redis
    I/O, not DB work), and status.media_item must remain accessible afterward via
    contains_eager rather than a lazy load that would raise once the session is gone."""

    def _spied_session(self, db_session, on_close):
        """Build a fresh session bound to the same (in-memory, StaticPool-shared) engine
        as db_session, with its .close() wrapped to call `on_close` first.

        Needed because the db_session fixture patches SessionLocal to a factory that
        returns a *new* Session() each call — not the fixture's own db_session object —
        so spying on db_session.close directly would watch an instance the sweep code
        never touches.
        """
        Session = sessionmaker(bind=db_session.get_bind())
        session = Session()
        original_close = session.close

        def spy_close():
            on_close()
            original_close()

        session.close = spy_close
        return session

    def test_db_session_closed_before_enqueue_loop(self, db_session, monkeypatch):
        item = _make_media_item(db_session, sha256_hash="s1" * 32, media_type="photo")
        _make_processing_status(db_session, item.id, ProcessingState.pending, age_minutes=10)

        call_order = []
        spied_session = self._spied_session(db_session, lambda: call_order.append("db_close"))
        monkeypatch.setattr(app_main, "SessionLocal", lambda: spied_session)

        fake_redis = _make_fake_redis()
        mock_queue = _make_mock_queue()
        mock_queue.enqueue.side_effect = lambda *a, **kw: call_order.append("enqueue")

        with patch("app.main.Queue", return_value=mock_queue), \
             patch("lucos_photos_common.jobs.process_photo", __name__="process_photo"), \
             patch("lucos_photos_common.jobs.process_video", __name__="process_video"):
            sweep_pending_photos(fake_redis)

        # SessionLocal is patched globally, so _enqueue_missing_profile_pictures'
        # unrelated later query/close also shows up in call_order — only the relative
        # order of the *first* close and the enqueue (from the stuck-item loop this
        # test is about) matters here.
        assert call_order.index("db_close") < call_order.index("enqueue"), (
            f"expected the DB session to close before the enqueue loop runs, got {call_order}"
        )

    def test_media_item_accessible_after_session_fully_detached(self, db_session, monkeypatch):
        """Even if the session aggressively expunges everything on close (harsher than
        the SQLite/StaticPool test fixture would naturally produce), status.media_item
        set via contains_eager must still be accessible with no DetachedInstanceError
        and no surprise lazy-load query against the now-closed session."""
        item = _make_media_item(db_session, sha256_hash="s2" * 32, media_type="video", file_extension="mp4")
        _make_processing_status(db_session, item.id, ProcessingState.pending, age_minutes=10)

        spied_session = self._spied_session(db_session, lambda: spied_session.expunge_all())
        monkeypatch.setattr(app_main, "SessionLocal", lambda: spied_session)

        fake_redis = _make_fake_redis()
        mock_queue = _make_mock_queue()

        with patch("app.main.Queue", return_value=mock_queue), \
             patch("lucos_photos_common.jobs.process_photo", __name__="process_photo"), \
             patch("lucos_photos_common.jobs.process_video", __name__="process_video") as mock_process_video:
            sweep_pending_photos(fake_redis)

        # If media_item weren't eagerly loaded, accessing it inside _enqueue_for_media_item
        # would raise DetachedInstanceError, which the outer except would swallow and log
        # as "sweep: error during pending photo sweep" — resulting in no enqueue call at all.
        mock_queue.enqueue.assert_called_once()
        args, kwargs = mock_queue.enqueue.call_args
        assert args[0] is mock_process_video
        assert args[1] == str(item.id)


class TestApplyReenqueueBackoff:
    """Unit tests for the per-item backoff helper that replaces the old global
    SWEEP_QUEUE_DEPTH_LIMIT breaker. Tested directly (not via a full sweep pass) for
    precise control over the simulated clock."""

    def _fixed_clock(self, monkeypatch, start=1000.0):
        clock = {"now": start}
        monkeypatch.setattr(app_main.time, "time", lambda: clock["now"])
        return clock

    def test_fresh_item_enqueues_immediately(self):
        fake_redis = _make_fake_redis()
        enqueue_fn = MagicMock()

        is_chronic = app_main._apply_reenqueue_backoff(fake_redis, "reenqueue", "item-1", enqueue_fn)

        enqueue_fn.assert_called_once()
        assert is_chronic is False

    def test_repeat_within_backoff_window_is_skipped(self, monkeypatch):
        fake_redis = _make_fake_redis()
        clock = self._fixed_clock(monkeypatch)
        enqueue_fn = MagicMock()

        app_main._apply_reenqueue_backoff(fake_redis, "reenqueue", "item-1", enqueue_fn)
        assert enqueue_fn.call_count == 1

        clock["now"] += 10  # well within BACKOFF_BASE_SECONDS
        app_main._apply_reenqueue_backoff(fake_redis, "reenqueue", "item-1", enqueue_fn)
        assert enqueue_fn.call_count == 1  # not re-enqueued

    def test_reenqueues_once_backoff_elapses(self, monkeypatch):
        fake_redis = _make_fake_redis()
        clock = self._fixed_clock(monkeypatch)
        enqueue_fn = MagicMock()

        app_main._apply_reenqueue_backoff(fake_redis, "reenqueue", "item-1", enqueue_fn)

        clock["now"] += app_main.BACKOFF_BASE_SECONDS + 1
        app_main._apply_reenqueue_backoff(fake_redis, "reenqueue", "item-1", enqueue_fn)
        assert enqueue_fn.call_count == 2

    def test_backoff_grows_exponentially(self, monkeypatch):
        """Second repeat should wait 2x as long as the first — the exponential part of
        'exponential backoff', not just a flat retry interval."""
        fake_redis = _make_fake_redis()
        clock = self._fixed_clock(monkeypatch)
        enqueue_fn = MagicMock()

        app_main._apply_reenqueue_backoff(fake_redis, "reenqueue", "item-1", enqueue_fn)  # count 0 -> 1

        clock["now"] += app_main.BACKOFF_BASE_SECONDS - 1
        app_main._apply_reenqueue_backoff(fake_redis, "reenqueue", "item-1", enqueue_fn)
        assert enqueue_fn.call_count == 1  # not due yet (needs BASE since last)
        clock["now"] += 2
        app_main._apply_reenqueue_backoff(fake_redis, "reenqueue", "item-1", enqueue_fn)
        assert enqueue_fn.call_count == 2  # due now, count 1 -> 2

        clock["now"] += app_main.BACKOFF_BASE_SECONDS * 2 - 1
        app_main._apply_reenqueue_backoff(fake_redis, "reenqueue", "item-1", enqueue_fn)
        assert enqueue_fn.call_count == 2  # not due yet (needs 2x BASE since last)
        clock["now"] += 2
        app_main._apply_reenqueue_backoff(fake_redis, "reenqueue", "item-1", enqueue_fn)
        assert enqueue_fn.call_count == 3

    def test_backoff_capped_at_ceiling(self, monkeypatch):
        fake_redis = _make_fake_redis()
        clock = self._fixed_clock(monkeypatch)
        enqueue_fn = MagicMock()

        # count=10 would demand base*2^9 (>> ceiling) without the cap
        fake_redis.hset("sweep:reenqueue:item-1", mapping={
            "count": 10,
            "first_enqueued_at": clock["now"] - 100000,
            "last_enqueued_at": clock["now"],
        })

        clock["now"] += app_main.BACKOFF_CEILING_SECONDS - 1
        app_main._apply_reenqueue_backoff(fake_redis, "reenqueue", "item-1", enqueue_fn)
        assert enqueue_fn.call_count == 0  # capped wait hasn't elapsed yet

        clock["now"] += 2
        app_main._apply_reenqueue_backoff(fake_redis, "reenqueue", "item-1", enqueue_fn)
        assert enqueue_fn.call_count == 1  # capped wait has now elapsed

    def test_distinct_items_are_independent(self, monkeypatch):
        """No global aggregate: one item's backoff state must not affect another's."""
        fake_redis = _make_fake_redis()
        self._fixed_clock(monkeypatch)
        enqueue_fn_1 = MagicMock()
        enqueue_fn_2 = MagicMock()

        app_main._apply_reenqueue_backoff(fake_redis, "reenqueue", "item-1", enqueue_fn_1)
        # item-1 now has count=1 and would be backed off — item-2 is unaffected
        app_main._apply_reenqueue_backoff(fake_redis, "reenqueue", "item-2", enqueue_fn_2)

        enqueue_fn_1.assert_called_once()
        enqueue_fn_2.assert_called_once()

    def test_restart_does_not_rearm(self, monkeypatch):
        """Pre-existing Redis state (as if left behind before a worker restart) must
        still be respected — the helper has no in-process memory of its own, so a
        'restarted worker' calling it fresh is indistinguishable from one that never
        restarted, other than reading the same Redis state either way."""
        fake_redis = _make_fake_redis()
        clock = self._fixed_clock(monkeypatch)

        fake_redis.hset("sweep:reenqueue:item-1", mapping={
            "count": 1,
            "first_enqueued_at": clock["now"] - 10,
            "last_enqueued_at": clock["now"] - 10,
        })

        enqueue_fn = MagicMock()
        app_main._apply_reenqueue_backoff(fake_redis, "reenqueue", "item-1", enqueue_fn)

        enqueue_fn.assert_not_called()  # backoff window hasn't elapsed since last_enqueued_at

    def test_chronic_threshold_by_count_emits_warning(self, monkeypatch, caplog):
        fake_redis = _make_fake_redis()
        clock = self._fixed_clock(monkeypatch)

        fake_redis.hset("sweep:reenqueue:item-1", mapping={
            "count": app_main.SWEEP_CHRONIC_THRESHOLD,
            "first_enqueued_at": clock["now"] - 100000,
            "last_enqueued_at": clock["now"] - 100000,
        })

        enqueue_fn = MagicMock()
        with caplog.at_level("WARNING"):
            is_chronic = app_main._apply_reenqueue_backoff(fake_redis, "reenqueue", "item-1", enqueue_fn)

        assert is_chronic is True
        assert any("chronically stuck" in r.message for r in caplog.records)

    def test_chronic_by_age_even_below_count_threshold(self, monkeypatch):
        """An item stuck continuously since before the ceiling is chronic even if its
        count hasn't reached SWEEP_CHRONIC_THRESHOLD."""
        fake_redis = _make_fake_redis()
        clock = self._fixed_clock(monkeypatch)

        fake_redis.hset("sweep:reenqueue:item-1", mapping={
            "count": 1,
            "first_enqueued_at": clock["now"] - app_main.BACKOFF_CEILING_SECONDS - 1,
            "last_enqueued_at": clock["now"] - app_main.BACKOFF_CEILING_SECONDS - 1,
        })

        enqueue_fn = MagicMock()
        is_chronic = app_main._apply_reenqueue_backoff(fake_redis, "reenqueue", "item-1", enqueue_fn)

        assert is_chronic is True
        enqueue_fn.assert_called_once()  # backoff had also elapsed, so it re-enqueued too

    def test_not_chronic_below_both_thresholds(self):
        fake_redis = _make_fake_redis()
        enqueue_fn = MagicMock()

        is_chronic = app_main._apply_reenqueue_backoff(fake_redis, "reenqueue", "item-1", enqueue_fn)

        assert is_chronic is False


class TestSweepPerItemBackoffIntegration:
    """Higher-level tests exercising sweep_pending_photos end-to-end, covering the
    ticket's acceptance criteria against real DB-backed stuck items."""

    def test_bulk_burst_all_distinct_items_enqueued(self, db_session):
        """N distinct stuck items in one pass are all re-enqueued — no global suppression
        of a healthy burst, unlike the old queue-depth breaker."""
        items = [_make_media_item(db_session, sha256_hash=str(i) * 64, media_type="photo") for i in range(5)]
        for item in items:
            _make_processing_status(db_session, item.id, ProcessingState.pending, age_minutes=10)

        fake_redis = _make_fake_redis()
        mock_queue = _make_mock_queue()

        with patch("app.main.Queue", return_value=mock_queue), \
             patch("lucos_photos_common.jobs.process_photo", __name__="process_photo"), \
             patch("lucos_photos_common.jobs.process_video", __name__="process_video"):
            sweep_pending_photos(fake_redis)

        assert mock_queue.enqueue.call_count == 5

    def test_deep_queue_does_not_suppress_sweep(self, db_session):
        """Queue depth is no longer consulted at all — a deep queue must not suppress
        the sweep the way the old breaker did."""
        item = _make_media_item(db_session, sha256_hash="9" * 64, media_type="photo")
        _make_processing_status(db_session, item.id, ProcessingState.pending, age_minutes=10)

        fake_redis = _make_fake_redis()
        mock_queue = _make_mock_queue()
        mock_queue.count = 999  # would have tripped the old breaker at any nonzero value

        with patch("app.main.Queue", return_value=mock_queue), \
             patch("lucos_photos_common.jobs.process_photo", __name__="process_photo"), \
             patch("lucos_photos_common.jobs.process_video", __name__="process_video"):
            sweep_pending_photos(fake_redis)

        mock_queue.enqueue.assert_called_once()

    def test_second_pass_skips_repeat_but_not_new_item(self, db_session, monkeypatch):
        """A repeat pass within the backoff window skips an already-seen stuck item, but a
        newly-stuck item appearing in the same pass is still enqueued."""
        clock = {"now": 1000.0}
        monkeypatch.setattr(app_main.time, "time", lambda: clock["now"])

        old_item = _make_media_item(db_session, sha256_hash="1" * 64, media_type="photo")
        _make_processing_status(db_session, old_item.id, ProcessingState.pending, age_minutes=10)

        fake_redis = _make_fake_redis()
        mock_queue = _make_mock_queue()

        with patch("app.main.Queue", return_value=mock_queue), \
             patch("lucos_photos_common.jobs.process_photo", __name__="process_photo"), \
             patch("lucos_photos_common.jobs.process_video", __name__="process_video"):
            sweep_pending_photos(fake_redis)  # first pass: enqueues old_item

        assert mock_queue.enqueue.call_count == 1

        new_item = _make_media_item(db_session, sha256_hash="2" * 64, media_type="photo")
        _make_processing_status(db_session, new_item.id, ProcessingState.pending, age_minutes=10)

        clock["now"] += 5  # well within old_item's backoff window
        mock_queue.enqueue.reset_mock()
        with patch("app.main.Queue", return_value=mock_queue), \
             patch("lucos_photos_common.jobs.process_photo", __name__="process_photo"), \
             patch("lucos_photos_common.jobs.process_video", __name__="process_video"):
            sweep_pending_photos(fake_redis)

        mock_queue.enqueue.assert_called_once()
        args, kwargs = mock_queue.enqueue.call_args
        assert args[1] == str(new_item.id)

    def test_chronic_gauge_published_to_redis(self, db_session, monkeypatch):
        """A chronically-stuck item is counted in the sweep:chronically_stuck_count gauge."""
        clock = {"now": 1000.0}
        monkeypatch.setattr(app_main.time, "time", lambda: clock["now"])

        item = _make_media_item(db_session, sha256_hash="3" * 64, media_type="photo")
        _make_processing_status(db_session, item.id, ProcessingState.pending, age_minutes=10)

        fake_redis = _make_fake_redis()
        # Seed state as if several passes had already occurred for this item
        fake_redis.hset(f"sweep:reenqueue:{item.id}", mapping={
            "count": app_main.SWEEP_CHRONIC_THRESHOLD,
            "first_enqueued_at": clock["now"] - 100000,
            "last_enqueued_at": clock["now"] - 100000,
        })

        mock_queue = _make_mock_queue()
        with patch("app.main.Queue", return_value=mock_queue), \
             patch("lucos_photos_common.jobs.process_photo", __name__="process_photo"), \
             patch("lucos_photos_common.jobs.process_video", __name__="process_video"):
            sweep_pending_photos(fake_redis)

        assert fake_redis.get(app_main.SWEEP_CHRONICALLY_STUCK_COUNT_KEY) == "1"

    def test_chronic_gauge_zero_when_nothing_chronic(self, db_session):
        fake_redis = _make_fake_redis()
        mock_queue = _make_mock_queue()

        with patch("app.main.Queue", return_value=mock_queue):
            sweep_pending_photos(fake_redis)

        assert fake_redis.get(app_main.SWEEP_CHRONICALLY_STUCK_COUNT_KEY) == "0"

    def test_clustering_and_contact_sync_always_run(self, db_session):
        """Unlike the old breaker (which could suppress the stuck-item re-enqueue),
        nothing here gates clustering or contact-name sync — they always run."""
        fake_redis = _make_fake_redis()
        mock_queue = _make_mock_queue()

        with patch("app.main.Queue", return_value=mock_queue), \
             patch("lucos_photos_common.jobs.cluster_faces") as mock_cluster_faces, \
             patch("lucos_photos_common.jobs.sweep_contact_display_names") as mock_sync_names:
            sweep_pending_photos(fake_redis)

        mock_cluster_faces.assert_called_once()
        mock_sync_names.assert_called_once()


class TestSweepMissingProfilePictures:
    """Tests for the backstop that enqueues generate_profile_picture for persons who have
    a face but no profile picture — the equivalent of the pending/processing sweep above,
    for a job type with no ProcessingStatus row to detect staleness by. Gated by the same
    per-item backoff, keyed per person."""

    def test_enqueues_person_with_face_and_no_profile_picture(self, db_session):
        item = _make_media_item(db_session, sha256_hash="b1" * 32, media_type="photo")
        person = _make_person(db_session)
        _make_face(db_session, item, person)
        db_session.commit()

        fake_redis = _make_fake_redis()
        mock_queue = _make_mock_queue()

        with patch("app.main.Queue", return_value=mock_queue), \
             patch("lucos_photos_common.jobs._enqueue_profile_picture_for_persons") as mock_enqueue:
            sweep_pending_photos(fake_redis)

        mock_enqueue.assert_called_once()
        (person_ids,), _kwargs = mock_enqueue.call_args
        assert person_ids == [str(person.id)]

    def test_does_not_enqueue_person_with_profile_picture(self, db_session):
        item = _make_media_item(db_session, sha256_hash="b2" * 32, media_type="photo")
        person = _make_person(db_session, profile_photo_id=item.id)
        _make_face(db_session, item, person)
        db_session.commit()

        fake_redis = _make_fake_redis()
        mock_queue = _make_mock_queue()

        with patch("app.main.Queue", return_value=mock_queue), \
             patch("lucos_photos_common.jobs._enqueue_profile_picture_for_persons") as mock_enqueue:
            sweep_pending_photos(fake_redis)

        mock_enqueue.assert_not_called()

    def test_does_not_enqueue_background_person(self, db_session):
        item = _make_media_item(db_session, sha256_hash="b3" * 32, media_type="photo")
        person = _make_person(db_session, is_background=True)
        _make_face(db_session, item, person)
        db_session.commit()

        fake_redis = _make_fake_redis()
        mock_queue = _make_mock_queue()

        with patch("app.main.Queue", return_value=mock_queue), \
             patch("lucos_photos_common.jobs._enqueue_profile_picture_for_persons") as mock_enqueue:
            sweep_pending_photos(fake_redis)

        mock_enqueue.assert_not_called()

    def test_does_not_enqueue_person_with_no_faces(self, db_session):
        _make_person(db_session)
        db_session.commit()

        fake_redis = _make_fake_redis()
        mock_queue = _make_mock_queue()

        with patch("app.main.Queue", return_value=mock_queue), \
             patch("lucos_photos_common.jobs._enqueue_profile_picture_for_persons") as mock_enqueue:
            sweep_pending_photos(fake_redis)

        mock_enqueue.assert_not_called()

    def test_dedupes_person_with_multiple_faces(self, db_session):
        photo1 = _make_media_item(db_session, sha256_hash="b4" * 32, media_type="photo")
        photo2 = _make_media_item(db_session, sha256_hash="b5" * 32, media_type="photo")
        person = _make_person(db_session)
        _make_face(db_session, photo1, person)
        _make_face(db_session, photo2, person)
        db_session.commit()

        fake_redis = _make_fake_redis()
        mock_queue = _make_mock_queue()

        with patch("app.main.Queue", return_value=mock_queue), \
             patch("lucos_photos_common.jobs._enqueue_profile_picture_for_persons") as mock_enqueue:
            sweep_pending_photos(fake_redis)

        mock_enqueue.assert_called_once()
        (person_ids,), _kwargs = mock_enqueue.call_args
        assert person_ids == [str(person.id)]

    def test_multiple_distinct_persons_all_enqueued(self, db_session):
        """No global suppression on this path either — several distinct persons missing
        a profile picture are all enqueued in one pass."""
        persons = []
        for i in range(3):
            item = _make_media_item(db_session, sha256_hash=f"c{i}" * 32, media_type="photo")
            person = _make_person(db_session)
            _make_face(db_session, item, person)
            persons.append(person)
        db_session.commit()

        fake_redis = _make_fake_redis()
        mock_queue = _make_mock_queue()

        with patch("app.main.Queue", return_value=mock_queue), \
             patch("lucos_photos_common.jobs._enqueue_profile_picture_for_persons") as mock_enqueue:
            sweep_pending_photos(fake_redis)

        assert mock_enqueue.call_count == 3
        enqueued_ids = {c.args[0][0] for c in mock_enqueue.call_args_list}
        assert enqueued_ids == {str(p.id) for p in persons}

    def test_second_pass_within_backoff_skips_repeat_person(self, db_session, monkeypatch):
        clock = {"now": 1000.0}
        monkeypatch.setattr(app_main.time, "time", lambda: clock["now"])

        item = _make_media_item(db_session, sha256_hash="b6" * 32, media_type="photo")
        person = _make_person(db_session)
        _make_face(db_session, item, person)
        db_session.commit()

        fake_redis = _make_fake_redis()
        mock_queue = _make_mock_queue()

        with patch("app.main.Queue", return_value=mock_queue), \
             patch("lucos_photos_common.jobs._enqueue_profile_picture_for_persons") as mock_enqueue:
            sweep_pending_photos(fake_redis)  # first pass
            assert mock_enqueue.call_count == 1

            clock["now"] += 5  # well within backoff window
            sweep_pending_photos(fake_redis)  # second pass
            assert mock_enqueue.call_count == 1  # not called again

    def test_chronic_profile_picture_counted_in_gauge(self, db_session, monkeypatch):
        clock = {"now": 1000.0}
        monkeypatch.setattr(app_main.time, "time", lambda: clock["now"])

        item = _make_media_item(db_session, sha256_hash="b7" * 32, media_type="photo")
        person = _make_person(db_session)
        _make_face(db_session, item, person)
        db_session.commit()

        fake_redis = _make_fake_redis()
        fake_redis.hset(f"sweep:profilepic:{person.id}", mapping={
            "count": app_main.SWEEP_CHRONIC_THRESHOLD,
            "first_enqueued_at": clock["now"] - 100000,
            "last_enqueued_at": clock["now"] - 100000,
        })

        mock_queue = _make_mock_queue()
        with patch("app.main.Queue", return_value=mock_queue), \
             patch("lucos_photos_common.jobs._enqueue_profile_picture_for_persons"):
            sweep_pending_photos(fake_redis)

        assert fake_redis.get(app_main.SWEEP_CHRONICALLY_STUCK_COUNT_KEY) == "1"
