import uuid

import pytest

from lucos_photos_common.models import Face, Person, Photo, PhotoPerson


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_photo(db):
    photo = Photo(sha256_hash="a" * 64, file_extension="jpg")
    db.add(photo)
    db.flush()
    return photo


def make_person(db, display_name="Alice"):
    person = Person(display_name=display_name)
    db.add(person)
    db.flush()
    return person


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


# ---------------------------------------------------------------------------
# GET /photos/{photo_id}/faces
# ---------------------------------------------------------------------------

class TestListFaces:
    def test_returns_empty_list_when_no_faces(self, authenticated_client, db_session):
        photo = make_photo(db_session)
        db_session.commit()

        response = authenticated_client.get(f"/photos/{photo.id}/faces")
        assert response.status_code == 200
        assert response.json() == []

    def test_returns_faces_for_photo(self, authenticated_client, db_session):
        photo = make_photo(db_session)
        face = make_face(db_session, photo)
        db_session.commit()

        response = authenticated_client.get(f"/photos/{photo.id}/faces")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["id"] == str(face.id)
        assert data[0]["photoId"] == str(photo.id)
        assert data[0]["personId"] is None
        assert data[0]["personConfirmed"] is False
        assert data[0]["boundingBox"] == {"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4}

    def test_returns_face_with_person_assigned(self, authenticated_client, db_session):
        photo = make_photo(db_session)
        person = make_person(db_session)
        face = make_face(db_session, photo, person=person, confirmed=True)
        db_session.commit()

        response = authenticated_client.get(f"/photos/{photo.id}/faces")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["personId"] == str(person.id)
        assert data[0]["personConfirmed"] is True

    def test_only_returns_faces_for_requested_photo(self, authenticated_client, db_session):
        photo1 = make_photo(db_session)
        photo2 = Photo(sha256_hash="b" * 64, file_extension="jpg")
        db_session.add(photo2)
        db_session.flush()
        make_face(db_session, photo1)
        make_face(db_session, photo2)
        db_session.commit()

        response = authenticated_client.get(f"/photos/{photo1.id}/faces")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["photoId"] == str(photo1.id)

    def test_returns_404_for_unknown_photo(self, authenticated_client, db_session):
        response = authenticated_client.get(f"/photos/{uuid.uuid4()}/faces")
        assert response.status_code == 404

    def test_returns_404_for_invalid_uuid(self, authenticated_client, db_session):
        response = authenticated_client.get("/photos/not-a-uuid/faces")
        assert response.status_code == 404

    def test_requires_authentication(self, client, db_session):
        photo = make_photo(db_session)
        db_session.commit()
        response = client.get(f"/photos/{photo.id}/faces", follow_redirects=False)
        assert response.status_code == 302


# ---------------------------------------------------------------------------
# PUT /faces/{face_id}/person
# ---------------------------------------------------------------------------

class TestAssignPerson:
    def test_assigns_person_to_face(self, authenticated_client, db_session):
        photo = make_photo(db_session)
        person = make_person(db_session)
        face = make_face(db_session, photo)
        db_session.commit()

        response = authenticated_client.put(
            f"/faces/{face.id}/person",
            json={"personId": str(person.id)},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["personId"] == str(person.id)
        assert data["personConfirmed"] is True

    def test_marks_person_confirmed_true(self, authenticated_client, db_session):
        photo = make_photo(db_session)
        person = make_person(db_session)
        face = make_face(db_session, photo)
        db_session.commit()

        authenticated_client.put(
            f"/faces/{face.id}/person",
            json={"personId": str(person.id)},
        )
        db_session.refresh(face)
        assert face.person_confirmed is True

    def test_updates_photo_person_table(self, authenticated_client, db_session):
        photo = make_photo(db_session)
        person = make_person(db_session)
        face = make_face(db_session, photo)
        db_session.commit()

        authenticated_client.put(
            f"/faces/{face.id}/person",
            json={"personId": str(person.id)},
        )

        pp = db_session.query(PhotoPerson).filter(
            PhotoPerson.photo_id == photo.id,
            PhotoPerson.person_id == person.id,
        ).first()
        assert pp is not None

    def test_reassigns_face_to_different_person(self, authenticated_client, db_session):
        photo = make_photo(db_session)
        person1 = make_person(db_session, "Alice")
        person2 = make_person(db_session, "Bob")
        face = make_face(db_session, photo, person=person1, confirmed=True)
        db_session.add(PhotoPerson(photo_id=photo.id, person_id=person1.id))
        db_session.commit()

        authenticated_client.put(
            f"/faces/{face.id}/person",
            json={"personId": str(person2.id)},
        )

        # Old photo_person row should be removed, new one added
        old_pp = db_session.query(PhotoPerson).filter(
            PhotoPerson.photo_id == photo.id,
            PhotoPerson.person_id == person1.id,
        ).first()
        new_pp = db_session.query(PhotoPerson).filter(
            PhotoPerson.photo_id == photo.id,
            PhotoPerson.person_id == person2.id,
        ).first()
        assert old_pp is None
        assert new_pp is not None

    def test_returns_404_for_unknown_face(self, authenticated_client, db_session):
        person = make_person(db_session)
        db_session.commit()
        response = authenticated_client.put(
            f"/faces/{uuid.uuid4()}/person",
            json={"personId": str(person.id)},
        )
        assert response.status_code == 404

    def test_returns_404_for_unknown_person(self, authenticated_client, db_session):
        photo = make_photo(db_session)
        face = make_face(db_session, photo)
        db_session.commit()
        response = authenticated_client.put(
            f"/faces/{face.id}/person",
            json={"personId": str(uuid.uuid4())},
        )
        assert response.status_code == 404

    def test_returns_422_when_person_id_missing(self, authenticated_client, db_session):
        photo = make_photo(db_session)
        face = make_face(db_session, photo)
        db_session.commit()
        response = authenticated_client.put(
            f"/faces/{face.id}/person",
            json={},
        )
        assert response.status_code == 422

    def test_returns_422_for_invalid_person_uuid(self, authenticated_client, db_session):
        photo = make_photo(db_session)
        face = make_face(db_session, photo)
        db_session.commit()
        response = authenticated_client.put(
            f"/faces/{face.id}/person",
            json={"personId": "not-a-uuid"},
        )
        assert response.status_code == 422

    def test_requires_authentication(self, client, db_session):
        photo = make_photo(db_session)
        person = make_person(db_session)
        face = make_face(db_session, photo)
        db_session.commit()
        response = client.put(
            f"/faces/{face.id}/person",
            json={"personId": str(person.id)},
            headers={"Origin": "https://photos.l42.eu"},
            follow_redirects=False,
        )
        assert response.status_code == 302

    def test_emits_loganne_event(self, authenticated_client, db_session):
        from unittest.mock import AsyncMock, patch
        photo = make_photo(db_session)
        person = make_person(db_session)
        face = make_face(db_session, photo)
        db_session.commit()

        with patch("app.routers.faces.emit_loganne_event", new_callable=AsyncMock) as mock_emit:
            authenticated_client.put(
                f"/faces/{face.id}/person",
                json={"personId": str(person.id)},
            )
            mock_emit.assert_called_once()
            call_args = mock_emit.call_args
            assert call_args[0][0] == "personTagged"


# ---------------------------------------------------------------------------
# DELETE /faces/{face_id}/person
# ---------------------------------------------------------------------------

class TestUnassignPerson:
    def test_clears_person_from_face(self, authenticated_client, db_session):
        photo = make_photo(db_session)
        person = make_person(db_session)
        face = make_face(db_session, photo, person=person, confirmed=True)
        db_session.add(PhotoPerson(photo_id=photo.id, person_id=person.id))
        db_session.commit()

        response = authenticated_client.delete(f"/faces/{face.id}/person")
        assert response.status_code == 204

        db_session.refresh(face)
        assert face.person_id is None
        assert face.person_confirmed is False

    def test_removes_photo_person_row_when_no_faces_have_person(self, authenticated_client, db_session):
        photo = make_photo(db_session)
        person = make_person(db_session)
        face = make_face(db_session, photo, person=person, confirmed=True)
        db_session.add(PhotoPerson(photo_id=photo.id, person_id=person.id))
        db_session.commit()

        authenticated_client.delete(f"/faces/{face.id}/person")

        pp = db_session.query(PhotoPerson).filter(
            PhotoPerson.photo_id == photo.id,
            PhotoPerson.person_id == person.id,
        ).first()
        assert pp is None

    def test_keeps_photo_person_row_when_another_face_still_has_person(self, authenticated_client, db_session):
        photo = make_photo(db_session)
        person = make_person(db_session)
        face1 = make_face(db_session, photo, person=person, confirmed=True)
        make_face(db_session, photo, person=person, confirmed=False)
        db_session.add(PhotoPerson(photo_id=photo.id, person_id=person.id))
        db_session.commit()

        authenticated_client.delete(f"/faces/{face1.id}/person")

        pp = db_session.query(PhotoPerson).filter(
            PhotoPerson.photo_id == photo.id,
            PhotoPerson.person_id == person.id,
        ).first()
        assert pp is not None

    def test_returns_404_for_unknown_face(self, authenticated_client, db_session):
        response = authenticated_client.delete(f"/faces/{uuid.uuid4()}/person")
        assert response.status_code == 404

    def test_returns_404_for_invalid_uuid(self, authenticated_client, db_session):
        response = authenticated_client.delete("/faces/not-a-uuid/person")
        assert response.status_code == 404

    def test_requires_authentication(self, client, db_session):
        photo = make_photo(db_session)
        person = make_person(db_session)
        face = make_face(db_session, photo, person=person, confirmed=True)
        db_session.commit()
        response = client.delete(
            f"/faces/{face.id}/person",
            headers={"Origin": "https://photos.l42.eu"},
            follow_redirects=False,
        )
        assert response.status_code == 302
