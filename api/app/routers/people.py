"""Routes for person management: list, create, get, profile picture, contact link/unlink."""

import os
import uuid
from pathlib import Path
from typing import Annotated, Optional

import mimeparse
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import verify_session
from app.database import get_db
from app.serializers import DERIVATIVES_DIR, person_profile_picture_url, person_to_dict, photo_to_dict
from app.services import emit_loganne_event, fetch_contact_name
from lucos_photos_common.models import Face, MediaItem, Person, PhotoPerson, ProcessingState, ProcessingStatus

router = APIRouter()

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

PEOPLE_SORT_ORDER = [
    Person.profile_photo_id.is_(None).asc(),
    Person.display_name.asc().nullslast(),
    Person.id.asc(),
]


@router.get("/people")
def list_people(
    request: Request,
    _: Annotated[None, Depends(verify_session)],
    includePhotoCounts: bool = False,
    limit: int = 100,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    total = db.query(func.count(Person.id)).scalar()

    if includePhotoCounts:
        # Join with PhotoPerson to count photos
        query = db.query(
            Person,
            func.count(PhotoPerson.photo_id).label("photo_count")
        ).outerjoin(PhotoPerson).group_by(Person.id).order_by(*PEOPLE_SORT_ORDER)

        people_with_counts = query.offset(offset).limit(limit).all()
        people_data = [person_to_dict(p, count) for p, count in people_with_counts]
    else:
        people = db.query(Person).order_by(*PEOPLE_SORT_ORDER).offset(offset).limit(limit).all()
        people_data = [person_to_dict(p) for p in people]

    accept_header = request.headers.get("accept", "*/*")
    best_match = mimeparse.best_match(["text/html", "application/json"], accept_header)
    if best_match == "text/html":
        arachne_key = os.environ.get("KEY_LUCOS_ARACHNE", "")
        prev_offset = max(0, offset - limit) if offset > 0 else None
        next_offset = offset + limit if offset + limit < total else None
        return templates.TemplateResponse(request, "people.html", {
            "people": people_data,
            "arachne_key": arachne_key,
            "current_page": "people",
            "offset": offset,
            "limit": limit,
            "total": total,
            "prev_offset": prev_offset,
            "next_offset": next_offset,
        }, headers={"Vary": "Accept"})

    return JSONResponse(content={"people": people_data, "total": total, "offset": offset, "limit": limit}, headers={"Vary": "Accept"})


@router.post("/people", status_code=status.HTTP_201_CREATED)
async def create_person(
    body: dict,
    _: Annotated[None, Depends(verify_session)],
    db: Session = Depends(get_db),
):
    name = body.get("name")
    if not name:
        raise HTTPException(status_code=422, detail="name is required")

    contact_id = body.get("contactId")

    if contact_id:
        contact_name = await fetch_contact_name(str(contact_id))
        if contact_name:
            name = contact_name

    person = Person(display_name=name, contact_id=contact_id)
    db.add(person)
    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
        # Handle unique constraint for contact_id if it already exists (Postgres code 23505, with SQLite fallback for tests)
        is_unique_violation = (e.orig and hasattr(e.orig, 'pgcode') and e.orig.pgcode == '23505') or \
                              ("UNIQUE constraint failed" in str(e))
        if is_unique_violation:
            raise HTTPException(status_code=409, detail="A person with this contactId already exists")
        raise
    except Exception:
        db.rollback()
        raise
    db.refresh(person)

    await emit_loganne_event("personCreated", f"Person {person.id} ({person.display_name}) created in lucos_photos")

    return person_to_dict(person)


@router.get("/people/{person_id}")
def get_person(
    person_id: str,
    request: Request,
    _: Annotated[None, Depends(verify_session)],
    db: Session = Depends(get_db),
):
    try:
        person_uuid = uuid.UUID(person_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Person not found")

    person = db.query(Person).filter(Person.id == person_uuid).first()
    if not person:
        raise HTTPException(status_code=404, detail="Person not found")

    face_count = db.query(Face).filter(Face.person_id == person_uuid).count()

    # Get photos assigned to this person (via PhotoPerson), only fully processed ones
    photos = (
        db.query(MediaItem)
        .join(PhotoPerson)
        .join(ProcessingStatus, MediaItem.id == ProcessingStatus.photo_id)
        .filter(PhotoPerson.person_id == person_uuid)
        .filter(ProcessingStatus.state == ProcessingState.complete)
        .all()
    )

    data = person_to_dict(person)
    data["faceCount"] = face_count
    data["photos"] = [photo_to_dict(p) for p in photos]

    accept_header = request.headers.get("accept", "*/*")
    best_match = mimeparse.best_match(["text/html", "application/json"], accept_header)
    if best_match == "text/html":
        contacts_url = os.environ.get("LUCOS_CONTACTS_URL", "")
        arachne_key = os.environ.get("KEY_LUCOS_ARACHNE", "")
        contact_page_url = f"{contacts_url}/people/{person.contact_id}" if person.contact_id and contacts_url else None
        return templates.TemplateResponse(request, "person.html", {
            "person": data,
            "contact_page_url": contact_page_url,
            "arachne_key": arachne_key,
            "current_page": "people",
        }, headers={"Vary": "Accept"})

    return JSONResponse(content=data, headers={"Vary": "Accept"})


@router.get("/people/{person_id}/profile-picture")
def get_person_profile_picture(
    person_id: str,
    _: Annotated[None, Depends(verify_session)],
):
    """Serve a person's profile picture derivative file."""
    try:
        safe_id = str(uuid.UUID(person_id))
    except ValueError:
        raise HTTPException(status_code=404, detail="Person not found")

    profile_path = DERIVATIVES_DIR / f"{safe_id}_profile.jpg"
    if not profile_path.exists():
        raise HTTPException(status_code=404, detail="Profile picture not yet generated")

    return FileResponse(str(profile_path), media_type="image/jpeg")


@router.put("/people/{person_id}/contact")
async def link_person_contact(
    person_id: str,
    body: dict,
    _: Annotated[None, Depends(verify_session)],
    db: Session = Depends(get_db),
):
    try:
        person_uuid = uuid.UUID(person_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Person not found")

    person = db.query(Person).filter(Person.id == person_uuid).first()
    if not person:
        raise HTTPException(status_code=404, detail="Person not found")

    contact_id = body.get("contactId")
    if not contact_id:
        raise HTTPException(status_code=422, detail="contactId is required")

    person.contact_id = str(contact_id)
    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
        is_unique_violation = (e.orig and hasattr(e.orig, 'pgcode') and e.orig.pgcode == '23505') or \
                              ("UNIQUE constraint failed" in str(e))
        if is_unique_violation:
            raise HTTPException(status_code=409, detail="A person with this contactId already exists")
        raise
    db.refresh(person)

    contact_name = await fetch_contact_name(str(contact_id))
    if contact_name:
        person.display_name = contact_name
        db.commit()
        db.refresh(person)

    await emit_loganne_event("personContactLinked", f"Person {person_uuid} linked to contact {contact_id} in lucos_photos")

    return person_to_dict(person)


@router.delete("/people/{person_id}/contact", status_code=status.HTTP_204_NO_CONTENT)
async def unlink_person_contact(
    person_id: str,
    _: Annotated[None, Depends(verify_session)],
    db: Session = Depends(get_db),
):
    try:
        person_uuid = uuid.UUID(person_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Person not found")

    person = db.query(Person).filter(Person.id == person_uuid).first()
    if not person:
        raise HTTPException(status_code=404, detail="Person not found")

    person.contact_id = None
    db.commit()

    await emit_loganne_event("personContactUnlinked", f"Person {person_uuid} unlinked from contact in lucos_photos")
