"""Negative face labels: "not this person" / "not a face" (lucas42/lucos_photos#622)."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from lucos_photos_common.models import Face, FacePersonRejection, Person, Photo, PhotoPerson


@pytest.fixture(autouse=True)
def no_side_effects():
    with patch("app.routers.faces.emit_loganne_event", new=AsyncMock()), \
         patch("app.routers.faces._enqueue_profile_picture"), \
         patch("app.routers.people.updateLoganne"), \
         patch("app.routers.people._enqueue_profile_picture"):
        yield


def make_photo(db, n=0):
    photo = Photo(sha256_hash=f"{n:064x}", file_extension="jpg")
    db.add(photo)
    db.flush()
    return photo


def make_person(db, name="Alice", **kwargs):
    person = Person(display_name=name, **kwargs)
    db.add(person)
    db.flush()
    return person


def make_face(db, photo, person=None, confirmed=False):
    face = Face(
        photo_id=photo.id, person_id=person.id if person else None, person_confirmed=confirmed,
        bbox_x=0.1, bbox_y=0.2, bbox_width=0.3, bbox_height=0.4,
    )
    db.add(face)
    db.flush()
    if person:
        db.flush()
    return face


def rejections(db):
    return {(r.face_id, r.person_id) for r in db.query(FacePersonRejection)}


class TestNotPerson:
    def test_records_rejection_and_detaches(self, authenticated_client, db_session):
        alice = make_person(db_session)
        f1 = make_face(db_session, make_photo(db_session, 1), alice, confirmed=True)
        f2 = make_face(db_session, make_photo(db_session, 2), alice)
        keep = make_face(db_session, make_photo(db_session, 3), alice)
        db_session.commit()

        r = authenticated_client.post("/faces/not-person", json={"faceIds": [str(f1.id), str(f2.id)], "personId": str(alice.id)})

        assert r.status_code == 200
        assert {c["faceId"] for c in r.json()["changed"]} == {str(f1.id), str(f2.id)}
        assert all(c["previousPersonId"] == str(alice.id) for c in r.json()["changed"])
        db_session.expire_all()
        assert rejections(db_session) == {(f1.id, alice.id), (f2.id, alice.id)}
        assert db_session.get(Face, f1.id).person_id is None
        assert db_session.get(Face, f1.id).person_confirmed is False
        assert db_session.get(Face, keep.id).person_id == alice.id
        assert db_session.query(PhotoPerson).filter_by(photo_id=f1.photo_id).count() == 0
        assert db_session.get(Person, alice.id).emptied_at is None

    def test_409_when_any_face_is_not_on_the_person_and_nothing_changes(self, authenticated_client, db_session):
        alice, bob = make_person(db_session), make_person(db_session, "Bob")
        f1 = make_face(db_session, make_photo(db_session, 1), alice)
        f2 = make_face(db_session, make_photo(db_session, 2), bob)
        db_session.commit()

        r = authenticated_client.post("/faces/not-person", json={"faceIds": [str(f1.id), str(f2.id)], "personId": str(alice.id)})

        assert r.status_code == 409
        db_session.expire_all()
        assert rejections(db_session) == set()
        assert db_session.get(Face, f1.id).person_id == alice.id

    def test_emptied_person_is_kept_with_emptied_at(self, authenticated_client, db_session):
        alice = make_person(db_session)
        f1 = make_face(db_session, make_photo(db_session, 1), alice)
        db_session.commit()

        authenticated_client.post("/faces/not-person", json={"faceIds": [str(f1.id)], "personId": str(alice.id)})

        db_session.expire_all()
        assert db_session.get(Person, alice.id).emptied_at is not None

    def test_regeneration_is_enqueued_for_an_emptied_person(self, authenticated_client, db_session):
        alice = make_person(db_session)
        linked = make_person(db_session, "Linked", contact_id="42")
        f1 = make_face(db_session, make_photo(db_session, 1), alice)
        f2 = make_face(db_session, make_photo(db_session, 2), linked)
        db_session.commit()

        with patch("app.routers.faces._enqueue_profile_picture") as enqueue:
            authenticated_client.post("/faces/not-person", json={"faceIds": [str(f1.id)], "personId": str(alice.id)})
            authenticated_client.post("/faces/not-a-face", json={"faceIds": [str(f2.id)]})

        assert {c.args[0] for c in enqueue.call_args_list} == {str(alice.id), str(linked.id)}

    def test_contact_linked_person_is_not_marked_emptied(self, authenticated_client, db_session):
        alice = make_person(db_session, contact_id="42")
        f1 = make_face(db_session, make_photo(db_session, 1), alice)
        db_session.commit()

        authenticated_client.post("/faces/not-person", json={"faceIds": [str(f1.id)], "personId": str(alice.id)})

        db_session.expire_all()
        assert db_session.get(Person, alice.id).emptied_at is None

    def test_delete_route_records_a_rejection(self, authenticated_client, db_session):
        alice = make_person(db_session)
        f1 = make_face(db_session, make_photo(db_session, 1), alice)
        make_face(db_session, make_photo(db_session, 2), alice)
        db_session.commit()

        assert authenticated_client.delete(f"/faces/{f1.id}/person").status_code == 204

        db_session.expire_all()
        assert rejections(db_session) == {(f1.id, alice.id)}
        assert db_session.get(Face, f1.id).person_id is None


class TestNotAFace:
    def test_marks_face_and_hides_it_from_photo_faces(self, authenticated_client, db_session):
        alice = make_person(db_session)
        photo = make_photo(db_session, 1)
        f1 = make_face(db_session, photo, alice)
        f2 = make_face(db_session, photo)
        db_session.commit()

        r = authenticated_client.post("/faces/not-a-face", json={"faceIds": [str(f1.id)]})

        assert r.status_code == 200
        assert r.json()["changed"] == [{"faceId": str(f1.id), "previousPersonId": str(alice.id)}]
        db_session.expire_all()
        face = db_session.get(Face, f1.id)
        assert face.not_a_face_at is not None and face.person_id is None
        assert rejections(db_session) == set()
        listed = authenticated_client.get(f"/photos/{photo.id}/faces").json()
        assert [f["id"] for f in listed] == [str(f2.id)]
        photo_json = authenticated_client.get(f"/photos/{photo.id}", headers={"Accept": "application/json"}).json()
        assert [f["id"] for f in photo_json["faces"]] == [str(f2.id)]


class TestExplicitAssignmentOverridesLabels:
    def test_put_clears_rejection_and_not_a_face(self, authenticated_client, db_session):
        alice = make_person(db_session)
        f1 = make_face(db_session, make_photo(db_session, 1), alice)
        f2 = make_face(db_session, make_photo(db_session, 2), alice)
        db_session.commit()
        authenticated_client.post("/faces/not-person", json={"faceIds": [str(f1.id)], "personId": str(alice.id)})
        authenticated_client.post("/faces/not-a-face", json={"faceIds": [str(f2.id)]})

        assert authenticated_client.put("/faces/person", json={"faceIds": [str(f1.id), str(f2.id)], "personId": str(alice.id)}).status_code == 200

        db_session.expire_all()
        assert rejections(db_session) == set()
        assert db_session.get(Face, f2.id).not_a_face_at is None
        assert db_session.get(Face, f1.id).person_confirmed is True

    def test_single_put_clears_labels(self, authenticated_client, db_session):
        alice = make_person(db_session)
        f1 = make_face(db_session, make_photo(db_session, 1), alice)
        db_session.commit()
        authenticated_client.post("/faces/not-person", json={"faceIds": [str(f1.id)], "personId": str(alice.id)})

        assert authenticated_client.put(f"/faces/{f1.id}/person", json={"personId": str(alice.id)}).status_code == 200

        db_session.expire_all()
        assert rejections(db_session) == set()

    def test_undo_after_sweep_moved_face_to_new_singleton(self, authenticated_client, db_session):
        alice = make_person(db_session)
        f1 = make_face(db_session, make_photo(db_session, 1), alice)
        db_session.commit()
        authenticated_client.post("/faces/not-person", json={"faceIds": [str(f1.id)], "personId": str(alice.id)})
        db_session.expire_all()
        q = make_person(db_session, None)
        face = db_session.get(Face, f1.id)
        face.person_id = q.id
        db_session.commit()

        r = authenticated_client.put("/faces/person", json={"faceIds": [str(f1.id)], "personId": str(alice.id)})

        assert r.status_code == 200
        db_session.expire_all()
        assert db_session.get(Face, f1.id).person_id == alice.id
        assert db_session.get(Person, q.id) is None


class TestMerge:
    def test_moves_rejections_to_winner_and_detaches_conflicts(self, authenticated_client, db_session):
        winner, loser, other = make_person(db_session, "W"), make_person(db_session, "L"), make_person(db_session, "O")
        keep = make_face(db_session, make_photo(db_session, 1), loser)
        conflict = make_face(db_session, make_photo(db_session, 2), loser)
        rejects_other = make_face(db_session, make_photo(db_session, 3), loser)
        make_face(db_session, make_photo(db_session, 4), winner)
        db_session.add_all([
            FacePersonRejection(face_id=conflict.id, person_id=winner.id),
            FacePersonRejection(face_id=rejects_other.id, person_id=other.id),
        ])
        db_session.commit()
        winner_id, loser_id = winner.id, loser.id

        r = authenticated_client.post("/people/merge", json={"personIds": [str(winner_id), str(loser_id)]})

        assert r.status_code == 200
        assert r.json()["detachedCount"] == 1
        db_session.expire_all()
        assert db_session.get(Face, conflict.id).person_id is None
        assert db_session.get(Face, conflict.id).person_confirmed is False
        assert db_session.get(Face, keep.id).person_id == winner_id
        assert db_session.get(Face, rejects_other.id).person_id == winner_id
        assert db_session.query(PhotoPerson).filter_by(photo_id=conflict.photo_id).count() == 0

    def test_rejection_of_a_loser_by_a_face_on_another_loser_detaches(self, authenticated_client, db_session):
        winner, l1, l2 = make_person(db_session, "W"), make_person(db_session, "L1"), make_person(db_session, "L2")
        f = make_face(db_session, make_photo(db_session, 1), l2)
        db_session.add(FacePersonRejection(face_id=f.id, person_id=l1.id))
        db_session.commit()

        r = authenticated_client.post("/people/merge", json={"personIds": [str(winner.id), str(l1.id), str(l2.id)]})

        assert r.json()["detachedCount"] == 1

    def test_rejection_is_moved_to_winner(self, authenticated_client, db_session):
        winner, loser = make_person(db_session, "W"), make_person(db_session, "L")
        f = make_face(db_session, make_photo(db_session, 1))
        db_session.add(FacePersonRejection(face_id=f.id, person_id=loser.id))
        db_session.commit()
        winner_id = winner.id

        authenticated_client.post("/people/merge", json={"personIds": [str(winner_id), str(loser.id)]})

        db_session.expire_all()
        assert rejections(db_session) == {(f.id, winner_id)}


class TestPeopleListing:
    def test_hides_emptied_people_without_faces_but_search_finds_named_ones(self, authenticated_client, db_session):
        now = datetime.now(timezone.utc)
        visible = make_person(db_session, "Visible")
        make_face(db_session, make_photo(db_session, 1), visible)
        make_person(db_session, "Gone", emptied_at=now)
        make_person(db_session, None, emptied_at=now)
        db_session.commit()

        data = authenticated_client.get("/people", headers={"Accept": "application/json"}).json()
        assert data["total"] == 1
        assert [p["name"] for p in data["people"]] == ["Visible"]

        found = authenticated_client.get("/people?search=Gone", headers={"Accept": "application/json"}).json()
        assert [p["name"] for p in found["people"]] == ["Gone"]
