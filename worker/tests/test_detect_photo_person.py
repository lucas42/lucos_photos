"""Auto-assigned faces count toward a person's photos (lucas42/lucos_photos#620, #659)."""

from unittest.mock import MagicMock, patch

import numpy as np

from lucos_photos_common.jobs import detect_and_save_faces
from lucos_photos_common.models import Face, FaceEmbedding, Person, Photo, PhotoPerson

EMB = [1.0] + [0.0] * 511


def test_relinked_face_appears_in_photo_person_without_any_sync(db_session, tmp_path):
    photo = Photo(sha256_hash="9" * 64, file_extension="jpg", width=1000, height=800)
    person = Person(display_name="Alice")
    db_session.add_all([photo, person])
    db_session.flush()
    db_session.add(Face(photo_id=photo.id, person_id=person.id, person_confirmed=True,
                        bbox_x=0, bbox_y=0, bbox_width=0.1, bbox_height=0.1, embedding_row=FaceEmbedding(embedding=EMB)))
    db_session.commit()
    path = tmp_path / "t.jpg"
    path.write_bytes(b"fake")
    detected = MagicMock()
    detected.bbox = [0.0, 0.0, 100.0, 100.0]
    detected.embedding = np.array(EMB)
    detected.det_score = 0.9
    detected.kps = None
    app = MagicMock()
    app.get.return_value = [detected]
    pil = MagicMock()
    pil.__enter__ = MagicMock(return_value=pil)
    pil.__exit__ = MagicMock(return_value=False)

    with patch("lucos_photos_common.jobs._get_face_analysis_app", return_value=app), \
         patch.dict("sys.modules", {"cv2": MagicMock()}), patch("PIL.Image.open", return_value=pil):
        detect_and_save_faces(db_session, photo, path)

    rows = db_session.query(PhotoPerson).filter_by(photo_id=photo.id).all()
    assert [r.person_id for r in rows] == [person.id]

    # and re-detection that finds no face leaves nothing behind
    app.get.return_value = []
    with patch("lucos_photos_common.jobs._get_face_analysis_app", return_value=app), \
         patch.dict("sys.modules", {"cv2": MagicMock()}), patch("PIL.Image.open", return_value=pil):
        detect_and_save_faces(db_session, photo, path)
    assert db_session.query(PhotoPerson).filter_by(photo_id=photo.id).count() == 0
