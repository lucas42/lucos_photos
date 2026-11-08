"""The step 2 nearest-neighbour SQL against real Postgres + pgvector (lucas42/lucos_photos#657).

Deselected by default; CI runs `pytest -m postgres` in test-db-integration after `alembic upgrade head`.
Locally: run the same against a Postgres with the migrations applied. Rows are written inside a
transaction that is rolled back, so nothing is left behind or deleted.
"""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from lucos_photos_common.database import get_engine
from lucos_photos_common.jobs import FACE_SIMILARITY_THRESHOLD, find_nearest_person, nearest_person_statement
from lucos_photos_common.models import Face, FaceEmbedding, MediaItem, Person

pytestmark = pytest.mark.postgres


def vec(*head):
    return list(head) + [0.0] * (512 - len(head))


QUERY = vec(1.0)
CLOSE = vec(1.0, 0.1)  # cosine distance ~0.005
FAR = vec(0.0, 1.0)  # cosine distance 1.0
MIDDLE = vec(1.0, 1.0)  # cosine distance ~0.293


@pytest.fixture
def db():
    connection = get_engine().connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    yield session
    session.close()
    transaction.rollback()
    connection.close()


def photo(db):
    item = MediaItem(sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex, file_extension="jpg")
    db.add(item)
    db.flush()
    return item


def person(db, name):
    p = Person(display_name=name)
    db.add(p)
    db.flush()
    return p


def face(db, on_photo, owner, embedding, **kwargs):
    f = Face(
        photo_id=on_photo.id, person_id=owner.id if owner else None,
        bbox_x=0, bbox_y=0, bbox_width=1, bbox_height=1,
        embedding_row=FaceEmbedding(embedding=embedding), **kwargs,
    )
    db.add(f)
    db.flush()
    return f


def test_nearest_face_on_another_photo_wins_and_distance_is_returned(db):
    query_photo, close_photo, far_photo = photo(db), photo(db), photo(db)
    close, far = person(db, "Close"), person(db, "Far")
    face(db, close_photo, close, CLOSE)
    face(db, far_photo, far, FAR)

    person_id, distance = find_nearest_person(db, QUERY, query_photo.id)

    assert str(person_id) == str(close.id)
    assert distance == pytest.approx(0.00496, abs=1e-4)
    assert distance < FACE_SIMILARITY_THRESHOLD


def test_distance_over_the_threshold_is_reported_for_the_caller_to_reject(db):
    face(db, photo(db), person(db, "Far"), FAR)

    _, distance = find_nearest_person(db, QUERY, photo(db).id)

    assert distance == pytest.approx(1.0, abs=1e-4)
    assert distance >= FACE_SIMILARITY_THRESHOLD


def test_faces_on_the_same_photo_are_excluded(db):
    query_photo, other_photo = photo(db), photo(db)
    same, other = person(db, "Same"), person(db, "Other")
    face(db, query_photo, same, QUERY)
    face(db, other_photo, other, MIDDLE)

    person_id, _ = find_nearest_person(db, QUERY, query_photo.id)

    assert str(person_id) == str(other.id)


def test_not_a_face_is_excluded(db):
    query_photo = photo(db)
    hidden, shown = person(db, "Hidden"), person(db, "Shown")
    face(db, photo(db), hidden, QUERY, not_a_face_at=datetime.now(timezone.utc))
    face(db, photo(db), shown, MIDDLE)

    person_id, _ = find_nearest_person(db, QUERY, query_photo.id)

    assert str(person_id) == str(shown.id)


def test_unassigned_faces_are_excluded(db):
    query_photo = photo(db)
    assigned = person(db, "Assigned")
    face(db, photo(db), None, QUERY)
    face(db, photo(db), assigned, MIDDLE)

    person_id, _ = find_nearest_person(db, QUERY, query_photo.id)

    assert str(person_id) == str(assigned.id)


def test_rejected_person_is_skipped_for_the_next_nearest(db):
    query_photo = photo(db)
    rejected, next_nearest = person(db, "Rejected"), person(db, "Next")
    face(db, photo(db), rejected, CLOSE)
    face(db, photo(db), next_nearest, MIDDLE)

    person_id, distance = find_nearest_person(db, QUERY, query_photo.id, {rejected.id})

    assert str(person_id) == str(next_nearest.id)
    assert distance == pytest.approx(0.2929, abs=1e-3)


def test_no_candidates_returns_none(db):
    only = person(db, "Only")
    only_photo = photo(db)
    face(db, only_photo, only, CLOSE)

    assert find_nearest_person(db, QUERY, only_photo.id) is None
    assert find_nearest_person(db, QUERY, photo(db).id, {only.id}) is None


def test_the_hnsw_index_can_serve_the_query(db):
    face(db, photo(db), person(db, "Indexed"), CLOSE)
    # Tiny tables: force the planner off its cheap alternatives so the check shows the index is usable.
    db.execute(text("SET LOCAL enable_seqscan = off"))
    db.execute(text("SET LOCAL enable_sort = off"))
    stmt, params = nearest_person_statement(QUERY, photo(db).id)

    plan = "\n".join(row[0] for row in db.execute(text("EXPLAIN " + stmt.text), params))

    assert "ix_face_embedding_embedding" in plan
