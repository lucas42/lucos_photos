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
        assert data["icon"] == "/icon.png"
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


class TestWorkerMemoryMetric:
    """Tests for the worker-memory-rss-bytes metric surfaced from the Redis heartbeat."""

    def _mock_redis_with_heartbeat(self, rss_bytes=52428800):
        import json
        mock_redis = MagicMock()
        mock_redis.get.return_value = json.dumps({
            "rss_bytes": rss_bytes,
            "pid": 42,
            "timestamp": "2026-04-23T12:00:00+00:00",
        }).encode()
        return mock_redis

    def test_worker_memory_metric_present_when_heartbeat_available(self, client):
        mock_redis = self._mock_redis_with_heartbeat(rss_bytes=52428800)
        with patch("app.main.get_redis", return_value=mock_redis):
            data = client.get("/_info").json()
        assert "worker-memory-rss-bytes" in data["metrics"]
        assert data["metrics"]["worker-memory-rss-bytes"]["value"] == 52428800

    def test_worker_memory_metric_absent_when_no_heartbeat(self, client):
        mock_redis = MagicMock()
        mock_redis.get.return_value = None
        with patch("app.main.get_redis", return_value=mock_redis):
            data = client.get("/_info").json()
        assert "worker-memory-rss-bytes" not in data["metrics"]

    def test_worker_memory_metric_absent_when_redis_fails(self, client):
        mock_redis = MagicMock()
        mock_redis.get.side_effect = Exception("Redis unreachable")
        with patch("app.main.get_redis", return_value=mock_redis):
            data = client.get("/_info").json()
        assert "worker-memory-rss-bytes" not in data["metrics"]

    def test_worker_memory_metric_has_correct_structure(self, client):
        mock_redis = self._mock_redis_with_heartbeat()
        with patch("app.main.get_redis", return_value=mock_redis):
            data = client.get("/_info").json()
        metric = data["metrics"]["worker-memory-rss-bytes"]
        assert "value" in metric
        assert "techDetail" in metric
        assert isinstance(metric["value"], int)


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
        assert "debug" in check
        assert "Connection refused" in check["debug"]

    def test_db_check_fails_on_timeout(self, client):
        mock_session = MagicMock()
        mock_session.execute.side_effect = asyncio.TimeoutError()
        with patch("app.main.SessionLocal", return_value=mock_session):
            data = client.get("/_info").json()
        check = data["checks"]["db-reachable"]
        assert check["ok"] is False
        assert "techDetail" in check
        assert "debug" in check
        assert "timeout" in check["debug"]

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
        assert "debug" in check
        assert "Connection refused" in check["debug"]

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

    def test_db_check_swallows_illegal_state_change_error_on_close(self, client):
        """IllegalStateChangeError on session.close() must not propagate — check result is unaffected."""
        from sqlalchemy.exc import IllegalStateChangeError
        mock_session = MagicMock()
        mock_session.execute.return_value = None
        mock_session.close.side_effect = IllegalStateChangeError(
            "Method 'close()' can't be called here; method '_connection_for_bind()' is already in progress"
        )
        with patch("app.main.SessionLocal", return_value=mock_session):
            data = client.get("/_info").json()
        assert data["checks"]["db-reachable"]["ok"] is True

    def test_metrics_swallows_illegal_state_change_error_on_close(self, client):
        """IllegalStateChangeError on session.close() in get_metrics must not propagate."""
        from sqlalchemy.exc import IllegalStateChangeError
        mock_session = MagicMock()
        mock_session.execute.return_value.fetchone.return_value = None
        mock_session.close.side_effect = IllegalStateChangeError(
            "Method 'close()' can't be called here; method '_connection_for_bind()' is already in progress"
        )
        with patch("app.main.SessionLocal", return_value=mock_session):
            data = client.get("/_info").json()
        assert "metrics" in data
        assert data["metrics"]["photo-count"]["value"] == 0


class TestIcon:
    def test_returns_200(self, client):
        response = client.get("/icon.png")
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

    def test_duplicate_upload_with_x_taken_at_updates_null_taken_at(self, client):
        """Re-uploading the same photo with X-Taken-At should backfill taken_at if it was null on the first upload."""
        content = VALID_IMAGE_CONTENT
        # First upload without X-Taken-At — taken_at is null
        first = client.post("/photos", files={"file": ("photo.jpg", content, "image/jpeg")}, headers=AUTH_HEADER)
        assert first.json()["takenAt"] is None
        # Second upload of same photo with X-Taken-At — taken_at should be updated
        second = client.post(
            "/photos",
            files={"file": ("photo.jpg", content, "image/jpeg")},
            headers={**AUTH_HEADER, "X-Taken-At": "1700000000000"},
        )
        assert second.status_code == 200
        assert second.json()["id"] == first.json()["id"]
        assert second.json()["takenAt"] is not None
        assert "2023-11-14" in second.json()["takenAt"]

    def test_duplicate_upload_does_not_overwrite_existing_taken_at(self, client):
        """Re-uploading a photo that already has taken_at set should not overwrite it."""
        content = VALID_IMAGE_CONTENT
        # First upload with X-Taken-At
        first = client.post(
            "/photos",
            files={"file": ("photo.jpg", content, "image/jpeg")},
            headers={**AUTH_HEADER, "X-Taken-At": "1700000000000"},
        )
        original_taken_at = first.json()["takenAt"]
        assert original_taken_at is not None
        # Second upload with a different X-Taken-At — should not change the existing value
        second = client.post(
            "/photos",
            files={"file": ("photo.jpg", content, "image/jpeg")},
            headers={**AUTH_HEADER, "X-Taken-At": "1600000000000"},
        )
        assert second.status_code == 200
        assert second.json()["takenAt"] == original_taken_at

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

    def test_x_taken_at_header_sets_taken_at(self, client):
        """X-Taken-At header (Unix milliseconds) should be stored as taken_at on the photo."""
        # 1700000000000 ms = 2023-11-14T22:13:20+00:00
        response = client.post(
            "/photos",
            files={"file": ("photo.jpg", VALID_IMAGE_CONTENT, "image/jpeg")},
            headers={**AUTH_HEADER, "X-Taken-At": "1700000000000"},
        )
        assert response.status_code == 201
        data = response.json()
        assert data["takenAt"] is not None
        assert "2023-11-14" in data["takenAt"]

    def test_x_taken_at_zero_is_ignored(self, client):
        """X-Taken-At of 0 should be treated as absent — takenAt must remain null."""
        response = client.post(
            "/photos",
            files={"file": ("photo2.jpg", VALID_IMAGE_CONTENT + b"\x00", "image/jpeg")},
            headers={**AUTH_HEADER, "X-Taken-At": "0"},
        )
        # takenAt should still be null because 0 is not a valid timestamp
        data = response.json()
        assert data["takenAt"] is None

    def test_malformed_x_taken_at_is_ignored(self, client):
        """A non-numeric X-Taken-At header should be silently ignored, not cause a 422."""
        response = client.post(
            "/photos",
            files={"file": ("photo3.jpg", VALID_IMAGE_CONTENT + b"\x01", "image/jpeg")},
            headers={**AUTH_HEADER, "X-Taken-At": "not-a-number"},
        )
        assert response.status_code == 201
        data = response.json()
        assert data["takenAt"] is None

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
        with patch("app.routers.photos.MAX_PHOTO_SIZE", 10):
            response = client.post(
                "/photos",
                files={"file": ("photo.jpg", b"too long for limit", "image/jpeg")},
                headers=AUTH_HEADER,
            )
            assert response.status_code == 413
            assert response.json() == {"detail": "File too large"}

    def test_file_too_large_enforced_during_streaming(self, client, tmp_path):
        """Size limit is enforced incrementally during the stream, not just on Content-Length.

        Patches UploadFile.__init__ to keep size=None throughout the request, bypassing the
        fast-path Content-Length check and forcing the streaming-path size enforcement to run.
        """
        from starlette.datastructures import UploadFile as StUploadFile
        _original_init = StUploadFile.__init__

        def _force_size_none(self, *args, **kwargs):
            _original_init(self, *args, **kwargs)
            self.size = None

        with patch("app.routers.photos.MAX_PHOTO_SIZE", len(VALID_IMAGE_CONTENT) - 1), \
             patch.object(StUploadFile, "__init__", _force_size_none):
            response = client.post(
                "/photos",
                files={"file": ("photo.jpg", VALID_IMAGE_CONTENT, "image/jpeg")},
                headers=AUTH_HEADER,
            )
        assert response.status_code == 413
        assert response.json() == {"detail": "File too large"}

    def test_no_temp_file_left_on_size_exceeded(self, client, tmp_path):
        """If the streaming path hits the size limit, no temp files should remain in uploads dir.

        Patches UploadFile.__init__ to keep size=None throughout the request, bypassing the
        fast-path Content-Length check so the streaming-path cleanup logic is actually exercised.
        Without this patch, file.size would be set by Starlette to the actual byte length, and the
        fast-path would reject the upload before any temp file is ever created — making the test
        pass trivially without testing the cleanup in the finally block.
        """
        from starlette.datastructures import UploadFile as StUploadFile
        _original_init = StUploadFile.__init__

        def _force_size_none(self, *args, **kwargs):
            _original_init(self, *args, **kwargs)
            self.size = None

        with patch("app.routers.photos.MAX_PHOTO_SIZE", 10), \
             patch.object(StUploadFile, "__init__", _force_size_none):
            client.post(
                "/photos",
                files={"file": ("photo.jpg", b"exceeds the limit by quite a lot", "image/jpeg")},
                headers=AUTH_HEADER,
            )
        remaining = list(tmp_path.iterdir())
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
        import app.routers.photos as photos_module
        assert hasattr(photos_module, "MAX_VIDEO_SIZE")
        assert photos_module.MAX_VIDEO_SIZE > photos_module.MAX_PHOTO_SIZE

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


# Minimal bytes to simulate a video file — PIL validation is skipped for videos,
# so any content works as long as it's non-empty and within the size limit.
VALID_VIDEO_CONTENT = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 100


class TestVideoUpload:
    """Tests for video upload acceptance and behaviour."""

    def test_mp4_upload_returns_201(self, client):
        response = client.post(
            "/photos",
            files={"file": ("video.mp4", VALID_VIDEO_CONTENT, "video/mp4")},
            headers=AUTH_HEADER,
        )
        assert response.status_code == 201

    def test_mov_upload_returns_201(self, client):
        response = client.post(
            "/photos",
            files={"file": ("video.mov", VALID_VIDEO_CONTENT, "video/quicktime")},
            headers=AUTH_HEADER,
        )
        assert response.status_code == 201

    def test_video_media_type_is_video(self, client):
        response = client.post(
            "/photos",
            files={"file": ("video.mp4", VALID_VIDEO_CONTENT, "video/mp4")},
            headers=AUTH_HEADER,
        )
        assert response.status_code == 201
        assert response.json()["mediaType"] == "video"

    def test_photo_media_type_is_photo(self, client):
        response = client.post(
            "/photos",
            files={"file": ("photo.jpg", VALID_IMAGE_CONTENT, "image/jpeg")},
            headers=AUTH_HEADER,
        )
        assert response.status_code == 201
        assert response.json()["mediaType"] == "photo"

    def test_video_response_contains_expected_fields(self, client):
        response = client.post(
            "/photos",
            files={"file": ("video.mp4", VALID_VIDEO_CONTENT, "video/mp4")},
            headers=AUTH_HEADER,
        )
        data = response.json()
        assert "id" in data
        assert data["fileExtension"] == "mp4"
        assert data["mediaType"] == "video"
        assert data["sha256Hash"] == hashlib.sha256(VALID_VIDEO_CONTENT).hexdigest()
        assert data["uploadedAt"] is not None

    def test_video_file_extension_taken_from_filename(self, client):
        response = client.post(
            "/photos",
            files={"file": ("myvideo.mov", VALID_VIDEO_CONTENT, "video/quicktime")},
            headers=AUTH_HEADER,
        )
        assert response.status_code == 201
        assert response.json()["fileExtension"] == "mov"

    def test_duplicate_video_upload_returns_200(self, client):
        content = VALID_VIDEO_CONTENT
        client.post("/photos", files={"file": ("v.mp4", content, "video/mp4")}, headers=AUTH_HEADER)
        response = client.post("/photos", files={"file": ("v.mp4", content, "video/mp4")}, headers=AUTH_HEADER)
        assert response.status_code == 200

    def test_duplicate_video_upload_returns_same_record(self, client):
        content = VALID_VIDEO_CONTENT
        first = client.post("/photos", files={"file": ("v.mp4", content, "video/mp4")}, headers=AUTH_HEADER)
        second = client.post("/photos", files={"file": ("v.mp4", content, "video/mp4")}, headers=AUTH_HEADER)
        assert first.json()["id"] == second.json()["id"]

    def test_video_file_written_to_staging_dir(self, client, tmp_path):
        content = VALID_VIDEO_CONTENT
        sha = hashlib.sha256(content).hexdigest()
        client.post(
            "/photos",
            files={"file": ("video.mp4", content, "video/mp4")},
            headers=AUTH_HEADER,
        )
        assert (tmp_path / f"{sha}.mp4").exists()

    def test_video_size_limit_uses_max_video_size(self, client):
        """Video uploads are checked against MAX_VIDEO_SIZE, not MAX_PHOTO_SIZE."""
        import app.main as main_module
        # Set MAX_PHOTO_SIZE to 1 byte — video should still be accepted because
        # it uses MAX_VIDEO_SIZE instead.
        with patch("app.routers.photos.MAX_PHOTO_SIZE", 1):
            response = client.post(
                "/photos",
                files={"file": ("video.mp4", VALID_VIDEO_CONTENT, "video/mp4")},
                headers=AUTH_HEADER,
            )
        assert response.status_code == 201

    def test_video_too_large_returns_413(self, client):
        with patch("app.routers.photos.MAX_VIDEO_SIZE", 10):
            response = client.post(
                "/photos",
                files={"file": ("video.mp4", VALID_VIDEO_CONTENT, "video/mp4")},
                headers=AUTH_HEADER,
            )
            assert response.status_code == 413
            assert response.json() == {"detail": "File too large"}


class TestVideoMetrics:
    """Tests for the video-count metric in /_info."""

    def test_video_count_metric_present(self, client):
        data = client.get("/_info").json()
        assert "video-count" in data["metrics"]

    def test_video_count_metric_structure(self, client):
        data = client.get("/_info").json()
        metric = data["metrics"]["video-count"]
        assert "value" in metric
        assert "techDetail" in metric
        assert isinstance(metric["value"], int)

    def test_video_count_reflects_uploaded_videos(self, client):
        # Initially zero
        data = client.get("/_info").json()
        assert data["metrics"]["video-count"]["value"] == 0

        # Upload a video
        client.post(
            "/photos",
            files={"file": ("video.mp4", VALID_VIDEO_CONTENT, "video/mp4")},
            headers=AUTH_HEADER,
        )

        data = client.get("/_info").json()
        assert data["metrics"]["video-count"]["value"] == 1

    def test_photo_count_excludes_videos(self, client):
        """photo-count should only count photos, not videos."""
        # Upload a photo
        client.post(
            "/photos",
            files={"file": ("photo.jpg", VALID_IMAGE_CONTENT, "image/jpeg")},
            headers=AUTH_HEADER,
        )
        # Upload a video
        client.post(
            "/photos",
            files={"file": ("video.mp4", VALID_VIDEO_CONTENT, "video/mp4")},
            headers=AUTH_HEADER,
        )

        data = client.get("/_info").json()
        assert data["metrics"]["photo-count"]["value"] == 1
        assert data["metrics"]["video-count"]["value"] == 1
