import uuid
from unittest.mock import patch

from lucos_photos_common.models import Face, MediaItem, Person, PhotoPerson, ProcessingState, ProcessingStatus

A = [1.0, 0.0, 0.0]
B = [0.0, 1.0, 0.0]
NEAR_A = [0.95, 0.1, 0.0]


def make_photo(db, tag="a", state=ProcessingState.complete):
    photo = MediaItem(sha256_hash=(tag * 64)[:64], file_extension="jpg")
    db.add(photo)
    db.flush()
    if state is not None:
        db.add(ProcessingStatus(photo_id=photo.id, state=state))
    db.flush()
    return photo


def make_face(db, photo, embedding=None, person=None, confirmed=False):
    face = Face(
        photo_id=photo.id, person_id=person.id if person else None, person_confirmed=confirmed,
        bbox_x=0.1, bbox_y=0.1, bbox_width=0.2, bbox_height=0.2, embedding=embedding,
    )
    db.add(face)
    db.flush()
    return face


def make_person(db, name):
    person = Person(display_name=name)
    db.add(person)
    db.flush()
    return person


def prename(client, photo, tier, names):
    with patch("app.routers.faces._enqueue_profile_picture"):
        return client.post(f"/photos/{photo.id}/prename", json={"tier": tier, "names": names})


class TestTier1:
    def test_creates_person_and_confirms_single_face(self, authenticated_client, db_session):
        photo = make_photo(db_session)
        face = make_face(db_session, photo)
        db_session.commit()

        response = prename(authenticated_client, photo, 1, ["Alice Smith"])

        assert response.status_code == 200
        assert response.json()["assigned"] == 1
        db_session.expire_all()
        face = db_session.query(Face).one()
        assert face.person_confirmed is True
        assert face.person.display_name == "Alice Smith"
        assert db_session.query(PhotoPerson).filter_by(photo_id=photo.id, person_id=face.person_id).count() == 1

    def test_reuses_existing_person_case_insensitively(self, authenticated_client, db_session):
        existing = make_person(db_session, "alice smith")
        photo = make_photo(db_session)
        make_face(db_session, photo)
        db_session.commit()

        prename(authenticated_client, photo, 1, ["Alice Smith"])

        db_session.expire_all()
        assert db_session.query(Person).count() == 1
        assert db_session.query(Face).one().person_id == existing.id

    def test_skips_when_not_exactly_one_face(self, authenticated_client, db_session):
        photo = make_photo(db_session)
        make_face(db_session, photo)
        make_face(db_session, photo)
        db_session.commit()

        response = prename(authenticated_client, photo, 1, ["Alice"])

        assert response.json()["assigned"] == 0
        assert db_session.query(Person).count() == 0

    def test_skips_when_multiple_names(self, authenticated_client, db_session):
        photo = make_photo(db_session)
        make_face(db_session, photo)
        db_session.commit()

        assert prename(authenticated_client, photo, 1, ["Alice", "Bob"]).json()["assigned"] == 0

    def test_duplicate_spellings_count_as_one_name(self, authenticated_client, db_session):
        photo = make_photo(db_session)
        make_face(db_session, photo)
        db_session.commit()

        assert prename(authenticated_client, photo, 1, ["Alice", " alice "]).json()["assigned"] == 1

    def test_does_not_override_a_confirmed_face(self, authenticated_client, db_session):
        bob = make_person(db_session, "Bob")
        photo = make_photo(db_session)
        make_face(db_session, photo, person=bob, confirmed=True)
        db_session.commit()

        response = prename(authenticated_client, photo, 1, ["Alice"])

        assert response.json()["assigned"] == 0
        db_session.expire_all()
        assert db_session.query(Face).one().person_id == bob.id

    def test_overrides_ml_guess_and_removes_emptied_person(self, authenticated_client, db_session):
        guess = make_person(db_session, None)
        photo = make_photo(db_session)
        face = make_face(db_session, photo, person=guess, confirmed=False)
        db_session.add(PhotoPerson(photo_id=photo.id, person_id=guess.id))
        db_session.commit()

        prename(authenticated_client, photo, 1, ["Alice"])

        db_session.expire_all()
        assert db_session.query(Person).filter_by(id=guess.id).first() is None
        assert db_session.query(Face).one().person.display_name == "Alice"

    def test_enqueues_profile_picture(self, authenticated_client, db_session):
        photo = make_photo(db_session)
        make_face(db_session, photo)
        db_session.commit()

        with patch("app.routers.faces._enqueue_profile_picture") as enqueue:
            authenticated_client.post(f"/photos/{photo.id}/prename", json={"tier": 1, "names": ["Alice"]})

        enqueue.assert_called_once()


class TestTier2:
    def _seed(self, db):
        alice, bob = make_person(db, "Alice"), make_person(db, "Bob")
        earlier = make_photo(db, "e")
        make_face(db, earlier, A, person=alice, confirmed=True)
        make_face(db, earlier, B, person=bob, confirmed=True)
        return alice, bob

    def test_assigns_faces_to_closest_listed_people(self, authenticated_client, db_session):
        alice, bob = self._seed(db_session)
        photo = make_photo(db_session, "b")
        fb = make_face(db_session, photo, B)
        fa = make_face(db_session, photo, NEAR_A)
        db_session.commit()

        response = prename(authenticated_client, photo, 2, ["Alice", "Bob"])

        assert response.json()["assigned"] == 2
        db_session.expire_all()
        assert db_session.get(Face, fa.id).person_id == alice.id
        assert db_session.get(Face, fb.id).person_id == bob.id
        assert db_session.get(Face, fa.id).person_confirmed is False

    def test_leaves_face_unassigned_when_over_threshold(self, authenticated_client, db_session):
        self._seed(db_session)
        photo = make_photo(db_session, "b")
        face = make_face(db_session, photo, [0.0, 0.0, 1.0])
        db_session.commit()

        response = prename(authenticated_client, photo, 2, ["Alice", "Bob"])

        assert response.json()["assigned"] == 0
        db_session.expire_all()
        assert db_session.get(Face, face.id).person_id is None

    def test_only_considers_the_names_listed_on_the_photo(self, authenticated_client, db_session):
        alice, _ = self._seed(db_session)
        carol = make_person(db_session, "Carol")
        make_face(db_session, make_photo(db_session, "c"), [0.0, 0.0, 1.0], person=carol, confirmed=True)
        photo = make_photo(db_session, "b")
        face = make_face(db_session, photo, [0.0, 0.0, 1.0])
        db_session.commit()

        prename(authenticated_client, photo, 2, ["Alice", "Bob"])

        db_session.expire_all()
        assert db_session.get(Face, face.id).person_id is None

    def test_each_person_is_used_at_most_once(self, authenticated_client, db_session):
        alice, _ = self._seed(db_session)
        photo = make_photo(db_session, "b")
        make_face(db_session, photo, A)
        make_face(db_session, photo, NEAR_A)
        db_session.commit()

        assert prename(authenticated_client, photo, 2, ["Alice", "Bob"]).json()["assigned"] == 1

    def test_ignores_unconfirmed_faces_when_building_centroids(self, authenticated_client, db_session):
        alice, bob = make_person(db_session, "Alice"), make_person(db_session, "Bob")
        make_face(db_session, make_photo(db_session, "e"), A, person=alice, confirmed=False)
        photo = make_photo(db_session, "b")
        make_face(db_session, photo, A)
        db_session.commit()

        assert prename(authenticated_client, photo, 2, ["Alice", "Bob"]).json()["assigned"] == 0


class TestEndpoint:
    def test_409_until_photo_is_processed(self, authenticated_client, db_session):
        photo = make_photo(db_session, state=ProcessingState.pending)
        db_session.commit()

        response = prename(authenticated_client, photo, 1, ["Alice"])

        assert response.status_code == 409
        assert response.json()["processingStatus"] == "pending"

    def test_404_for_unknown_photo(self, authenticated_client):
        response = authenticated_client.post(f"/photos/{uuid.uuid4()}/prename", json={"tier": 1, "names": ["A"]})
        assert response.status_code == 404

    def test_422_for_bad_body(self, authenticated_client, db_session):
        photo = make_photo(db_session)
        db_session.commit()
        assert prename(authenticated_client, photo, 3, ["A"]).status_code == 422
        assert prename(authenticated_client, photo, 1, "A").status_code == 422
