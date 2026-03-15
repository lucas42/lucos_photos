"""Tests for worker job handlers in lucos_photos_common.jobs."""

import io
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from PIL import Image

from lucos_photos_common.jobs import (
    _extract_video_metadata,
    _frontality_score,
    _score_face,
    cluster_faces,
    detect_and_save_faces,
    generate_profile_picture,
    process_photo,
    process_video,
    reprocess_photo,
    resweep_thumbnails,
)
from lucos_photos_common.models import Face, MediaItem, Person, Photo, PhotoPerson, ProcessingState, ProcessingStatus

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


# EXIF Orientation tag ID
EXIF_TAG_ORIENTATION = 274


def make_jpeg_with_orientation(width: int, height: int, orientation: int) -> bytes:
    """Create a JPEG with the given pixel dimensions and EXIF Orientation tag.

    The pixels are stored as width x height (raw storage dimensions), with the
    Orientation tag indicating how the viewer should rotate/flip to display correctly.
    For orientation 6 (rotate 90° CW), the display dimensions are height x width.
    """
    img = Image.new("RGB", (width, height), color=(100, 150, 200))
    exif = img.getexif()
    exif[EXIF_TAG_ORIENTATION] = orientation
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif.tobytes())
    return buf.getvalue()


class TestProcessPhoto:
    @pytest.fixture(autouse=True)
    def mock_face_detection(self):
        """Patch detect_and_save_faces so process_photo tests don't need InsightFace installed."""
        with patch("lucos_photos_common.jobs.detect_and_save_faces") as mock_detect:
            yield mock_detect

    def test_moves_file_to_originals(self, db_session, pending_photo, tmp_path):
        uploads_dir = tmp_path / "uploads"
        originals_dir = tmp_path / "originals"
        derivatives_dir = tmp_path / "derivatives"
        uploads_dir.mkdir()

        src = uploads_dir / f"{pending_photo.sha256_hash}.{pending_photo.file_extension}"
        src.write_bytes(VALID_JPEG)

        with patch("lucos_photos_common.jobs.UPLOADS_DIR", uploads_dir), \
             patch("lucos_photos_common.jobs.ORIGINALS_DIR", originals_dir), \
             patch("lucos_photos_common.jobs.DERIVATIVES_DIR", derivatives_dir):
            process_photo(str(pending_photo.id))

        assert not src.exists(), "Source file should have been moved"
        assert (originals_dir / src.name).exists(), "File should exist in originals"

    def test_sets_processing_state_to_complete(self, db_session, pending_photo, tmp_path):
        uploads_dir = tmp_path / "uploads"
        originals_dir = tmp_path / "originals"
        derivatives_dir = tmp_path / "derivatives"
        uploads_dir.mkdir()
        src = uploads_dir / f"{pending_photo.sha256_hash}.{pending_photo.file_extension}"
        src.write_bytes(VALID_JPEG)

        with patch("lucos_photos_common.jobs.UPLOADS_DIR", uploads_dir), \
             patch("lucos_photos_common.jobs.ORIGINALS_DIR", originals_dir), \
             patch("lucos_photos_common.jobs.DERIVATIVES_DIR", derivatives_dir):
            process_photo(str(pending_photo.id))

        db_session.refresh(pending_photo)
        assert pending_photo.processing_status.state == ProcessingState.complete

    def test_sets_width_and_height(self, db_session, pending_photo, tmp_path):
        uploads_dir = tmp_path / "uploads"
        originals_dir = tmp_path / "originals"
        derivatives_dir = tmp_path / "derivatives"
        uploads_dir.mkdir()
        src = uploads_dir / f"{pending_photo.sha256_hash}.{pending_photo.file_extension}"
        src.write_bytes(VALID_JPEG)

        with patch("lucos_photos_common.jobs.UPLOADS_DIR", uploads_dir), \
             patch("lucos_photos_common.jobs.ORIGINALS_DIR", originals_dir), \
             patch("lucos_photos_common.jobs.DERIVATIVES_DIR", derivatives_dir):
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
        derivatives_dir = tmp_path / "derivatives"
        uploads_dir.mkdir()

        # No file in uploads — would fail if it tried to process
        with patch("lucos_photos_common.jobs.UPLOADS_DIR", uploads_dir), \
             patch("lucos_photos_common.jobs.ORIGINALS_DIR", originals_dir), \
             patch("lucos_photos_common.jobs.DERIVATIVES_DIR", derivatives_dir):
            process_photo(str(pending_photo.id))  # Should not raise

    def test_reconciles_status_when_work_already_done(self, db_session, pending_photo, tmp_path):
        """If the file and thumbnail already exist but status isn't complete, reconcile to complete."""
        # Status is stuck in 'processing' (e.g. crashed after work, before status write)
        pending_photo.processing_status.state = ProcessingState.processing
        db_session.commit()

        uploads_dir = tmp_path / "uploads"
        originals_dir = tmp_path / "originals"
        derivatives_dir = tmp_path / "derivatives"
        originals_dir.mkdir(parents=True)
        derivatives_dir.mkdir(parents=True)

        # Pre-create the work products so the job detects them as already done
        dest = originals_dir / f"{pending_photo.sha256_hash}.{pending_photo.file_extension}"
        dest.write_bytes(VALID_JPEG)
        thumb = derivatives_dir / f"{pending_photo.sha256_hash}_thumb.jpg"
        thumb.write_bytes(b"fake thumbnail")

        with patch("lucos_photos_common.jobs.UPLOADS_DIR", uploads_dir), \
             patch("lucos_photos_common.jobs.ORIGINALS_DIR", originals_dir), \
             patch("lucos_photos_common.jobs.DERIVATIVES_DIR", derivatives_dir):
            process_photo(str(pending_photo.id))  # Should not raise

        db_session.refresh(pending_photo)
        assert pending_photo.processing_status.state == ProcessingState.complete

    def test_reconciles_status_when_work_done_but_status_pending(self, db_session, pending_photo, tmp_path):
        """Status stuck in 'pending' with work products present should also be reconciled."""
        # Status is pending — work products already exist from a previous run
        uploads_dir = tmp_path / "uploads"
        originals_dir = tmp_path / "originals"
        derivatives_dir = tmp_path / "derivatives"
        originals_dir.mkdir(parents=True)
        derivatives_dir.mkdir(parents=True)

        dest = originals_dir / f"{pending_photo.sha256_hash}.{pending_photo.file_extension}"
        dest.write_bytes(VALID_JPEG)
        thumb = derivatives_dir / f"{pending_photo.sha256_hash}_thumb.jpg"
        thumb.write_bytes(b"fake thumbnail")

        with patch("lucos_photos_common.jobs.UPLOADS_DIR", uploads_dir), \
             patch("lucos_photos_common.jobs.ORIGINALS_DIR", originals_dir), \
             patch("lucos_photos_common.jobs.DERIVATIVES_DIR", derivatives_dir):
            process_photo(str(pending_photo.id))

        db_session.refresh(pending_photo)
        assert pending_photo.processing_status.state == ProcessingState.complete

    def test_marks_failed_when_file_missing(self, db_session, pending_photo, tmp_path):
        uploads_dir = tmp_path / "uploads"
        originals_dir = tmp_path / "originals"
        derivatives_dir = tmp_path / "derivatives"
        uploads_dir.mkdir()
        # Don't create the source file

        with patch("lucos_photos_common.jobs.UPLOADS_DIR", uploads_dir), \
             patch("lucos_photos_common.jobs.ORIGINALS_DIR", originals_dir), \
             patch("lucos_photos_common.jobs.DERIVATIVES_DIR", derivatives_dir):
            with pytest.raises(FileNotFoundError):
                process_photo(str(pending_photo.id))

        db_session.refresh(pending_photo)
        assert pending_photo.processing_status.state == ProcessingState.failed
        assert pending_photo.processing_status.error_message is not None

    def test_skips_move_if_already_in_originals(self, db_session, pending_photo, tmp_path):
        """If the file is already in originals (e.g. retry after partial failure), skip the move."""
        uploads_dir = tmp_path / "uploads"
        originals_dir = tmp_path / "originals"
        derivatives_dir = tmp_path / "derivatives"
        uploads_dir.mkdir()
        originals_dir.mkdir()

        dest = originals_dir / f"{pending_photo.sha256_hash}.{pending_photo.file_extension}"
        dest.write_bytes(VALID_JPEG)

        with patch("lucos_photos_common.jobs.UPLOADS_DIR", uploads_dir), \
             patch("lucos_photos_common.jobs.ORIGINALS_DIR", originals_dir), \
             patch("lucos_photos_common.jobs.DERIVATIVES_DIR", derivatives_dir):
            process_photo(str(pending_photo.id))

        db_session.refresh(pending_photo)
        assert pending_photo.processing_status.state == ProcessingState.complete

    def test_nonexistent_photo_id_is_a_noop(self, db_session, tmp_path):
        """process_photo should log a warning and return cleanly for unknown photo IDs."""
        fake_id = str(uuid.uuid4())
        uploads_dir = tmp_path / "uploads"
        originals_dir = tmp_path / "originals"
        derivatives_dir = tmp_path / "derivatives"
        uploads_dir.mkdir()

        with patch("lucos_photos_common.jobs.UPLOADS_DIR", uploads_dir), \
             patch("lucos_photos_common.jobs.ORIGINALS_DIR", originals_dir), \
             patch("lucos_photos_common.jobs.DERIVATIVES_DIR", derivatives_dir):
            process_photo(fake_id)  # Should not raise

    def test_extracts_taken_at_from_exif(self, db_session, pending_photo, tmp_path):
        """EXIF DateTimeOriginal should be parsed and stored as a UTC-aware datetime.

        SQLite (used in tests) strips timezone info on round-trip, so we compare
        the naive UTC value. In production (PostgreSQL with timezone=True), the
        timezone is preserved.
        """
        uploads_dir = tmp_path / "uploads"
        originals_dir = tmp_path / "originals"
        derivatives_dir = tmp_path / "derivatives"
        uploads_dir.mkdir()

        jpeg_with_exif = make_jpeg_with_exif("2023:06:15 14:30:00")
        src = uploads_dir / f"{pending_photo.sha256_hash}.{pending_photo.file_extension}"
        src.write_bytes(jpeg_with_exif)

        with patch("lucos_photos_common.jobs.UPLOADS_DIR", uploads_dir), \
             patch("lucos_photos_common.jobs.ORIGINALS_DIR", originals_dir), \
             patch("lucos_photos_common.jobs.DERIVATIVES_DIR", derivatives_dir):
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
        derivatives_dir = tmp_path / "derivatives"
        uploads_dir.mkdir()

        jpeg_no_exif = make_jpeg_with_exif(datetime_original=None)
        src = uploads_dir / f"{pending_photo.sha256_hash}.{pending_photo.file_extension}"
        src.write_bytes(jpeg_no_exif)

        with patch("lucos_photos_common.jobs.UPLOADS_DIR", uploads_dir), \
             patch("lucos_photos_common.jobs.ORIGINALS_DIR", originals_dir), \
             patch("lucos_photos_common.jobs.DERIVATIVES_DIR", derivatives_dir):
            process_photo(str(pending_photo.id))

        db_session.refresh(pending_photo)
        assert pending_photo.taken_at is None

    def test_sets_correct_dimensions_from_pillow_jpeg(self, db_session, pending_photo, tmp_path):
        """Dimensions should be read from the actual image, not hardcoded."""
        uploads_dir = tmp_path / "uploads"
        originals_dir = tmp_path / "originals"
        derivatives_dir = tmp_path / "derivatives"
        uploads_dir.mkdir()

        # make_jpeg_with_exif creates a 2x3 image
        jpeg_bytes = make_jpeg_with_exif()
        src = uploads_dir / f"{pending_photo.sha256_hash}.{pending_photo.file_extension}"
        src.write_bytes(jpeg_bytes)

        with patch("lucos_photos_common.jobs.UPLOADS_DIR", uploads_dir), \
             patch("lucos_photos_common.jobs.ORIGINALS_DIR", originals_dir), \
             patch("lucos_photos_common.jobs.DERIVATIVES_DIR", derivatives_dir):
            process_photo(str(pending_photo.id))

        db_session.refresh(pending_photo)
        assert pending_photo.width == 2
        assert pending_photo.height == 3

    def test_generates_thumbnail(self, db_session, pending_photo, tmp_path):
        """process_photo should generate a thumbnail JPEG in the derivatives directory."""
        uploads_dir = tmp_path / "uploads"
        originals_dir = tmp_path / "originals"
        derivatives_dir = tmp_path / "derivatives"
        uploads_dir.mkdir()

        # Create a 400x600 image so thumbnail width math is straightforward
        img = Image.new("RGB", (400, 600), color=(10, 20, 30))
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        src = uploads_dir / f"{pending_photo.sha256_hash}.{pending_photo.file_extension}"
        src.write_bytes(buf.getvalue())

        with patch("lucos_photos_common.jobs.UPLOADS_DIR", uploads_dir), \
             patch("lucos_photos_common.jobs.ORIGINALS_DIR", originals_dir), \
             patch("lucos_photos_common.jobs.DERIVATIVES_DIR", derivatives_dir):
            process_photo(str(pending_photo.id))

        thumb_path = derivatives_dir / f"{pending_photo.sha256_hash}_thumb.jpg"
        assert thumb_path.exists(), "Thumbnail file should have been created"

        with Image.open(thumb_path) as thumb:
            assert thumb.width == 400
            assert thumb.height == 600  # 600 * 400/400 = 600

    def test_thumbnail_preserves_aspect_ratio(self, db_session, pending_photo, tmp_path):
        """Thumbnail height should be scaled proportionally from width."""
        uploads_dir = tmp_path / "uploads"
        originals_dir = tmp_path / "originals"
        derivatives_dir = tmp_path / "derivatives"
        uploads_dir.mkdir()

        # Create an 800x600 image; at 400px wide, height should be 300
        img = Image.new("RGB", (800, 600), color=(50, 100, 150))
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        src = uploads_dir / f"{pending_photo.sha256_hash}.{pending_photo.file_extension}"
        src.write_bytes(buf.getvalue())

        with patch("lucos_photos_common.jobs.UPLOADS_DIR", uploads_dir), \
             patch("lucos_photos_common.jobs.ORIGINALS_DIR", originals_dir), \
             patch("lucos_photos_common.jobs.DERIVATIVES_DIR", derivatives_dir):
            process_photo(str(pending_photo.id))

        thumb_path = derivatives_dir / f"{pending_photo.sha256_hash}_thumb.jpg"
        with Image.open(thumb_path) as thumb:
            assert thumb.width == 400
            assert thumb.height == 300

    def test_thumbnail_path_uses_sha256(self, db_session, pending_photo, tmp_path):
        """Thumbnail should be named {sha256}_thumb.jpg for predictable path construction."""
        uploads_dir = tmp_path / "uploads"
        originals_dir = tmp_path / "originals"
        derivatives_dir = tmp_path / "derivatives"
        uploads_dir.mkdir()

        src = uploads_dir / f"{pending_photo.sha256_hash}.{pending_photo.file_extension}"
        src.write_bytes(make_jpeg_with_exif())

        with patch("lucos_photos_common.jobs.UPLOADS_DIR", uploads_dir), \
             patch("lucos_photos_common.jobs.ORIGINALS_DIR", originals_dir), \
             patch("lucos_photos_common.jobs.DERIVATIVES_DIR", derivatives_dir):
            process_photo(str(pending_photo.id))

        expected_name = f"{pending_photo.sha256_hash}_thumb.jpg"
        assert (derivatives_dir / expected_name).exists()

    def test_exif_orientation_corrects_dimensions(self, db_session, pending_photo, tmp_path):
        """Width/height stored in DB should reflect display orientation, not raw pixel order.

        Orientation 6 means the raw pixels are 200x400 (landscape) but the photo is
        displayed as 400x200 (portrait, rotated 90° CW). The DB should store 400x200.
        """
        uploads_dir = tmp_path / "uploads"
        originals_dir = tmp_path / "originals"
        derivatives_dir = tmp_path / "derivatives"
        uploads_dir.mkdir()

        # Raw pixels: 200 wide x 400 tall, but Orientation=6 rotates 90° CW → display 400x200
        jpeg_bytes = make_jpeg_with_orientation(width=200, height=400, orientation=6)
        src = uploads_dir / f"{pending_photo.sha256_hash}.{pending_photo.file_extension}"
        src.write_bytes(jpeg_bytes)

        with patch("lucos_photos_common.jobs.UPLOADS_DIR", uploads_dir), \
             patch("lucos_photos_common.jobs.ORIGINALS_DIR", originals_dir), \
             patch("lucos_photos_common.jobs.DERIVATIVES_DIR", derivatives_dir):
            process_photo(str(pending_photo.id))

        db_session.refresh(pending_photo)
        assert pending_photo.width == 400, "Width should reflect post-transpose display width"
        assert pending_photo.height == 200, "Height should reflect post-transpose display height"

    def test_exif_orientation_corrects_thumbnail(self, db_session, pending_photo, tmp_path):
        """Thumbnails should have dimensions matching the display orientation, not raw pixels.

        Orientation 6 means raw pixels are 200x400 but display is 400x200 (portrait rotated).
        After exif_transpose the image is 400x200 — thumbnail width stays 400, height is 200.
        """
        uploads_dir = tmp_path / "uploads"
        originals_dir = tmp_path / "originals"
        derivatives_dir = tmp_path / "derivatives"
        uploads_dir.mkdir()

        # Raw pixels: 200 wide x 400 tall, Orientation=6 → display 400x200
        jpeg_bytes = make_jpeg_with_orientation(width=200, height=400, orientation=6)
        src = uploads_dir / f"{pending_photo.sha256_hash}.{pending_photo.file_extension}"
        src.write_bytes(jpeg_bytes)

        with patch("lucos_photos_common.jobs.UPLOADS_DIR", uploads_dir), \
             patch("lucos_photos_common.jobs.ORIGINALS_DIR", originals_dir), \
             patch("lucos_photos_common.jobs.DERIVATIVES_DIR", derivatives_dir):
            process_photo(str(pending_photo.id))

        thumb_path = derivatives_dir / f"{pending_photo.sha256_hash}_thumb.jpg"
        assert thumb_path.exists()
        with Image.open(thumb_path) as thumb:
            # After transpose: 400w x 200h. THUMBNAIL_WIDTH=400, height = 200 * 400/400 = 200.
            assert thumb.width == 400
            assert thumb.height == 200

    def test_thumbnail_idempotent_when_already_exists(self, db_session, pending_photo, tmp_path):
        """If a thumbnail already exists, process_photo should not overwrite it."""
        uploads_dir = tmp_path / "uploads"
        originals_dir = tmp_path / "originals"
        derivatives_dir = tmp_path / "derivatives"
        uploads_dir.mkdir()
        derivatives_dir.mkdir()

        src = uploads_dir / f"{pending_photo.sha256_hash}.{pending_photo.file_extension}"
        src.write_bytes(make_jpeg_with_exif())

        # Pre-create a sentinel thumbnail file
        existing_thumb = derivatives_dir / f"{pending_photo.sha256_hash}_thumb.jpg"
        existing_thumb.write_bytes(b"sentinel")

        with patch("lucos_photos_common.jobs.UPLOADS_DIR", uploads_dir), \
             patch("lucos_photos_common.jobs.ORIGINALS_DIR", originals_dir), \
             patch("lucos_photos_common.jobs.DERIVATIVES_DIR", derivatives_dir):
            process_photo(str(pending_photo.id))

        # Thumbnail should not have been replaced
        assert existing_thumb.read_bytes() == b"sentinel", "Existing thumbnail should not be overwritten"


class TestDetectAndSaveFaces:
    """Tests for detect_and_save_faces().

    InsightFace and OpenCV are not installed in the test environment, so all ML
    calls are mocked. The tests focus on the data pipeline: normalisation,
    database persistence, idempotency, and person auto-assignment logic.

    Each test patches:
    - ``lucos_photos_common.jobs._get_face_analysis_app`` — returns a per-test mock
      app instance, avoiding singleton state leaking between tests.
    - ``cv2`` in sys.modules — so the ``import cv2`` inside detect_and_save_faces
      resolves to a mock without requiring OpenCV to be installed.
    """

    @pytest.fixture
    def photo_with_dimensions(self, db_session):
        """A pending photo with width/height already set (as if metadata extraction completed)."""
        photo = Photo(sha256_hash="b" * 64, file_extension="jpg", width=1000, height=800)
        db_session.add(photo)
        db_session.flush()
        status = ProcessingStatus(photo_id=photo.id, state=ProcessingState.processing)
        db_session.add(status)
        db_session.commit()
        db_session.refresh(photo)
        return photo

    def _make_mock_face(self, bbox, embedding=None, det_score=0.95, kps=None):
        """Build a mock InsightFace face object with the given bbox and optional embedding."""
        face = MagicMock()
        face.bbox = bbox
        face.embedding = np.array(embedding) if embedding is not None else None
        face.det_score = det_score
        # kps is a numpy array of shape (5, 2); tolist() must return a real list
        if kps is not None:
            kps_array = MagicMock()
            kps_array.tolist.return_value = kps
            face.kps = kps_array
        else:
            face.kps = None
        return face

    def _mock_insightface(self, mock_app_instance, mock_cv2=None):
        """Return a context manager patching _get_face_analysis_app and cv2.

        Patches _get_face_analysis_app to return mock_app_instance so the singleton
        is bypassed entirely — each test gets its own app mock with independent
        return values. cv2 is patched in sys.modules so the bare ``import cv2``
        inside detect_and_save_faces resolves without OpenCV being installed.
        """
        if mock_cv2 is None:
            mock_cv2 = MagicMock()
            mock_cv2.imread.return_value = MagicMock()
        return (
            patch("lucos_photos_common.jobs._get_face_analysis_app", return_value=mock_app_instance),
            patch.dict("sys.modules", {"cv2": mock_cv2}),
            mock_cv2,
        )

    def test_saves_face_record_for_each_detected_face(self, db_session, photo_with_dimensions, tmp_path):
        """One Face row should be inserted per detected face."""
        photo = photo_with_dimensions
        img_path = tmp_path / "test.jpg"
        img_path.write_bytes(b"fake")

        face1 = self._make_mock_face([100, 200, 300, 400], embedding=[0.1] * 512)
        face2 = self._make_mock_face([500, 100, 700, 350], embedding=[0.2] * 512)

        mock_app_instance = MagicMock()
        mock_app_instance.get.return_value = [face1, face2]
        patch_app, patch_cv2, _ = self._mock_insightface(mock_app_instance)

        with patch_app, patch_cv2:
            detect_and_save_faces(db_session, photo, img_path)

        faces = db_session.query(Face).filter(Face.photo_id == photo.id).all()
        assert len(faces) == 2

    def test_normalises_bounding_box_coordinates(self, db_session, photo_with_dimensions, tmp_path):
        """Bounding box should be converted from pixel coords to normalised 0.0-1.0 values."""
        photo = photo_with_dimensions  # width=1000, height=800
        img_path = tmp_path / "test.jpg"
        img_path.write_bytes(b"fake")

        # bbox: x1=100, y1=200, x2=600, y2=600 in a 1000x800 image
        # expected: x=0.1, y=0.25, w=0.5, h=0.5
        face = self._make_mock_face([100.0, 200.0, 600.0, 600.0], embedding=[0.0] * 512)

        mock_app_instance = MagicMock()
        mock_app_instance.get.return_value = [face]
        patch_app, patch_cv2, _ = self._mock_insightface(mock_app_instance)

        with patch_app, patch_cv2:
            detect_and_save_faces(db_session, photo, img_path)

        saved = db_session.query(Face).filter(Face.photo_id == photo.id).first()
        assert saved is not None
        assert abs(saved.bbox_x - 0.1) < 1e-6
        assert abs(saved.bbox_y - 0.25) < 1e-6
        assert abs(saved.bbox_width - 0.5) < 1e-6
        assert abs(saved.bbox_height - 0.5) < 1e-6

    def test_saves_embedding_vector(self, db_session, photo_with_dimensions, tmp_path):
        """The 512-dim embedding should be saved to the face record."""
        photo = photo_with_dimensions
        img_path = tmp_path / "test.jpg"
        img_path.write_bytes(b"fake")

        embedding = [float(i) / 512 for i in range(512)]
        face = self._make_mock_face([0.0, 0.0, 100.0, 100.0], embedding=embedding)

        mock_app_instance = MagicMock()
        mock_app_instance.get.return_value = [face]
        patch_app, patch_cv2, _ = self._mock_insightface(mock_app_instance)

        with patch_app, patch_cv2:
            detect_and_save_faces(db_session, photo, img_path)

        saved = db_session.query(Face).filter(Face.photo_id == photo.id).first()
        assert saved is not None
        assert saved.embedding is not None
        assert len(saved.embedding) == 512

    def test_person_confirmed_is_false_for_ml_guess(self, db_session, photo_with_dimensions, tmp_path):
        """Auto-assigned persons should always be unconfirmed (ML guess)."""
        photo = photo_with_dimensions
        img_path = tmp_path / "test.jpg"
        img_path.write_bytes(b"fake")

        face = self._make_mock_face([0.0, 0.0, 100.0, 100.0], embedding=[0.5] * 512)

        mock_app_instance = MagicMock()
        mock_app_instance.get.return_value = [face]
        patch_app, patch_cv2, _ = self._mock_insightface(mock_app_instance)

        with patch_app, patch_cv2:
            detect_and_save_faces(db_session, photo, img_path)

        saved = db_session.query(Face).filter(Face.photo_id == photo.id).first()
        assert saved.person_confirmed is False

    def test_no_faces_creates_no_records(self, db_session, photo_with_dimensions, tmp_path):
        """If InsightFace detects no faces, no Face rows should be inserted."""
        photo = photo_with_dimensions
        img_path = tmp_path / "test.jpg"
        img_path.write_bytes(b"fake")

        mock_app_instance = MagicMock()
        mock_app_instance.get.return_value = []
        patch_app, patch_cv2, _ = self._mock_insightface(mock_app_instance)

        with patch_app, patch_cv2:
            detect_and_save_faces(db_session, photo, img_path)

        count = db_session.query(Face).filter(Face.photo_id == photo.id).count()
        assert count == 0

    def test_face_without_embedding_saved_with_null_embedding(self, db_session, photo_with_dimensions, tmp_path):
        """A face with no embedding (e.g. detection-only model) should be saved with embedding=None."""
        photo = photo_with_dimensions
        img_path = tmp_path / "test.jpg"
        img_path.write_bytes(b"fake")

        face = self._make_mock_face([0.0, 0.0, 100.0, 100.0], embedding=None)

        mock_app_instance = MagicMock()
        mock_app_instance.get.return_value = [face]
        patch_app, patch_cv2, _ = self._mock_insightface(mock_app_instance)

        with patch_app, patch_cv2:
            detect_and_save_faces(db_session, photo, img_path)

        saved = db_session.query(Face).filter(Face.photo_id == photo.id).first()
        assert saved is not None
        assert saved.embedding is None
        assert saved.person_id is None

    def test_idempotent_deletes_existing_faces_before_rerun(self, db_session, photo_with_dimensions, tmp_path):
        """On re-run, existing face records for the photo should be deleted and recreated."""
        photo = photo_with_dimensions
        img_path = tmp_path / "test.jpg"
        img_path.write_bytes(b"fake")

        # Pre-create a stale face record
        stale_face = Face(
            photo_id=photo.id,
            bbox_x=0.5, bbox_y=0.5, bbox_width=0.1, bbox_height=0.1,
            person_confirmed=False,
        )
        db_session.add(stale_face)
        db_session.commit()

        face = self._make_mock_face([0.0, 0.0, 200.0, 200.0], embedding=[0.1] * 512)

        mock_app_instance = MagicMock()
        mock_app_instance.get.return_value = [face]
        patch_app, patch_cv2, _ = self._mock_insightface(mock_app_instance)

        with patch_app, patch_cv2:
            detect_and_save_faces(db_session, photo, img_path)

        faces = db_session.query(Face).filter(Face.photo_id == photo.id).all()
        # The stale face should be gone; only the freshly detected one should remain
        assert len(faces) == 1
        assert abs(faces[0].bbox_x - 0.0) < 1e-6

    def test_raises_if_cv2_cannot_read_image(self, db_session, photo_with_dimensions, tmp_path):
        """If cv2.imread returns None, detect_and_save_faces should raise a ValueError."""
        photo = photo_with_dimensions
        img_path = tmp_path / "test.jpg"
        img_path.write_bytes(b"not a real image")

        mock_cv2 = MagicMock()
        mock_cv2.imread.return_value = None  # Simulate unreadable file

        mock_app_instance = MagicMock()
        patch_app, patch_cv2, _ = self._mock_insightface(mock_app_instance, mock_cv2=mock_cv2)

        with patch_app, patch_cv2:
            with pytest.raises(ValueError, match="cv2.imread returned None"):
                detect_and_save_faces(db_session, photo, img_path)

    def test_skips_detection_when_photo_has_no_dimensions(self, db_session, tmp_path):
        """If width/height are None (metadata not yet extracted), detection should be skipped."""
        photo = Photo(sha256_hash="c" * 64, file_extension="jpg", width=None, height=None)
        db_session.add(photo)
        db_session.commit()

        img_path = tmp_path / "test.jpg"
        img_path.write_bytes(b"fake")

        mock_app_instance = MagicMock()
        patch_app, patch_cv2, mock_cv2 = self._mock_insightface(mock_app_instance)

        with patch_app, patch_cv2:
            detect_and_save_faces(db_session, photo, img_path)

        # cv2.imread should never have been called (we return early before loading the image)
        mock_cv2.imread.assert_not_called()
        count = db_session.query(Face).filter(Face.photo_id == photo.id).count()
        assert count == 0

    def test_process_photo_calls_face_detection(self, db_session, pending_photo, tmp_path):
        """process_photo should invoke detect_and_save_faces after metadata extraction."""
        uploads_dir = tmp_path / "uploads"
        originals_dir = tmp_path / "originals"
        derivatives_dir = tmp_path / "derivatives"
        uploads_dir.mkdir()

        src = uploads_dir / f"{pending_photo.sha256_hash}.{pending_photo.file_extension}"
        src.write_bytes(make_jpeg_with_exif())

        with patch("lucos_photos_common.jobs.UPLOADS_DIR", uploads_dir), \
             patch("lucos_photos_common.jobs.ORIGINALS_DIR", originals_dir), \
             patch("lucos_photos_common.jobs.DERIVATIVES_DIR", derivatives_dir), \
             patch("lucos_photos_common.jobs.detect_and_save_faces") as mock_detect:
            process_photo(str(pending_photo.id))

        mock_detect.assert_called_once()


class TestUpdateLoganne:
    """Tests that the jobs module calls the loganne library correctly."""

    def test_posts_to_loganne_endpoint(self):
        """Should POST a JSON payload to the Loganne endpoint via the library."""
        with patch("loganne.session") as mock_session:
            mock_response = MagicMock()
            mock_response.raise_for_status.return_value = None
            mock_session.post.return_value = mock_response

            from loganne import updateLoganne
            updateLoganne("photoProcessed", "Photo test-id processed by lucos_photos")

        mock_session.post.assert_called_once()
        call_args = mock_session.post.call_args
        payload = call_args[1]["json"]
        assert payload["type"] == "photoProcessed"
        assert payload["humanReadable"] == "Photo test-id processed by lucos_photos"

    def test_swallows_http_errors(self):
        """A failed HTTP call to Loganne must not propagate — the library handles this."""
        import requests

        with patch("loganne.session") as mock_session:
            mock_session.post.side_effect = requests.ConnectionError("connection refused")
            # Should not raise — the library swallows the error
            from loganne import updateLoganne
            updateLoganne("photoProcessed", "Photo test-id processed by lucos_photos")


class TestProcessPhotoLoganne:
    """Tests that process_photo emits a Loganne event at the right times."""

    @pytest.fixture(autouse=True)
    def mock_face_detection(self):
        with patch("lucos_photos_common.jobs.detect_and_save_faces"):
            yield

    def test_emits_loganne_event_on_success(self, db_session, pending_photo, tmp_path):
        """process_photo should call updateLoganne with photoProcessed after successful completion."""
        uploads_dir = tmp_path / "uploads"
        originals_dir = tmp_path / "originals"
        derivatives_dir = tmp_path / "derivatives"
        uploads_dir.mkdir()
        src = uploads_dir / f"{pending_photo.sha256_hash}.{pending_photo.file_extension}"
        src.write_bytes(make_jpeg_with_exif())

        with patch("lucos_photos_common.jobs.UPLOADS_DIR", uploads_dir), \
             patch("lucos_photos_common.jobs.ORIGINALS_DIR", originals_dir), \
             patch("lucos_photos_common.jobs.DERIVATIVES_DIR", derivatives_dir), \
             patch("lucos_photos_common.jobs.updateLoganne") as mock_update:
            process_photo(str(pending_photo.id))

        mock_update.assert_called_once()
        assert mock_update.call_args[0][0] == "photoProcessed"

    def test_does_not_emit_loganne_when_already_complete(self, db_session, pending_photo, tmp_path):
        """process_photo should not call updateLoganne when exiting early (already complete)."""
        pending_photo.processing_status.state = ProcessingState.complete
        db_session.commit()

        uploads_dir = tmp_path / "uploads"
        originals_dir = tmp_path / "originals"
        derivatives_dir = tmp_path / "derivatives"
        uploads_dir.mkdir()

        with patch("lucos_photos_common.jobs.UPLOADS_DIR", uploads_dir), \
             patch("lucos_photos_common.jobs.ORIGINALS_DIR", originals_dir), \
             patch("lucos_photos_common.jobs.DERIVATIVES_DIR", derivatives_dir), \
             patch("lucos_photos_common.jobs.updateLoganne") as mock_update:
            process_photo(str(pending_photo.id))

        mock_update.assert_not_called()

    def test_does_not_emit_loganne_on_failure(self, db_session, pending_photo, tmp_path):
        """process_photo should not call updateLoganne when processing fails."""
        uploads_dir = tmp_path / "uploads"
        originals_dir = tmp_path / "originals"
        derivatives_dir = tmp_path / "derivatives"
        uploads_dir.mkdir()
        # No file in uploads — will cause a failure

        with patch("lucos_photos_common.jobs.UPLOADS_DIR", uploads_dir), \
             patch("lucos_photos_common.jobs.ORIGINALS_DIR", originals_dir), \
             patch("lucos_photos_common.jobs.DERIVATIVES_DIR", derivatives_dir), \
             patch("lucos_photos_common.jobs.updateLoganne") as mock_update:
            with pytest.raises(FileNotFoundError):
                process_photo(str(pending_photo.id))

        mock_update.assert_not_called()


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


class TestResweepThumbnails:
    def test_deletes_existing_thumbnails_and_resets_to_pending(self, db_session, tmp_path):
        """resweep_thumbnails should delete thumbnail files and reset complete photos to pending."""
        derivatives_dir = tmp_path / "derivatives"
        derivatives_dir.mkdir()

        # Create a complete photo with a thumbnail on disk
        photo = Photo(sha256_hash="c" * 64, file_extension="jpg")
        db_session.add(photo)
        db_session.flush()
        status = ProcessingStatus(photo_id=photo.id, state=ProcessingState.complete)
        db_session.add(status)
        db_session.commit()
        db_session.refresh(photo)

        thumb = derivatives_dir / f"{photo.sha256_hash}_thumb.jpg"
        thumb.write_bytes(b"old thumbnail")

        with patch("lucos_photos_common.jobs.DERIVATIVES_DIR", derivatives_dir):
            resweep_thumbnails()

        assert not thumb.exists(), "Thumbnail should have been deleted"
        db_session.refresh(status)
        assert status.state == ProcessingState.pending

    def test_resets_only_complete_photos(self, db_session, tmp_path):
        """resweep_thumbnails should not touch pending or failed photos."""
        derivatives_dir = tmp_path / "derivatives"
        derivatives_dir.mkdir()

        pending = Photo(sha256_hash="d" * 64, file_extension="jpg")
        db_session.add(pending)
        db_session.flush()
        pending_status = ProcessingStatus(photo_id=pending.id, state=ProcessingState.pending)
        db_session.add(pending_status)
        db_session.commit()

        with patch("lucos_photos_common.jobs.DERIVATIVES_DIR", derivatives_dir):
            resweep_thumbnails()

        db_session.refresh(pending_status)
        assert pending_status.state == ProcessingState.pending, "Pending photo should remain pending"

    def test_skips_photos_without_thumbnail_on_disk(self, db_session, tmp_path):
        """resweep_thumbnails should still reset the state even if no thumbnail exists on disk."""
        derivatives_dir = tmp_path / "derivatives"
        derivatives_dir.mkdir()

        photo = Photo(sha256_hash="e" * 64, file_extension="jpg")
        db_session.add(photo)
        db_session.flush()
        status = ProcessingStatus(photo_id=photo.id, state=ProcessingState.complete)
        db_session.add(status)
        db_session.commit()

        # No thumbnail file created — resweep should not raise
        with patch("lucos_photos_common.jobs.DERIVATIVES_DIR", derivatives_dir):
            resweep_thumbnails()

        db_session.refresh(status)
        assert status.state == ProcessingState.pending


# ---------------------------------------------------------------------------
# Fixtures for video tests
# ---------------------------------------------------------------------------

SAMPLE_FFPROBE_OUTPUT = {
    "streams": [
        {
            "codec_type": "video",
            "codec_name": "h264",
            "width": 1280,
            "height": 720,
            "r_frame_rate": "30/1",
            "duration": "10.0",
        }
    ],
    "format": {
        "duration": "10.0",
    },
}


class TestExtractVideoMetadata:
    """Tests for the _extract_video_metadata helper."""

    def _mock_ffprobe(self, ffprobe_output: dict):
        """Return a context-manager patch that makes subprocess.run return ffprobe JSON."""
        import json
        mock_result = MagicMock()
        mock_result.stdout = json.dumps(ffprobe_output)
        return patch("lucos_photos_common.jobs.subprocess.run", return_value=mock_result)

    def test_returns_expected_metadata(self, tmp_path):
        video_path = tmp_path / "test.mp4"
        video_path.write_bytes(b"fake")

        with self._mock_ffprobe(SAMPLE_FFPROBE_OUTPUT):
            meta = _extract_video_metadata(video_path)

        assert meta["codec"] == "h264"
        assert meta["video_width"] == 1280
        assert meta["video_height"] == 720
        assert meta["fps"] == 30.0
        assert meta["duration"] == 10.0

    def test_parses_fractional_fps(self, tmp_path):
        video_path = tmp_path / "test.mp4"
        video_path.write_bytes(b"fake")

        output = {
            "streams": [{
                "codec_type": "video",
                "codec_name": "h264",
                "width": 1920,
                "height": 1080,
                "r_frame_rate": "30000/1001",
                "duration": "5.0",
            }],
            "format": {"duration": "5.0"},
        }

        with self._mock_ffprobe(output):
            meta = _extract_video_metadata(video_path)

        assert abs(meta["fps"] - 29.97) < 0.01

    def test_falls_back_to_format_duration(self, tmp_path):
        """Duration should be read from format-level if not present in stream."""
        video_path = tmp_path / "test.mp4"
        video_path.write_bytes(b"fake")

        output = {
            "streams": [{
                "codec_type": "video",
                "codec_name": "vp9",
                "width": 640,
                "height": 360,
                "r_frame_rate": "25/1",
                # no duration here
            }],
            "format": {"duration": "42.5"},
        }

        with self._mock_ffprobe(output):
            meta = _extract_video_metadata(video_path)

        assert meta["duration"] == 42.5

    def test_raises_if_no_video_stream(self, tmp_path):
        video_path = tmp_path / "audio_only.mp3"
        video_path.write_bytes(b"fake")

        output = {
            "streams": [{"codec_type": "audio", "codec_name": "aac"}],
            "format": {"duration": "3.0"},
        }

        with self._mock_ffprobe(output):
            with pytest.raises(ValueError, match="No video stream"):
                _extract_video_metadata(video_path)

    def test_raises_if_ffprobe_fails(self, tmp_path):
        import subprocess as sp
        video_path = tmp_path / "test.mp4"
        video_path.write_bytes(b"fake")

        with patch("lucos_photos_common.jobs.subprocess.run",
                   side_effect=sp.CalledProcessError(1, "ffprobe")):
            with pytest.raises(sp.CalledProcessError):
                _extract_video_metadata(video_path)


class TestProcessVideo:
    """Tests for the process_video job handler."""

    @pytest.fixture
    def pending_video(self, db_session):
        """Create a video MediaItem with a pending processing status."""
        video = MediaItem(sha256_hash="v" * 64, file_extension="mp4", media_type="video")
        db_session.add(video)
        db_session.flush()
        status = ProcessingStatus(photo_id=video.id, state=ProcessingState.pending)
        db_session.add(status)
        db_session.commit()
        db_session.refresh(video)
        return video

    def _mock_ffprobe(self, ffprobe_output: dict):
        import json
        mock_result = MagicMock()
        mock_result.stdout = json.dumps(ffprobe_output)
        return patch("lucos_photos_common.jobs.subprocess.run", return_value=mock_result)

    def test_moves_file_to_originals(self, db_session, pending_video, tmp_path):
        uploads_dir = tmp_path / "uploads"
        originals_dir = tmp_path / "originals"
        derivatives_dir = tmp_path / "derivatives"
        uploads_dir.mkdir()

        src = uploads_dir / f"{pending_video.sha256_hash}.{pending_video.file_extension}"
        src.write_bytes(b"fake video")

        with patch("lucos_photos_common.jobs.UPLOADS_DIR", uploads_dir), \
             patch("lucos_photos_common.jobs.ORIGINALS_DIR", originals_dir), \
             patch("lucos_photos_common.jobs.DERIVATIVES_DIR", derivatives_dir), \
             self._mock_ffprobe(SAMPLE_FFPROBE_OUTPUT):
            process_video(str(pending_video.id))

        assert not src.exists(), "Source file should have been moved"
        assert (originals_dir / src.name).exists(), "File should exist in originals"

    def test_sets_processing_state_to_complete(self, db_session, pending_video, tmp_path):
        uploads_dir = tmp_path / "uploads"
        originals_dir = tmp_path / "originals"
        derivatives_dir = tmp_path / "derivatives"
        uploads_dir.mkdir()
        src = uploads_dir / f"{pending_video.sha256_hash}.{pending_video.file_extension}"
        src.write_bytes(b"fake video")

        with patch("lucos_photos_common.jobs.UPLOADS_DIR", uploads_dir), \
             patch("lucos_photos_common.jobs.ORIGINALS_DIR", originals_dir), \
             patch("lucos_photos_common.jobs.DERIVATIVES_DIR", derivatives_dir), \
             self._mock_ffprobe(SAMPLE_FFPROBE_OUTPUT):
            process_video(str(pending_video.id))

        db_session.refresh(pending_video)
        assert pending_video.processing_status.state == ProcessingState.complete

    def test_stores_video_metadata(self, db_session, pending_video, tmp_path):
        uploads_dir = tmp_path / "uploads"
        originals_dir = tmp_path / "originals"
        derivatives_dir = tmp_path / "derivatives"
        uploads_dir.mkdir()
        src = uploads_dir / f"{pending_video.sha256_hash}.{pending_video.file_extension}"
        src.write_bytes(b"fake video")

        with patch("lucos_photos_common.jobs.UPLOADS_DIR", uploads_dir), \
             patch("lucos_photos_common.jobs.ORIGINALS_DIR", originals_dir), \
             patch("lucos_photos_common.jobs.DERIVATIVES_DIR", derivatives_dir), \
             self._mock_ffprobe(SAMPLE_FFPROBE_OUTPUT):
            process_video(str(pending_video.id))

        db_session.refresh(pending_video)
        assert pending_video.codec == "h264"
        assert pending_video.video_width == 1280
        assert pending_video.video_height == 720
        assert pending_video.fps == 30.0
        assert pending_video.duration == 10.0

    def test_generates_thumbnail(self, db_session, pending_video, tmp_path):
        uploads_dir = tmp_path / "uploads"
        originals_dir = tmp_path / "originals"
        derivatives_dir = tmp_path / "derivatives"
        uploads_dir.mkdir()
        src = uploads_dir / f"{pending_video.sha256_hash}.{pending_video.file_extension}"
        src.write_bytes(b"fake video")

        import json

        def mock_run(cmd, **kwargs):
            if cmd[0] == "ffprobe":
                result = MagicMock()
                result.stdout = json.dumps(SAMPLE_FFPROBE_OUTPUT)
                return result
            else:
                # ffmpeg: simulate thumbnail creation by writing the output file
                out_path = Path(cmd[-1])
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_bytes(b"fake thumbnail")
                return MagicMock()

        with patch("lucos_photos_common.jobs.UPLOADS_DIR", uploads_dir), \
             patch("lucos_photos_common.jobs.ORIGINALS_DIR", originals_dir), \
             patch("lucos_photos_common.jobs.DERIVATIVES_DIR", derivatives_dir), \
             patch("lucos_photos_common.jobs.subprocess.run", side_effect=mock_run):
            process_video(str(pending_video.id))

        thumb_path = derivatives_dir / f"{pending_video.sha256_hash}_thumb.jpg"
        assert thumb_path.exists(), "Thumbnail file should have been created"

    def test_thumbnail_path_uses_sha256(self, db_session, pending_video, tmp_path):
        uploads_dir = tmp_path / "uploads"
        originals_dir = tmp_path / "originals"
        derivatives_dir = tmp_path / "derivatives"
        uploads_dir.mkdir()
        src = uploads_dir / f"{pending_video.sha256_hash}.{pending_video.file_extension}"
        src.write_bytes(b"fake video")

        import json

        def mock_run(cmd, **kwargs):
            if cmd[0] == "ffprobe":
                result = MagicMock()
                result.stdout = json.dumps(SAMPLE_FFPROBE_OUTPUT)
                return result
            else:
                out_path = Path(cmd[-1])
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_bytes(b"fake thumbnail")
                return MagicMock()

        with patch("lucos_photos_common.jobs.UPLOADS_DIR", uploads_dir), \
             patch("lucos_photos_common.jobs.ORIGINALS_DIR", originals_dir), \
             patch("lucos_photos_common.jobs.DERIVATIVES_DIR", derivatives_dir), \
             patch("lucos_photos_common.jobs.subprocess.run", side_effect=mock_run):
            process_video(str(pending_video.id))

        expected_name = f"{pending_video.sha256_hash}_thumb.jpg"
        assert (derivatives_dir / expected_name).exists()

    def test_thumbnail_idempotent_when_already_exists(self, db_session, pending_video, tmp_path):
        """If a thumbnail already exists, process_video should not overwrite it."""
        uploads_dir = tmp_path / "uploads"
        originals_dir = tmp_path / "originals"
        derivatives_dir = tmp_path / "derivatives"
        uploads_dir.mkdir()
        derivatives_dir.mkdir()

        src = uploads_dir / f"{pending_video.sha256_hash}.{pending_video.file_extension}"
        src.write_bytes(b"fake video")

        # Pre-create a sentinel thumbnail file
        existing_thumb = derivatives_dir / f"{pending_video.sha256_hash}_thumb.jpg"
        existing_thumb.write_bytes(b"sentinel")

        with patch("lucos_photos_common.jobs.UPLOADS_DIR", uploads_dir), \
             patch("lucos_photos_common.jobs.ORIGINALS_DIR", originals_dir), \
             patch("lucos_photos_common.jobs.DERIVATIVES_DIR", derivatives_dir), \
             self._mock_ffprobe(SAMPLE_FFPROBE_OUTPUT):
            process_video(str(pending_video.id))

        # Thumbnail should not have been replaced
        assert existing_thumb.read_bytes() == b"sentinel", "Existing thumbnail should not be overwritten"

    def test_idempotent_when_already_complete(self, db_session, pending_video, tmp_path):
        """If a video is already complete, process_video should exit early without error."""
        pending_video.processing_status.state = ProcessingState.complete
        db_session.commit()

        uploads_dir = tmp_path / "uploads"
        originals_dir = tmp_path / "originals"
        derivatives_dir = tmp_path / "derivatives"
        uploads_dir.mkdir()

        with patch("lucos_photos_common.jobs.UPLOADS_DIR", uploads_dir), \
             patch("lucos_photos_common.jobs.ORIGINALS_DIR", originals_dir), \
             patch("lucos_photos_common.jobs.DERIVATIVES_DIR", derivatives_dir):
            process_video(str(pending_video.id))  # Should not raise

    def test_reconciles_status_when_work_already_done(self, db_session, pending_video, tmp_path):
        """If the file and thumbnail already exist but status isn't complete, reconcile to complete."""
        # Status is stuck in 'processing' (e.g. crashed after work, before status write)
        pending_video.processing_status.state = ProcessingState.processing
        db_session.commit()

        uploads_dir = tmp_path / "uploads"
        originals_dir = tmp_path / "originals"
        derivatives_dir = tmp_path / "derivatives"
        originals_dir.mkdir(parents=True)
        derivatives_dir.mkdir(parents=True)

        dest = originals_dir / f"{pending_video.sha256_hash}.{pending_video.file_extension}"
        dest.write_bytes(b"fake video")
        thumb = derivatives_dir / f"{pending_video.sha256_hash}_thumb.jpg"
        thumb.write_bytes(b"fake thumbnail")

        with patch("lucos_photos_common.jobs.UPLOADS_DIR", uploads_dir), \
             patch("lucos_photos_common.jobs.ORIGINALS_DIR", originals_dir), \
             patch("lucos_photos_common.jobs.DERIVATIVES_DIR", derivatives_dir):
            process_video(str(pending_video.id))

        db_session.refresh(pending_video)
        assert pending_video.processing_status.state == ProcessingState.complete

    def test_reconciles_status_when_work_done_but_status_pending(self, db_session, pending_video, tmp_path):
        """Status stuck in 'pending' with work products present should also be reconciled."""
        uploads_dir = tmp_path / "uploads"
        originals_dir = tmp_path / "originals"
        derivatives_dir = tmp_path / "derivatives"
        originals_dir.mkdir(parents=True)
        derivatives_dir.mkdir(parents=True)

        dest = originals_dir / f"{pending_video.sha256_hash}.{pending_video.file_extension}"
        dest.write_bytes(b"fake video")
        thumb = derivatives_dir / f"{pending_video.sha256_hash}_thumb.jpg"
        thumb.write_bytes(b"fake thumbnail")

        with patch("lucos_photos_common.jobs.UPLOADS_DIR", uploads_dir), \
             patch("lucos_photos_common.jobs.ORIGINALS_DIR", originals_dir), \
             patch("lucos_photos_common.jobs.DERIVATIVES_DIR", derivatives_dir):
            process_video(str(pending_video.id))

        db_session.refresh(pending_video)
        assert pending_video.processing_status.state == ProcessingState.complete

    def test_marks_failed_when_file_missing(self, db_session, pending_video, tmp_path):
        uploads_dir = tmp_path / "uploads"
        originals_dir = tmp_path / "originals"
        derivatives_dir = tmp_path / "derivatives"
        uploads_dir.mkdir()
        # Don't create the source file

        with patch("lucos_photos_common.jobs.UPLOADS_DIR", uploads_dir), \
             patch("lucos_photos_common.jobs.ORIGINALS_DIR", originals_dir), \
             patch("lucos_photos_common.jobs.DERIVATIVES_DIR", derivatives_dir):
            with pytest.raises(FileNotFoundError):
                process_video(str(pending_video.id))

        db_session.refresh(pending_video)
        assert pending_video.processing_status.state == ProcessingState.failed
        assert pending_video.processing_status.error_message is not None

    def test_marks_failed_when_ffprobe_fails(self, db_session, pending_video, tmp_path):
        """If ffprobe fails, processing_status should be set to failed."""
        import subprocess as sp
        uploads_dir = tmp_path / "uploads"
        originals_dir = tmp_path / "originals"
        derivatives_dir = tmp_path / "derivatives"
        uploads_dir.mkdir()
        src = uploads_dir / f"{pending_video.sha256_hash}.{pending_video.file_extension}"
        src.write_bytes(b"corrupt")

        with patch("lucos_photos_common.jobs.UPLOADS_DIR", uploads_dir), \
             patch("lucos_photos_common.jobs.ORIGINALS_DIR", originals_dir), \
             patch("lucos_photos_common.jobs.DERIVATIVES_DIR", derivatives_dir), \
             patch("lucos_photos_common.jobs.subprocess.run",
                   side_effect=sp.CalledProcessError(1, "ffprobe")):
            with pytest.raises(sp.CalledProcessError):
                process_video(str(pending_video.id))

        db_session.refresh(pending_video)
        assert pending_video.processing_status.state == ProcessingState.failed

    def test_skips_move_if_already_in_originals(self, db_session, pending_video, tmp_path):
        """If the file is already in originals (e.g. retry after partial failure), skip the move."""
        uploads_dir = tmp_path / "uploads"
        originals_dir = tmp_path / "originals"
        derivatives_dir = tmp_path / "derivatives"
        uploads_dir.mkdir()
        originals_dir.mkdir()

        dest = originals_dir / f"{pending_video.sha256_hash}.{pending_video.file_extension}"
        dest.write_bytes(b"fake video")

        with patch("lucos_photos_common.jobs.UPLOADS_DIR", uploads_dir), \
             patch("lucos_photos_common.jobs.ORIGINALS_DIR", originals_dir), \
             patch("lucos_photos_common.jobs.DERIVATIVES_DIR", derivatives_dir), \
             self._mock_ffprobe(SAMPLE_FFPROBE_OUTPUT):
            process_video(str(pending_video.id))

        db_session.refresh(pending_video)
        assert pending_video.processing_status.state == ProcessingState.complete

    def test_nonexistent_id_is_a_noop(self, db_session, tmp_path):
        """process_video should log a warning and return cleanly for unknown IDs."""
        fake_id = str(uuid.uuid4())
        uploads_dir = tmp_path / "uploads"
        originals_dir = tmp_path / "originals"
        derivatives_dir = tmp_path / "derivatives"
        uploads_dir.mkdir()

        with patch("lucos_photos_common.jobs.UPLOADS_DIR", uploads_dir), \
             patch("lucos_photos_common.jobs.ORIGINALS_DIR", originals_dir), \
             patch("lucos_photos_common.jobs.DERIVATIVES_DIR", derivatives_dir):
            process_video(fake_id)  # Should not raise

    def test_emits_loganne_event_on_success(self, db_session, pending_video, tmp_path):
        uploads_dir = tmp_path / "uploads"
        originals_dir = tmp_path / "originals"
        derivatives_dir = tmp_path / "derivatives"
        uploads_dir.mkdir()
        src = uploads_dir / f"{pending_video.sha256_hash}.{pending_video.file_extension}"
        src.write_bytes(b"fake video")

        with patch("lucos_photos_common.jobs.UPLOADS_DIR", uploads_dir), \
             patch("lucos_photos_common.jobs.ORIGINALS_DIR", originals_dir), \
             patch("lucos_photos_common.jobs.DERIVATIVES_DIR", derivatives_dir), \
             self._mock_ffprobe(SAMPLE_FFPROBE_OUTPUT), \
             patch("lucos_photos_common.jobs.updateLoganne") as mock_update:
            process_video(str(pending_video.id))

        mock_update.assert_called_once()
        assert mock_update.call_args[0][0] == "videoProcessed"

    def test_does_not_emit_loganne_on_failure(self, db_session, pending_video, tmp_path):
        uploads_dir = tmp_path / "uploads"
        originals_dir = tmp_path / "originals"
        derivatives_dir = tmp_path / "derivatives"
        uploads_dir.mkdir()
        # No file in uploads — will cause a failure

        with patch("lucos_photos_common.jobs.UPLOADS_DIR", uploads_dir), \
             patch("lucos_photos_common.jobs.ORIGINALS_DIR", originals_dir), \
             patch("lucos_photos_common.jobs.DERIVATIVES_DIR", derivatives_dir), \
             patch("lucos_photos_common.jobs.updateLoganne") as mock_update:
            with pytest.raises(FileNotFoundError):
                process_video(str(pending_video.id))

        mock_update.assert_not_called()

    def test_ffmpeg_seek_at_10_percent(self, db_session, pending_video, tmp_path):
        """ffmpeg should be called with a seek time of 10% of the video duration."""
        uploads_dir = tmp_path / "uploads"
        originals_dir = tmp_path / "originals"
        derivatives_dir = tmp_path / "derivatives"
        uploads_dir.mkdir()
        src = uploads_dir / f"{pending_video.sha256_hash}.{pending_video.file_extension}"
        src.write_bytes(b"fake video")

        ffprobe_calls = []
        ffmpeg_calls = []

        def mock_run(cmd, **kwargs):
            import json
            if cmd[0] == "ffprobe":
                ffprobe_calls.append(cmd)
                result = MagicMock()
                result.stdout = json.dumps(SAMPLE_FFPROBE_OUTPUT)
                return result
            else:
                ffmpeg_calls.append(cmd)
                return MagicMock()

        with patch("lucos_photos_common.jobs.UPLOADS_DIR", uploads_dir), \
             patch("lucos_photos_common.jobs.ORIGINALS_DIR", originals_dir), \
             patch("lucos_photos_common.jobs.DERIVATIVES_DIR", derivatives_dir), \
             patch("lucos_photos_common.jobs.subprocess.run", side_effect=mock_run):
            process_video(str(pending_video.id))

        assert len(ffmpeg_calls) == 1
        # SAMPLE_FFPROBE_OUTPUT duration=10.0, so 10% = 1.0 second
        assert "-ss" in ffmpeg_calls[0]
        seek_index = ffmpeg_calls[0].index("-ss")
        assert float(ffmpeg_calls[0][seek_index + 1]) == pytest.approx(1.0)


class TestClusterFaces:
    """Tests for the cluster_faces() job that groups unassigned faces into Person records."""

    def _make_photo(self, db_session, sha256_hash):
        photo = Photo(sha256_hash=sha256_hash, file_extension="jpg", width=100, height=100)
        db_session.add(photo)
        db_session.flush()
        return photo

    def _make_face(self, db_session, photo_id, embedding, *, person_id=None, person_confirmed=False):
        face = Face(
            photo_id=photo_id,
            person_id=person_id,
            person_confirmed=person_confirmed,
            bbox_x=0.1, bbox_y=0.1, bbox_width=0.2, bbox_height=0.2,
            embedding=embedding,
        )
        db_session.add(face)
        db_session.flush()
        return face

    def test_no_faces_is_a_noop(self, db_session):
        """cluster_faces should return cleanly when there are no unassigned faces."""
        cluster_faces()
        # No exception — that's the assertion

    def test_two_similar_faces_get_same_person(self, db_session):
        """Two faces with nearly identical embeddings should be clustered into one Person."""
        photo_a = self._make_photo(db_session, "a" * 64)
        photo_b = self._make_photo(db_session, "b" * 64)

        # Nearly identical embeddings — should cluster together
        emb = [1.0] + [0.0] * 511
        self._make_face(db_session, photo_a.id, embedding=emb)
        self._make_face(db_session, photo_b.id, embedding=emb)
        db_session.commit()

        cluster_faces()

        faces = db_session.query(Face).all()
        assert all(f.person_id is not None for f in faces)
        # Both faces should share the same Person
        person_ids = {f.person_id for f in faces}
        assert len(person_ids) == 1

        person_count = db_session.query(Person).count()
        assert person_count == 1

    def test_two_dissimilar_faces_get_different_persons(self, db_session):
        """Two faces with very different embeddings should each form their own Person cluster."""
        photo_a = self._make_photo(db_session, "a" * 64)
        photo_b = self._make_photo(db_session, "b" * 64)

        # Orthogonal embeddings — maximum cosine distance
        emb_a = [1.0] + [0.0] * 511
        emb_b = [0.0, 1.0] + [0.0] * 510
        self._make_face(db_session, photo_a.id, embedding=emb_a)
        self._make_face(db_session, photo_b.id, embedding=emb_b)
        db_session.commit()

        cluster_faces()

        faces = db_session.query(Face).all()
        assert all(f.person_id is not None for f in faces)
        person_ids = {f.person_id for f in faces}
        assert len(person_ids) == 2

        person_count = db_session.query(Person).count()
        assert person_count == 2

    def test_face_without_embedding_is_skipped(self, db_session):
        """Faces with no embedding should not be clustered."""
        photo = self._make_photo(db_session, "a" * 64)
        face = self._make_face(db_session, photo.id, embedding=None)
        db_session.commit()

        cluster_faces()

        db_session.refresh(face)
        assert face.person_id is None
        assert db_session.query(Person).count() == 0

    def test_confirmed_face_is_not_modified(self, db_session):
        """A face with person_confirmed=True should never be reassigned by clustering."""
        photo = self._make_photo(db_session, "a" * 64)
        existing_person = Person(display_name="Known Person")
        db_session.add(existing_person)
        db_session.flush()

        face = self._make_face(
            db_session, photo.id,
            embedding=[1.0] + [0.0] * 511,
            person_id=existing_person.id,
            person_confirmed=True,
        )
        db_session.commit()

        cluster_faces()

        db_session.refresh(face)
        assert face.person_id == existing_person.id
        assert face.person_confirmed is True

    def test_photo_person_join_table_is_populated(self, db_session):
        """After clustering, photo_person rows should be created for each assigned face."""
        photo = self._make_photo(db_session, "a" * 64)
        emb = [1.0] + [0.0] * 511
        self._make_face(db_session, photo.id, embedding=emb)
        db_session.commit()

        cluster_faces()

        rows = db_session.query(PhotoPerson).filter(PhotoPerson.photo_id == photo.id).all()
        assert len(rows) == 1

    def test_two_faces_same_photo_same_person_one_join_row(self, db_session):
        """Two similar faces on the same photo should yield one photo_person row, not two."""
        photo = self._make_photo(db_session, "a" * 64)
        emb = [1.0] + [0.0] * 511
        self._make_face(db_session, photo.id, embedding=emb)
        self._make_face(db_session, photo.id, embedding=emb)
        db_session.commit()

        cluster_faces()

        rows = db_session.query(PhotoPerson).filter(PhotoPerson.photo_id == photo.id).all()
        assert len(rows) == 1

    def test_idempotent_when_called_twice(self, db_session):
        """cluster_faces should not create duplicate Person records on repeated calls."""
        photo = self._make_photo(db_session, "a" * 64)
        emb = [1.0] + [0.0] * 511
        self._make_face(db_session, photo.id, embedding=emb)
        db_session.commit()

        cluster_faces()
        # On second call, all faces are already assigned (person_id is set),
        # so they are excluded from clustering — nothing new should be created.
        cluster_faces()

        assert db_session.query(Person).count() == 1
        assert db_session.query(PhotoPerson).count() == 1


class TestFrontalityScore:
    """Tests for _frontality_score()."""

    def test_perfectly_centred(self):
        # Nose at midpoint of eyes → score 1.0
        kps = [[100, 200], [200, 200], [150, 250], [110, 280], [190, 280]]
        assert _frontality_score(kps) == pytest.approx(1.0)

    def test_nose_at_left_eye(self):
        # Nose coincides with left eye → maximum offset → score 0.0
        kps = [[100, 200], [200, 200], [100, 250], [110, 280], [190, 280]]
        assert _frontality_score(kps) == pytest.approx(0.0)

    def test_slight_offset(self):
        # Nose 25px right of centre in a 100px span → mid_offset=0.25 → score=0.5
        kps = [[100, 200], [200, 200], [175, 250], [110, 280], [190, 280]]
        score = _frontality_score(kps)
        assert score == pytest.approx(0.5)

    def test_none_returns_zero(self):
        assert _frontality_score(None) == 0.0

    def test_malformed_returns_zero(self):
        assert _frontality_score([]) == 0.0


class TestScoreFace:
    """Tests for _score_face()."""

    def _make_face(self, det_score=0.9, kps=None, bbox_width=0.4, bbox_height=0.4):
        face = MagicMock(spec=Face)
        face.det_score = det_score
        face.kps = kps
        face.bbox_width = bbox_width
        face.bbox_height = bbox_height
        return face

    def _make_photo(self, width=1000, height=800):
        photo = MagicMock(spec=MediaItem)
        photo.width = width
        photo.height = height
        return photo

    def test_all_criteria_met(self):
        # Perfectly frontal face, high det_score, large bbox
        kps = [[100, 200], [200, 200], [150, 250], [110, 280], [190, 280]]
        face = self._make_face(det_score=0.95, kps=kps, bbox_width=0.4, bbox_height=0.45)
        photo = self._make_photo(width=1000, height=800)
        assert _score_face(face, photo) == 4

    def test_no_criteria_met(self):
        # Low det_score, profile face (nose at edge), small bbox
        kps = [[100, 200], [200, 200], [100, 250], [110, 280], [190, 280]]  # nose at left eye
        face = self._make_face(det_score=0.5, kps=kps, bbox_width=0.1, bbox_height=0.1)
        photo = self._make_photo(width=1000, height=800)
        assert _score_face(face, photo) == 0

    def test_face_size_threshold(self):
        # Face exactly 300px wide: 300/1000 = 0.3 — NOT > 300px so criterion fails
        kps = [[100, 200], [200, 200], [100, 250], [110, 280], [190, 280]]
        face = self._make_face(det_score=0.5, kps=kps, bbox_width=0.3, bbox_height=0.3)
        photo = self._make_photo(width=1000, height=1000)
        assert _score_face(face, photo) == 0

    def test_none_det_score(self):
        face = self._make_face(det_score=None, kps=None, bbox_width=0.1, bbox_height=0.1)
        photo = self._make_photo()
        assert _score_face(face, photo) == 0


class TestGenerateProfilePicture:
    """Tests for generate_profile_picture()."""

    def _make_person(self, db_session):
        person = Person()
        db_session.add(person)
        db_session.flush()
        return person

    def _make_photo(self, db_session, sha256_hash, width=1000, height=800, taken_at=None):
        photo = MediaItem(sha256_hash=sha256_hash, file_extension="jpg", width=width, height=height, taken_at=taken_at)
        db_session.add(photo)
        db_session.flush()
        return photo

    def _make_face(self, db_session, photo, person, bbox_x=0.1, bbox_y=0.1, bbox_width=0.4, bbox_height=0.4, det_score=0.9, kps=None):
        face = Face(
            photo_id=photo.id,
            person_id=person.id,
            person_confirmed=False,
            bbox_x=bbox_x,
            bbox_y=bbox_y,
            bbox_width=bbox_width,
            bbox_height=bbox_height,
            det_score=det_score,
            kps=kps,
        )
        db_session.add(face)
        db_session.flush()
        return face

    def test_generates_profile_picture(self, db_session, tmp_path):
        """Should crop and save a profile picture for a person with a qualifying face."""
        person = self._make_person(db_session)
        photo = self._make_photo(db_session, "a" * 64, width=200, height=200)
        self._make_face(db_session, photo, person, bbox_x=0.2, bbox_y=0.2, bbox_width=0.6, bbox_height=0.6)
        db_session.commit()

        # Write a real JPEG to the originals dir
        originals_dir = tmp_path / "originals"
        originals_dir.mkdir()
        derivatives_dir = tmp_path / "derivatives"
        derivatives_dir.mkdir()
        img_path = originals_dir / f"{'a' * 64}.jpg"
        img = Image.new("RGB", (200, 200), color=(128, 64, 32))
        img.save(img_path, format="JPEG")

        person_id = person.id
        photo_id = photo.id

        with patch("lucos_photos_common.jobs.ORIGINALS_DIR", originals_dir), \
             patch("lucos_photos_common.jobs.DERIVATIVES_DIR", derivatives_dir), \
             patch("lucos_photos_common.jobs.SessionLocal") as mock_session_local, \
             patch("lucos_photos_common.jobs.updateLoganne") as mock_update:
            mock_session_local.return_value = db_session

            generate_profile_picture(str(person_id))

        profile_path = derivatives_dir / f"{person_id}_profile.jpg"
        assert profile_path.exists(), "Profile picture file should be created"

        # DB should be updated — re-query since job closes its own session
        updated = db_session.query(Person).filter(Person.id == person_id).first()
        assert updated.profile_photo_id == photo_id
        assert updated.profile_auto_generated is True

        mock_update.assert_called_once_with(
            "profilePhotoUpdated",
            f"Profile photo updated for person {person_id} in lucos_photos",
            url=mock_update.call_args[1]["url"],
        )
        assert f"/people/{person_id}" in mock_update.call_args[1]["url"]

    def test_emits_loganne_event_on_success(self, db_session, tmp_path):
        """Should emit profilePhotoUpdated to Loganne after a successful profile picture write."""
        person = self._make_person(db_session)
        photo = self._make_photo(db_session, "f" * 64, width=200, height=200)
        self._make_face(db_session, photo, person, bbox_x=0.2, bbox_y=0.2, bbox_width=0.6, bbox_height=0.6)
        db_session.commit()

        originals_dir = tmp_path / "originals"
        originals_dir.mkdir()
        derivatives_dir = tmp_path / "derivatives"
        derivatives_dir.mkdir()
        img = Image.new("RGB", (200, 200), color=(50, 100, 150))
        img.save(originals_dir / f"{'f' * 64}.jpg", format="JPEG")

        person_id = person.id

        with patch("lucos_photos_common.jobs.ORIGINALS_DIR", originals_dir), \
             patch("lucos_photos_common.jobs.DERIVATIVES_DIR", derivatives_dir), \
             patch("lucos_photos_common.jobs.SessionLocal") as mock_session_local, \
             patch("lucos_photos_common.jobs.updateLoganne") as mock_update, \
             patch.dict("os.environ", {"APP_ORIGIN": "https://photos.example.com"}):
            mock_session_local.return_value = db_session

            generate_profile_picture(str(person_id))

        mock_update.assert_called_once_with(
            "profilePhotoUpdated",
            f"Profile photo updated for person {person_id} in lucos_photos",
            url=f"https://photos.example.com/people/{person_id}",
        )

    def test_does_not_emit_loganne_when_no_suitable_face(self, db_session, tmp_path):
        """Should not emit profilePhotoUpdated when no suitable face is found."""
        person = self._make_person(db_session)
        person_id = person.id
        db_session.commit()

        with patch("lucos_photos_common.jobs.ORIGINALS_DIR", tmp_path / "originals"), \
             patch("lucos_photos_common.jobs.DERIVATIVES_DIR", tmp_path / "derivatives"), \
             patch("lucos_photos_common.jobs.SessionLocal") as mock_session_local, \
             patch("lucos_photos_common.jobs.updateLoganne") as mock_update:
            mock_session_local.return_value = db_session

            generate_profile_picture(str(person_id))

        mock_update.assert_not_called()

    def test_skips_if_no_faces(self, db_session, tmp_path):
        """Should do nothing if the person has no faces."""
        person = self._make_person(db_session)
        person_id = person.id
        db_session.commit()

        derivatives_dir = tmp_path / "derivatives"
        derivatives_dir.mkdir()

        with patch("lucos_photos_common.jobs.ORIGINALS_DIR", tmp_path / "originals"), \
             patch("lucos_photos_common.jobs.DERIVATIVES_DIR", derivatives_dir), \
             patch("lucos_photos_common.jobs.SessionLocal") as mock_session_local:
            mock_session_local.return_value = db_session

            generate_profile_picture(str(person_id))

        assert not any(derivatives_dir.iterdir())
        updated = db_session.query(Person).filter(Person.id == person_id).first()
        assert updated.profile_photo_id is None

    def test_skips_if_manually_set(self, db_session, tmp_path):
        """Should skip if profile_auto_generated is False (manual override)."""
        person = self._make_person(db_session)
        photo = self._make_photo(db_session, "b" * 64)
        person.profile_photo_id = photo.id
        person.profile_auto_generated = False
        person_id = person.id
        photo_id = photo.id
        db_session.commit()

        derivatives_dir = tmp_path / "derivatives"
        derivatives_dir.mkdir()

        with patch("lucos_photos_common.jobs.ORIGINALS_DIR", tmp_path / "originals"), \
             patch("lucos_photos_common.jobs.DERIVATIVES_DIR", derivatives_dir), \
             patch("lucos_photos_common.jobs.SessionLocal") as mock_session_local:
            mock_session_local.return_value = db_session

            generate_profile_picture(str(person_id))

        # profile_photo_id should be unchanged
        updated = db_session.query(Person).filter(Person.id == person_id).first()
        assert updated.profile_photo_id == photo_id
        assert updated.profile_auto_generated is False

    def test_picks_best_scored_face(self, db_session, tmp_path):
        """Should pick the face with the highest score, not just the first."""
        person = self._make_person(db_session)
        person_id = person.id
        # photo1: small face, low score
        photo1 = self._make_photo(db_session, "c" * 64, width=1000, height=1000)
        self._make_face(db_session, photo1, person, bbox_width=0.1, bbox_height=0.1, det_score=0.5)
        # photo2: large face, high score
        photo2 = self._make_photo(db_session, "d" * 64, width=1000, height=1000)
        photo2_id = photo2.id
        self._make_face(db_session, photo2, person, bbox_width=0.5, bbox_height=0.5, det_score=0.95)
        db_session.commit()

        originals_dir = tmp_path / "originals"
        originals_dir.mkdir()
        derivatives_dir = tmp_path / "derivatives"
        derivatives_dir.mkdir()
        for sha, ext in [("c" * 64, "jpg"), ("d" * 64, "jpg")]:
            img = Image.new("RGB", (1000, 1000), color=(100, 100, 100))
            img.save(originals_dir / f"{sha}.{ext}", format="JPEG")

        with patch("lucos_photos_common.jobs.ORIGINALS_DIR", originals_dir), \
             patch("lucos_photos_common.jobs.DERIVATIVES_DIR", derivatives_dir), \
             patch("lucos_photos_common.jobs.SessionLocal") as mock_session_local:
            mock_session_local.return_value = db_session

            generate_profile_picture(str(person_id))

        updated = db_session.query(Person).filter(Person.id == person_id).first()
        assert updated.profile_photo_id == photo2_id

    def test_crop_is_square_when_face_near_edge(self, db_session, tmp_path):
        """Profile picture must be square even when the face is near an image edge."""
        person = self._make_person(db_session)
        person_id = person.id
        # 1000x800 image, face centred at x=50 (very close to left edge)
        # bbox_x=0.005, bbox_width=0.09 → face centre x=50px, face width=90px
        photo = self._make_photo(db_session, "e" * 64, width=1000, height=800)
        self._make_face(db_session, photo, person,
                        bbox_x=0.005, bbox_y=0.4, bbox_width=0.09, bbox_height=0.09,
                        det_score=0.9)
        db_session.commit()

        originals_dir = tmp_path / "originals"
        originals_dir.mkdir()
        derivatives_dir = tmp_path / "derivatives"
        derivatives_dir.mkdir()
        img = Image.new("RGB", (1000, 800), color=(100, 100, 100))
        img.save(originals_dir / f"{'e' * 64}.jpg", format="JPEG")

        with patch("lucos_photos_common.jobs.ORIGINALS_DIR", originals_dir), \
             patch("lucos_photos_common.jobs.DERIVATIVES_DIR", derivatives_dir), \
             patch("lucos_photos_common.jobs.SessionLocal") as mock_session_local:
            mock_session_local.return_value = db_session

            generate_profile_picture(str(person_id))

        profile_path = derivatives_dir / f"{person_id}_profile.jpg"
        assert profile_path.exists()
        with Image.open(profile_path) as result:
            assert result.width == result.height, "Profile picture must be square"

    def test_profile_picture_capped_at_600px(self, db_session, tmp_path):
        """Profile picture should be scaled down to 600x600 when the crop is larger."""
        person = self._make_person(db_session)
        person_id = person.id
        # Large image with a big face — crop will exceed 600px
        photo = self._make_photo(db_session, "g" * 64, width=2000, height=2000)
        # face at 50% of 2000px = 1000px; crop = 1000 / sqrt(0.6) ≈ 1291px → must be scaled down
        self._make_face(db_session, photo, person,
                        bbox_x=0.25, bbox_y=0.25, bbox_width=0.5, bbox_height=0.5,
                        det_score=0.9)
        db_session.commit()

        originals_dir = tmp_path / "originals"
        originals_dir.mkdir()
        derivatives_dir = tmp_path / "derivatives"
        derivatives_dir.mkdir()
        img = Image.new("RGB", (2000, 2000), color=(200, 100, 50))
        img.save(originals_dir / f"{'g' * 64}.jpg", format="JPEG")

        with patch("lucos_photos_common.jobs.ORIGINALS_DIR", originals_dir), \
             patch("lucos_photos_common.jobs.DERIVATIVES_DIR", derivatives_dir), \
             patch("lucos_photos_common.jobs.SessionLocal") as mock_session_local, \
             patch("lucos_photos_common.jobs.updateLoganne"):
            mock_session_local.return_value = db_session

            generate_profile_picture(str(person_id))

        profile_path = derivatives_dir / f"{person_id}_profile.jpg"
        assert profile_path.exists()
        with Image.open(profile_path) as result:
            assert result.width == 600, f"Expected width 600, got {result.width}"
            assert result.height == 600, f"Expected height 600, got {result.height}"

    def test_profile_picture_not_scaled_up_if_small(self, db_session, tmp_path):
        """Profile picture should not be scaled up when the crop is smaller than 600px."""
        person = self._make_person(db_session)
        person_id = person.id
        # Small image with a small face — crop will be well under 600px
        photo = self._make_photo(db_session, "h" * 64, width=200, height=200)
        # face at 20% of 200px = 40px; crop = 40 / sqrt(0.6) ≈ 52px — well under 600
        self._make_face(db_session, photo, person,
                        bbox_x=0.2, bbox_y=0.2, bbox_width=0.2, bbox_height=0.2,
                        det_score=0.9)
        db_session.commit()

        originals_dir = tmp_path / "originals"
        originals_dir.mkdir()
        derivatives_dir = tmp_path / "derivatives"
        derivatives_dir.mkdir()
        img = Image.new("RGB", (200, 200), color=(50, 100, 150))
        img.save(originals_dir / f"{'h' * 64}.jpg", format="JPEG")

        with patch("lucos_photos_common.jobs.ORIGINALS_DIR", originals_dir), \
             patch("lucos_photos_common.jobs.DERIVATIVES_DIR", derivatives_dir), \
             patch("lucos_photos_common.jobs.SessionLocal") as mock_session_local, \
             patch("lucos_photos_common.jobs.updateLoganne"):
            mock_session_local.return_value = db_session

            generate_profile_picture(str(person_id))

        profile_path = derivatives_dir / f"{person_id}_profile.jpg"
        assert profile_path.exists()
        with Image.open(profile_path) as result:
            assert result.width < 600, f"Expected width under 600, got {result.width}"
            assert result.width == result.height, "Must still be square"
