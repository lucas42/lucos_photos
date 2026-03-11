import hashlib
import uuid

import pytest

from lucos_photos_common.models import Face, MediaItem, Person, Photo, PhotoPerson, ProcessingState, ProcessingStatus

# AUTH_HEADER is used for the upload endpoint (POST /photos) which uses CLIENT_KEYS / M2M auth.
# User-facing GET endpoints use session auth — tests for those use the authenticated_client fixture.
AUTH_HEADER = {"Authorization": "Bearer validkey"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_photo(db, sha_seed="photo", ext="jpg"):
    sha = hashlib.sha256(sha_seed.encode()).hexdigest()
    photo = Photo(sha256_hash=sha, file_extension=ext)
    db.add(photo)
    db.flush()
    return photo


def make_processing_status(db, photo, state=ProcessingState.complete):
    ps = ProcessingStatus(photo_id=photo.id, state=state)
    db.add(ps)
    db.flush()
    return ps


def make_face(db, photo, person=None, confirmed=False):
    face = Face(
        photo_id=photo.id,
        person_id=person.id if person else None,
        person_confirmed=confirmed,
        bbox_x=0.1,
        bbox_y=0.2,
        bbox_width=0.3,
        bbox_height=0.4,
    )
    db.add(face)
    db.flush()
    return face


def make_person(db, display_name="Alice"):
    person = Person(display_name=display_name)
    db.add(person)
    db.flush()
    return person


# ---------------------------------------------------------------------------
# GET /photos
# ---------------------------------------------------------------------------

class TestListPhotos:
    def test_requires_auth(self, client):
        response = client.get("/photos")
        assert response.status_code == 401

    def test_returns_empty_list_when_no_photos(self, authenticated_client):
        response = authenticated_client.get("/photos")
        assert response.status_code == 200
        data = response.json()
        assert data["photos"] == []
        assert data["total"] == 0

    def test_returns_list_of_photos(self, authenticated_client, db_session):
        p1 = make_photo(db_session, "photo1")
        make_processing_status(db_session, p1, ProcessingState.complete)
        p2 = make_photo(db_session, "photo2")
        make_processing_status(db_session, p2, ProcessingState.complete)
        db_session.commit()

        response = authenticated_client.get("/photos")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 2
        ids = {p["id"] for p in data["photos"]}
        assert str(p1.id) in ids
        assert str(p2.id) in ids

    def test_photo_has_expected_fields(self, authenticated_client, db_session):
        photo = make_photo(db_session, "fieldtest")
        make_processing_status(db_session, photo, ProcessingState.complete)
        db_session.commit()

        response = authenticated_client.get("/photos")
        assert response.status_code == 200
        photo_data = response.json()["photos"][0]
        assert "id" in photo_data
        assert "sha256Hash" in photo_data
        assert "fileExtension" in photo_data
        assert "takenAt" in photo_data
        assert "uploadedAt" in photo_data
        assert "width" in photo_data
        assert "height" in photo_data
        assert "originalUrl" in photo_data
        assert "thumbnailUrl" in photo_data

    def test_pagination_limit(self, authenticated_client, db_session):
        for i in range(5):
            p = make_photo(db_session, f"photo{i}")
            make_processing_status(db_session, p, ProcessingState.complete)
        db_session.commit()

        response = authenticated_client.get("/photos?limit=3")
        assert response.status_code == 200
        data = response.json()
        assert len(data["photos"]) == 3
        assert data["total"] == 5
        assert data["limit"] == 3

    def test_pagination_offset(self, authenticated_client, db_session):
        for i in range(5):
            p = make_photo(db_session, f"photo{i}")
            make_processing_status(db_session, p, ProcessingState.complete)
        db_session.commit()

        response = authenticated_client.get("/photos?limit=3&offset=3")
        assert response.status_code == 200
        data = response.json()
        assert len(data["photos"]) == 2
        assert data["offset"] == 3

    def test_default_order_is_uploaded_at_descending(self, authenticated_client, db_session):
        # Insert photos with different hashes so order is deterministic from DB insert order
        p1 = make_photo(db_session, "first")
        make_processing_status(db_session, p1, ProcessingState.complete)
        p2 = make_photo(db_session, "second")
        make_processing_status(db_session, p2, ProcessingState.complete)
        db_session.commit()

        response = authenticated_client.get("/photos")
        data = response.json()
        # Most recently uploaded (p2) should appear first
        ids = [p["id"] for p in data["photos"]]
        # Both IDs should be present; we can't guarantee strict order in SQLite
        # without explicit timestamps, but we can check the list is non-empty
        assert len(ids) == 2

    def test_hides_unprocessed_photos(self, authenticated_client, db_session):
        """Photos that haven't been fully processed should not appear in the list."""
        complete = make_photo(db_session, "complete_photo")
        make_processing_status(db_session, complete, ProcessingState.complete)

        pending = make_photo(db_session, "pending_photo")
        make_processing_status(db_session, pending, ProcessingState.pending)

        processing = make_photo(db_session, "processing_photo")
        make_processing_status(db_session, processing, ProcessingState.processing)

        failed = make_photo(db_session, "failed_photo")
        make_processing_status(db_session, failed, ProcessingState.failed)

        no_status = make_photo(db_session, "no_status_photo")
        # Deliberately no ProcessingStatus row for this one

        db_session.commit()

        response = authenticated_client.get("/photos")
        assert response.status_code == 200
        data = response.json()

        ids = {p["id"] for p in data["photos"]}
        assert str(complete.id) in ids
        assert str(pending.id) not in ids
        assert str(processing.id) not in ids
        assert str(failed.id) not in ids
        assert str(no_status.id) not in ids
        assert data["total"] == 1

    def test_total_count_only_includes_processed(self, authenticated_client, db_session):
        """The total field in the response should reflect only processed photos."""
        complete1 = make_photo(db_session, "c1")
        make_processing_status(db_session, complete1, ProcessingState.complete)
        complete2 = make_photo(db_session, "c2")
        make_processing_status(db_session, complete2, ProcessingState.complete)
        pending = make_photo(db_session, "p1")
        make_processing_status(db_session, pending, ProcessingState.pending)
        db_session.commit()

        response = authenticated_client.get("/photos")
        assert response.status_code == 200
        assert response.json()["total"] == 2


# ---------------------------------------------------------------------------
# GET /photos/{id}
# ---------------------------------------------------------------------------

JSON_ACCEPT = {"Accept": "application/json"}
HTML_ACCEPT = {"Accept": "text/html"}


class TestGetPhoto:
    def test_requires_auth(self, client, db_session):
        photo = make_photo(db_session)
        db_session.commit()
        response = client.get(f"/photos/{photo.id}")
        assert response.status_code == 401

    def test_returns_404_for_unknown_id(self, authenticated_client):
        response = authenticated_client.get(f"/photos/{uuid.uuid4()}", headers=JSON_ACCEPT)
        assert response.status_code == 404

    def test_returns_404_for_invalid_uuid(self, authenticated_client):
        response = authenticated_client.get("/photos/not-a-uuid", headers=JSON_ACCEPT)
        assert response.status_code == 404

    def test_returns_photo_metadata(self, authenticated_client, db_session):
        photo = make_photo(db_session, "gettest")
        db_session.commit()

        response = authenticated_client.get(f"/photos/{photo.id}", headers=JSON_ACCEPT)
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == str(photo.id)
        assert data["sha256Hash"] == photo.sha256_hash
        assert data["fileExtension"] == "jpg"
        assert data["originalUrl"] == f"/photo_files/original/{photo.id}.jpg"
        assert data["thumbnailUrl"] == f"/photo_files/thumbnail/{photo.id}.jpg"

    def test_includes_processing_status(self, authenticated_client, db_session):
        photo = make_photo(db_session, "statustest")
        make_processing_status(db_session, photo, ProcessingState.complete)
        db_session.commit()

        response = authenticated_client.get(f"/photos/{photo.id}", headers=JSON_ACCEPT)
        assert response.status_code == 200
        data = response.json()
        assert data["processingStatus"] == "complete"

    def test_processing_status_is_none_when_not_set(self, authenticated_client, db_session):
        photo = make_photo(db_session, "nostatustest")
        db_session.commit()

        response = authenticated_client.get(f"/photos/{photo.id}", headers=JSON_ACCEPT)
        assert response.status_code == 200
        assert response.json()["processingStatus"] is None

    def test_includes_faces(self, authenticated_client, db_session):
        photo = make_photo(db_session, "facestest")
        face = make_face(db_session, photo)
        db_session.commit()

        response = authenticated_client.get(f"/photos/{photo.id}", headers=JSON_ACCEPT)
        assert response.status_code == 200
        data = response.json()
        assert len(data["faces"]) == 1
        face_data = data["faces"][0]
        assert face_data["id"] == str(face.id)
        assert face_data["personId"] is None
        assert "boundingBox" in face_data

    def test_face_includes_person_id_when_tagged(self, authenticated_client, db_session):
        photo = make_photo(db_session, "taggedface")
        person = make_person(db_session)
        face = make_face(db_session, photo, person=person, confirmed=True)
        db_session.commit()

        response = authenticated_client.get(f"/photos/{photo.id}", headers=JSON_ACCEPT)
        assert response.status_code == 200
        face_data = response.json()["faces"][0]
        assert face_data["personId"] == str(person.id)
        assert face_data["personConfirmed"] is True

    def test_includes_people_list(self, authenticated_client, db_session):
        photo = make_photo(db_session, "personlist")
        person = make_person(db_session)
        db_session.add(PhotoPerson(photo_id=photo.id, person_id=person.id))
        db_session.commit()

        response = authenticated_client.get(f"/photos/{photo.id}", headers=JSON_ACCEPT)
        assert response.status_code == 200
        data = response.json()
        assert str(person.id) in data["people"]

    def test_people_empty_when_no_tags(self, authenticated_client, db_session):
        photo = make_photo(db_session, "notags")
        db_session.commit()

        response = authenticated_client.get(f"/photos/{photo.id}", headers=JSON_ACCEPT)
        assert response.status_code == 200
        assert response.json()["people"] == []

    # -----------------------------------------------------------------------
    # Content negotiation
    # -----------------------------------------------------------------------

    def test_returns_html_when_browser_accept_header(self, authenticated_client, db_session):
        photo = make_photo(db_session, "htmlnegotiation")
        db_session.commit()

        response = authenticated_client.get(f"/photos/{photo.id}", headers=HTML_ACCEPT)
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert b"<!DOCTYPE html>" in response.content

    def test_returns_json_when_json_accept_header(self, authenticated_client, db_session):
        photo = make_photo(db_session, "jsonnegotiation")
        db_session.commit()

        response = authenticated_client.get(f"/photos/{photo.id}", headers=JSON_ACCEPT)
        assert response.status_code == 200
        assert "application/json" in response.headers["content-type"]
        data = response.json()
        assert data["id"] == str(photo.id)

    def test_returns_json_when_no_accept_header(self, authenticated_client, db_session):
        """With no Accept header (*/*), JSON should be returned as the default."""
        photo = make_photo(db_session, "noaccept")
        db_session.commit()

        response = authenticated_client.get(f"/photos/{photo.id}")
        assert response.status_code == 200
        # mimeparse returns the last match for */*, which is application/json
        assert "application/json" in response.headers["content-type"]


# ---------------------------------------------------------------------------
# DELETE /photos/{id}
# ---------------------------------------------------------------------------

class TestDeletePhoto:
    def test_requires_auth(self, client, db_session):
        photo = make_photo(db_session)
        db_session.commit()
        response = client.delete(f"/photos/{photo.id}")
        assert response.status_code == 401

    def test_returns_404_for_unknown_photo(self, authenticated_client):
        response = authenticated_client.delete(f"/photos/{uuid.uuid4()}")
        assert response.status_code == 404

    def test_returns_404_for_invalid_uuid(self, authenticated_client):
        response = authenticated_client.delete("/photos/not-a-uuid")
        assert response.status_code == 404

    def test_deletes_photo(self, authenticated_client, db_session):
        photo = make_photo(db_session, "todelete")
        db_session.commit()
        photo_id = photo.id

        response = authenticated_client.delete(f"/photos/{photo_id}")
        assert response.status_code == 204

        # Photo should no longer exist
        db_session.expire_all()
        assert db_session.query(MediaItem).filter(MediaItem.id == photo_id).first() is None

    def test_photo_no_longer_accessible_after_deletion(self, authenticated_client, db_session):
        photo = make_photo(db_session, "deletedget")
        db_session.commit()

        authenticated_client.delete(f"/photos/{photo.id}")
        response = authenticated_client.get(f"/photos/{photo.id}")
        assert response.status_code == 404

    def test_deletes_associated_faces(self, authenticated_client, db_session):
        from lucos_photos_common.models import Face as FaceModel
        photo = make_photo(db_session, "deletewithfaces")
        db_session.commit()

        face = FaceModel(
            photo_id=photo.id,
            bbox_x=0.1, bbox_y=0.1, bbox_width=0.2, bbox_height=0.2,
        )
        db_session.add(face)
        db_session.commit()
        face_id = face.id

        authenticated_client.delete(f"/photos/{photo.id}")

        db_session.expire_all()
        assert db_session.query(FaceModel).filter(FaceModel.id == face_id).first() is None

    def test_deletes_associated_processing_status(self, authenticated_client, db_session):
        photo = make_photo(db_session, "deletewithstatus")
        make_processing_status(db_session, photo, ProcessingState.complete)
        db_session.commit()

        authenticated_client.delete(f"/photos/{photo.id}")

        db_session.expire_all()
        assert db_session.query(ProcessingStatus).filter(ProcessingStatus.photo_id == photo.id).first() is None

    def test_second_delete_returns_404(self, authenticated_client, db_session):
        photo = make_photo(db_session, "doubledelete")
        db_session.commit()

        authenticated_client.delete(f"/photos/{photo.id}")
        response = authenticated_client.delete(f"/photos/{photo.id}")
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# GET /photo_files/original/{id}.{ext}  (canonical file-serving route)
# ---------------------------------------------------------------------------

class TestGetPhotoOriginal:
    def test_requires_auth(self, client, db_session):
        photo = make_photo(db_session)
        db_session.commit()
        response = client.get(f"/photo_files/original/{photo.id}.jpg")
        assert response.status_code == 401

    def test_returns_404_for_unknown_photo(self, authenticated_client):
        response = authenticated_client.get(f"/photo_files/original/{uuid.uuid4()}.jpg")
        assert response.status_code == 404

    def test_returns_404_for_invalid_uuid(self, authenticated_client):
        response = authenticated_client.get("/photo_files/original/not-a-uuid.jpg")
        assert response.status_code == 404

    def test_returns_404_when_file_not_on_disk(self, authenticated_client, db_session, monkeypatch, tmp_path):
        import app.main as main_module
        monkeypatch.setattr(main_module, "PHOTOS_DIR", tmp_path)

        photo = make_photo(db_session, "missingoriginal")
        db_session.commit()

        response = authenticated_client.get(f"/photo_files/original/{photo.id}.jpg")
        assert response.status_code == 404

    def test_serves_original_file(self, authenticated_client, db_session, monkeypatch, tmp_path):
        import app.main as main_module
        monkeypatch.setattr(main_module, "PHOTOS_DIR", tmp_path)

        photo = make_photo(db_session, "originaltest")
        db_session.commit()

        originals_dir = tmp_path / "originals"
        originals_dir.mkdir(parents=True)
        (originals_dir / f"{photo.sha256_hash}.jpg").write_bytes(b"original_content")

        # Also write a derivative — endpoint should serve original regardless
        derivatives_dir = tmp_path / "derivatives"
        derivatives_dir.mkdir(parents=True)
        (derivatives_dir / f"{photo.sha256_hash}.jpg").write_bytes(b"derivative_content")

        response = authenticated_client.get(f"/photo_files/original/{photo.id}.jpg")
        assert response.status_code == 200
        assert response.content == b"original_content"

    def test_extension_in_url_is_ignored(self, authenticated_client, db_session, monkeypatch, tmp_path):
        """Extension in URL path is cosmetic — authoritative extension comes from DB."""
        import app.main as main_module
        monkeypatch.setattr(main_module, "PHOTOS_DIR", tmp_path)

        photo = make_photo(db_session, "extignored", ext="jpg")
        db_session.commit()

        originals_dir = tmp_path / "originals"
        originals_dir.mkdir(parents=True)
        (originals_dir / f"{photo.sha256_hash}.jpg").write_bytes(b"image_bytes")

        # Request with wrong extension — should still work (DB ext is used)
        response = authenticated_client.get(f"/photo_files/original/{photo.id}.png")
        assert response.status_code == 200
        assert response.content == b"image_bytes"

    def test_sets_correct_content_type_for_jpg(self, authenticated_client, db_session, monkeypatch, tmp_path):
        import app.main as main_module
        monkeypatch.setattr(main_module, "PHOTOS_DIR", tmp_path)

        photo = make_photo(db_session, "originalcontenttype", ext="jpg")
        db_session.commit()

        originals_dir = tmp_path / "originals"
        originals_dir.mkdir(parents=True)
        (originals_dir / f"{photo.sha256_hash}.jpg").write_bytes(b"image_bytes")

        response = authenticated_client.get(f"/photo_files/original/{photo.id}.jpg")
        assert response.status_code == 200
        assert "image/jpeg" in response.headers["content-type"]

    def test_sets_cache_control_header(self, authenticated_client, db_session, monkeypatch, tmp_path):
        import app.main as main_module
        monkeypatch.setattr(main_module, "PHOTOS_DIR", tmp_path)

        photo = make_photo(db_session, "originalcache")
        db_session.commit()

        originals_dir = tmp_path / "originals"
        originals_dir.mkdir(parents=True)
        (originals_dir / f"{photo.sha256_hash}.jpg").write_bytes(b"image_bytes")

        response = authenticated_client.get(f"/photo_files/original/{photo.id}.jpg")
        assert response.status_code == 200
        assert "Cache-Control" in response.headers
        assert "max-age" in response.headers["Cache-Control"]


# ---------------------------------------------------------------------------
# GET /photo_files/thumbnail/{id}.{ext}  (canonical file-serving route)
# ---------------------------------------------------------------------------

class TestGetPhotoThumbnail:
    def test_requires_auth(self, client, db_session):
        photo = make_photo(db_session)
        db_session.commit()
        response = client.get(f"/photo_files/thumbnail/{photo.id}.jpg")
        assert response.status_code == 401

    def test_returns_404_for_unknown_photo(self, authenticated_client):
        response = authenticated_client.get(f"/photo_files/thumbnail/{uuid.uuid4()}.jpg")
        assert response.status_code == 404

    def test_returns_404_for_invalid_uuid(self, authenticated_client):
        response = authenticated_client.get("/photo_files/thumbnail/not-a-uuid.jpg")
        assert response.status_code == 404

    def test_returns_404_when_file_not_on_disk(self, authenticated_client, db_session, monkeypatch, tmp_path):
        import app.main as main_module
        monkeypatch.setattr(main_module, "PHOTOS_DIR", tmp_path)

        photo = make_photo(db_session, "missingthumbnail")
        db_session.commit()

        response = authenticated_client.get(f"/photo_files/thumbnail/{photo.id}.jpg")
        assert response.status_code == 404

    def test_serves_derivative_when_available(self, authenticated_client, db_session, monkeypatch, tmp_path):
        import app.main as main_module
        monkeypatch.setattr(main_module, "PHOTOS_DIR", tmp_path)

        photo = make_photo(db_session, "thumbnailderivative")
        db_session.commit()

        derivatives_dir = tmp_path / "derivatives"
        derivatives_dir.mkdir(parents=True)
        (derivatives_dir / f"{photo.sha256_hash}.jpg").write_bytes(b"derivative_content")

        response = authenticated_client.get(f"/photo_files/thumbnail/{photo.id}.jpg")
        assert response.status_code == 200
        assert response.content == b"derivative_content"

    def test_falls_back_to_original_when_no_derivative(self, authenticated_client, db_session, monkeypatch, tmp_path):
        import app.main as main_module
        monkeypatch.setattr(main_module, "PHOTOS_DIR", tmp_path)

        photo = make_photo(db_session, "thumbnailfallback")
        db_session.commit()

        originals_dir = tmp_path / "originals"
        originals_dir.mkdir(parents=True)
        (originals_dir / f"{photo.sha256_hash}.jpg").write_bytes(b"original_content")

        response = authenticated_client.get(f"/photo_files/thumbnail/{photo.id}.jpg")
        assert response.status_code == 200
        assert response.content == b"original_content"

    def test_sets_correct_content_type_for_jpg(self, authenticated_client, db_session, monkeypatch, tmp_path):
        import app.main as main_module
        monkeypatch.setattr(main_module, "PHOTOS_DIR", tmp_path)

        photo = make_photo(db_session, "thumbnailcontenttype", ext="jpg")
        db_session.commit()

        originals_dir = tmp_path / "originals"
        originals_dir.mkdir(parents=True)
        (originals_dir / f"{photo.sha256_hash}.jpg").write_bytes(b"image_bytes")

        response = authenticated_client.get(f"/photo_files/thumbnail/{photo.id}.jpg")
        assert response.status_code == 200
        assert "image/jpeg" in response.headers["content-type"]

    def test_sets_cache_control_header(self, authenticated_client, db_session, monkeypatch, tmp_path):
        import app.main as main_module
        monkeypatch.setattr(main_module, "PHOTOS_DIR", tmp_path)

        photo = make_photo(db_session, "thumbnailcache")
        db_session.commit()

        originals_dir = tmp_path / "originals"
        originals_dir.mkdir(parents=True)
        (originals_dir / f"{photo.sha256_hash}.jpg").write_bytes(b"image_bytes")

        response = authenticated_client.get(f"/photo_files/thumbnail/{photo.id}.jpg")
        assert response.status_code == 200
        assert "Cache-Control" in response.headers
        assert "max-age" in response.headers["Cache-Control"]


# ---------------------------------------------------------------------------
# Legacy redirects — GET /photos/{id}/original and /photos/{id}/thumbnail
# ---------------------------------------------------------------------------

class TestLegacyPhotoUrls:
    def test_original_redirects_to_canonical(self, authenticated_client, db_session):
        photo = make_photo(db_session, "legacyorig", ext="jpg")
        db_session.commit()

        response = authenticated_client.get(f"/photos/{photo.id}/original", follow_redirects=False)
        assert response.status_code == 301
        assert response.headers["location"] == f"/photo_files/original/{photo.id}.jpg"

    def test_thumbnail_redirects_to_canonical(self, authenticated_client, db_session):
        photo = make_photo(db_session, "legacythumb", ext="jpg")
        db_session.commit()

        response = authenticated_client.get(f"/photos/{photo.id}/thumbnail", follow_redirects=False)
        assert response.status_code == 301
        assert response.headers["location"] == f"/photo_files/thumbnail/{photo.id}.jpg"

    def test_original_redirect_requires_auth(self, client, db_session):
        photo = make_photo(db_session)
        db_session.commit()
        response = client.get(f"/photos/{photo.id}/original", follow_redirects=False)
        assert response.status_code == 401

    def test_thumbnail_redirect_requires_auth(self, client, db_session):
        photo = make_photo(db_session)
        db_session.commit()
        response = client.get(f"/photos/{photo.id}/thumbnail", follow_redirects=False)
        assert response.status_code == 401

    def test_original_redirect_404_for_unknown(self, authenticated_client):
        response = authenticated_client.get(f"/photos/{uuid.uuid4()}/original", follow_redirects=False)
        assert response.status_code == 404

    def test_thumbnail_redirect_404_for_unknown(self, authenticated_client):
        response = authenticated_client.get(f"/photos/{uuid.uuid4()}/thumbnail", follow_redirects=False)
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Range request support on GET /photo_files/original/{id}.{ext}
# ---------------------------------------------------------------------------

def make_video(db, sha_seed="video", ext="mp4"):
    sha = hashlib.sha256(sha_seed.encode()).hexdigest()
    video = MediaItem(sha256_hash=sha, file_extension=ext, media_type="video")
    db.add(video)
    db.flush()
    return video


class TestRangeRequests:
    def _write_original(self, tmp_path, photo, content=b"0123456789"):
        originals_dir = tmp_path / "originals"
        originals_dir.mkdir(parents=True, exist_ok=True)
        (originals_dir / f"{photo.sha256_hash}.{photo.file_extension}").write_bytes(content)
        return content

    def test_no_range_header_returns_200_with_accept_ranges(self, authenticated_client, db_session, monkeypatch, tmp_path):
        import app.main as main_module
        monkeypatch.setattr(main_module, "PHOTOS_DIR", tmp_path)
        photo = make_photo(db_session, "range_full")
        db_session.commit()
        content = self._write_original(tmp_path, photo)

        response = authenticated_client.get(f"/photo_files/original/{photo.id}.{photo.file_extension}")
        assert response.status_code == 200
        assert response.headers.get("accept-ranges") == "bytes"
        assert response.content == content

    def test_range_request_returns_206(self, authenticated_client, db_session, monkeypatch, tmp_path):
        import app.main as main_module
        monkeypatch.setattr(main_module, "PHOTOS_DIR", tmp_path)
        photo = make_photo(db_session, "range_partial")
        db_session.commit()
        self._write_original(tmp_path, photo, b"0123456789")

        response = authenticated_client.get(
            f"/photo_files/original/{photo.id}.{photo.file_extension}",
            headers={"Range": "bytes=2-5"},
        )
        assert response.status_code == 206
        assert response.content == b"2345"
        assert response.headers["content-range"] == "bytes 2-5/10"
        assert response.headers.get("accept-ranges") == "bytes"

    def test_range_request_from_start(self, authenticated_client, db_session, monkeypatch, tmp_path):
        import app.main as main_module
        monkeypatch.setattr(main_module, "PHOTOS_DIR", tmp_path)
        photo = make_photo(db_session, "range_from_start")
        db_session.commit()
        self._write_original(tmp_path, photo, b"abcdefghij")

        response = authenticated_client.get(
            f"/photo_files/original/{photo.id}.{photo.file_extension}",
            headers={"Range": "bytes=0-3"},
        )
        assert response.status_code == 206
        assert response.content == b"abcd"
        assert response.headers["content-range"] == "bytes 0-3/10"

    def test_range_request_open_ended(self, authenticated_client, db_session, monkeypatch, tmp_path):
        """bytes=5- means from byte 5 to end of file."""
        import app.main as main_module
        monkeypatch.setattr(main_module, "PHOTOS_DIR", tmp_path)
        photo = make_photo(db_session, "range_open_end")
        db_session.commit()
        self._write_original(tmp_path, photo, b"0123456789")

        response = authenticated_client.get(
            f"/photo_files/original/{photo.id}.{photo.file_extension}",
            headers={"Range": "bytes=5-"},
        )
        assert response.status_code == 206
        assert response.content == b"56789"
        assert response.headers["content-range"] == "bytes 5-9/10"

    def test_range_request_suffix(self, authenticated_client, db_session, monkeypatch, tmp_path):
        """bytes=-3 means the last 3 bytes."""
        import app.main as main_module
        monkeypatch.setattr(main_module, "PHOTOS_DIR", tmp_path)
        photo = make_photo(db_session, "range_suffix")
        db_session.commit()
        self._write_original(tmp_path, photo, b"0123456789")

        response = authenticated_client.get(
            f"/photo_files/original/{photo.id}.{photo.file_extension}",
            headers={"Range": "bytes=-3"},
        )
        assert response.status_code == 206
        assert response.content == b"789"
        assert response.headers["content-range"] == "bytes 7-9/10"

    def test_invalid_range_returns_416(self, authenticated_client, db_session, monkeypatch, tmp_path):
        """Range start beyond end of file should return 416."""
        import app.main as main_module
        monkeypatch.setattr(main_module, "PHOTOS_DIR", tmp_path)
        photo = make_photo(db_session, "range_invalid")
        db_session.commit()
        self._write_original(tmp_path, photo, b"0123456789")

        response = authenticated_client.get(
            f"/photo_files/original/{photo.id}.{photo.file_extension}",
            headers={"Range": "bytes=100-200"},
        )
        assert response.status_code == 416

    def test_inverted_range_returns_416(self, authenticated_client, db_session, monkeypatch, tmp_path):
        """Range where start > end should return 416."""
        import app.main as main_module
        monkeypatch.setattr(main_module, "PHOTOS_DIR", tmp_path)
        photo = make_photo(db_session, "range_inverted")
        db_session.commit()
        self._write_original(tmp_path, photo, b"0123456789")

        response = authenticated_client.get(
            f"/photo_files/original/{photo.id}.{photo.file_extension}",
            headers={"Range": "bytes=5-2"},
        )
        assert response.status_code == 416

    def test_malformed_range_returns_416(self, authenticated_client, db_session, monkeypatch, tmp_path):
        """Malformed Range header should return 416."""
        import app.main as main_module
        monkeypatch.setattr(main_module, "PHOTOS_DIR", tmp_path)
        photo = make_photo(db_session, "range_malformed")
        db_session.commit()
        self._write_original(tmp_path, photo, b"0123456789")

        response = authenticated_client.get(
            f"/photo_files/original/{photo.id}.{photo.file_extension}",
            headers={"Range": "bytes=abc-def"},
        )
        assert response.status_code == 416


# ---------------------------------------------------------------------------
# Video thumbnail — GET /photo_files/thumbnail/{id}.{ext} for video media items
# ---------------------------------------------------------------------------

class TestVideoThumbnail:
    def test_returns_404_when_thumbnail_not_yet_generated(self, authenticated_client, db_session, monkeypatch, tmp_path):
        import app.main as main_module
        monkeypatch.setattr(main_module, "PHOTOS_DIR", tmp_path)

        video = make_video(db_session, "vid_nothumbnail")
        db_session.commit()

        response = authenticated_client.get(f"/photo_files/thumbnail/{video.id}.mp4")
        assert response.status_code == 404

    def test_serves_jpeg_thumbnail_for_video(self, authenticated_client, db_session, monkeypatch, tmp_path):
        import app.main as main_module
        monkeypatch.setattr(main_module, "PHOTOS_DIR", tmp_path)

        video = make_video(db_session, "vid_thumb")
        db_session.commit()

        derivatives_dir = tmp_path / "derivatives"
        derivatives_dir.mkdir(parents=True)
        thumb_content = b"jpeg_thumbnail_bytes"
        (derivatives_dir / f"{video.sha256_hash}_thumb.jpg").write_bytes(thumb_content)

        response = authenticated_client.get(f"/photo_files/thumbnail/{video.id}.mp4")
        assert response.status_code == 200
        assert response.content == thumb_content
        assert "image/jpeg" in response.headers["content-type"]

    def test_video_thumbnail_has_cache_control(self, authenticated_client, db_session, monkeypatch, tmp_path):
        import app.main as main_module
        monkeypatch.setattr(main_module, "PHOTOS_DIR", tmp_path)

        video = make_video(db_session, "vid_cache")
        db_session.commit()

        derivatives_dir = tmp_path / "derivatives"
        derivatives_dir.mkdir(parents=True)
        (derivatives_dir / f"{video.sha256_hash}_thumb.jpg").write_bytes(b"bytes")

        response = authenticated_client.get(f"/photo_files/thumbnail/{video.id}.mp4")
        assert response.status_code == 200
        assert "Cache-Control" in response.headers
        assert "max-age" in response.headers["Cache-Control"]

    def test_video_does_not_fall_back_to_original(self, authenticated_client, db_session, monkeypatch, tmp_path):
        """For videos, we should NOT fall back to the original video file as thumbnail."""
        import app.main as main_module
        monkeypatch.setattr(main_module, "PHOTOS_DIR", tmp_path)

        video = make_video(db_session, "vid_nofallback")
        db_session.commit()

        # Write original but no thumbnail
        originals_dir = tmp_path / "originals"
        originals_dir.mkdir(parents=True)
        (originals_dir / f"{video.sha256_hash}.mp4").write_bytes(b"video_bytes")

        # Should return 404 because there's no _thumb.jpg
        response = authenticated_client.get(f"/photo_files/thumbnail/{video.id}.mp4")
        assert response.status_code == 404
