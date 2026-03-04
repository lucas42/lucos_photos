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

from lucos_photos_common.jobs import detect_and_save_faces, process_photo, reprocess_photo
from lucos_photos_common.models import Face, Photo, ProcessingState, ProcessingStatus

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

    def _make_mock_face(self, bbox, embedding=None):
        """Build a mock InsightFace face object with the given bbox and optional embedding."""
        face = MagicMock()
        face.bbox = bbox
        face.embedding = np.array(embedding) if embedding is not None else None
        return face

    def _patch_insightface(self, detected_faces):
        """Return a context manager that patches cv2 and insightface to return detected_faces."""
        mock_app = MagicMock()
        mock_app.get.return_value = detected_faces

        mock_face_analysis_cls = MagicMock(return_value=mock_app)

        cv2_mock = MagicMock()
        # cv2.imread returns a non-None value to simulate a valid image
        cv2_mock.imread.return_value = MagicMock()

        import sys
        import types

        insightface_mock = types.ModuleType("insightface")
        insightface_app_mock = types.ModuleType("insightface.app")
        insightface_app_mock.FaceAnalysis = mock_face_analysis_cls
        insightface_mock.app = insightface_app_mock

        return patch.multiple(
            "sys.modules",
            cv2=cv2_mock,
            insightface=insightface_mock,
            **{"insightface.app": insightface_app_mock},
        ), mock_app, cv2_mock

    def test_saves_face_record_for_each_detected_face(self, db_session, photo_with_dimensions, tmp_path):
        """One Face row should be inserted per detected face."""
        photo = photo_with_dimensions
        img_path = tmp_path / "test.jpg"
        img_path.write_bytes(b"fake")

        face1 = self._make_mock_face([100, 200, 300, 400], embedding=[0.1] * 512)
        face2 = self._make_mock_face([500, 100, 700, 350], embedding=[0.2] * 512)

        mock_cv2 = MagicMock()
        mock_cv2.imread.return_value = MagicMock()
        mock_app_instance = MagicMock()
        mock_app_instance.get.return_value = [face1, face2]
        mock_face_analysis = MagicMock(return_value=mock_app_instance)

        with patch.dict("sys.modules", {"cv2": mock_cv2, "insightface": MagicMock(), "insightface.app": MagicMock(FaceAnalysis=mock_face_analysis)}):
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

        mock_cv2 = MagicMock()
        mock_cv2.imread.return_value = MagicMock()
        mock_app_instance = MagicMock()
        mock_app_instance.get.return_value = [face]
        mock_face_analysis = MagicMock(return_value=mock_app_instance)

        with patch.dict("sys.modules", {"cv2": mock_cv2, "insightface": MagicMock(), "insightface.app": MagicMock(FaceAnalysis=mock_face_analysis)}):
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

        mock_cv2 = MagicMock()
        mock_cv2.imread.return_value = MagicMock()
        mock_app_instance = MagicMock()
        mock_app_instance.get.return_value = [face]
        mock_face_analysis = MagicMock(return_value=mock_app_instance)

        with patch.dict("sys.modules", {"cv2": mock_cv2, "insightface": MagicMock(), "insightface.app": MagicMock(FaceAnalysis=mock_face_analysis)}):
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

        mock_cv2 = MagicMock()
        mock_cv2.imread.return_value = MagicMock()
        mock_app_instance = MagicMock()
        mock_app_instance.get.return_value = [face]
        mock_face_analysis = MagicMock(return_value=mock_app_instance)

        with patch.dict("sys.modules", {"cv2": mock_cv2, "insightface": MagicMock(), "insightface.app": MagicMock(FaceAnalysis=mock_face_analysis)}):
            detect_and_save_faces(db_session, photo, img_path)

        saved = db_session.query(Face).filter(Face.photo_id == photo.id).first()
        assert saved.person_confirmed is False

    def test_no_faces_creates_no_records(self, db_session, photo_with_dimensions, tmp_path):
        """If InsightFace detects no faces, no Face rows should be inserted."""
        photo = photo_with_dimensions
        img_path = tmp_path / "test.jpg"
        img_path.write_bytes(b"fake")

        mock_cv2 = MagicMock()
        mock_cv2.imread.return_value = MagicMock()
        mock_app_instance = MagicMock()
        mock_app_instance.get.return_value = []
        mock_face_analysis = MagicMock(return_value=mock_app_instance)

        with patch.dict("sys.modules", {"cv2": mock_cv2, "insightface": MagicMock(), "insightface.app": MagicMock(FaceAnalysis=mock_face_analysis)}):
            detect_and_save_faces(db_session, photo, img_path)

        count = db_session.query(Face).filter(Face.photo_id == photo.id).count()
        assert count == 0

    def test_face_without_embedding_saved_with_null_embedding(self, db_session, photo_with_dimensions, tmp_path):
        """A face with no embedding (e.g. detection-only model) should be saved with embedding=None."""
        photo = photo_with_dimensions
        img_path = tmp_path / "test.jpg"
        img_path.write_bytes(b"fake")

        face = self._make_mock_face([0.0, 0.0, 100.0, 100.0], embedding=None)

        mock_cv2 = MagicMock()
        mock_cv2.imread.return_value = MagicMock()
        mock_app_instance = MagicMock()
        mock_app_instance.get.return_value = [face]
        mock_face_analysis = MagicMock(return_value=mock_app_instance)

        with patch.dict("sys.modules", {"cv2": mock_cv2, "insightface": MagicMock(), "insightface.app": MagicMock(FaceAnalysis=mock_face_analysis)}):
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

        mock_cv2 = MagicMock()
        mock_cv2.imread.return_value = MagicMock()
        mock_app_instance = MagicMock()
        mock_app_instance.get.return_value = [face]
        mock_face_analysis = MagicMock(return_value=mock_app_instance)

        with patch.dict("sys.modules", {"cv2": mock_cv2, "insightface": MagicMock(), "insightface.app": MagicMock(FaceAnalysis=mock_face_analysis)}):
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
        mock_face_analysis = MagicMock(return_value=mock_app_instance)

        with patch.dict("sys.modules", {"cv2": mock_cv2, "insightface": MagicMock(), "insightface.app": MagicMock(FaceAnalysis=mock_face_analysis)}):
            with pytest.raises(ValueError, match="cv2.imread returned None"):
                detect_and_save_faces(db_session, photo, img_path)

    def test_skips_detection_when_photo_has_no_dimensions(self, db_session, tmp_path):
        """If width/height are None (metadata not yet extracted), detection should be skipped."""
        photo = Photo(sha256_hash="c" * 64, file_extension="jpg", width=None, height=None)
        db_session.add(photo)
        db_session.commit()

        img_path = tmp_path / "test.jpg"
        img_path.write_bytes(b"fake")

        mock_cv2 = MagicMock()
        mock_app_instance = MagicMock()
        mock_face_analysis = MagicMock(return_value=mock_app_instance)

        with patch.dict("sys.modules", {"cv2": mock_cv2, "insightface": MagicMock(), "insightface.app": MagicMock(FaceAnalysis=mock_face_analysis)}):
            detect_and_save_faces(db_session, photo, img_path)

        # cv2.imread should never have been called
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
