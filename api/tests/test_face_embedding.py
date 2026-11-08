"""face_embedding side table (lucas42/lucos_photos#654)."""

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from lucos_photos_common.database import Base
from lucos_photos_common.models import Face, FaceEmbedding, Photo


def _session_with_foreign_keys():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _face(db, embedding):
    photo = Photo(sha256_hash="a" * 64, file_extension="jpg")
    db.add(photo)
    db.flush()
    face = Face(photo_id=photo.id, bbox_x=0, bbox_y=0, bbox_width=1, bbox_height=1,
                embedding_row=FaceEmbedding(embedding=embedding) if embedding else None)
    db.add(face)
    db.commit()
    return face


def test_bulk_face_delete_cascades_to_the_embedding():
    db = _session_with_foreign_keys()
    face = _face(db, [0.1] * 512)
    assert db.query(FaceEmbedding).count() == 1

    db.query(Face).filter(Face.id == face.id).delete()
    db.commit()

    assert db.query(FaceEmbedding).count() == 0


def test_orm_face_delete_removes_the_embedding():
    db = _session_with_foreign_keys()
    face = _face(db, [0.1] * 512)

    db.delete(face)
    db.commit()

    assert db.query(FaceEmbedding).count() == 0


def test_face_without_embedding_has_no_row():
    db = _session_with_foreign_keys()
    face = _face(db, None)

    assert face.embedding_row is None
    assert db.query(FaceEmbedding).count() == 0
