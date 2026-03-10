import uuid
import pytest
from unittest.mock import AsyncMock, patch
from lucos_photos_common.models import Person, Photo, PhotoPerson, Face


def make_person(db, display_name="Alice", contact_id=None):
    person = Person(display_name=display_name, contact_id=contact_id)
    db.add(person)
    db.flush()
    return person

def make_photo(db, sha256_hash="a"*64):
    photo = Photo(sha256_hash=sha256_hash, file_extension="jpg")
    db.add(photo)
    db.flush()
    return photo

class TestListpeople:
    def test_list_people_empty(self, authenticated_client, db_session):
        response = authenticated_client.get("/people")
        assert response.status_code == 200
        assert response.json() == []

    def test_list_people(self, authenticated_client, db_session):
        make_person(db_session, "Alice")
        make_person(db_session, "Bob")
        db_session.commit()

        response = authenticated_client.get("/people")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        names = {p["name"] for p in data}
        assert names == {"Alice", "Bob"}

    def test_list_people_pagination(self, authenticated_client, db_session):
        for i in range(5):
            make_person(db_session, f"Person {i}")
        db_session.commit()

        response = authenticated_client.get("/people?limit=2&offset=1")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        assert data[0]["name"] == "Person 1"
        assert data[1]["name"] == "Person 2"

    def test_list_people_include_photo_counts(self, authenticated_client, db_session):
        person1 = make_person(db_session, "Alice")
        person2 = make_person(db_session, "Bob")
        photo1 = make_photo(db_session, "1"*64)
        photo2 = make_photo(db_session, "2"*64)

        db_session.add(PhotoPerson(photo_id=photo1.id, person_id=person1.id))
        db_session.add(PhotoPerson(photo_id=photo2.id, person_id=person1.id))
        db_session.commit()

        response = authenticated_client.get("/people?includePhotoCounts=true")
        assert response.status_code == 200
        data = response.json()

        alice = next(p for p in data if p["name"] == "Alice")
        bob = next(p for p in data if p["name"] == "Bob")

        assert alice["photoCount"] == 2
        assert bob["photoCount"] == 0

    def test_requires_authentication(self, client):
        response = client.get("/people")
        assert response.status_code == 401

class TestCreatePerson:
    def test_create_person(self, authenticated_client, db_session):
        with patch("app.main.emit_loganne_event", new_callable=AsyncMock) as mock_emit:
            response = authenticated_client.post(
                "/people",
                json={"name": "Charlie", "contactId": "charlie-123"},
            )
            assert response.status_code == 201
            data = response.json()
            assert data["name"] == "Charlie"
            assert data["contactId"] == "charlie-123"
            assert "id" in data

            mock_emit.assert_called_once()
            assert mock_emit.call_args[0][0] == "personCreated"

    def test_create_person_missing_name(self, authenticated_client):
        response = authenticated_client.post(
            "/people",
            json={"contactId": "charlie-123"},
        )
        assert response.status_code == 422

    def test_create_person_duplicate_contact_id(self, authenticated_client, db_session):
        make_person(db_session, "Alice", contact_id="alice-123")
        db_session.commit()

        response = authenticated_client.post(
            "/people",
            json={"name": "Alice 2", "contactId": "alice-123"},
        )
        assert response.status_code == 409
        assert "already exists" in response.json()["detail"]

    def test_requires_authentication(self, client):
        response = client.post("/people", json={"name": "Charlie"})
        assert response.status_code == 401

class TestLinkPersonContact:
    def test_link_contact(self, authenticated_client, db_session):
        person = make_person(db_session, "Alice")
        db_session.commit()

        with patch("app.main.emit_loganne_event", new_callable=AsyncMock) as mock_emit:
            response = authenticated_client.put(
                f"/people/{person.id}/contact",
                json={"contactId": "42"},
            )
            assert response.status_code == 200
            data = response.json()
            assert data["contactId"] == "42"
            mock_emit.assert_called_once()
            assert mock_emit.call_args[0][0] == "personContactLinked"

    def test_link_contact_overwrites_existing(self, authenticated_client, db_session):
        person = make_person(db_session, "Alice", contact_id="old-id")
        db_session.commit()

        with patch("app.main.emit_loganne_event", new_callable=AsyncMock):
            response = authenticated_client.put(
                f"/people/{person.id}/contact",
                json={"contactId": "new-id"},
            )
            assert response.status_code == 200
            assert response.json()["contactId"] == "new-id"

    def test_link_contact_duplicate(self, authenticated_client, db_session):
        make_person(db_session, "Bob", contact_id="shared-id")
        alice = make_person(db_session, "Alice")
        db_session.commit()

        with patch("app.main.emit_loganne_event", new_callable=AsyncMock):
            response = authenticated_client.put(
                f"/people/{alice.id}/contact",
                json={"contactId": "shared-id"},
            )
            assert response.status_code == 409
            assert "already exists" in response.json()["detail"]

    def test_link_contact_missing_contact_id(self, authenticated_client, db_session):
        person = make_person(db_session, "Alice")
        db_session.commit()

        response = authenticated_client.put(
            f"/people/{person.id}/contact",
            json={},
        )
        assert response.status_code == 422

    def test_link_contact_person_not_found(self, authenticated_client):
        with patch("app.main.emit_loganne_event", new_callable=AsyncMock):
            response = authenticated_client.put(
                f"/people/{uuid.uuid4()}/contact",
                json={"contactId": "42"},
            )
            assert response.status_code == 404

    def test_requires_authentication(self, client, db_session):
        person = make_person(db_session, "Alice")
        db_session.commit()
        response = client.put(f"/people/{person.id}/contact", json={"contactId": "42"})
        assert response.status_code == 401


class TestUnlinkPersonContact:
    def test_unlink_contact(self, authenticated_client, db_session):
        person = make_person(db_session, "Alice", contact_id="42")
        db_session.commit()

        with patch("app.main.emit_loganne_event", new_callable=AsyncMock) as mock_emit:
            response = authenticated_client.delete(f"/people/{person.id}/contact")
            assert response.status_code == 204
            mock_emit.assert_called_once()
            assert mock_emit.call_args[0][0] == "personContactUnlinked"

        db_session.refresh(person)
        assert person.contact_id is None

    def test_unlink_contact_when_not_linked(self, authenticated_client, db_session):
        person = make_person(db_session, "Alice")
        db_session.commit()

        with patch("app.main.emit_loganne_event", new_callable=AsyncMock):
            response = authenticated_client.delete(f"/people/{person.id}/contact")
        assert response.status_code == 204

    def test_unlink_contact_person_not_found(self, authenticated_client):
        response = authenticated_client.delete(f"/people/{uuid.uuid4()}/contact")
        assert response.status_code == 404

    def test_requires_authentication(self, client, db_session):
        person = make_person(db_session, "Alice", contact_id="42")
        db_session.commit()
        response = client.delete(f"/people/{person.id}/contact")
        assert response.status_code == 401


class TestGetPerson:
    def test_get_person(self, authenticated_client, db_session):
        person = make_person(db_session, "Alice", contact_id="alice-123")
        photo = make_photo(db_session)
        db_session.add(PhotoPerson(photo_id=photo.id, person_id=person.id))

        # Add a face for this person
        face = Face(
            id=uuid.uuid4(),
            photo_id=photo.id,
            person_id=person.id,
            person_confirmed=True,
            bbox_x=0, bbox_y=0, bbox_width=0.1, bbox_height=0.1
        )
        db_session.add(face)
        db_session.commit()

        response = authenticated_client.get(f"/people/{person.id}")
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Alice"
        assert data["contactId"] == "alice-123"
        assert data["faceCount"] == 1
        assert len(data["photos"]) == 1
        assert data["photos"][0]["id"] == str(photo.id)

    def test_get_person_not_found(self, authenticated_client):
        response = authenticated_client.get(f"/people/{uuid.uuid4()}")
        assert response.status_code == 404

    def test_get_person_invalid_uuid(self, authenticated_client):
        response = authenticated_client.get("/people/not-a-uuid")
        assert response.status_code == 404

    def test_requires_authentication(self, client, db_session):
        person = make_person(db_session, "Alice")
        db_session.commit()
        response = client.get(f"/people/{person.id}")
        assert response.status_code == 401
