"""Negative face labels: "not this person" and "not a face" (lucas42/lucos_photos#622)."""

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.routers.people import unpin_if_untagged
from lucos_photos_common.models import Face, FacePersonRejection, Person


def apply_face_label(db: Session, faces: list[Face], not_a_face: bool) -> tuple[list[dict], set]:
    """Detach `faces` from their person and record why. Does not commit.

    Returns (changed, regen_person_ids): `changed` is [{"faceId", "previousPersonId"}] for the
    response; the caller enqueues profile-picture regeneration for each id in the set after commit.
    """
    now = datetime.now(timezone.utc)
    changed = []
    previous = {}
    existing = {
        (r.face_id, r.person_id)
        for r in db.query(FacePersonRejection).filter(FacePersonRejection.face_id.in_([f.id for f in faces]))
    }
    for face in faces:
        previous[face.id] = face.person_id
        changed.append({"faceId": str(face.id), "previousPersonId": str(face.person_id) if face.person_id else None})
        if not_a_face:
            face.not_a_face_at = now
        elif face.person_id is not None and (face.id, face.person_id) not in existing:
            db.add(FacePersonRejection(face_id=face.id, person_id=face.person_id))
        face.person_id = None
        face.person_confirmed = False
    db.flush()

    regen = set()
    for face in faces:
        if previous[face.id] is not None:
            unpin_if_untagged(db, previous[face.id], face.photo_id)
    for person_id in {p for p in previous.values() if p is not None}:
        if db.query(Face.id).filter(Face.person_id == person_id).first() is not None:
            regen.add(person_id)
            continue
        db.query(Person).filter(Person.id == person_id, Person.contact_id.is_(None)).update(
            {Person.emptied_at: now}, synchronize_session=False
        )
    return changed, regen
