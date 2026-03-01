import hashlib
import io
import os
import shutil
import uuid
from pathlib import Path
from typing import Annotated, Optional

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse
from PIL import Image
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lucos_photos_common.database import SessionLocal
from lucos_photos_common.models import Face, Person, Photo, PhotoPerson, ProcessingState, ProcessingStatus

app = FastAPI(title="lucos_photos")

UPLOADS_DIR = Path("/data/uploads")
STATIC_DIR = Path(__file__).parent / "static"

MAX_PHOTO_SIZE = int(os.environ.get("MAX_PHOTO_SIZE", 100 * 1024 * 1024))
MIN_FREE_DISK_SPACE = int(os.environ.get("MIN_FREE_DISK_SPACE", 500 * 1024 * 1024))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


WWW_AUTHENTICATE = {"WWW-Authenticate": 'key realm="lucos_photos"'}


def verify_key(authorization: Annotated[str | None, Header()] = None):
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header required", headers=WWW_AUTHENTICATE)
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "key":
        raise HTTPException(status_code=401, detail="Expected 'key' authorization scheme", headers=WWW_AUTHENTICATE)
    token = parts[1]
    client_keys_str = os.environ.get("CLIENT_KEYS", "")
    valid_keys = {entry.split("=", 1)[1] for entry in client_keys_str.split(";") if "=" in entry}
    if token not in valid_keys:
        raise HTTPException(status_code=401, detail="Invalid key", headers=WWW_AUTHENTICATE)


async def emit_loganne_event(event_type: str, human_readable: str):
    endpoint = os.environ.get("LOGANNE_ENDPOINT")
    if not endpoint:
        return
    try:
        async with httpx.AsyncClient() as client:
            await client.post(endpoint, json={
                "type": event_type,
                "source": os.environ.get("SYSTEM", "lucos_photos"),
                "humanReadable": human_readable,
            })
    except Exception as e:
        print(f"Error calling Loganne: {e}", flush=True)


def photo_to_dict(photo: Photo) -> dict:
    return {
        "id": str(photo.id),
        "sha256Hash": photo.sha256_hash,
        "fileExtension": photo.file_extension,
        "takenAt": photo.taken_at.isoformat() if photo.taken_at else None,
        "uploadedAt": photo.uploaded_at.isoformat() if photo.uploaded_at else None,
        "width": photo.width,
        "height": photo.height,
    }


@app.get("/healthcheck")
def healthcheck():
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
async def root():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/icon", include_in_schema=False)
async def icon():
    return FileResponse(STATIC_DIR / "icon.png")


@app.get("/lucos_navbar.js", include_in_schema=False)
async def navbar_js():
    return FileResponse(STATIC_DIR / "lucos_navbar.js")


@app.get("/_info")
def info():
    return {
        "system": os.environ.get("SYSTEM", "lucos_photos"),
        "checks": {},
        "metrics": {},
        "ci": {
            "circle": "gh/lucas42/lucos_photos",
        },
        "icon": "/icon",
        "network_only": True,
        "title": "Photos",
        "show_on_homepage": True,
        "start_url": "/",
    }


@app.post("/photos", status_code=status.HTTP_201_CREATED)
async def upload_photo(
    file: UploadFile,
    _: Annotated[None, Depends(verify_key)],
    db: Session = Depends(get_db),
):
    # Check if file size is too large (before reading into memory if possible)
    if file.size and file.size > MAX_PHOTO_SIZE:
        raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="File too large")

    # Check for sufficient free disk space
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    _, _, free = shutil.disk_usage(UPLOADS_DIR)
    if free < MIN_FREE_DISK_SPACE:
        raise HTTPException(status_code=status.HTTP_507_INSUFFICIENT_STORAGE, detail="Insufficient storage")

    contents = await file.read()

    # Re-check file size after reading (in case file.size was missing or incorrect)
    if len(contents) > MAX_PHOTO_SIZE:
        raise HTTPException(status_code=status.HTTP_413_PAYLOAD_TOO_LARGE, detail="File too large")

    sha256_hash = hashlib.sha256(contents).hexdigest()

    # Validate that the file is a valid image
    try:
        with Image.open(io.BytesIO(contents)) as img:
            img.verify()
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid image file")

    # Idempotency: if a photo with this hash already exists, return it
    existing = db.query(Photo).filter(Photo.sha256_hash == sha256_hash).first()
    if existing:
        return JSONResponse(status_code=200, content=photo_to_dict(existing))

    # Determine file extension from filename, falling back to content type
    filename = file.filename or ""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if not ext:
        ext = {
            "image/jpeg": "jpg",
            "image/png": "png",
            "image/heic": "heic",
            "image/heif": "heif",
        }.get(file.content_type or "", "jpg")

    # Save to uploads staging area (worker will move to /data/photos/originals/)
    file_path = UPLOADS_DIR / f"{sha256_hash}.{ext}"
    file_path.write_bytes(contents)

    try:
        # Create photo record and initial processing status
        photo = Photo(sha256_hash=sha256_hash, file_extension=ext)
        db.add(photo)
        db.flush()
        db.add(ProcessingStatus(photo_id=photo.id, state=ProcessingState.pending))
        db.commit()
        db.refresh(photo)
    except Exception:
        db.rollback()
        if file_path.exists():
            file_path.unlink()
        raise

    await emit_loganne_event("photoAdded", f"Photo {photo.id} added to lucos_photos")

    return photo_to_dict(photo)


def face_to_dict(face: Face) -> dict:
    return {
        "id": str(face.id),
        "photoId": str(face.photo_id),
        "personId": str(face.person_id) if face.person_id else None,
        "personConfirmed": face.person_confirmed,
        "boundingBox": {
            "x": face.bbox_x,
            "y": face.bbox_y,
            "width": face.bbox_width,
            "height": face.bbox_height,
        },
    }


def person_to_dict(person: Person, photo_count: Optional[int] = None) -> dict:
    data = {
        "id": str(person.id),
        "name": person.display_name,
        "contactId": person.contact_id,
        "createdAt": person.created_at.isoformat() if person.created_at else None,
    }
    if photo_count is not None:
        data["photoCount"] = photo_count
    return data


@app.get("/persons")
def list_persons(
    _: Annotated[None, Depends(verify_key)],
    includePhotoCounts: bool = False,
    limit: int = 100,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    query = db.query(Person).order_by(Person.created_at.asc())

    if includePhotoCounts:
        # Join with PhotoPerson to count photos
        query = db.query(
            Person,
            func.count(PhotoPerson.photo_id).label("photo_count")
        ).outerjoin(PhotoPerson).group_by(Person.id).order_by(Person.created_at.asc())

        persons_with_counts = query.offset(offset).limit(limit).all()
        return [person_to_dict(p, count) for p, count in persons_with_counts]
    else:
        persons = query.offset(offset).limit(limit).all()
        return [person_to_dict(p) for p in persons]


@app.post("/persons", status_code=status.HTTP_201_CREATED)
async def create_person(
    body: dict,
    _: Annotated[None, Depends(verify_key)],
    db: Session = Depends(get_db),
):
    name = body.get("name")
    if not name:
        raise HTTPException(status_code=422, detail="name is required")

    contact_id = body.get("contactId")

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


@app.get("/persons/{person_id}")
def get_person(
    person_id: str,
    _: Annotated[None, Depends(verify_key)],
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

    # Get photos assigned to this person (via PhotoPerson)
    photos = db.query(Photo).join(PhotoPerson).filter(PhotoPerson.person_id == person_uuid).all()

    data = person_to_dict(person)
    data["faceCount"] = face_count
    data["photos"] = [photo_to_dict(p) for p in photos]

    return data


def sync_photo_person(db: Session, photo_id) -> None:
    """Ensure photo_person table reflects all confirmed/unconfirmed person assignments for a photo."""
    # Collect the set of person_ids currently assigned to faces for this photo
    face_person_ids = {
        f.person_id
        for f in db.query(Face).filter(Face.photo_id == photo_id, Face.person_id.is_not(None)).all()
    }

    # Remove photo_person rows for persons no longer assigned to any face
    existing_rows = db.query(PhotoPerson).filter(PhotoPerson.photo_id == photo_id).all()
    for row in existing_rows:
        if row.person_id not in face_person_ids:
            db.delete(row)

    # Add missing photo_person rows
    existing_person_ids = {row.person_id for row in existing_rows}
    for pid in face_person_ids:
        if pid not in existing_person_ids:
            db.add(PhotoPerson(photo_id=photo_id, person_id=pid))


@app.get("/photos/{photo_id}/faces")
def list_faces(
    photo_id: str,
    _: Annotated[None, Depends(verify_key)],
    db: Session = Depends(get_db),
):
    try:
        photo_uuid = uuid.UUID(photo_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Photo not found")

    photo = db.query(Photo).filter(Photo.id == photo_uuid).first()
    if not photo:
        raise HTTPException(status_code=404, detail="Photo not found")

    faces = db.query(Face).filter(Face.photo_id == photo_uuid).all()
    return [face_to_dict(f) for f in faces]


@app.put("/faces/{face_id}/person")
async def assign_person(
    face_id: str,
    body: dict,
    _: Annotated[None, Depends(verify_key)],
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

    await emit_loganne_event("personTagged", f"Person {person_uuid} tagged on face {face_uuid} in photo {face.photo_id}")

    return face_to_dict(face)


@app.delete("/faces/{face_id}/person", status_code=status.HTTP_204_NO_CONTENT)
def unassign_person(
    face_id: str,
    _: Annotated[None, Depends(verify_key)],
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
