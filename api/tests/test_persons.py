import io
import uuid
import pytest
from unittest.mock import AsyncMock, patch
from PIL import Image
from lucos_photos_common.models import Person, Photo, PhotoPerson, Face, ProcessingStatus, ProcessingState


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
        body = response.json()
        assert body["people"] == []
        assert body["total"] == 0

    def test_list_people(self, authenticated_client, db_session):
        make_person(db_session, "Alice")
        make_person(db_session, "Bob")
        db_session.commit()

        response = authenticated_client.get("/people")
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 2
        names = {p["name"] for p in body["people"]}
        assert names == {"Alice", "Bob"}

    def test_list_people_search_filters_by_name(self, authenticated_client, db_session):
        make_person(db_session, "Alice")
        make_person(db_session, "Bob")
        db_session.commit()

        response = authenticated_client.get("/people", params={"search": "ali"})
        assert response.status_code == 200
        body = response.json()
        names = {p["name"] for p in body["people"]}
        assert names == {"Alice"}

    def test_list_people_search_is_case_insensitive(self, authenticated_client, db_session):
        make_person(db_session, "Alice")
        db_session.commit()

        response = authenticated_client.get("/people", params={"search": "ALICE"})
        assert response.json()["total"] == 1

    def test_list_people_search_no_match(self, authenticated_client, db_session):
        make_person(db_session, "Alice")
        db_session.commit()

        response = authenticated_client.get("/people", params={"search": "nobody"})
        assert response.json()["total"] == 0

    def test_list_people_pagination(self, authenticated_client, db_session):
        for i in range(5):
            make_person(db_session, f"Person {i}")
        db_session.commit()

        response = authenticated_client.get("/people?limit=2&offset=1")
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 5
        assert body["offset"] == 1
        assert body["limit"] == 2
        assert len(body["people"]) == 2

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
        body = response.json()

        alice = next(p for p in body["people"] if p["name"] == "Alice")
        bob = next(p for p in body["people"] if p["name"] == "Bob")

        assert alice["photoCount"] == 2
        assert bob["photoCount"] == 0

    def test_requires_authentication(self, client):
        response = client.get("/people", follow_redirects=False)
        assert response.status_code == 302  # redirects to aithne login

    def test_sort_order_profile_pictures_first(self, authenticated_client, db_session):
        """Persons with profile pictures sort before those without."""
        no_pic = make_person(db_session, "Alice")
        with_pic = make_person(db_session, "Zara")
        photo = make_photo(db_session, "z"*64)
        with_pic.profile_photo_id = photo.id
        db_session.commit()

        response = authenticated_client.get("/people")
        assert response.status_code == 200
        names = [p["name"] for p in response.json()["people"]]
        assert names.index("Zara") < names.index("Alice")

    def test_sort_order_alphabetical_within_group(self, authenticated_client, db_session):
        """Within the no-profile-picture group, persons sort alphabetically by name."""
        make_person(db_session, "Zara")
        make_person(db_session, "Alice")
        make_person(db_session, "Bob")
        db_session.commit()

        response = authenticated_client.get("/people")
        assert response.status_code == 200
        names = [p["name"] for p in response.json()["people"]]
        assert names == ["Alice", "Bob", "Zara"]

    def test_sort_order_stable_with_same_created_at(self, authenticated_client, db_session):
        """Persons with identical created_at values produce a deterministic order (by id)."""
        import datetime
        ts = datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc)
        persons = []
        for name in ["Charlie", "Alice", "Bob"]:
            p = Person(display_name=name, created_at=ts)
            db_session.add(p)
            persons.append(p)
        db_session.commit()

        response = authenticated_client.get("/people")
        assert response.status_code == 200
        names = [p["name"] for p in response.json()["people"]]
        # All have same created_at — must be alphabetical, then UUID as tiebreaker
        assert names == sorted(names)

class TestCreatePerson:
    def test_create_person(self, authenticated_client, db_session):
        with patch("app.routers.people.emit_loganne_event", new_callable=AsyncMock) as mock_emit, \
             patch("app.routers.people.fetch_contact_name", new_callable=AsyncMock, return_value=None):
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
            assert "Charlie" in mock_emit.call_args[0][1]

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

    def test_create_person_fetches_contact_name(self, authenticated_client, db_session):
        """When a contactId is supplied and fetch_contact_name succeeds, the returned name comes from contacts."""
        with patch("app.routers.people.emit_loganne_event", new_callable=AsyncMock), \
             patch("app.routers.people.fetch_contact_name", new_callable=AsyncMock, return_value="Alice From Contacts"):
            response = authenticated_client.post(
                "/people",
                json={"name": "Alice Original", "contactId": "alice-123"},
            )
            assert response.status_code == 201
            data = response.json()
            assert data["name"] == "Alice From Contacts"

    def test_create_person_falls_back_to_caller_name_when_fetch_fails(self, authenticated_client, db_session):
        """When fetch_contact_name returns None, the caller-supplied name is used."""
        with patch("app.routers.people.emit_loganne_event", new_callable=AsyncMock), \
             patch("app.routers.people.fetch_contact_name", new_callable=AsyncMock, return_value=None):
            response = authenticated_client.post(
                "/people",
                json={"name": "Alice Original", "contactId": "alice-123"},
            )
            assert response.status_code == 201
            assert response.json()["name"] == "Alice Original"

    def test_create_person_without_contact_id_skips_fetch(self, authenticated_client, db_session):
        """When no contactId is supplied, fetch_contact_name is never called."""
        with patch("app.routers.people.emit_loganne_event", new_callable=AsyncMock), \
             patch("app.routers.people.fetch_contact_name", new_callable=AsyncMock) as mock_fetch:
            response = authenticated_client.post(
                "/people",
                json={"name": "Charlie"},
            )
            assert response.status_code == 201
            mock_fetch.assert_not_called()

    def test_requires_authentication(self, client):
        # CSRF header required on write methods (aithne_session is SameSite=None);
        # with a valid CSRF header but no session cookie → 302 redirect to aithne login.
        response = client.post(
            "/people", json={"name": "Charlie"},
            headers={"Origin": "https://photos.l42.eu"},
            follow_redirects=False,
        )
        assert response.status_code == 302

class TestLinkPersonContact:
    def test_link_contact(self, authenticated_client, db_session):
        person = make_person(db_session, "Alice")
        db_session.commit()

        with patch("app.routers.people.emit_loganne_event", new_callable=AsyncMock) as mock_emit, \
             patch("app.routers.people.fetch_contact_name", new_callable=AsyncMock, return_value=None):
            response = authenticated_client.put(
                f"/people/{person.id}/contact",
                json={"contactId": "42"},
            )
            assert response.status_code == 200
            data = response.json()
            assert data["contactId"] == "42"
            mock_emit.assert_called_once()
            assert mock_emit.call_args[0][0] == "personContactLinked"
            assert "Alice" in mock_emit.call_args[0][1]

    def test_link_contact_overwrites_existing(self, authenticated_client, db_session):
        person = make_person(db_session, "Alice", contact_id="old-id")
        db_session.commit()

        with patch("app.routers.people.emit_loganne_event", new_callable=AsyncMock), \
             patch("app.routers.people.fetch_contact_name", new_callable=AsyncMock, return_value=None):
            response = authenticated_client.put(
                f"/people/{person.id}/contact",
                json={"contactId": "new-id"},
            )
            assert response.status_code == 200
            assert response.json()["contactId"] == "new-id"

    def test_link_contact_duplicate(self, authenticated_client, db_session):
        bob = make_person(db_session, "Bob", contact_id="shared-id")
        alice = make_person(db_session, "Alice")
        db_session.commit()

        with patch("app.routers.people.emit_loganne_event", new_callable=AsyncMock):
            response = authenticated_client.put(
                f"/people/{alice.id}/contact",
                json={"contactId": "shared-id"},
            )
            assert response.status_code == 409
            detail = response.json()["detail"]
            assert "already exists" in detail["message"]
            assert detail["existingPersonId"] == str(bob.id)

    def test_link_contact_missing_contact_id(self, authenticated_client, db_session):
        person = make_person(db_session, "Alice")
        db_session.commit()

        response = authenticated_client.put(
            f"/people/{person.id}/contact",
            json={},
        )
        assert response.status_code == 422

    def test_link_contact_person_not_found(self, authenticated_client):
        with patch("app.routers.people.emit_loganne_event", new_callable=AsyncMock):
            response = authenticated_client.put(
                f"/people/{uuid.uuid4()}/contact",
                json={"contactId": "42"},
            )
            assert response.status_code == 404

    def test_link_contact_updates_display_name_from_contacts(self, authenticated_client, db_session):
        """After linking, if fetch_contact_name succeeds the display_name is updated."""
        person = make_person(db_session, "Old Name")
        db_session.commit()

        with patch("app.routers.people.emit_loganne_event", new_callable=AsyncMock), \
             patch("app.routers.people.fetch_contact_name", new_callable=AsyncMock, return_value="Name From Contacts"):
            response = authenticated_client.put(
                f"/people/{person.id}/contact",
                json={"contactId": "42"},
            )
            assert response.status_code == 200
            assert response.json()["name"] == "Name From Contacts"

        db_session.refresh(person)
        assert person.display_name == "Name From Contacts"

    def test_link_contact_keeps_display_name_when_fetch_fails(self, authenticated_client, db_session):
        """If fetch_contact_name returns None, the existing display_name is not changed."""
        person = make_person(db_session, "Original Name")
        db_session.commit()

        with patch("app.routers.people.emit_loganne_event", new_callable=AsyncMock), \
             patch("app.routers.people.fetch_contact_name", new_callable=AsyncMock, return_value=None):
            response = authenticated_client.put(
                f"/people/{person.id}/contact",
                json={"contactId": "42"},
            )
            assert response.status_code == 200
            assert response.json()["name"] == "Original Name"

    def test_link_contact_logs_warning_when_fetch_fails(self, authenticated_client, db_session):
        """If fetch_contact_name returns None, a warning is logged so the failure is visible."""
        person = make_person(db_session, "Original Name")
        db_session.commit()

        with patch("app.routers.people.emit_loganne_event", new_callable=AsyncMock), \
             patch("app.routers.people.fetch_contact_name", new_callable=AsyncMock, return_value=None), \
             patch("app.routers.people.logging") as mock_logging:
            response = authenticated_client.put(
                f"/people/{person.id}/contact",
                json={"contactId": "42"},
            )
            assert response.status_code == 200
            mock_logging.warning.assert_called_once()
            warning_msg = mock_logging.warning.call_args[0][0]
            assert "failed to fetch name" in warning_msg

    def test_requires_authentication(self, client, db_session):
        person = make_person(db_session, "Alice")
        db_session.commit()
        response = client.put(
            f"/people/{person.id}/contact", json={"contactId": "42"},
            headers={"Origin": "https://photos.l42.eu"},
            follow_redirects=False,
        )
        assert response.status_code == 302


class TestUnlinkPersonContact:
    def test_unlink_contact(self, authenticated_client, db_session):
        person = make_person(db_session, "Alice", contact_id="42")
        db_session.commit()

        with patch("app.routers.people.emit_loganne_event", new_callable=AsyncMock) as mock_emit:
            response = authenticated_client.delete(f"/people/{person.id}/contact")
            assert response.status_code == 204
            mock_emit.assert_called_once()
            assert mock_emit.call_args[0][0] == "personContactUnlinked"
            assert "Alice" in mock_emit.call_args[0][1]

        db_session.refresh(person)
        assert person.contact_id is None

    def test_unlink_contact_when_not_linked(self, authenticated_client, db_session):
        person = make_person(db_session, "Alice")
        db_session.commit()

        with patch("app.routers.people.emit_loganne_event", new_callable=AsyncMock):
            response = authenticated_client.delete(f"/people/{person.id}/contact")
        assert response.status_code == 204

    def test_unlink_contact_person_not_found(self, authenticated_client):
        response = authenticated_client.delete(f"/people/{uuid.uuid4()}/contact")
        assert response.status_code == 404

    def test_requires_authentication(self, client, db_session):
        person = make_person(db_session, "Alice", contact_id="42")
        db_session.commit()
        response = client.delete(
            f"/people/{person.id}/contact",
            headers={"Origin": "https://photos.l42.eu"},
            follow_redirects=False,
        )
        assert response.status_code == 302


class TestGetPerson:
    def test_get_person(self, authenticated_client, db_session):
        person = make_person(db_session, "Alice", contact_id="alice-123")
        photo = make_photo(db_session)
        db_session.add(PhotoPerson(photo_id=photo.id, person_id=person.id))
        db_session.add(ProcessingStatus(photo_id=photo.id, state=ProcessingState.complete))

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
        assert data["photos"][0]["faceId"] == str(face.id)

    def test_get_person_not_found(self, authenticated_client):
        response = authenticated_client.get(f"/people/{uuid.uuid4()}")
        assert response.status_code == 404

    def test_get_person_invalid_uuid(self, authenticated_client):
        response = authenticated_client.get("/people/not-a-uuid")
        assert response.status_code == 404

    def test_requires_authentication(self, client, db_session):
        person = make_person(db_session, "Alice")
        db_session.commit()
        response = client.get(f"/people/{person.id}", follow_redirects=False)
        assert response.status_code == 302

    def test_returns_html_when_browser_accept_header(self, authenticated_client, db_session):
        person = make_person(db_session, "Alice")
        db_session.commit()

        response = authenticated_client.get(f"/people/{person.id}", headers={"Accept": "text/html"})
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert b"<!DOCTYPE html>" in response.content
        assert b"Alice" in response.content

    def test_returns_json_when_json_accept_header(self, authenticated_client, db_session):
        person = make_person(db_session, "Alice")
        db_session.commit()

        response = authenticated_client.get(f"/people/{person.id}", headers={"Accept": "application/json"})
        assert response.status_code == 200
        assert "application/json" in response.headers["content-type"]
        assert response.json()["name"] == "Alice"

    def test_returns_json_when_no_accept_header(self, authenticated_client, db_session):
        """With no Accept header (*/*), JSON should be returned as the default."""
        person = make_person(db_session, "Alice")
        db_session.commit()

        response = authenticated_client.get(f"/people/{person.id}")
        assert response.status_code == 200
        assert "application/json" in response.headers["content-type"]

    def test_html_shows_person_photos(self, authenticated_client, db_session):
        person = make_person(db_session, "Alice")
        photo = make_photo(db_session, "a" * 63 + "b")
        db_session.add(PhotoPerson(photo_id=photo.id, person_id=person.id))
        db_session.add(ProcessingStatus(photo_id=photo.id, state=ProcessingState.complete))
        db_session.commit()

        response = authenticated_client.get(f"/people/{person.id}", headers={"Accept": "text/html"})
        assert response.status_code == 200
        assert str(photo.id).encode() in response.content

    def test_filters_out_unprocessed_photos(self, authenticated_client, db_session):
        """Photos without a complete processing status should not appear in the per-person grid."""
        person = make_person(db_session, "Alice")
        complete_photo = make_photo(db_session, "c" * 64)
        pending_photo = make_photo(db_session, "d" * 64)
        db_session.add(PhotoPerson(photo_id=complete_photo.id, person_id=person.id))
        db_session.add(PhotoPerson(photo_id=pending_photo.id, person_id=person.id))
        db_session.add(ProcessingStatus(photo_id=complete_photo.id, state=ProcessingState.complete))
        db_session.add(ProcessingStatus(photo_id=pending_photo.id, state=ProcessingState.pending))
        db_session.commit()

        response = authenticated_client.get(f"/people/{person.id}")
        assert response.status_code == 200
        data = response.json()
        photo_ids = [p["id"] for p in data["photos"]]
        assert str(complete_photo.id) in photo_ids
        assert str(pending_photo.id) not in photo_ids


class TestGetPersonProfilePicture:
    def test_returns_404_when_no_profile_picture(self, authenticated_client, db_session):
        person = make_person(db_session, "Alice")
        db_session.commit()

        with patch("app.routers.people.DERIVATIVES_DIR") as mock_dir:
            mock_path = mock_dir.__truediv__.return_value
            mock_path.exists.return_value = False
            response = authenticated_client.get(f"/people/{person.id}/profile-picture")

        assert response.status_code == 404

    def test_returns_jpeg_when_profile_picture_exists(self, authenticated_client, db_session, tmp_path):
        person = make_person(db_session, "Alice")
        db_session.commit()

        derivatives_dir = tmp_path / "derivatives"
        derivatives_dir.mkdir()
        profile_path = derivatives_dir / f"{person.id}_profile.jpg"
        img = Image.new("RGB", (80, 80), color=(100, 150, 200))
        img.save(profile_path, format="JPEG")

        with patch("app.routers.people.DERIVATIVES_DIR", derivatives_dir):
            response = authenticated_client.get(f"/people/{person.id}/profile-picture")

        assert response.status_code == 200
        assert response.headers["content-type"] == "image/jpeg"

    def test_returns_404_for_invalid_uuid(self, authenticated_client):
        response = authenticated_client.get("/people/not-a-uuid/profile-picture")
        assert response.status_code == 404

    def test_requires_authentication(self, client, db_session):
        person = make_person(db_session, "Alice")
        db_session.commit()
        response = client.get(f"/people/{person.id}/profile-picture", follow_redirects=False)
        assert response.status_code == 302


class TestListPeopleIncludesProfilePictureUrl:
    def test_profile_picture_url_null_when_no_file(self, authenticated_client, db_session):
        make_person(db_session, "Alice")
        db_session.commit()

        with patch("app.serializers.DERIVATIVES_DIR") as mock_dir:
            mock_path = mock_dir.__truediv__.return_value
            mock_path.exists.return_value = False
            response = authenticated_client.get("/people")

        assert response.status_code == 200
        data = response.json()["people"]
        assert data[0]["profilePictureUrl"] is None

    def test_profile_picture_url_set_when_file_exists(self, authenticated_client, db_session, tmp_path):
        person = make_person(db_session, "Alice")
        db_session.commit()

        derivatives_dir = tmp_path / "derivatives"
        derivatives_dir.mkdir()
        profile_path = derivatives_dir / f"{person.id}_profile.jpg"
        profile_path.write_bytes(b"fake jpeg")

        with patch("app.serializers.DERIVATIVES_DIR", derivatives_dir):
            response = authenticated_client.get("/people")

        assert response.status_code == 200
        data = response.json()["people"]
        assert data[0]["profilePictureUrl"] is not None
        assert f"/people/{person.id}/profile-picture" in data[0]["profilePictureUrl"]


class TestMergePeople:
    def test_merge_two_people(self, authenticated_client, db_session):
        """Merging two people combines their faces and deletes the loser."""
        with patch("app.routers.people._enqueue_profile_picture"), \
             patch("app.routers.people.emit_loganne_event", new_callable=AsyncMock):
            person_a = make_person(db_session, "Alice")
            person_b = make_person(db_session, "Bob")
            db_session.commit()

            response = authenticated_client.post("/people/merge", json={"personIds": [str(person_a.id), str(person_b.id)]})

        assert response.status_code == 200
        data = response.json()
        assert "mergedPersonId" in data
        # One of the persons should survive
        assert data["mergedPersonId"] in [str(person_a.id), str(person_b.id)]
        # The other should be deleted
        surviving_id = uuid.UUID(data["mergedPersonId"])
        deleted_id = person_b.id if surviving_id == person_a.id else person_a.id
        assert db_session.query(Person).filter(Person.id == deleted_id).first() is None

    def test_merge_loganne_message_uses_names(self, authenticated_client, db_session):
        """peopleMerged loganne message should use names, not IDs."""
        with patch("app.routers.people._enqueue_profile_picture"), \
             patch("app.routers.people.emit_loganne_event", new_callable=AsyncMock) as mock_emit:
            person_a = make_person(db_session, "Alice")
            person_b = make_person(db_session, "Bob")
            db_session.commit()

            authenticated_client.post("/people/merge", json={"personIds": [str(person_a.id), str(person_b.id)]})

        assert mock_emit.call_args[0][0] == "peopleMerged"
        message = mock_emit.call_args[0][1]
        assert "Alice" in message or "Bob" in message
        # The loser's name and winner's name should both appear
        assert "Alice" in message
        assert "Bob" in message

    def test_merge_keeps_person_with_contact(self, authenticated_client, db_session):
        """When one person has a contact link, they should be the winner."""
        with patch("app.routers.people._enqueue_profile_picture"), \
             patch("app.routers.people.emit_loganne_event", new_callable=AsyncMock):
            person_a = make_person(db_session, "Alice", contact_id="42")
            person_b = make_person(db_session, "Bob")
            db_session.commit()

            response = authenticated_client.post("/people/merge", json={"personIds": [str(person_b.id), str(person_a.id)]})

        assert response.status_code == 200
        assert response.json()["mergedPersonId"] == str(person_a.id)
        assert db_session.query(Person).filter(Person.id == person_b.id).first() is None

    def test_merge_reassigns_faces(self, authenticated_client, db_session):
        """All faces from the loser should be reassigned to the winner."""
        with patch("app.routers.people._enqueue_profile_picture"), \
             patch("app.routers.people.emit_loganne_event", new_callable=AsyncMock):
            person_a = make_person(db_session, "Alice")
            person_b = make_person(db_session, "Bob")
            photo = make_photo(db_session, "b" * 64)
            face = Face(photo_id=photo.id, person_id=person_b.id, person_confirmed=False,
                        bbox_x=0.1, bbox_y=0.1, bbox_width=0.2, bbox_height=0.2)
            db_session.add(face)
            db_session.commit()

            response = authenticated_client.post("/people/merge", json={"personIds": [str(person_a.id), str(person_b.id)]})

        assert response.status_code == 200
        winner_id = uuid.UUID(response.json()["mergedPersonId"])
        db_session.expire_all()
        reassigned = db_session.query(Face).filter(Face.id == face.id).first()
        assert reassigned.person_id == winner_id

    def test_merge_marks_reassigned_faces_as_confirmed(self, authenticated_client, db_session):
        """Merged faces must be marked person_confirmed=True so they survive reprocessing."""
        with patch("app.routers.people._enqueue_profile_picture"), \
             patch("app.routers.people.emit_loganne_event", new_callable=AsyncMock):
            person_a = make_person(db_session, "Alice")
            person_b = make_person(db_session, "Bob")
            photo = make_photo(db_session, "b" * 64)
            face = Face(photo_id=photo.id, person_id=person_b.id, person_confirmed=False,
                        bbox_x=0.1, bbox_y=0.1, bbox_width=0.2, bbox_height=0.2)
            db_session.add(face)
            db_session.commit()

            response = authenticated_client.post("/people/merge", json={"personIds": [str(person_a.id), str(person_b.id)]})

        assert response.status_code == 200
        db_session.expire_all()
        reassigned = db_session.query(Face).filter(Face.id == face.id).first()
        assert reassigned.person_confirmed is True

    def test_merge_conflict_both_have_contacts(self, authenticated_client, db_session):
        """Merging two contact-linked people should return 409."""
        person_a = make_person(db_session, "Alice", contact_id="42")
        person_b = make_person(db_session, "Bob", contact_id="99")
        db_session.commit()

        response = authenticated_client.post("/people/merge", json={"personIds": [str(person_a.id), str(person_b.id)]})

        assert response.status_code == 409
        # Both persons should still exist
        assert db_session.query(Person).filter(Person.id == person_a.id).first() is not None
        assert db_session.query(Person).filter(Person.id == person_b.id).first() is not None

    def test_merge_person_not_found(self, authenticated_client, db_session):
        """A non-existent person ID should return 404."""
        person_a = make_person(db_session, "Alice")
        db_session.commit()
        fake_id = str(uuid.uuid4())

        response = authenticated_client.post("/people/merge", json={"personIds": [str(person_a.id), fake_id]})

        assert response.status_code == 404

    def test_merge_requires_at_least_two(self, authenticated_client, db_session):
        """Fewer than 2 IDs should return 422."""
        person_a = make_person(db_session, "Alice")
        db_session.commit()

        response = authenticated_client.post("/people/merge", json={"personIds": [str(person_a.id)]})
        assert response.status_code == 422

    def test_merge_invalid_uuid(self, authenticated_client, db_session):
        """Invalid UUID in the list should return 422."""
        person_a = make_person(db_session, "Alice")
        db_session.commit()

        response = authenticated_client.post("/people/merge", json={"personIds": [str(person_a.id), "not-a-uuid"]})
        assert response.status_code == 422

    def test_merge_requires_authentication(self, client, db_session):
        """Merge endpoint should require authentication."""
        response = client.post(
            "/people/merge", json={"personIds": ["a" * 32, "b" * 32]},
            headers={"Origin": "https://photos.l42.eu"},
            follow_redirects=False,
        )
        assert response.status_code == 302

    def test_merge_three_people(self, authenticated_client, db_session):
        """Merging three people should delete two and keep one."""
        with patch("app.routers.people._enqueue_profile_picture"), \
             patch("app.routers.people.emit_loganne_event", new_callable=AsyncMock):
            person_a = make_person(db_session, "Alice")
            person_b = make_person(db_session, "Bob")
            person_c = make_person(db_session, "Carol")
            db_session.commit()

            response = authenticated_client.post("/people/merge", json={
                "personIds": [str(person_a.id), str(person_b.id), str(person_c.id)]
            })

        assert response.status_code == 200
        winner_id = uuid.UUID(response.json()["mergedPersonId"])
        # Only the winner survives
        remaining = db_session.query(Person).all()
        assert len(remaining) == 1
        assert remaining[0].id == winner_id


class TestMarkBackgroundFace:
    def test_mark_background(self, authenticated_client, db_session):
        person = make_person(db_session, "Background Bob")
        db_session.commit()

        with patch("app.routers.people.emit_loganne_event", new_callable=AsyncMock) as mock_emit:
            response = authenticated_client.put(f"/people/{person.id}/background")

        assert response.status_code == 200
        data = response.json()
        assert data["isBackground"] is True
        db_session.refresh(person)
        assert person.is_background is True
        mock_emit.assert_called_once()
        assert mock_emit.call_args[0][0] == "personMarkedBackground"

    def test_mark_background_person_not_found(self, authenticated_client):
        with patch("app.routers.people.emit_loganne_event", new_callable=AsyncMock):
            response = authenticated_client.put(f"/people/{uuid.uuid4()}/background")
        assert response.status_code == 404

    def test_mark_background_invalid_uuid(self, authenticated_client):
        response = authenticated_client.put("/people/not-a-uuid/background")
        assert response.status_code == 404

    def test_mark_background_requires_authentication(self, client, db_session):
        person = make_person(db_session, "Alice")
        db_session.commit()
        response = client.put(
            f"/people/{person.id}/background",
            headers={"Origin": "https://photos.l42.eu"},
            follow_redirects=False,
        )
        assert response.status_code == 302

    def test_unmark_background(self, authenticated_client, db_session):
        person = Person(display_name="Bob", is_background=True)
        db_session.add(person)
        db_session.commit()

        with patch("app.routers.people.emit_loganne_event", new_callable=AsyncMock) as mock_emit:
            response = authenticated_client.delete(f"/people/{person.id}/background")

        assert response.status_code == 200
        data = response.json()
        assert data["isBackground"] is False
        db_session.refresh(person)
        assert person.is_background is False
        mock_emit.assert_called_once()
        assert mock_emit.call_args[0][0] == "personUnmarkedBackground"

    def test_unmark_background_person_not_found(self, authenticated_client):
        with patch("app.routers.people.emit_loganne_event", new_callable=AsyncMock):
            response = authenticated_client.delete(f"/people/{uuid.uuid4()}/background")
        assert response.status_code == 404

    def test_unmark_background_requires_authentication(self, client, db_session):
        person = make_person(db_session, "Alice")
        db_session.commit()
        response = client.delete(
            f"/people/{person.id}/background",
            headers={"Origin": "https://photos.l42.eu"},
            follow_redirects=False,
        )
        assert response.status_code == 302

    def test_background_people_hidden_from_list(self, authenticated_client, db_session):
        make_person(db_session, "Visible")
        bg_person = Person(display_name="Background Bob", is_background=True)
        db_session.add(bg_person)
        db_session.commit()

        response = authenticated_client.get("/people")
        assert response.status_code == 200
        body = response.json()
        names = [p["name"] for p in body["people"]]
        assert "Visible" in names
        assert "Background Bob" not in names

    def test_background_people_excluded_from_total(self, authenticated_client, db_session):
        make_person(db_session, "Visible")
        bg_person = Person(display_name="Background Bob", is_background=True)
        db_session.add(bg_person)
        db_session.commit()

        response = authenticated_client.get("/people")
        assert response.status_code == 200
        assert response.json()["total"] == 1

    def test_background_person_still_has_detail_page(self, authenticated_client, db_session):
        person = Person(display_name="Background Bob", is_background=True)
        db_session.add(person)
        db_session.commit()

        response = authenticated_client.get(f"/people/{person.id}")
        assert response.status_code == 200
        assert response.json()["isBackground"] is True

    def test_is_background_false_by_default(self, authenticated_client, db_session):
        person = make_person(db_session, "Alice")
        db_session.commit()

        response = authenticated_client.get(f"/people/{person.id}")
        assert response.status_code == 200
        assert response.json()["isBackground"] is False

    def test_serializer_includes_is_background(self, authenticated_client, db_session):
        make_person(db_session, "Alice")
        db_session.commit()

        response = authenticated_client.get("/people")
        assert response.status_code == 200
        person_data = response.json()["people"][0]
        assert "isBackground" in person_data


class TestFlagPerson:
    """Tests for the flag/unflag endpoints (lucas42/lucos_photos#471) — a standing
    "this grouping needs attention" marker, manually set and cleared, mirroring the
    is_background toggle pair exactly."""

    def test_flag_person(self, authenticated_client, db_session):
        person = make_person(db_session, "Wrongly Grouped")
        db_session.commit()

        with patch("app.routers.people.emit_loganne_event", new_callable=AsyncMock) as mock_emit:
            response = authenticated_client.put(f"/people/{person.id}/flag")

        assert response.status_code == 200
        data = response.json()
        assert data["flaggedAt"] is not None
        db_session.refresh(person)
        assert person.flagged_at is not None
        mock_emit.assert_called_once()
        assert mock_emit.call_args[0][0] == "personFlagged"

    def test_flag_person_not_found(self, authenticated_client):
        with patch("app.routers.people.emit_loganne_event", new_callable=AsyncMock):
            response = authenticated_client.put(f"/people/{uuid.uuid4()}/flag")
        assert response.status_code == 404

    def test_flag_person_invalid_uuid(self, authenticated_client):
        response = authenticated_client.put("/people/not-a-uuid/flag")
        assert response.status_code == 404

    def test_flag_person_requires_authentication(self, client, db_session):
        person = make_person(db_session, "Alice")
        db_session.commit()
        response = client.put(
            f"/people/{person.id}/flag",
            headers={"Origin": "https://photos.l42.eu"},
            follow_redirects=False,
        )
        assert response.status_code == 302

    def test_unflag_person(self, authenticated_client, db_session):
        from datetime import datetime, timezone
        person = Person(display_name="Bob", flagged_at=datetime.now(timezone.utc))
        db_session.add(person)
        db_session.commit()

        with patch("app.routers.people.emit_loganne_event", new_callable=AsyncMock) as mock_emit:
            response = authenticated_client.delete(f"/people/{person.id}/flag")

        assert response.status_code == 200
        data = response.json()
        assert data["flaggedAt"] is None
        db_session.refresh(person)
        assert person.flagged_at is None
        mock_emit.assert_called_once()
        assert mock_emit.call_args[0][0] == "personUnflagged"

    def test_unflag_person_not_found(self, authenticated_client):
        with patch("app.routers.people.emit_loganne_event", new_callable=AsyncMock):
            response = authenticated_client.delete(f"/people/{uuid.uuid4()}/flag")
        assert response.status_code == 404

    def test_unflag_person_requires_authentication(self, client, db_session):
        person = make_person(db_session, "Alice")
        db_session.commit()
        response = client.delete(
            f"/people/{person.id}/flag",
            headers={"Origin": "https://photos.l42.eu"},
            follow_redirects=False,
        )
        assert response.status_code == 302

    def test_serializer_includes_flagged_at(self, authenticated_client, db_session):
        make_person(db_session, "Alice")
        db_session.commit()

        response = authenticated_client.get("/people")
        assert response.status_code == 200
        person_data = response.json()["people"][0]
        assert "flaggedAt" in person_data
        assert person_data["flaggedAt"] is None


class TestCreatePersonFromFaces:
    """Tests for POST /people extended with an optional faceIds seed — the "move to
    new group" path of lucas42/lucos_photos#471."""

    def _make_face(self, db, photo, person=None):
        face = Face(
            photo_id=photo.id,
            person_id=person.id if person else None,
            person_confirmed=False,
            bbox_x=0.1, bbox_y=0.1, bbox_width=0.4, bbox_height=0.4,
        )
        db.add(face)
        db.flush()
        return face

    def test_creates_person_from_faces_with_name(self, authenticated_client, db_session):
        photo = make_photo(db_session, "e1" * 32)
        other_photo = Photo(sha256_hash="e5" * 32, file_extension="jpg")
        db_session.add(other_photo)
        db_session.flush()
        source = make_person(db_session, "Source")
        face = self._make_face(db_session, photo, source)
        self._make_face(db_session, other_photo, source)  # keeps source non-empty after the move
        db_session.commit()

        with patch("app.routers.people.emit_loganne_event", new_callable=AsyncMock) as mock_emit, \
             patch("app.routers.people._enqueue_profile_picture") as mock_enqueue:
            response = authenticated_client.post("/people", json={"name": "New Group", "faceIds": [str(face.id)]})

        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "New Group"

        db_session.refresh(face)
        assert str(face.person_id) == data["id"]
        assert face.person_confirmed is True

        mock_emit.assert_called_once()
        assert mock_emit.call_args[0][0] == "peopleSplit"
        # Regen enqueued for both the new person and the (surviving) source
        enqueued_ids = {c.args[0] for c in mock_enqueue.call_args_list}
        assert data["id"] in enqueued_ids
        assert str(source.id) in enqueued_ids

    def test_creates_unnamed_person_from_faces(self, authenticated_client, db_session):
        """An unnamed group is allowed specifically when faceIds are provided —
        "Create new (unnamed) person" in the move-lightbox."""
        photo = make_photo(db_session, "e2" * 32)
        face = self._make_face(db_session, photo)
        db_session.commit()

        with patch("app.routers.people.emit_loganne_event", new_callable=AsyncMock), \
             patch("app.routers.people._enqueue_profile_picture"):
            response = authenticated_client.post("/people", json={"faceIds": [str(face.id)]})

        assert response.status_code == 201
        assert response.json()["name"] is None

    def test_still_requires_name_without_face_ids(self, authenticated_client):
        response = authenticated_client.post("/people", json={})
        assert response.status_code == 422

    def test_deletes_emptied_unlinked_source(self, authenticated_client, db_session):
        """If the source person is left with zero faces and has no contact link, it's
        deleted as part of the move (lucas42/lucos_photos#471's emptied-source policy)."""
        photo = make_photo(db_session, "e3" * 32)
        source = make_person(db_session, "Source")
        face = self._make_face(db_session, photo, source)
        db_session.commit()
        source_id = source.id

        with patch("app.routers.people.emit_loganne_event", new_callable=AsyncMock), \
             patch("app.routers.people._enqueue_profile_picture"):
            response = authenticated_client.post("/people", json={"name": "New Group", "faceIds": [str(face.id)]})

        assert response.status_code == 201
        assert db_session.query(Person).filter(Person.id == source_id).first() is None

    def test_keeps_emptied_contact_linked_source(self, authenticated_client, db_session):
        photo = make_photo(db_session, "e4" * 32)
        source = make_person(db_session, "Source", contact_id="contact-123")
        face = self._make_face(db_session, photo, source)
        db_session.commit()
        source_id = source.id

        with patch("app.routers.people.emit_loganne_event", new_callable=AsyncMock), \
             patch("app.routers.people._enqueue_profile_picture"):
            response = authenticated_client.post("/people", json={"name": "New Group", "faceIds": [str(face.id)]})

        assert response.status_code == 201
        surviving = db_session.query(Person).filter(Person.id == source_id).first()
        assert surviving is not None

    def test_face_not_found(self, authenticated_client):
        response = authenticated_client.post("/people", json={"name": "New Group", "faceIds": [str(uuid.uuid4())]})
        assert response.status_code == 404
