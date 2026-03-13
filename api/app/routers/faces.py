"""Routes for face listing and person assignment."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.auth import verify_session
from app.database import get_db
from app.serializers import face_to_dict, photo_url
from app.services import emit_loganne_event
from lucos_photos_common.jobs import sync_photo_person
from lucos_photos_common.models import Face, MediaItem, Person

router = APIRouter()


@router.get("/photos/{photo_id}/faces")
def list_faces(
    photo_id: str,
    _: Annotated[None, Depends(verify_session)],
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
    _: Annotated[None, Depends(verify_session)],
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

    face.person_id = person_uuid
    face.person_confirmed = True
    sync_photo_person(db, face.photo_id)
    db.commit()
    db.refresh(face)

    await emit_loganne_event("personTagged", f"Person {person_uuid} tagged on face {face_uuid} in photo {face.photo_id}", url=photo_url(face.photo_id))

    return face_to_dict(face)


@router.delete("/faces/{face_id}/person", status_code=status.HTTP_204_NO_CONTENT)
def unassign_person(
    face_id: str,
    _: Annotated[None, Depends(verify_session)],
    db: Session = Depends(get_db),
):
    try:
        face_uuid = uuid.UUID(face_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Face not found")

    face = db.query(Face).filter(Face.id == face_uuid).first()
    if not face:
        raise HTTPException(status_code=404, detail="Face not found")

    face.person_id = None
    face.person_confirmed = False
    sync_photo_person(db, face.photo_id)
    db.commit()
