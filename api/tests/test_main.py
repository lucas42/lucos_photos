import hashlib
from unittest.mock import patch

AUTH_HEADER = {"Authorization": "key validkey"}
VALID_IMAGE_CONTENT = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300080606070605080707070909080a0c140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20242e2720222c231c1c2837292c30313434341f27393d38323c2e333432ffdb0043010909090c0b0c180d0d1832211c213232323232323232323232323232323232323232323232323232323232323232323232323232323232323232323232323232ffc00011080001000103012200021101031101ffc4001f0000010501010101010100000000000000000102030405060708090a0bffc400b5100002010303020403050504040000017d01020300041105122131410613516107227114328191a1082342b1c11552d1f02433627282090a161718191a25262728292a3435363738393a434445464748494a535455565758595a636465666768696a737475767778797a838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9faffc4001f0100030101010101010101010000000000000102030405060708090a0bffc400b51100020102040403040705040400010277000102031104052131061241510761711322328108144291a1b1c109233352f0156272d10a162434e125f11718191a262728292a35363738393a434445464748494a535455565758595a636465666768696a737475767778797a82838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae2e3e4e5e6e7e8e9eaf2f3f4f5f6f7f8f9faffda000c03010002110311003f00e2e8a28af993f713ffd9"
)


class TestHealthcheck:
    def test_returns_ok(self, client):
        response = client.get("/healthcheck")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


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
            headers={"Authorization": "Bearer sometoken"},
        )
        assert response.status_code == 401

    def test_invalid_key_returns_401(self, client):
        response = client.post(
            "/photos",
            files={"file": ("a.jpg", b"data", "image/jpeg")},
            headers={"Authorization": "key wrongkey"},
        )
        assert response.status_code == 401

    def test_401_responses_include_www_authenticate(self, client):
        for headers in [
            {},
            {"Authorization": "Bearer token"},
            {"Authorization": "key wrongkey"},
        ]:
            response = client.post(
                "/photos",
                files={"file": ("a.jpg", b"data", "image/jpeg")},
                headers=headers,
            )
            assert "WWW-Authenticate" in response.headers, f"Missing WWW-Authenticate for headers={headers}"
            assert response.headers["WWW-Authenticate"].startswith("key ")


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
