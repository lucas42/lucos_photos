import hashlib

AUTH_HEADER = {"Authorization": "key validkey"}


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
            files={"file": ("photo.jpg", b"fake image bytes", "image/jpeg")},
            headers=AUTH_HEADER,
        )
        assert response.status_code == 201

    def test_response_body_contains_expected_fields(self, client):
        content = b"fake image bytes"
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
        content = b"same image bytes"
        client.post("/photos", files={"file": ("photo.jpg", content, "image/jpeg")}, headers=AUTH_HEADER)
        response = client.post("/photos", files={"file": ("photo.jpg", content, "image/jpeg")}, headers=AUTH_HEADER)
        assert response.status_code == 200

    def test_duplicate_upload_returns_same_record(self, client):
        content = b"same image bytes again"
        first = client.post("/photos", files={"file": ("photo.jpg", content, "image/jpeg")}, headers=AUTH_HEADER)
        second = client.post("/photos", files={"file": ("photo.jpg", content, "image/jpeg")}, headers=AUTH_HEADER)
        assert first.json()["id"] == second.json()["id"]

    def test_extension_taken_from_filename(self, client):
        response = client.post(
            "/photos",
            files={"file": ("holiday.png", b"png image data", "image/jpeg")},
            headers=AUTH_HEADER,
        )
        assert response.json()["fileExtension"] == "png"

    def test_extension_falls_back_to_content_type(self, client):
        response = client.post(
            "/photos",
            files={"file": ("photo", b"jpeg image data", "image/jpeg")},
            headers=AUTH_HEADER,
        )
        assert response.json()["fileExtension"] == "jpg"

    def test_file_written_to_staging_dir(self, client, tmp_path):
        content = b"image to stage"
        sha = hashlib.sha256(content).hexdigest()
        client.post(
            "/photos",
            files={"file": ("photo.jpg", content, "image/jpeg")},
            headers=AUTH_HEADER,
        )
        assert (tmp_path / f"{sha}.jpg").exists()

    def test_different_content_creates_different_records(self, client):
        first = client.post("/photos", files={"file": ("a.jpg", b"image one", "image/jpeg")}, headers=AUTH_HEADER)
        second = client.post("/photos", files={"file": ("b.jpg", b"image two", "image/jpeg")}, headers=AUTH_HEADER)
        assert first.status_code == 201
        assert second.status_code == 201
        assert first.json()["id"] != second.json()["id"]
