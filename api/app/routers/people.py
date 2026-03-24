"""Routes for person management: list, create, get, profile picture, contact link/unlink."""

import logging
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
from lucos_photos_common.jobs import sync_photo_person
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
    base_filter = Person.is_background == False  # noqa: E712
    total = db.query(func.count(Person.id)).filter(base_filter).scalar()

    if includePhotoCounts:
        # Join with PhotoPerson to count photos
        query = db.query(
            Person,
            func.count(PhotoPerson.photo_id).label("photo_count")
        ).filter(base_filter).outerjoin(PhotoPerson).group_by(Person.id).order_by(*PEOPLE_SORT_ORDER)

        people_with_counts = query.offset(offset).limit(limit).all()
        people_data = [person_to_dict(p, count) for p, count in people_with_counts]
    else:
        people = db.query(Person).filter(base_filter).order_by(*PEOPLE_SORT_ORDER).offset(offset).limit(limit).all()
        people_data = [person_to_dict(p) for p in people]

    accept_header = request.headers.get("accept", "*/*")
    best_match = mimeparse.best_match(["text/html", "application/json"], accept_header)
    if best_match == "text/html":
        prev_offset = max(0, offset - limit) if offset > 0 else None
        next_offset = offset + limit if offset + limit < total else None
        return templates.TemplateResponse(request, "people.html", {
            "people": people_data,
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

    await emit_loganne_event("personCreated", f"{person.display_name or person.id} created in lucos_photos")

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
        contacts_url = os.environ.get("LUCOS_CONTACTS_URL", "").rstrip("/")
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
            existing = db.query(Person).filter(Person.contact_id == str(contact_id)).first()
            existing_person_id = str(existing.id) if existing else None
            raise HTTPException(
                status_code=409,
                detail={"message": "A person with this contactId already exists", "existingPersonId": existing_person_id},
            )
        raise
    db.refresh(person)

    contact_name = await fetch_contact_name(str(contact_id))
    if contact_name:
        person.display_name = contact_name
        db.commit()
        db.refresh(person)
    else:
        logging.warning(
            "link_person_contact: failed to fetch name for contact %s (person %s); "
            "display_name will be corrected by the next contact name sweep",
            contact_id, person_uuid,
        )

    await emit_loganne_event("personContactLinked", f"{person.display_name or str(person_uuid)} linked to contact {contact_id} in lucos_photos")

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

    await emit_loganne_event("personContactUnlinked", f"{person.display_name or str(person_uuid)} unlinked from contact in lucos_photos")


@router.post("/people/merge")
async def merge_people(
    body: dict,
    _: Annotated[None, Depends(verify_session)],
    db: Session = Depends(get_db),
):
    """Merge two or more people into one.

    Accepts a JSON body with a ``personIds`` list. The person with a contact
    link (if any) is kept as the winner; all others are deleted after their
    faces are reassigned to the winner.

    Returns ``{"mergedPersonId": "<uuid>"}`` on success.
    Responds with 409 if more than one of the persons is linked to a contact.
    """
    person_id_strs = body.get("personIds", [])
    if not isinstance(person_id_strs, list) or len(person_id_strs) < 2:
        raise HTTPException(status_code=422, detail="personIds must be a list of at least 2 person IDs")

    # Parse and validate UUIDs
    person_uuids = []
    for pid in person_id_strs:
        try:
            person_uuids.append(uuid.UUID(str(pid)))
        except (ValueError, AttributeError):
            raise HTTPException(status_code=422, detail=f"Invalid person ID: {pid!r}")

    # Deduplicate while preserving order
    seen = set()
    unique_uuids = []
    for u in person_uuids:
        if u not in seen:
            seen.add(u)
            unique_uuids.append(u)
    person_uuids = unique_uuids

    if len(person_uuids) < 2:
        raise HTTPException(status_code=422, detail="personIds must contain at least 2 distinct person IDs")

    # Load all persons
    persons = db.query(Person).filter(Person.id.in_(person_uuids)).all()
    if len(persons) != len(person_uuids):
        found_ids = {p.id for p in persons}
        missing = [str(u) for u in person_uuids if u not in found_ids]
        raise HTTPException(status_code=404, detail=f"Person(s) not found: {', '.join(missing)}")

    # Check contact constraint: at most one may be linked to a contact
    linked = [p for p in persons if p.contact_id is not None]
    if len(linked) > 1:
        raise HTTPException(
            status_code=409,
            detail="Cannot merge: more than one of the selected people is linked to a contact",
        )

    # Pick the winner: prefer the one with a contact link, otherwise use the first in the request order
    person_map = {p.id: p for p in persons}
    if linked:
        winner = linked[0]
    else:
        winner = person_map[person_uuids[0]]

    losers = [p for p in persons if p.id != winner.id]

    # Collect photo_ids affected by the losers (for photo_person sync after merge)
    loser_ids = {p.id for p in losers}
    affected_photo_ids = {
        f.photo_id
        for f in db.query(Face).filter(Face.person_id.in_(loser_ids)).all()
    }

    # Reassign all faces from losers to winner, marking them as confirmed.
    # A user-initiated merge is a manual confirmation that these faces belong
    # to the same person; without person_confirmed=True the assignments would
    # be lost if the photo is later reprocessed (detect_and_save_faces only
    # snapshots confirmed assignments before re-detecting).
    db.query(Face).filter(Face.person_id.in_(loser_ids)).update(
        {Face.person_id: winner.id, Face.person_confirmed: True},
        synchronize_session="fetch",
    )

    # Delete photo_person rows for the loser persons (sync_photo_person will rebuild correctly)
    db.query(PhotoPerson).filter(PhotoPerson.person_id.in_(loser_ids)).delete(synchronize_session="fetch")

    # Delete the loser persons
    for loser in losers:
        db.delete(loser)

    db.commit()

    # Rebuild photo_person for all affected photos
    for photo_id in affected_photo_ids:
        sync_photo_person(db, photo_id)
    db.commit()

    # Enqueue profile picture regeneration for the winner
    _enqueue_profile_picture(str(winner.id))

    loser_names = ", ".join(p.display_name or str(p.id) for p in losers)
    await emit_loganne_event("peopleMerged", f"Merged {loser_names} into {winner.display_name or str(winner.id)} in lucos_photos")

    return {"mergedPersonId": str(winner.id)}


@router.put("/people/{person_id}/background", status_code=status.HTTP_200_OK)
async def mark_person_background(
    person_id: str,
    _: Annotated[None, Depends(verify_session)],
    db: Session = Depends(get_db),
):
    """Mark a person as a background face (hides them from the people list)."""
    try:
        person_uuid = uuid.UUID(person_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Person not found")

    person = db.query(Person).filter(Person.id == person_uuid).first()
    if not person:
        raise HTTPException(status_code=404, detail="Person not found")

    person.is_background = True
    db.commit()
    db.refresh(person)

    await emit_loganne_event("personMarkedBackground", f"{person.display_name or str(person_uuid)} marked as background face in lucos_photos")

    return person_to_dict(person)


@router.delete("/people/{person_id}/background", status_code=status.HTTP_200_OK)
async def unmark_person_background(
    person_id: str,
    _: Annotated[None, Depends(verify_session)],
    db: Session = Depends(get_db),
):
    """Unmark a person as a background face (shows them in the people list again)."""
    try:
        person_uuid = uuid.UUID(person_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Person not found")

    person = db.query(Person).filter(Person.id == person_uuid).first()
    if not person:
        raise HTTPException(status_code=404, detail="Person not found")

    person.is_background = False
    db.commit()
    db.refresh(person)

    await emit_loganne_event("personUnmarkedBackground", f"{person.display_name or str(person_uuid)} unmarked as background face in lucos_photos")

    return person_to_dict(person)


def _enqueue_profile_picture(person_id: str) -> None:
    """Enqueue generate_profile_picture for a person. Non-fatal on Redis errors."""
    try:
        from app.redis_client import get_redis
        from rq import Queue
        from rq.job import Retry
        from lucos_photos_common.jobs import generate_profile_picture
        redis_conn = get_redis()
        queue = Queue("photos", connection=redis_conn)
        queue.enqueue(generate_profile_picture, person_id, retry=Retry(max=3, interval=[10, 30, 60]))
    except Exception as exc:
        print(f"Warning: failed to enqueue generate_profile_picture for {person_id}: {exc}", flush=True)
