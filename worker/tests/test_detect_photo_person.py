"""detect_and_save_faces keeps photo_person in step with face assignments (lucas42/lucos_photos#659)."""

from unittest.mock import MagicMock, patch

import numpy as np

from lucos_photos_common.jobs import detect_and_save_faces
from lucos_photos_common.models import Face, FaceEmbedding, Person, Photo, PhotoPerson

EMB = [1.0] + [0.0] * 511


def _run(db, photo, tmp_path, embeddings):
    path = tmp_path / "t.jpg"
    path.write_bytes(b"fake")
    faces = []
    for emb in embeddings:
        f = MagicMock()
        f.bbox = [0.0, 0.0, 100.0, 100.0]
        f.embedding = np.array(emb)
        f.det_score = 0.9
        f.kps = None
        faces.append(f)
    app = MagicMock()
    app.get.return_value = faces
    pil = MagicMock()
    pil.__enter__ = MagicMock(return_value=pil)
    pil.__exit__ = MagicMock(return_value=False)
    with patch("lucos_photos_common.jobs._get_face_analysis_app", return_value=app), \
         patch.dict("sys.modules", {"cv2": MagicMock()}), patch("PIL.Image.open", return_value=pil):
        detect_and_save_faces(db, photo, path)


def _setup(db, confirmed=True):
    photo = Photo(sha256_hash="9" * 64, file_extension="jpg", width=1000, height=800)
    person = Person(display_name="Alice")
    db.add_all([photo, person])
    db.flush()
    db.add(Face(photo_id=photo.id, person_id=person.id, person_confirmed=confirmed,
                bbox_x=0, bbox_y=0, bbox_width=0.1, bbox_height=0.1, embedding_row=FaceEmbedding(embedding=EMB)))
    db.commit()
    return photo, person


def test_relinked_face_gets_a_photo_person_row(db_session, tmp_path):
    photo, person = _setup(db_session)

    _run(db_session, photo, tmp_path, [EMB])

    rows = db_session.query(PhotoPerson).filter_by(photo_id=photo.id).all()
    assert [r.person_id for r in rows] == [person.id]


def test_redetection_that_drops_the_face_removes_the_stale_row(db_session, tmp_path):
    photo, person = _setup(db_session)
    db_session.add(PhotoPerson(photo_id=photo.id, person_id=person.id))
    db_session.commit()

    _run(db_session, photo, tmp_path, [])

    assert db_session.query(PhotoPerson).filter_by(photo_id=photo.id).count() == 0
