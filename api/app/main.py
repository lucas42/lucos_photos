import hashlib
import io
import os
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, UploadFile, status
from fastapi.responses import JSONResponse
from PIL import Image
from sqlalchemy.orm import Session

from lucos_photos_common.database import SessionLocal
from lucos_photos_common.models import Photo, ProcessingState, ProcessingStatus

app = FastAPI(title="lucos_photos")

UPLOADS_DIR = Path("/data/uploads")


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
    contents = await file.read()
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
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    (UPLOADS_DIR / f"{sha256_hash}.{ext}").write_bytes(contents)

    # Create photo record and initial processing status
    photo = Photo(sha256_hash=sha256_hash, file_extension=ext)
    db.add(photo)
    db.flush()
    db.add(ProcessingStatus(photo_id=photo.id, state=ProcessingState.pending))
    db.commit()
    db.refresh(photo)

    await emit_loganne_event("photoAdded", f"Photo {photo.id} added to lucos_photos")

    return photo_to_dict(photo)
