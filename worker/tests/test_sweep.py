"""Tests for the worker's pending/processing sweep logic."""

import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, call, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import lucos_photos_common.models  # noqa: F401 - registers all models with Base.metadata
from lucos_photos_common.database import Base
from lucos_photos_common.models import MediaItem, ProcessingState, ProcessingStatus

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


def _make_mock_queue(count=0):
    """Return a MagicMock queue with a configurable count (queue depth)."""
    mock_queue = MagicMock()
    mock_queue.count = count
    return mock_queue


class TestSweepPendingPhotos:
    def test_enqueues_process_photo_for_stuck_pending_photo(self, db_session):
        """A photo stuck in pending should be re-enqueued via process_photo."""
        item = _make_media_item(db_session, sha256_hash="b" * 64, media_type="photo")
        _make_processing_status(db_session, item.id, ProcessingState.pending, age_minutes=10)

        mock_redis = MagicMock()
        mock_queue = _make_mock_queue(count=0)

        with patch("app.main.Queue", return_value=mock_queue), \
             patch("lucos_photos_common.jobs.process_photo", __name__="process_photo") as mock_process_photo, \
             patch("lucos_photos_common.jobs.process_video", __name__="process_video") as mock_process_video:
            sweep_pending_photos(mock_redis)

        mock_queue.enqueue.assert_called_once()
        args, kwargs = mock_queue.enqueue.call_args
        assert args[0] is mock_process_photo
        assert args[1] == str(item.id)

    def test_enqueues_process_video_for_stuck_pending_video(self, db_session):
        """A video stuck in pending should be re-enqueued via process_video, not process_photo."""
        item = _make_media_item(db_session, sha256_hash="c" * 64, media_type="video", file_extension="mp4")
        _make_processing_status(db_session, item.id, ProcessingState.pending, age_minutes=10)

        mock_redis = MagicMock()
        mock_queue = _make_mock_queue(count=0)

        with patch("app.main.Queue", return_value=mock_queue), \
             patch("lucos_photos_common.jobs.process_photo", __name__="process_photo") as mock_process_photo, \
             patch("lucos_photos_common.jobs.process_video", __name__="process_video") as mock_process_video:
            sweep_pending_photos(mock_redis)

        mock_queue.enqueue.assert_called_once()
        args, kwargs = mock_queue.enqueue.call_args
        assert args[0] is mock_process_video
        assert args[1] == str(item.id)

    def test_does_not_enqueue_recently_pending_item(self, db_session):
        """An item pending for less than the threshold should not be swept."""
        item = _make_media_item(db_session, sha256_hash="d" * 64, media_type="photo")
        _make_processing_status(db_session, item.id, ProcessingState.pending, age_minutes=1)

        mock_redis = MagicMock()
        mock_queue = _make_mock_queue(count=0)

        with patch("app.main.Queue", return_value=mock_queue):
            sweep_pending_photos(mock_redis)

        mock_queue.enqueue.assert_not_called()

    def test_enqueues_photo_stuck_in_processing(self, db_session):
        """A photo stuck in processing (likely from a worker crash) should be re-enqueued."""
        item = _make_media_item(db_session, sha256_hash="e" * 64, media_type="photo")
        _make_processing_status(db_session, item.id, ProcessingState.processing, age_minutes=60)

        mock_redis = MagicMock()
        mock_queue = _make_mock_queue(count=0)

        with patch("app.main.Queue", return_value=mock_queue), \
             patch("lucos_photos_common.jobs.process_photo", __name__="process_photo") as mock_process_photo, \
             patch("lucos_photos_common.jobs.process_video", __name__="process_video") as mock_process_video:
            sweep_pending_photos(mock_redis)

        mock_queue.enqueue.assert_called_once()
        args, kwargs = mock_queue.enqueue.call_args
        assert args[0] is mock_process_photo
        assert args[1] == str(item.id)

    def test_enqueues_video_stuck_in_processing(self, db_session):
        """A video stuck in processing should be re-enqueued via process_video."""
        item = _make_media_item(db_session, sha256_hash="f" * 64, media_type="video", file_extension="mp4")
        _make_processing_status(db_session, item.id, ProcessingState.processing, age_minutes=60)

        mock_redis = MagicMock()
        mock_queue = _make_mock_queue(count=0)

        with patch("app.main.Queue", return_value=mock_queue), \
             patch("lucos_photos_common.jobs.process_photo", __name__="process_photo") as mock_process_photo, \
             patch("lucos_photos_common.jobs.process_video", __name__="process_video") as mock_process_video:
            sweep_pending_photos(mock_redis)

        mock_queue.enqueue.assert_called_once()
        args, kwargs = mock_queue.enqueue.call_args
        assert args[0] is mock_process_video
        assert args[1] == str(item.id)

    def test_does_not_enqueue_recently_processing_item(self, db_session):
        """An item in processing for less than the processing threshold is left alone."""
        item = _make_media_item(db_session, sha256_hash="0" * 64, media_type="photo")
        _make_processing_status(db_session, item.id, ProcessingState.processing, age_minutes=5)

        mock_redis = MagicMock()
        mock_queue = _make_mock_queue(count=0)

        with patch("app.main.Queue", return_value=mock_queue):
            sweep_pending_photos(mock_redis)

        mock_queue.enqueue.assert_not_called()

    def test_does_not_enqueue_complete_items(self, db_session):
        """Complete items should never be re-enqueued."""
        item = _make_media_item(db_session, sha256_hash="1" * 64, media_type="photo")
        _make_processing_status(db_session, item.id, ProcessingState.complete, age_minutes=60)

        mock_redis = MagicMock()
        mock_queue = _make_mock_queue(count=0)

        with patch("app.main.Queue", return_value=mock_queue):
            sweep_pending_photos(mock_redis)

        mock_queue.enqueue.assert_not_called()

    def test_does_not_enqueue_failed_items(self, db_session):
        """Failed items should not be swept — they need manual intervention."""
        item = _make_media_item(db_session, sha256_hash="2" * 64, media_type="photo")
        _make_processing_status(db_session, item.id, ProcessingState.failed, age_minutes=60)

        mock_redis = MagicMock()
        mock_queue = _make_mock_queue(count=0)

        with patch("app.main.Queue", return_value=mock_queue):
            sweep_pending_photos(mock_redis)

        mock_queue.enqueue.assert_not_called()

    def test_enqueues_multiple_mixed_items(self, db_session):
        """Multiple stuck items of mixed types should each get the correct job."""
        photo = _make_media_item(db_session, sha256_hash="3" * 64, media_type="photo")
        video = _make_media_item(db_session, sha256_hash="4" * 64, media_type="video", file_extension="mp4")
        _make_processing_status(db_session, photo.id, ProcessingState.pending, age_minutes=10)
        _make_processing_status(db_session, video.id, ProcessingState.pending, age_minutes=10)

        mock_redis = MagicMock()
        mock_queue = _make_mock_queue(count=0)

        with patch("app.main.Queue", return_value=mock_queue), \
             patch("lucos_photos_common.jobs.process_photo", __name__="process_photo") as mock_process_photo, \
             patch("lucos_photos_common.jobs.process_video", __name__="process_video") as mock_process_video:
            sweep_pending_photos(mock_redis)

        assert mock_queue.enqueue.call_count == 2

        # Collect all enqueued (job_fn, photo_id) pairs
        enqueued = [(c.args[0], c.args[1]) for c in mock_queue.enqueue.call_args_list]
        assert (mock_process_photo, str(photo.id)) in enqueued
        assert (mock_process_video, str(video.id)) in enqueued


class TestSweepCircuitBreaker:
    """Tests for the queue-depth circuit breaker that prevents runaway queue floods."""

    def test_skips_enqueue_when_queue_has_items(self, db_session):
        """Sweep should not enqueue anything when the queue already has jobs waiting."""
        item = _make_media_item(db_session, sha256_hash="5" * 64, media_type="photo")
        _make_processing_status(db_session, item.id, ProcessingState.pending, age_minutes=10)

        mock_redis = MagicMock()
        # Simulate a non-empty queue
        mock_queue = _make_mock_queue(count=100)

        with patch("app.main.Queue", return_value=mock_queue):
            sweep_pending_photos(mock_redis)

        mock_queue.enqueue.assert_not_called()

    def test_skips_when_queue_has_exactly_one_item(self, db_session):
        """Even a single queued job should trigger the circuit breaker (default limit=0)."""
        item = _make_media_item(db_session, sha256_hash="6" * 64, media_type="photo")
        _make_processing_status(db_session, item.id, ProcessingState.pending, age_minutes=10)

        mock_redis = MagicMock()
        mock_queue = _make_mock_queue(count=1)

        with patch("app.main.Queue", return_value=mock_queue):
            sweep_pending_photos(mock_redis)

        mock_queue.enqueue.assert_not_called()

    def test_enqueues_when_queue_is_empty(self, db_session):
        """When the queue is fully drained, the sweep should resume enqueuing stuck items."""
        item = _make_media_item(db_session, sha256_hash="7" * 64, media_type="photo")
        _make_processing_status(db_session, item.id, ProcessingState.pending, age_minutes=10)

        mock_redis = MagicMock()
        mock_queue = _make_mock_queue(count=0)

        with patch("app.main.Queue", return_value=mock_queue), \
             patch("lucos_photos_common.jobs.process_photo", __name__="process_photo") as mock_process_photo, \
             patch("lucos_photos_common.jobs.process_video", __name__="process_video"):
            sweep_pending_photos(mock_redis)

        mock_queue.enqueue.assert_called_once()

    def test_respects_custom_depth_limit(self, db_session):
        """A custom SWEEP_QUEUE_DEPTH_LIMIT should allow sweeping up to that many queued items."""
        item = _make_media_item(db_session, sha256_hash="8" * 64, media_type="photo")
        _make_processing_status(db_session, item.id, ProcessingState.pending, age_minutes=10)

        mock_redis = MagicMock()
        # Queue has 5 items, limit is 10 — should still enqueue
        mock_queue = _make_mock_queue(count=5)

        import app.main as app_main
        original_limit = app_main.SWEEP_QUEUE_DEPTH_LIMIT
        try:
            app_main.SWEEP_QUEUE_DEPTH_LIMIT = 10
            with patch("app.main.Queue", return_value=mock_queue), \
                 patch("lucos_photos_common.jobs.process_photo", __name__="process_photo"), \
                 patch("lucos_photos_common.jobs.process_video", __name__="process_video"):
                sweep_pending_photos(mock_redis)
        finally:
            app_main.SWEEP_QUEUE_DEPTH_LIMIT = original_limit

        mock_queue.enqueue.assert_called_once()

    def test_skips_when_queue_exceeds_custom_depth_limit(self, db_session):
        """When queue depth exceeds a custom limit, the sweep should be skipped."""
        item = _make_media_item(db_session, sha256_hash="9" * 64, media_type="photo")
        _make_processing_status(db_session, item.id, ProcessingState.pending, age_minutes=10)

        mock_redis = MagicMock()
        # Queue has 15 items, limit is 10 — should skip
        mock_queue = _make_mock_queue(count=15)

        import app.main as app_main
        original_limit = app_main.SWEEP_QUEUE_DEPTH_LIMIT
        try:
            app_main.SWEEP_QUEUE_DEPTH_LIMIT = 10
            with patch("app.main.Queue", return_value=mock_queue):
                sweep_pending_photos(mock_redis)
        finally:
            app_main.SWEEP_QUEUE_DEPTH_LIMIT = original_limit

        mock_queue.enqueue.assert_not_called()

    def test_breaker_trip_still_runs_face_clustering_and_contact_sync(self, db_session):
        """A breaker trip should suppress the stuck-item re-enqueue only — clustering and
        contact display-name sync don't feed the re-enqueue loop the breaker guards, so they
        must still run."""
        item = _make_media_item(db_session, sha256_hash="a1" * 32, media_type="photo")
        _make_processing_status(db_session, item.id, ProcessingState.pending, age_minutes=10)

        mock_redis = MagicMock()
        mock_queue = _make_mock_queue(count=100)

        with patch("app.main.Queue", return_value=mock_queue), \
             patch("lucos_photos_common.jobs.cluster_faces") as mock_cluster_faces, \
             patch("lucos_photos_common.jobs.sweep_contact_display_names") as mock_sync_names:
            sweep_pending_photos(mock_redis)

        mock_queue.enqueue.assert_not_called()
        mock_cluster_faces.assert_called_once()
        mock_sync_names.assert_called_once()
