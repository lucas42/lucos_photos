"""Tests for worker job handlers in lucos_photos_common.jobs."""

import io
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from lucos_photos_common.jobs import process_photo, reprocess_photo
from lucos_photos_common.models import Photo, ProcessingState, ProcessingStatus

# Minimal valid 1x1 JPEG bytes
VALID_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000"
    "ffdb004300080606070605080707070909080a0c"
    "140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20"
    "242e2720222c231c1c2837292c30313434341f27"
    "393d38323c2e333432ffdb004301090909090c0b"
    "0c180d0d1832211c213232323232323232323232"
    "3232323232323232323232323232323232323232"
    "3232323232323232323232323232323232323232"
    "ffc00011080001000103012200021101031101ff"
    "c4001f0000010501010101010100000000000000"
    "000102030405060708090a0bffc400b510000201"
    "0303020403050504040000017d010203000411051"
    "22131410613516107227114328191a1082342b1c1"
    "1552d1f02433627282090a161718191a25262728"
    "292a3435363738393a434445464748494a535455"
    "565758595a636465666768696a737475767778797"
    "a838485868788898a92939495969798999aa2a3a4"
    "a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6"
    "c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7"
    "e8e9eaf1f2f3f4f5f6f7f8f9faffc4001f010003"
    "01010101010101010100000000000001020304050"
    "60708090a0bffc400b51100020102040403040705"
    "040400010277000102031104052131061241510761"
    "711322328108144291a1b1c109233352f01562727"
    "2d10a162434e125f11718191a262728292a353637"
    "38393a434445464748494a535455565758595a636"
    "465666768696a737475767778797a828384858687"
    "88898a92939495969798999aa2a3a4a5a6a7a8a9"
    "aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2"
    "d3d4d5d6d7d8d9dae2e3e4e5e6e7e8e9eaf2f3f4"
    "f5f6f7f8f9faffda000c03010002110311003f00"
    "e2e8a28af993f713ffd9"
)

# EXIF tag ID for DateTimeOriginal
EXIF_TAG_DATETIME_ORIGINAL = 36867


def make_jpeg_with_exif(datetime_original: str | None = None) -> bytes:
    """Create a minimal 2x3 JPEG, optionally embedding a DateTimeOriginal EXIF tag.

    Args:
        datetime_original: EXIF datetime string in format "YYYY:MM:DD HH:MM:SS",
                           or None to omit the tag.
    """
    img = Image.new("RGB", (2, 3), color=(128, 64, 32))
    exif = img.getexif()
    if datetime_original is not None:
        exif[EXIF_TAG_DATETIME_ORIGINAL] = datetime_original
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif.tobytes())
    return buf.getvalue()


class TestProcessPhoto:
    def test_moves_file_to_originals(self, db_session, pending_photo, tmp_path):
        uploads_dir = tmp_path / "uploads"
        originals_dir = tmp_path / "originals"
        uploads_dir.mkdir()

        src = uploads_dir / f"{pending_photo.sha256_hash}.{pending_photo.file_extension}"
        src.write_bytes(VALID_JPEG)

        with patch("lucos_photos_common.jobs.UPLOADS_DIR", uploads_dir), \
             patch("lucos_photos_common.jobs.ORIGINALS_DIR", originals_dir):
            process_photo(str(pending_photo.id))

        assert not src.exists(), "Source file should have been moved"
        assert (originals_dir / src.name).exists(), "File should exist in originals"

    def test_sets_processing_state_to_complete(self, db_session, pending_photo, tmp_path):
        uploads_dir = tmp_path / "uploads"
        originals_dir = tmp_path / "originals"
        uploads_dir.mkdir()
        src = uploads_dir / f"{pending_photo.sha256_hash}.{pending_photo.file_extension}"
        src.write_bytes(VALID_JPEG)

        with patch("lucos_photos_common.jobs.UPLOADS_DIR", uploads_dir), \
             patch("lucos_photos_common.jobs.ORIGINALS_DIR", originals_dir):
            process_photo(str(pending_photo.id))

        db_session.refresh(pending_photo)
        assert pending_photo.processing_status.state == ProcessingState.complete

    def test_sets_width_and_height(self, db_session, pending_photo, tmp_path):
        uploads_dir = tmp_path / "uploads"
        originals_dir = tmp_path / "originals"
        uploads_dir.mkdir()
        src = uploads_dir / f"{pending_photo.sha256_hash}.{pending_photo.file_extension}"
        src.write_bytes(VALID_JPEG)

        with patch("lucos_photos_common.jobs.UPLOADS_DIR", uploads_dir), \
             patch("lucos_photos_common.jobs.ORIGINALS_DIR", originals_dir):
            process_photo(str(pending_photo.id))

        db_session.refresh(pending_photo)
        assert pending_photo.width == 1
        assert pending_photo.height == 1

    def test_idempotent_when_already_complete(self, db_session, pending_photo, tmp_path):
        """If a photo is already complete, process_photo should exit early without error."""
        pending_photo.processing_status.state = ProcessingState.complete
        db_session.commit()

        uploads_dir = tmp_path / "uploads"
        originals_dir = tmp_path / "originals"
        uploads_dir.mkdir()

        # No file in uploads — would fail if it tried to process
        with patch("lucos_photos_common.jobs.UPLOADS_DIR", uploads_dir), \
             patch("lucos_photos_common.jobs.ORIGINALS_DIR", originals_dir):
            process_photo(str(pending_photo.id))  # Should not raise

    def test_marks_failed_when_file_missing(self, db_session, pending_photo, tmp_path):
        uploads_dir = tmp_path / "uploads"
        originals_dir = tmp_path / "originals"
        uploads_dir.mkdir()
        # Don't create the source file

        with patch("lucos_photos_common.jobs.UPLOADS_DIR", uploads_dir), \
             patch("lucos_photos_common.jobs.ORIGINALS_DIR", originals_dir):
            with pytest.raises(FileNotFoundError):
                process_photo(str(pending_photo.id))

        db_session.refresh(pending_photo)
        assert pending_photo.processing_status.state == ProcessingState.failed
        assert pending_photo.processing_status.error_message is not None

    def test_skips_move_if_already_in_originals(self, db_session, pending_photo, tmp_path):
        """If the file is already in originals (e.g. retry after partial failure), skip the move."""
        uploads_dir = tmp_path / "uploads"
        originals_dir = tmp_path / "originals"
        uploads_dir.mkdir()
        originals_dir.mkdir()

        dest = originals_dir / f"{pending_photo.sha256_hash}.{pending_photo.file_extension}"
        dest.write_bytes(VALID_JPEG)

        with patch("lucos_photos_common.jobs.UPLOADS_DIR", uploads_dir), \
             patch("lucos_photos_common.jobs.ORIGINALS_DIR", originals_dir):
            process_photo(str(pending_photo.id))

        db_session.refresh(pending_photo)
        assert pending_photo.processing_status.state == ProcessingState.complete

    def test_nonexistent_photo_id_is_a_noop(self, db_session, tmp_path):
        """process_photo should log a warning and return cleanly for unknown photo IDs."""
        fake_id = str(uuid.uuid4())
        uploads_dir = tmp_path / "uploads"
        originals_dir = tmp_path / "originals"
        uploads_dir.mkdir()

        with patch("lucos_photos_common.jobs.UPLOADS_DIR", uploads_dir), \
             patch("lucos_photos_common.jobs.ORIGINALS_DIR", originals_dir):
            process_photo(fake_id)  # Should not raise

    def test_extracts_taken_at_from_exif(self, db_session, pending_photo, tmp_path):
        """EXIF DateTimeOriginal should be parsed and stored as a UTC-aware datetime.

        SQLite (used in tests) strips timezone info on round-trip, so we compare
        the naive UTC value. In production (PostgreSQL with timezone=True), the
        timezone is preserved.
        """
        uploads_dir = tmp_path / "uploads"
        originals_dir = tmp_path / "originals"
        uploads_dir.mkdir()

        jpeg_with_exif = make_jpeg_with_exif("2023:06:15 14:30:00")
        src = uploads_dir / f"{pending_photo.sha256_hash}.{pending_photo.file_extension}"
        src.write_bytes(jpeg_with_exif)

        with patch("lucos_photos_common.jobs.UPLOADS_DIR", uploads_dir), \
             patch("lucos_photos_common.jobs.ORIGINALS_DIR", originals_dir):
            process_photo(str(pending_photo.id))

        db_session.refresh(pending_photo)
        assert pending_photo.taken_at is not None
        # Strip tz for comparison since SQLite drops timezone info on round-trip
        actual_naive = pending_photo.taken_at.replace(tzinfo=None)
        assert actual_naive == datetime(2023, 6, 15, 14, 30, 0)

    def test_taken_at_is_none_when_no_exif(self, db_session, pending_photo, tmp_path):
        """Photos without EXIF DateTimeOriginal should have taken_at remain None."""
        uploads_dir = tmp_path / "uploads"
        originals_dir = tmp_path / "originals"
        uploads_dir.mkdir()

        jpeg_no_exif = make_jpeg_with_exif(datetime_original=None)
        src = uploads_dir / f"{pending_photo.sha256_hash}.{pending_photo.file_extension}"
        src.write_bytes(jpeg_no_exif)

        with patch("lucos_photos_common.jobs.UPLOADS_DIR", uploads_dir), \
             patch("lucos_photos_common.jobs.ORIGINALS_DIR", originals_dir):
            process_photo(str(pending_photo.id))

        db_session.refresh(pending_photo)
        assert pending_photo.taken_at is None

    def test_sets_correct_dimensions_from_pillow_jpeg(self, db_session, pending_photo, tmp_path):
        """Dimensions should be read from the actual image, not hardcoded."""
        uploads_dir = tmp_path / "uploads"
        originals_dir = tmp_path / "originals"
        uploads_dir.mkdir()

        # make_jpeg_with_exif creates a 2x3 image
        jpeg_bytes = make_jpeg_with_exif()
        src = uploads_dir / f"{pending_photo.sha256_hash}.{pending_photo.file_extension}"
        src.write_bytes(jpeg_bytes)

        with patch("lucos_photos_common.jobs.UPLOADS_DIR", uploads_dir), \
             patch("lucos_photos_common.jobs.ORIGINALS_DIR", originals_dir):
            process_photo(str(pending_photo.id))

        db_session.refresh(pending_photo)
        assert pending_photo.width == 2
        assert pending_photo.height == 3


class TestReprocessPhoto:
    def test_resets_state_to_pending(self, db_session, pending_photo, tmp_path):
        # Set state to complete first
        pending_photo.processing_status.state = ProcessingState.complete
        db_session.commit()

        mock_queue = MagicMock()
        with patch("lucos_photos_common.jobs.Redis") as mock_redis_cls, \
             patch("lucos_photos_common.jobs.Queue", return_value=mock_queue):
            mock_redis_cls.from_url.return_value = MagicMock()
            reprocess_photo(str(pending_photo.id))

        db_session.refresh(pending_photo)
        assert pending_photo.processing_status.state == ProcessingState.pending

    def test_enqueues_process_photo(self, db_session, pending_photo):
        mock_queue = MagicMock()
        with patch("lucos_photos_common.jobs.Redis") as mock_redis_cls, \
             patch("lucos_photos_common.jobs.Queue", return_value=mock_queue):
            mock_redis_cls.from_url.return_value = MagicMock()
            reprocess_photo(str(pending_photo.id))

        mock_queue.enqueue.assert_called_once()
        call_args = mock_queue.enqueue.call_args
        # First arg should be the process_photo function
        assert call_args[0][0] is process_photo
        # Second arg should be the photo ID string
        assert call_args[0][1] == str(pending_photo.id)

    def test_clears_error_message_on_reset(self, db_session, pending_photo):
        pending_photo.processing_status.state = ProcessingState.failed
        pending_photo.processing_status.error_message = "Something went wrong"
        db_session.commit()

        mock_queue = MagicMock()
        with patch("lucos_photos_common.jobs.Redis") as mock_redis_cls, \
             patch("lucos_photos_common.jobs.Queue", return_value=mock_queue):
            mock_redis_cls.from_url.return_value = MagicMock()
            reprocess_photo(str(pending_photo.id))

        db_session.refresh(pending_photo)
        assert pending_photo.processing_status.error_message is None
