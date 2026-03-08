import asyncio
import hashlib
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Annotated, Optional
from urllib.parse import quote, urlencode, urlparse

import httpx
from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from redis import Redis
from rq import Queue
from rq.job import Retry
from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lucos_photos_common.database import SessionLocal
from lucos_photos_common.models import Face, MediaItem, Person, PhotoPerson, ProcessingState, ProcessingStatus

AUTH_DOMAIN = "https://auth.l42.eu"


def safe_path(path: str, fallback: str = "/") -> str:
    """Validate a URL path to prevent open redirects.

    Only allows relative paths (no scheme or netloc). Anything that would be
    interpreted as an external URL — e.g. //evil.com (protocol-relative) or
    https://evil.com — is rejected and replaced with the fallback.

    This should be applied to user-influenced path components *before* they are
    combined with APP_ORIGIN, so that an empty APP_ORIGIN cannot be combined
    with a crafted path to produce an external redirect.
    """
    parsed = urlparse(path)
    if parsed.scheme or parsed.netloc:
        return fallback
    return path


app = FastAPI(title="lucos_photos")


class _RedirectWithCookie(Exception):
    """Raised inside verify_session to deliver a redirect response with a Set-Cookie header.

    FastAPI's dependency injection doesn't support returning responses directly from
    dependencies, so we raise this exception and catch it in a middleware.
    """
    def __init__(self, response: RedirectResponse):
        self.response = response


@app.middleware("http")
async def catch_redirect_with_cookie(request: Request, call_next):
    """Catch _RedirectWithCookie raised inside verify_session and return the redirect response."""
    try:
        return await call_next(request)
    except _RedirectWithCookie as exc:
        return exc.response


UPLOADS_DIR = Path("/data/uploads")
PHOTOS_DIR = Path("/data/photos")
STATIC_DIR = Path(__file__).parent / "static"

EXTENSION_MIME_TYPES = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "heic": "image/heic",
    "heif": "image/heif",
    "gif": "image/gif",
    "webp": "image/webp",
    "mp4": "video/mp4",
    "mov": "video/quicktime",
}

MAX_PHOTO_SIZE = int(os.environ.get("MAX_PHOTO_SIZE", 100 * 1024 * 1024))
MAX_VIDEO_SIZE = int(os.environ.get("MAX_VIDEO_SIZE", 500 * 1024 * 1024))
MIN_FREE_DISK_SPACE = int(os.environ.get("MIN_FREE_DISK_SPACE", 500 * 1024 * 1024))

_UPLOAD_CHUNK_SIZE = 64 * 1024  # 64KB chunks

VIDEO_MIME_TYPES = {"video/mp4", "video/quicktime"}

_CONTENT_TYPE_TO_EXT = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/heic": "heic",
    "image/heif": "heif",
    "video/mp4": "mp4",
    "video/quicktime": "mov",
}

_redis_conn: Redis | None = None


def get_redis() -> Redis:
    global _redis_conn
    if _redis_conn is None:
        redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
        _redis_conn = Redis.from_url(redis_url)
    return _redis_conn


def enqueue_process_media(photo_id: str, media_type: str = "photo") -> None:
    """Enqueue a processing job for the given media item UUID string.

    Routes to process_video for videos, process_photo for photos.
    """
    if media_type == "video":
        from lucos_photos_common.jobs import process_video as _job_fn
    else:
        from lucos_photos_common.jobs import process_photo as _job_fn
    try:
        redis_conn = get_redis()
        queue = Queue("photos", connection=redis_conn)
        queue.enqueue(
            _job_fn,
            photo_id,
            retry=Retry(max=3, interval=[10, 30, 60]),
        )
    except Exception as exc:
        # Log but don't fail the upload — the worker's pending sweep will catch it.
        print(f"Warning: failed to enqueue {_job_fn.__name__} for {photo_id}: {exc}", flush=True)


# Keep the old name as an alias for backwards compatibility
enqueue_process_photo = enqueue_process_media


_RANGE_CHUNK_SIZE = 256 * 1024  # 256 KB streaming chunks for range responses


def _serve_file_with_range_support(
    file_path: Path,
    media_type: str,
    extra_headers: dict | None = None,
    range_header: str | None = None,
) -> Response:
    """Serve a file with HTTP range request support.

    Returns:
    - 206 Partial Content with the requested byte range if a valid Range header is present.
    - 416 Range Not Satisfiable if the range is invalid.
    - 200 OK with the full file and `Accept-Ranges: bytes` if no Range header is given.

    All responses include `Accept-Ranges: bytes` so clients know they can request ranges.
    """
    file_size = file_path.stat().st_size
    headers = dict(extra_headers or {})
    headers["Accept-Ranges"] = "bytes"

    if range_header:
        # Parse "bytes=start-end" (end is inclusive)
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
        if not match:
            raise HTTPException(
                status_code=416,
                detail="Range Not Satisfiable",
                headers={"Content-Range": f"bytes */{file_size}"},
            )

        start_str, end_str = match.group(1), match.group(2)

        if start_str == "" and end_str == "":
            raise HTTPException(
                status_code=416,
                detail="Range Not Satisfiable",
                headers={"Content-Range": f"bytes */{file_size}"},
            )

        if start_str == "":
            # Suffix range: last N bytes
            suffix_length = int(end_str)
            start = max(0, file_size - suffix_length)
            end = file_size - 1
        elif end_str == "":
            start = int(start_str)
            end = file_size - 1
        else:
            start = int(start_str)
            end = int(end_str)

        if start > end or start >= file_size or end >= file_size:
            raise HTTPException(
                status_code=416,
                detail="Range Not Satisfiable",
                headers={"Content-Range": f"bytes */{file_size}"},
            )

        content_length = end - start + 1
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
        headers["Content-Length"] = str(content_length)

        def _iter_file_range():
            with open(file_path, "rb") as f:
                f.seek(start)
                remaining = content_length
                while remaining > 0:
                    chunk = f.read(min(_RANGE_CHUNK_SIZE, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        return StreamingResponse(
            _iter_file_range(),
            status_code=206,
            media_type=media_type,
            headers=headers,
        )

    # No range header — serve the full file with Accept-Ranges advertised
    headers["Content-Length"] = str(file_size)
    return FileResponse(
        path=file_path,
        media_type=media_type,
        headers=headers,
    )


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


WWW_AUTHENTICATE = {"WWW-Authenticate": 'Bearer realm="lucos_photos"'}


def verify_key(authorization: Annotated[str | None, Header()] = None):
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header required", headers=WWW_AUTHENTICATE)
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() not in ("bearer", "key"):
        raise HTTPException(status_code=401, detail="Expected 'Bearer' authorization scheme", headers=WWW_AUTHENTICATE)
    token = parts[1]
    client_keys_str = os.environ.get("CLIENT_KEYS", "")
    valid_keys = {entry.split("=", 1)[1] for entry in client_keys_str.split(";") if "=" in entry}
    if token not in valid_keys:
        raise HTTPException(status_code=401, detail="Invalid key", headers=WWW_AUTHENTICATE)


async def verify_session(request: Request, auth_token: Annotated[str | None, Cookie()] = None):
    """Validate a user session via the lucos_authentication service.

    - If a ?token= query parameter is present (auth callback), validate it, set a
      cookie on the photos domain, and redirect to strip the token from the URL.
    - Browser requests (Accept: text/html) without a token are redirected to the
      auth service login page.
    - API requests receive a 401 JSON response.
    """
    # Check for token in query parameter (auth service callback)
    query_token = request.query_params.get("token")
    if query_token:
        data = await _validate_token_with_auth_service(query_token)
        if data and data.get("id"):
            # Strip the token from the URL so it doesn't linger in browser history
            app_origin = os.environ.get("APP_ORIGIN", "")
            # Validate the path before combining with APP_ORIGIN to prevent open redirects:
            # a crafted path like //evil.com would become a valid external redirect if
            # APP_ORIGIN is empty.
            path = safe_path(request.url.path)
            clean_url = f"{app_origin}{path}"
            # Preserve any other query params except 'token'
            other_params = {k: v for k, v in request.query_params.items() if k != "token"}
            if other_params:
                clean_url += "?" + urlencode(other_params)
            response = RedirectResponse(url=clean_url, status_code=status.HTTP_302_FOUND)
            response.set_cookie(
                key="auth_token",
                value=query_token,
                httponly=True,
                secure=True,
                samesite="lax",
            )
            raise _RedirectWithCookie(response)
        _auth_challenge(request)

    if not auth_token:
        _auth_challenge(request)

    data = await _validate_token_with_auth_service(auth_token)
    if not data or not data.get("id"):
        _auth_challenge(request)


async def _validate_token_with_auth_service(token: str) -> dict | None:
    """Call auth.l42.eu/data?token=<token> and return the JSON payload, or None on failure."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{AUTH_DOMAIN}/data",
                params={"token": token},
                timeout=5.0,
            )
            resp.raise_for_status()
            return resp.json()
    except (httpx.HTTPError, httpx.TimeoutException):
        return None


def _auth_challenge(request: Request):
    """Return redirect or 401 depending on whether the client is a browser."""
    accept = request.headers.get("accept", "")
    if "text/html" in accept:
        app_origin = os.environ.get("APP_ORIGIN", "")
        redirect_uri = quote(f"{app_origin}{request.url.path}", safe="")
        raise HTTPException(
            status_code=status.HTTP_302_FOUND,
            headers={"Location": f"{AUTH_DOMAIN}/authenticate?redirect_uri={redirect_uri}"},
        )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required",
        headers={"WWW-Authenticate": f'Bearer realm="{AUTH_DOMAIN}"'},
    )


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


def photo_to_dict(photo: MediaItem) -> dict:
    return {
        "id": str(photo.id),
        "sha256Hash": photo.sha256_hash,
        "fileExtension": photo.file_extension,
        "mediaType": photo.media_type,
        "takenAt": photo.taken_at.isoformat() if photo.taken_at else None,
        "uploadedAt": photo.uploaded_at.isoformat() if photo.uploaded_at else None,
        "width": photo.width,
        "height": photo.height,
    }


@app.get("/healthcheck")
def healthcheck():
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
async def root(_: Annotated[None, Depends(verify_session)]):
    return FileResponse(STATIC_DIR / "index.html")


CHECK_TIMEOUT = 0.5  # seconds — must be well under monitoring system's 1s hard limit


async def check_db() -> dict:
    """Check whether a connection to PostgreSQL can be established."""
    tech_detail = "Checks whether a connection to PostgreSQL can be established"
    try:
        db = SessionLocal()
        try:
            await asyncio.wait_for(asyncio.to_thread(db.execute, text("SELECT 1")), timeout=CHECK_TIMEOUT)
        finally:
            db.close()
        return {"ok": True, "techDetail": tech_detail}
    except Exception:
        return {"ok": False, "techDetail": tech_detail}


async def check_redis() -> dict:
    """Check whether Redis is reachable."""
    tech_detail = "Checks whether a connection to Redis can be established"
    try:
        redis_conn = get_redis()
        await asyncio.wait_for(asyncio.to_thread(redis_conn.ping), timeout=CHECK_TIMEOUT)
        return {"ok": True, "techDetail": tech_detail}
    except Exception:
        return {"ok": False, "techDetail": tech_detail}



async def get_metrics() -> dict:
    """Return live metrics: photo count, video count, and pending processing queue depth."""
    try:
        db = SessionLocal()
        try:
            photo_count = await asyncio.to_thread(
                lambda: db.query(MediaItem).filter(MediaItem.media_type == "photo").count()
            )
            video_count = await asyncio.to_thread(
                lambda: db.query(MediaItem).filter(MediaItem.media_type == "video").count()
            )
            pending_count = await asyncio.to_thread(
                lambda: db.query(ProcessingStatus).filter(
                    ProcessingStatus.state == ProcessingState.pending
                ).count()
            )
        finally:
            db.close()
        return {
            "photo-count": {
                "value": photo_count,
                "techDetail": "Total number of photos stored",
            },
            "video-count": {
                "value": video_count,
                "techDetail": "Total number of videos stored",
            },
            "processing-pending-count": {
                "value": pending_count,
                "techDetail": "Number of media items awaiting processing",
            },
        }
    except Exception:
        return {
            "photo-count": {
                "value": 0,
                "techDetail": "Total number of photos stored",
            },
            "video-count": {
                "value": 0,
                "techDetail": "Total number of videos stored",
            },
            "processing-pending-count": {
                "value": 0,
                "techDetail": "Number of media items awaiting processing",
            },
        }


@app.get("/_info")
async def info():
    db_check, redis_check, metrics = await asyncio.gather(
        check_db(),
        check_redis(),
        get_metrics(),
    )
    return {
        "system": os.environ.get("SYSTEM", "lucos_photos"),
        "checks": {
            "db-reachable": db_check,
            "redis-reachable": redis_check,
        },
        "metrics": metrics,
        "ci": {
            "circle": "gh/lucas42/lucos_photos",
        },
        "icon": "/icon.png",
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
    content_type = file.content_type or ""
    is_video = content_type in VIDEO_MIME_TYPES
    size_limit = MAX_VIDEO_SIZE if is_video else MAX_PHOTO_SIZE

    # Fast-path rejection: if Content-Length is provided and already too large, reject before streaming
    if file.size and file.size > size_limit:
        raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="File too large")

    # Check for sufficient free disk space
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    _, _, free = shutil.disk_usage(UPLOADS_DIR)
    if free < MIN_FREE_DISK_SPACE:
        raise HTTPException(status_code=status.HTTP_507_INSUFFICIENT_STORAGE, detail="Insufficient storage")

    # Stream the upload to a temp file, computing SHA256 incrementally.
    # We never hold the full file in memory — each chunk is written directly to disk.
    tmp_file = tempfile.NamedTemporaryFile(dir=UPLOADS_DIR, delete=False)
    tmp_path = Path(tmp_file.name)
    try:
        hasher = hashlib.sha256()
        total_bytes = 0
        try:
            while True:
                chunk = await file.read(_UPLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > size_limit:
                    raise HTTPException(
                        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                        detail="File too large",
                    )
                hasher.update(chunk)
                tmp_file.write(chunk)
        finally:
            tmp_file.close()

        sha256_hash = hasher.hexdigest()

        # Validate that the file is a valid image (videos are not validated here)
        if not is_video:
            try:
                with Image.open(tmp_path) as img:
                    img.verify()
            except HTTPException:
                raise
            except Exception:
                raise HTTPException(status_code=422, detail="Invalid image file")

        # Idempotency: if a media item with this hash already exists, return it
        existing = db.query(MediaItem).filter(MediaItem.sha256_hash == sha256_hash).first()
        if existing:
            return JSONResponse(status_code=200, content=photo_to_dict(existing))

        # Determine file extension from filename, falling back to content type
        filename = file.filename or ""
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if not ext:
            ext = _CONTENT_TYPE_TO_EXT.get(content_type, "jpg")

        # Move temp file to its final staging location
        file_path = UPLOADS_DIR / f"{sha256_hash}.{ext}"
        tmp_path.rename(file_path)
        tmp_path = None  # temp file has been moved; don't delete it in finally block

        try:
            # Create media item record and initial processing status
            media_type = "video" if is_video else "photo"
            photo = MediaItem(sha256_hash=sha256_hash, file_extension=ext, media_type=media_type)
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

    finally:
        # Clean up the temp file if it was never moved (failure path)
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink()

    if is_video:
        await emit_loganne_event("videoAdded", f"Video {photo.id} added to lucos_photos")
    else:
        await emit_loganne_event("photoAdded", f"Photo {photo.id} added to lucos_photos")

    # Enqueue a job for the worker to process this media item.
    # If Redis is unavailable, we log a warning and continue — the worker's
    # periodic pending sweep will catch it within a few minutes.
    enqueue_process_media(str(photo.id), media_type=media_type)

    return photo_to_dict(photo)


@app.get("/photos")
def list_photos(
    _: Annotated[None, Depends(verify_session)],
    limit: int = 100,
    offset: int = 0,
    order_by: str = "uploaded_at",
    db: Session = Depends(get_db),
):
    if order_by == "taken_at":
        order_col = MediaItem.taken_at.desc().nullslast()
    else:
        order_col = MediaItem.uploaded_at.desc()

    photos = db.query(MediaItem).order_by(order_col).offset(offset).limit(limit).all()
    total = db.query(MediaItem).count()
    return {
        "photos": [photo_to_dict(p) for p in photos],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@app.get("/photos/{photo_id}")
def get_photo(
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

    processing_status = db.query(ProcessingStatus).filter(ProcessingStatus.photo_id == photo_uuid).first()
    faces = db.query(Face).filter(Face.photo_id == photo_uuid).all()

    data = photo_to_dict(photo)
    data["processingStatus"] = processing_status.state.value if processing_status else None
    data["faces"] = [face_to_dict_simple(f) for f in faces]
    data["people"] = [
        str(pp.person_id)
        for pp in db.query(PhotoPerson).filter(PhotoPerson.photo_id == photo_uuid).all()
    ]
    return data


def face_to_dict_simple(face: Face) -> dict:
    """Minimal face dict for embedding inside a photo response."""
    return {
        "id": str(face.id),
        "personId": str(face.person_id) if face.person_id else None,
        "personConfirmed": face.person_confirmed,
        "boundingBox": {
            "x": face.bbox_x,
            "y": face.bbox_y,
            "width": face.bbox_width,
            "height": face.bbox_height,
        },
    }


def _get_photo_or_404(photo_id: str, db: Session) -> MediaItem:
    """Resolve a photo UUID string to a MediaItem model, raising 404 on not found or invalid UUID."""
    try:
        photo_uuid = uuid.UUID(photo_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Photo not found")
    photo = db.query(MediaItem).filter(MediaItem.id == photo_uuid).first()
    if not photo:
        raise HTTPException(status_code=404, detail="Photo not found")
    return photo


@app.get("/photos/{photo_id}/original")
def get_photo_original(
    photo_id: str,
    _: Annotated[None, Depends(verify_session)],
    db: Session = Depends(get_db),
    range: Annotated[str | None, Header()] = None,
):
    """Serve the full-resolution original file, with HTTP range request support for video seeking."""
    photo = _get_photo_or_404(photo_id, db)
    ext = photo.file_extension
    media_type = EXTENSION_MIME_TYPES.get(ext, "application/octet-stream")
    file_path = PHOTOS_DIR / "originals" / f"{photo.sha256_hash}.{ext}"
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Photo file not found")
    return _serve_file_with_range_support(
        file_path=file_path,
        media_type=media_type,
        extra_headers={"Cache-Control": "public, max-age=31536000, immutable"},
        range_header=range,
    )


@app.get("/photos/{photo_id}/thumbnail")
def get_photo_thumbnail(
    photo_id: str,
    _: Annotated[None, Depends(verify_session)],
    db: Session = Depends(get_db),
):
    """Serve the thumbnail/derivative of a media item.

    For photos: tries a resized derivative, falls back to the original.
    For videos: serves the ffmpeg-generated JPEG thumbnail (``{hash}_thumb.jpg``).
    """
    photo = _get_photo_or_404(photo_id, db)
    ext = photo.file_extension

    if photo.media_type == "video":
        # Video thumbnails are JPEG stills extracted by the worker
        thumb_path = PHOTOS_DIR / "derivatives" / f"{photo.sha256_hash}_thumb.jpg"
        if not thumb_path.exists():
            raise HTTPException(status_code=404, detail="Thumbnail not yet available")
        return FileResponse(
            path=thumb_path,
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    # Photos: try resized derivative first, fall back to original
    media_type = EXTENSION_MIME_TYPES.get(ext, "application/octet-stream")
    derivative_path = PHOTOS_DIR / "derivatives" / f"{photo.sha256_hash}.{ext}"
    if derivative_path.exists():
        file_path = derivative_path
    else:
        file_path = PHOTOS_DIR / "originals" / f"{photo.sha256_hash}.{ext}"
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Photo file not found")
    return FileResponse(
        path=file_path,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


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


@app.get("/people")
def list_people(
    _: Annotated[None, Depends(verify_session)],
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

        people_with_counts = query.offset(offset).limit(limit).all()
        return [person_to_dict(p, count) for p, count in people_with_counts]
    else:
        people = query.offset(offset).limit(limit).all()
        return [person_to_dict(p) for p in people]


@app.post("/people", status_code=status.HTTP_201_CREATED)
async def create_person(
    body: dict,
    _: Annotated[None, Depends(verify_session)],
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


@app.get("/people/{person_id}")
def get_person(
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

    face_count = db.query(Face).filter(Face.person_id == person_uuid).count()

    # Get photos assigned to this person (via PhotoPerson)
    photos = db.query(MediaItem).join(PhotoPerson).filter(PhotoPerson.person_id == person_uuid).all()

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

    # Remove photo_person rows for people no longer assigned to any face
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


@app.put("/faces/{face_id}/person")
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

    await emit_loganne_event("personTagged", f"Person {person_uuid} tagged on face {face_uuid} in photo {face.photo_id}")

    return face_to_dict(face)


@app.delete("/faces/{face_id}/person", status_code=status.HTTP_204_NO_CONTENT)
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


app.mount("/", StaticFiles(directory=STATIC_DIR), name="static")