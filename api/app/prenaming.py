"""Pre-name migrated faces from Google Takeout `people` names (lucas42/lucos_photos#469)."""

import math

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.routers.people import delete_person_if_emptied
from lucos_photos_common.jobs import FACE_SIMILARITY_THRESHOLD, sync_photo_person
from lucos_photos_common.models import Face, Person


def normalise_names(names) -> list[str]:
    """Strip, drop blanks and de-duplicate case-insensitively, keeping first-seen spelling."""
    seen = {}
    for name in names:
        if isinstance(name, str) and name.strip():
            seen.setdefault(name.strip().casefold(), name.strip())
    return list(seen.values())


def _find_person(db: Session, name: str) -> Person | None:
    """Exact case-insensitive display_name match; the oldest wins if there are duplicates."""
    return (
        db.query(Person)
        .filter(func.lower(Person.display_name) == name.lower(), Person.is_background.is_(False))
        .order_by(Person.created_at.asc(), Person.id.asc())
        .first()
    )


def _assign(db: Session, face: Face, person: Person, confirmed: bool) -> None:
    previous_person_id = face.person_id
    face.person_id = person.id
    face.person_confirmed = confirmed
    db.flush()
    sync_photo_person(db, face.photo_id)
    if previous_person_id is not None and previous_person_id != person.id:
        delete_person_if_emptied(db, previous_person_id)


def prename_tier1(db: Session, photo_id, names) -> dict:
    """One name + one detected face: assign it directly, creating the Person if needed."""
    names = normalise_names(names)
    if len(names) != 1:
        return {"assigned": 0, "reason": "not exactly one name", "personIds": []}
    faces = db.query(Face).filter(Face.photo_id == photo_id).all()
    if len(faces) != 1:
        return {"assigned": 0, "reason": f"{len(faces)} faces detected", "personIds": []}
    face = faces[0]
    if face.person_confirmed:
        return {"assigned": 0, "reason": "face already confirmed", "personIds": []}
    person = _find_person(db, names[0])
    if person is None:
        person = Person(display_name=names[0])
        db.add(person)
        db.flush()
    _assign(db, face, person, confirmed=True)
    return {"assigned": 1, "reason": None, "personIds": [person.id]}


def _unit(vec) -> "list[float] | None":
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec] if norm > 0 else None


def _centroid(db: Session, person_id) -> "list[float] | None":
    rows = db.query(Face.embedding).filter(
        Face.person_id == person_id, Face.person_confirmed.is_(True), Face.embedding.isnot(None)
    ).all()
    units = [u for u in (_unit(list(r[0])) for r in rows) if u is not None]
    if not units:
        return None
    return _unit([sum(col) / len(units) for col in zip(*units)])


def prename_tier2(db: Session, photo_id, names, threshold: float = FACE_SIMILARITY_THRESHOLD) -> dict:
    """Several names: match unconfirmed faces to the listed people's confirmed-face centroids,
    closest pair first, leaving anything not under the threshold unassigned."""
    names = normalise_names(names)
    if len(names) < 2:
        return {"assigned": 0, "reason": "fewer than two names", "personIds": []}
    candidates = []
    for name in names:
        person = _find_person(db, name)
        centroid = _centroid(db, person.id) if person else None
        if centroid is not None:
            candidates.append((person, centroid))
    if not candidates:
        return {"assigned": 0, "reason": "no listed person has confirmed faces", "personIds": []}
    faces = [
        f for f in db.query(Face).filter(Face.photo_id == photo_id).all()
        if not f.person_confirmed and f.embedding is not None
    ]
    pairs = []
    for fi, face in enumerate(faces):
        vec = _unit(list(face.embedding))
        if vec is None:
            continue
        for ci, (_, centroid) in enumerate(candidates):
            pairs.append((1.0 - sum(a * b for a, b in zip(vec, centroid)), fi, ci))
    used_faces, used_candidates, person_ids = set(), set(), []
    for distance, fi, ci in sorted(pairs):
        if distance >= threshold:
            break
        if fi in used_faces or ci in used_candidates:
            continue
        used_faces.add(fi)
        used_candidates.add(ci)
        _assign(db, faces[fi], candidates[ci][0], confirmed=False)
        person_ids.append(candidates[ci][0].id)
    return {"assigned": len(person_ids), "reason": None if person_ids else "no face under threshold", "personIds": person_ids}
