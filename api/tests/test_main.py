import asyncio
import hashlib
from unittest.mock import AsyncMock, MagicMock, patch

AUTH_HEADER = {"Authorization": "Bearer validkey"}
VALID_IMAGE_CONTENT = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300080606070605080707070909080a0c140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20242e2720222c231c1c2837292c30313434341f27393d38323c2e333432ffdb0043010909090c0b0c180d0d1832211c213232323232323232323232323232323232323232323232323232323232323232323232323232323232323232323232323232ffc00011080001000103012200021101031101ffc4001f0000010501010101010100000000000000000102030405060708090a0bffc400b5100002010303020403050504040000017d01020300041105122131410613516107227114328191a1082342b1c11552d1f02433627282090a161718191a25262728292a3435363738393a434445464748494a535455565758595a636465666768696a737475767778797a838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9faffc4001f0100030101010101010101010000000000000102030405060708090a0bffc400b51100020102040403040705040400010277000102031104052131061241510761711322328108144291a1b1c109233352f0156272d10a162434e125f11718191a262728292a35363738393a434445464748494a535455565758595a636465666768696a737475767778797a82838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae2e3e4e5e6e7e8e9eaf2f3f4f5f6f7f8f9faffda000c03010002110311003f00e2e8a28af993f713ffd9"
)


class TestHealthcheck:
    def test_returns_ok(self, client):
        response = client.get("/healthcheck")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


def _make_mock_redis(ping_raises=None):
    mock_redis = MagicMock()
    if ping_raises:
        mock_redis.ping.side_effect = ping_raises
    else:
        mock_redis.ping.return_value = True
    return mock_redis


class TestInfo:
    def test_returns_200(self, client):
        assert client.get("/_info").status_code == 200

    def test_required_fields_present(self, client):
        data = client.get("/_info").json()
        assert data["system"] == "lucos_photos"
        assert data["title"] == "Photos"
        assert "checks" in data
        assert "metrics" in data
        assert "ci" in data
        assert data["icon"] == "/icon"
        assert data["show_on_homepage"] is True

    def test_checks_structure(self, client):
        data = client.get("/_info").json()
        checks = data["checks"]
        assert set(checks.keys()) == {"db-reachable", "redis-reachable"}
        for name, check in checks.items():
            assert "ok" in check, f"check '{name}' missing 'ok' field"
            assert "techDetail" in check, f"check '{name}' missing 'techDetail' field"
            assert isinstance(check["ok"], bool), f"check '{name}' 'ok' is not a bool"
            assert isinstance(check["techDetail"], str), f"check '{name}' 'techDetail' is not a str"


class TestMetrics:
    """Tests for the metrics section of /_info."""

    def test_metrics_keys_present(self, client):
        data = client.get("/_info").json()
        metrics = data["metrics"]
        assert "photo-count" in metrics
        assert "processing-pending-count" in metrics

    def test_metrics_structure(self, client):
        data = client.get("/_info").json()
        for name, metric in data["metrics"].items():
            assert "value" in metric, f"metric '{name}' missing 'value' field"
            assert "techDetail" in metric, f"metric '{name}' missing 'techDetail' field"
            assert isinstance(metric["value"], int), f"metric '{name}' 'value' is not an int"
            assert isinstance(metric["techDetail"], str), f"metric '{name}' 'techDetail' is not a str"

    def test_photo_count_reflects_db(self, client, db_session):
        from lucos_photos_common.models import Photo, ProcessingState, ProcessingStatus

        # Initially zero photos
        data = client.get("/_info").json()
        assert data["metrics"]["photo-count"]["value"] == 0

        # Add two photos
        import uuid, hashlib
        for i in range(2):
            p = Photo(sha256_hash=hashlib.sha256(f"photo{i}".encode()).hexdigest(), file_extension="jpg")
            db_session.add(p)
        db_session.commit()

        data = client.get("/_info").json()
        assert data["metrics"]["photo-count"]["value"] == 2

    def test_processing_pending_count_reflects_db(self, client, db_session):
        from lucos_photos_common.models import Photo, ProcessingState, ProcessingStatus
        import hashlib

        # Initially zero pending
        data = client.get("/_info").json()
        assert data["metrics"]["processing-pending-count"]["value"] == 0

        # Add a photo with pending status
        p = Photo(sha256_hash=hashlib.sha256(b"pendingphoto").hexdigest(), file_extension="jpg")
        db_session.add(p)
        db_session.flush()
        db_session.add(ProcessingStatus(photo_id=p.id, state=ProcessingState.pending))
        db_session.commit()

        data = client.get("/_info").json()
        assert data["metrics"]["processing-pending-count"]["value"] == 1

    def test_processing_pending_count_excludes_non_pending(self, client, db_session):
        from lucos_photos_common.models import Photo, ProcessingState, ProcessingStatus
        import hashlib

        # Add a photo with complete status — should not count as pending
        p = Photo(sha256_hash=hashlib.sha256(b"completephoto").hexdigest(), file_extension="jpg")
        db_session.add(p)
        db_session.flush()
        db_session.add(ProcessingStatus(photo_id=p.id, state=ProcessingState.complete))
        db_session.commit()

        data = client.get("/_info").json()
        assert data["metrics"]["processing-pending-count"]["value"] == 0


class TestHealthChecks:
    """Tests for individual health check behaviour — happy paths and failure paths."""

    def test_db_check_ok_when_db_reachable(self, client):
        # The test client uses a real SQLite in-memory DB, so the check should pass.
        # We patch SessionLocal to return a mock that executes cleanly.
        mock_session = MagicMock()
        mock_session.execute.return_value = None
        with patch("app.main.SessionLocal", return_value=mock_session):
            data = client.get("/_info").json()
        assert data["checks"]["db-reachable"]["ok"] is True

    def test_db_check_fails_when_db_unreachable(self, client):
        mock_session = MagicMock()
        mock_session.execute.side_effect = Exception("Connection refused")
        with patch("app.main.SessionLocal", return_value=mock_session):
            data = client.get("/_info").json()
        check = data["checks"]["db-reachable"]
        assert check["ok"] is False
        assert "techDetail" in check

    def test_db_check_fails_on_timeout(self, client):
        mock_session = MagicMock()
        mock_session.execute.side_effect = asyncio.TimeoutError()
        with patch("app.main.SessionLocal", return_value=mock_session):
            data = client.get("/_info").json()
        check = data["checks"]["db-reachable"]
        assert check["ok"] is False
        assert "techDetail" in check

    def test_redis_check_ok_when_redis_reachable(self, client):
        mock_redis = _make_mock_redis()
        with patch("app.main.get_redis", return_value=mock_redis):
            data = client.get("/_info").json()
        assert data["checks"]["redis-reachable"]["ok"] is True

    def test_redis_check_fails_when_redis_unreachable(self, client):
        mock_redis = _make_mock_redis(ping_raises=Exception("Connection refused"))
        with patch("app.main.get_redis", return_value=mock_redis):
            data = client.get("/_info").json()
        check = data["checks"]["redis-reachable"]
        assert check["ok"] is False
        assert "techDetail" in check

    def test_one_check_failure_does_not_prevent_others(self, client):
        """A failure in one check must not propagate — all checks must always appear."""
        mock_session = MagicMock()
        mock_session.execute.side_effect = Exception("DB down")
        with patch("app.main.SessionLocal", return_value=mock_session):
            data = client.get("/_info").json()
        checks = data["checks"]
        assert "db-reachable" in checks
        assert "redis-reachable" in checks
        assert checks["db-reachable"]["ok"] is False


class TestIcon:
    def test_returns_200(self, client):
        response = client.get("/icon")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"


class TestUploadAuth:
    def test_no_auth_header_returns_401(self, client):
        response = client.post("/photos", files={"file": ("a.jpg", b"data", "image/jpeg")})
        assert response.status_code == 401

    def test_wrong_scheme_returns_401(self, client):
        response = client.post(
            "/photos",
            files={"file": ("a.jpg", b"data", "image/jpeg")},
            headers={"Authorization": "Basic sometoken"},
        )
        assert response.status_code == 401

    def test_invalid_key_returns_401(self, client):
        response = client.post(
            "/photos",
            files={"file": ("a.jpg", b"data", "image/jpeg")},
            headers={"Authorization": "Bearer wrongkey"},
        )
        assert response.status_code == 401

    def test_401_responses_include_www_authenticate(self, client):
        for headers in [
            {},
            {"Authorization": "Basic token"},
            {"Authorization": "Bearer wrongkey"},
        ]:
            response = client.post(
                "/photos",
                files={"file": ("a.jpg", b"data", "image/jpeg")},
                headers=headers,
            )
            assert "WWW-Authenticate" in response.headers, f"Missing WWW-Authenticate for headers={headers}"
            assert response.headers["WWW-Authenticate"].startswith("Bearer ")


class TestUpload:
    def test_new_photo_returns_201(self, client):
        response = client.post(
            "/photos",
            files={"file": ("photo.jpg", VALID_IMAGE_CONTENT, "image/jpeg")},
            headers=AUTH_HEADER,
        )
        assert response.status_code == 201

    def test_response_body_contains_expected_fields(self, client):
        content = VALID_IMAGE_CONTENT
        response = client.post(
            "/photos",
            files={"file": ("photo.jpg", content, "image/jpeg")},
            headers=AUTH_HEADER,
        )
        data = response.json()
        assert "id" in data
        assert data["fileExtension"] == "jpg"
        assert data["sha256Hash"] == hashlib.sha256(content).hexdigest()
        assert data["uploadedAt"] is not None
        assert data["takenAt"] is None
        assert data["width"] is None
        assert data["height"] is None

    def test_duplicate_upload_returns_200(self, client):
        content = VALID_IMAGE_CONTENT
        client.post("/photos", files={"file": ("photo.jpg", content, "image/jpeg")}, headers=AUTH_HEADER)
        response = client.post("/photos", files={"file": ("photo.jpg", content, "image/jpeg")}, headers=AUTH_HEADER)
        assert response.status_code == 200

    def test_duplicate_upload_returns_same_record(self, client):
        content = VALID_IMAGE_CONTENT
        first = client.post("/photos", files={"file": ("photo.jpg", content, "image/jpeg")}, headers=AUTH_HEADER)
        second = client.post("/photos", files={"file": ("photo.jpg", content, "image/jpeg")}, headers=AUTH_HEADER)
        assert first.json()["id"] == second.json()["id"]

    def test_extension_taken_from_filename(self, client):
        response = client.post(
            "/photos",
            files={"file": ("holiday.png", VALID_IMAGE_CONTENT, "image/jpeg")},
            headers=AUTH_HEADER,
        )
        assert response.json()["fileExtension"] == "png"

    def test_extension_falls_back_to_content_type(self, client):
        response = client.post(
            "/photos",
            files={"file": ("photo", VALID_IMAGE_CONTENT, "image/jpeg")},
            headers=AUTH_HEADER,
        )
        assert response.json()["fileExtension"] == "jpg"

    def test_file_written_to_staging_dir(self, client, tmp_path):
        content = VALID_IMAGE_CONTENT
        sha = hashlib.sha256(content).hexdigest()
        client.post(
            "/photos",
            files={"file": ("photo.jpg", content, "image/jpeg")},
            headers=AUTH_HEADER,
        )
        assert (tmp_path / f"{sha}.jpg").exists()

    def test_different_content_creates_different_records(self, client):
        first = client.post("/photos", files={"file": ("a.jpg", VALID_IMAGE_CONTENT, "image/jpeg")}, headers=AUTH_HEADER)
        # Use a slightly different image for the second upload to have a different hash
        second_content = VALID_IMAGE_CONTENT + b"\0"
        # Wait, if I add a null byte, it might still be a valid JPEG if it's after EOI, but Pillow might be picky.
        # Actually, let's just use two different valid images if needed, or just append something that doesn't break headers.
        # Most JPEGs ignore data after EOI (ffd9).
        second = client.post("/photos", files={"file": ("b.jpg", second_content, "image/jpeg")}, headers=AUTH_HEADER)
        assert first.status_code == 201
        assert second.status_code == 201
        assert first.json()["id"] != second.json()["id"]

    def test_invalid_image_returns_422(self, client):
        response = client.post(
            "/photos",
            files={"file": ("not_an_image.txt", b"this is not a valid image", "text/plain")},
            headers=AUTH_HEADER,
        )
        assert response.status_code == 422
        assert response.json() == {"detail": "Invalid image file"}

    def test_staged_file_cleaned_up_on_db_error(self, client, db_session, tmp_path):
        content = VALID_IMAGE_CONTENT
        sha = hashlib.sha256(content).hexdigest()
        with patch.object(db_session, "commit", side_effect=Exception("Database error")):
            try:
                client.post(
                    "/photos",
                    files={"file": ("photo.jpg", content, "image/jpeg")},
                    headers=AUTH_HEADER,
                )
            except Exception:
                pass
            assert not (tmp_path / f"{sha}.jpg").exists()


class TestUploadLimits:
    def test_file_too_large_returns_413(self, client):
        with patch("app.main.MAX_PHOTO_SIZE", 10):
            response = client.post(
                "/photos",
                files={"file": ("photo.jpg", b"too long for limit", "image/jpeg")},
                headers=AUTH_HEADER,
            )
            assert response.status_code == 413
            assert response.json() == {"detail": "File too large"}

    def test_file_too_large_enforced_during_streaming(self, client, tmp_path):
        """Size limit is enforced incrementally during the stream, not just on Content-Length."""
        # Patch MAX_PHOTO_SIZE to something smaller than the valid image
        with patch("app.main.MAX_PHOTO_SIZE", len(VALID_IMAGE_CONTENT) - 1):
            response = client.post(
                "/photos",
                # Omit file.size by not providing a named tuple — TestClient sends the bytes directly
                files={"file": ("photo.jpg", VALID_IMAGE_CONTENT, "image/jpeg")},
                headers=AUTH_HEADER,
            )
        assert response.status_code == 413
        assert response.json() == {"detail": "File too large"}

    def test_no_temp_file_left_on_size_exceeded(self, client, tmp_path):
        """If the size limit is hit during streaming, no temp files should remain in uploads dir."""
        with patch("app.main.MAX_PHOTO_SIZE", 10):
            client.post(
                "/photos",
                files={"file": ("photo.jpg", b"exceeds the limit by quite a lot", "image/jpeg")},
                headers=AUTH_HEADER,
            )
        # tmp_path is the UPLOADS_DIR; only expected file is the one for the valid upload, not a stray temp file
        remaining = list(tmp_path.iterdir())
        # There should be no files left (the upload was rejected)
        assert remaining == []

    def test_insufficient_storage_returns_507(self, client):
        with patch("shutil.disk_usage") as mock_usage:
            # Mock 100 bytes free space
            mock_usage.return_value = (1000, 900, 100)
            response = client.post(
                "/photos",
                files={"file": ("photo.jpg", VALID_IMAGE_CONTENT, "image/jpeg")},
                headers=AUTH_HEADER,
            )
            assert response.status_code == 507
            assert response.json() == {"detail": "Insufficient storage"}

    def test_max_video_size_constant_exists(self):
        """MAX_VIDEO_SIZE should be defined and larger than MAX_PHOTO_SIZE."""
        import app.main as main_module
        assert hasattr(main_module, "MAX_VIDEO_SIZE")
        assert main_module.MAX_VIDEO_SIZE > main_module.MAX_PHOTO_SIZE

    def test_sha256_hash_correct_for_streamed_upload(self, client):
        """SHA256 computed during streaming should match the actual file content."""
        content = VALID_IMAGE_CONTENT
        expected_hash = hashlib.sha256(content).hexdigest()
        response = client.post(
            "/photos",
            files={"file": ("photo.jpg", content, "image/jpeg")},
            headers=AUTH_HEADER,
        )
        assert response.status_code == 201
        assert response.json()["sha256Hash"] == expected_hash
