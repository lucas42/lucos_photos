"""photo_person is a view over face on real Postgres (lucas42/lucos_photos#620). Run with `pytest -m postgres`; rolled back."""

import uuid

import pytest
from sqlalchemy.orm import Session

from lucos_photos_common.database import get_engine
from lucos_photos_common.models import Face, MediaItem, Person, PhotoPerson

pytestmark = pytest.mark.postgres


@pytest.fixture
def db():
    connection = get_engine().connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    yield session
    session.close()
    transaction.rollback()
    connection.close()


def test_view_lists_each_assigned_pair_once_and_follows_face_changes(db):
    a, b = Person(display_name="A"), Person(display_name="B")
    photo = MediaItem(sha256_hash=uuid.uuid4().hex * 2, file_extension="jpg")
    db.add_all([a, b, photo])
    db.flush()
    box = dict(bbox_x=0, bbox_y=0, bbox_width=1, bbox_height=1)
    first = Face(photo_id=photo.id, person_id=a.id, **box)
    db.add_all([first, Face(photo_id=photo.id, person_id=a.id, **box), Face(photo_id=photo.id, person_id=None, **box)])
    db.flush()

    assert {(r.photo_id, r.person_id) for r in db.query(PhotoPerson).filter_by(photo_id=photo.id)} == {(photo.id, a.id)}

    first.person_id = b.id
    db.flush()

    assert {r.person_id for r in db.query(PhotoPerson).filter_by(photo_id=photo.id)} == {a.id, b.id}
