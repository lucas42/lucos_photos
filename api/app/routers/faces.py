"""Routes for face listing and person assignment."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.auth import verify_session_or_key
from app.database import get_db
from app.prenaming import prename_tier1, prename_tier2
from app.routers.people import _enqueue_profile_picture, delete_person_if_emptied, move_faces_to_person
from app.serializers import face_to_dict, photo_url
from app.services import emit_loganne_event
from lucos_photos_common.jobs import sync_photo_person
from lucos_photos_common.models import Face, MediaItem, Person, ProcessingState, ProcessingStatus

router = APIRouter()


@router.get("/photos/{photo_id}/faces")
def list_faces(
    photo_id: str,
    _: Annotated[None, Depends(verify_session_or_key)],
    db: Session = Depends(get_db),
):
    try:
        photo_uuid = uuid.UUID(photo_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Photo not found")

    photo = db.query(MediaItem).filter(MediaItem.id == photo_uuid).first()
    if not photo:
        raise HTTPException(status_code=404, detail="Photo not found")

    faces = db.query(Face).filter(Face.photo_id == photo_uuid).all()
    return [face_to_dict(f) for f in faces]


@router.put("/faces/{face_id}/person")
async def assign_person(
    face_id: str,
    body: dict,
    _: Annotated[None, Depends(verify_session_or_key)],
    db: Session = Depends(get_db),
):
    try:
        face_uuid = uuid.UUID(face_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Face not found")

    face = db.query(Face).filter(Face.id == face_uuid).first()
    if not face:
        raise HTTPException(status_code=404, detail="Face not found")

    person_id_str = body.get("personId")
    if not person_id_str:
        raise HTTPException(status_code=422, detail="personId is required")

    try:
        person_uuid = uuid.UUID(person_id_str)
    except ValueError:
        raise HTTPException(status_code=422, detail="personId must be a valid UUID")

    person = db.query(Person).filter(Person.id == person_uuid).first()
    if not person:
        raise HTTPException(status_code=404, detail="Person not found")

    previous_person_id = face.person_id
    face.person_id = person_uuid
    face.person_confirmed = True
    db.flush()
    sync_photo_person(db, face.photo_id)
    if previous_person_id is not None and previous_person_id != person_uuid:
        delete_person_if_emptied(db, previous_person_id)
    db.commit()
    db.refresh(face)

    await emit_loganne_event("personTagged", f"Person {person_uuid} tagged on face {face_uuid} in photo {face.photo_id}", url=photo_url(face.photo_id))

    return face_to_dict(face)


@router.delete("/faces/{face_id}/person", status_code=status.HTTP_204_NO_CONTENT)
def unassign_person(
    face_id: str,
    _: Annotated[None, Depends(verify_session_or_key)],
    db: Session = Depends(get_db),
):
    try:
        face_uuid = uuid.UUID(face_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Face not found")

    face = db.query(Face).filter(Face.id == face_uuid).first()
    if not face:
        raise HTTPException(status_code=404, detail="Face not found")

    previous_person_id = face.person_id
    face.person_id = None
    face.person_confirmed = False
    db.flush()
    sync_photo_person(db, face.photo_id)
    if previous_person_id is not None:
        delete_person_if_emptied(db, previous_person_id)
    db.commit()


@router.put("/faces/person")
async def bulk_assign_person(
    body: dict,
    _: Annotated[None, Depends(verify_session_or_key)],
    db: Session = Depends(get_db),
):
    """Bulk-reassign a set of faces to a person in one transaction — the "move selected
    photos to another person" action (lucas42/lucos_photos#471). One transaction, one
    resync pass, rather than N sequential single-face PUTs with partial-failure risk.

    Accepts {"faceIds": [...], "personId": "<uuid>"}. Applies the same transactional
    contract as a manual merge (see move_faces_to_person): moved faces are marked
    confirmed, photo_person is resynced for every affected photo, an emptied unlinked
    source person is deleted, and profile-picture regeneration is re-enqueued for every
    person whose faces changed.
    """
    face_id_strs = body.get("faceIds")
    if not isinstance(face_id_strs, list) or not face_id_strs:
        raise HTTPException(status_code=422, detail="faceIds must be a non-empty list")

    person_id_str = body.get("personId")
    if not person_id_str:
        raise HTTPException(status_code=422, detail="personId is required")

    try:
        person_uuid = uuid.UUID(str(person_id_str))
    except ValueError:
        raise HTTPException(status_code=422, detail="personId must be a valid UUID")

    face_uuids = []
    for fid in face_id_strs:
        try:
            face_uuids.append(uuid.UUID(str(fid)))
        except (ValueError, AttributeError):
            raise HTTPException(status_code=422, detail=f"Invalid face ID: {fid!r}")

    destination = db.query(Person).filter(Person.id == person_uuid).first()
    if not destination:
        raise HTTPException(status_code=404, detail="Destination person not found")

    faces = db.query(Face).filter(Face.id.in_(face_uuids)).all()
    if len(faces) != len(set(face_uuids)):
        found_ids = {f.id for f in faces}
        missing = [str(u) for u in face_uuids if u not in found_ids]
        raise HTTPException(status_code=404, detail=f"Face(s) not found: {', '.join(missing)}")

    needs_regen = move_faces_to_person(db, faces, destination)
    db.commit()

    for pid in needs_regen:
        _enqueue_profile_picture(str(pid))

    await emit_loganne_event(
        "facesMoved",
        f"Moved {len(faces)} face(s) to {destination.display_name or str(person_uuid)} in lucos_photos",
        url=photo_url(faces[0].photo_id),
    )

    return {"movedCount": len(faces), "personId": str(person_uuid)}


@router.post("/photos/{photo_id}/prename")
def prename_photo(
    photo_id: str,
    body: dict,
    _: Annotated[None, Depends(verify_session_or_key)],
    db: Session = Depends(get_db),
):
    """Pre-name a photo's faces from names supplied by the Google Photos migration
    (lucas42/lucos_photos#469). tier 1: one name + one face. tier 2: several names,
    matched against each listed person's confirmed faces. Returns 409 until the photo
    has finished processing, so the caller can retry."""
    tier = body.get("tier")
    names = body.get("names")
    if tier not in (1, 2) or not isinstance(names, list):
        raise HTTPException(status_code=422, detail="tier (1 or 2) and names (a list) are required")
    try:
        photo_uuid = uuid.UUID(photo_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Photo not found")
    if not db.query(MediaItem).filter(MediaItem.id == photo_uuid).first():
        raise HTTPException(status_code=404, detail="Photo not found")
    proc = db.query(ProcessingStatus).filter(ProcessingStatus.photo_id == photo_uuid).first()
    state = proc.state.value if proc else None
    if state != ProcessingState.complete.value:
        return JSONResponse(status_code=409, content={"detail": "Photo not processed yet", "processingStatus": state})

    result = (prename_tier1 if tier == 1 else prename_tier2)(db, photo_uuid, names)
    db.commit()
    for person_id in set(result["personIds"]):
        _enqueue_profile_picture(str(person_id))
    return {"photoId": photo_id, "tier": tier, "assigned": result["assigned"], "reason": result["reason"]}
