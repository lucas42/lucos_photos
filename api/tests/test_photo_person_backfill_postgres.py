"""The photo_person backfill migration SQL against real Postgres (lucas42/lucos_photos#659).

Run with `pytest -m postgres` (see test_nearest_person_postgres.py); rows are rolled back.
"""

import importlib.util
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from lucos_photos_common.database import get_engine
from lucos_photos_common.models import Face, MediaItem, Person, PhotoPerson

pytestmark = pytest.mark.postgres

MIGRATION = Path(__file__).parent.parent / "alembic" / "versions" / "d0e2f4a6b8c9_backfill_photo_person.py"


@pytest.fixture
def db():
    connection = get_engine().connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    yield session
    session.close()
    transaction.rollback()
    connection.close()


def _backfill_sql():
    spec = importlib.util.spec_from_file_location("backfill_photo_person", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.BACKFILL_SQL


def test_backfill_inserts_missing_pairs_once_and_leaves_existing_rows(db):
    person_a, person_b = Person(display_name="A"), Person(display_name="B")
    missing = MediaItem(sha256_hash=uuid.uuid4().hex * 2, file_extension="jpg")
    present = MediaItem(sha256_hash=uuid.uuid4().hex * 2, file_extension="jpg")
    unassigned = MediaItem(sha256_hash=uuid.uuid4().hex * 2, file_extension="jpg")
    db.add_all([person_a, person_b, missing, present, unassigned])
    db.flush()
    box = dict(bbox_x=0, bbox_y=0, bbox_width=1, bbox_height=1)
    db.add_all([
        Face(photo_id=missing.id, person_id=person_a.id, **box),
        Face(photo_id=missing.id, person_id=person_a.id, **box),  # two faces, one pair
        Face(photo_id=missing.id, person_id=person_b.id, **box),
        Face(photo_id=present.id, person_id=person_a.id, **box),
        Face(photo_id=unassigned.id, person_id=None, **box),
        PhotoPerson(photo_id=present.id, person_id=person_a.id),
    ])
    db.flush()

    for _ in range(2):  # idempotent
        db.execute(text(_backfill_sql()))

    pairs = {(r.photo_id, r.person_id) for r in db.query(PhotoPerson).filter(PhotoPerson.photo_id.in_([missing.id, present.id, unassigned.id]))}
    assert pairs == {(missing.id, person_a.id), (missing.id, person_b.id), (present.id, person_a.id)}
