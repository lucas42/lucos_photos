import asyncio
import hashlib
import os
import re
import shutil
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Optional
from urllib.parse import quote, urlencode, urlparse

import httpx
import mimeparse
from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image
from redis import Redis
from rq import Queue
from rq.job import Retry
from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lucos_photos_common.database import SessionLocal
from lucos_photos_common.jobs import sync_photo_person
from lucos_photos_common.models import Face, MediaItem, Person, PhotoPerson, ProcessingState, ProcessingStatus, TelemetryEvent

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
DERIVATIVES_DIR = Path("/data/photos/derivatives")
STATIC_DIR = Path(__file__).parent / "static"
TEMPLATES_DIR = Path(__file__).parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

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


def _is_valid_key(authorization: str | None) -> bool:
    """Return True if the Authorization header contains a valid CLIENT_KEYS entry.

    Accepts both 'Bearer <token>' and 'key <token>' schemes (the Android app uses
    the latter). Returns False if the header is absent, malformed, or the token is
    not in CLIENT_KEYS.
    """
    if not authorization:
        return False
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() not in ("bearer", "key"):
        return False
    token = parts[1]
    client_keys_str = os.environ.get("CLIENT_KEYS", "")
    valid_keys = {entry.split("=", 1)[1] for entry in client_keys_str.split(";") if "=" in entry}
    return token in valid_keys


def verify_key(authorization: Annotated[str | None, Header()] = None):
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header required", headers=WWW_AUTHENTICATE)
    if not _is_valid_key(authorization):
        raise HTTPException(status_code=401, detail="Invalid key", headers=WWW_AUTHENTICATE)


async def verify_session_or_key(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    auth_token: Annotated[str | None, Cookie()] = None,
):
    """Accept either key auth (Authorization: key/Bearer <token>) or a session cookie.

    Used for endpoints that need to be callable both from the browser (cookie auth)
    and from machine-to-machine clients like the Android app (key auth).
    If an Authorization header is present and contains a valid key, auth succeeds
    immediately. Otherwise, falls through to session cookie validation.

    Note: if an Authorization header is present but the key is invalid, the request
    still falls through to session cookie validation. This is intentional — a browser
    user with a stale or unrelated Authorization header set (e.g. from a dev tool)
    will still be authenticated via cookie rather than being locked out.
    """
    if _is_valid_key(authorization):
        return  # key auth succeeded

    await verify_session(request, auth_token)


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


async def emit_loganne_event(event_type: str, human_readable: str, url: str | None = None):
    from loganne import updateLoganne
    await asyncio.to_thread(updateLoganne, event_type, human_readable, url)


async def fetch_contact_name(contact_id: str) -> Optional[str]:
    """Fetch a contact's name from lucos_contacts. Returns None on any failure."""
    contacts_url = os.environ.get("LUCOS_CONTACTS_URL", "")
    contacts_key = os.environ.get("KEY_LUCOS_CONTACTS", "")
    if not contacts_url or not contacts_key:
        print(f"Warning: LUCOS_CONTACTS_URL or KEY_LUCOS_CONTACTS not set, cannot fetch contact name for {contact_id}")
        return None
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{contacts_url}/people/{contact_id}",
                headers={"Accept": "application/json", "Authorization": f"key {contacts_key}"},
                timeout=5.0,
            )
            response.raise_for_status()
            data = response.json()
            return data.get("name") or None
    except Exception as e:
        print(f"Warning: failed to fetch contact name for {contact_id}: {e}")
        return None


def photo_url(photo_id) -> str:
    """Return the absolute URL for a photo's HTML view."""
    app_origin = os.environ.get("APP_ORIGIN", "")
    return f"{app_origin}/photos/{photo_id}"


def person_profile_picture_url(person_id) -> Optional[str]:
    """Return the absolute URL for a person's profile picture, or None if none exists."""
    profile_path = DERIVATIVES_DIR / f"{person_id}_profile.jpg"
    if not profile_path.exists():
        return None
    app_origin = os.environ.get("APP_ORIGIN", "")
    return f"{app_origin}/people/{person_id}/profile-picture"


def photo_file_urls(photo: MediaItem) -> tuple[str, str]:
    """Return (originalUrl, thumbnailUrl) for a photo, using the canonical file-serving routes."""
    ext = photo.file_extension or "bin"
    app_origin = os.environ.get("APP_ORIGIN", "")
    original_url = f"{app_origin}/photo_files/original/{photo.id}.{ext}"
    thumbnail_url = f"{app_origin}/photo_files/thumbnail/{photo.id}.{ext}"
    return original_url, thumbnail_url


def photo_to_dict(photo: MediaItem) -> dict:
    original_url, thumbnail_url = photo_file_urls(photo)
    return {
        "id": str(photo.id),
        "sha256Hash": photo.sha256_hash,
        "fileExtension": photo.file_extension,
        "mediaType": photo.media_type,
        "takenAt": photo.taken_at.isoformat() if photo.taken_at else None,
        "uploadedAt": photo.uploaded_at.isoformat() if photo.uploaded_at else None,
        "width": photo.width,
        "height": photo.height,
        "originalUrl": original_url,
        "thumbnailUrl": thumbnail_url,
    }


@app.get("/healthcheck")
def healthcheck():
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
async def root(request: Request, _: Annotated[None, Depends(verify_session)]):
    return templates.TemplateResponse(request, "index.html", {"current_page": "photos"})


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
    x_taken_at: Annotated[str | None, Header()] = None,
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

        # Parse the X-Taken-At header (Unix milliseconds) into a timezone-aware datetime.
        # This is a client-supplied hint (e.g. MediaStore DATE_TAKEN on Android) used as a
        # fallback taken_at for photos that lack an EXIF DateTimeOriginal tag.  The worker
        # will overwrite this with the EXIF value if one is present, since EXIF is more
        # authoritative than the OS-level timestamp.
        client_taken_at = None
        if x_taken_at:
            try:
                taken_at_ms = int(x_taken_at)
                if taken_at_ms > 0:
                    client_taken_at = datetime.fromtimestamp(taken_at_ms / 1000.0, tz=timezone.utc)
            except (ValueError, OverflowError, OSError):
                # Ignore malformed or out-of-range values; taken_at will remain null.
                pass

        # Idempotency: if a media item with this hash already exists, return it.
        # If the existing record lacks taken_at and the client supplied one this time,
        # update the record — the first upload may have been sent without the header.
        existing = db.query(MediaItem).filter(MediaItem.sha256_hash == sha256_hash).first()
        if existing:
            if client_taken_at is not None and existing.taken_at is None:
                existing.taken_at = client_taken_at
                db.commit()
                db.refresh(existing)
                print(f"upload_photo: updated taken_at to client-supplied {client_taken_at} for existing photo {sha256_hash}", flush=True)
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

        if client_taken_at is not None:
            print(f"upload_photo: storing client-supplied taken_at {client_taken_at} for photo {sha256_hash}", flush=True)

        try:
            # Create media item record and initial processing status
            media_type = "video" if is_video else "photo"
            photo = MediaItem(sha256_hash=sha256_hash, file_extension=ext, media_type=media_type, taken_at=client_taken_at)
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
        await emit_loganne_event("videoAdded", f"Video {photo.id} added to lucos_photos", url=photo_url(photo.id))
    else:
        await emit_loganne_event("photoAdded", f"Photo {photo.id} added to lucos_photos", url=photo_url(photo.id))

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
    db: Session = Depends(get_db),
):
    # Order by taken_at (most recent first), falling back to uploaded_at for photos
    # without a known taken_at date.
    order_cols = [MediaItem.taken_at.desc().nullslast(), MediaItem.uploaded_at.desc()]

    # Only include media items that have been fully processed (thumbnail generated,
    # face detection complete). Items with pending/processing/failed status — or no
    # status row at all — are hidden until the worker finishes with them.
    processed_filter = (
        db.query(MediaItem)
        .join(ProcessingStatus, MediaItem.id == ProcessingStatus.photo_id)
        .filter(ProcessingStatus.state == ProcessingState.complete)
    )

    photos = processed_filter.order_by(*order_cols).offset(offset).limit(limit).all()
    total = processed_filter.count()
    return {
        "photos": [photo_to_dict(p) for p in photos],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@app.get("/photos/{photo_id}")
def get_photo(
    photo_id: str,
    request: Request,
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

    # Content negotiation: use python-mimeparse to pick between HTML and JSON
    # following the HTTP standard (quality values, specificity rules, etc.).
    # text/html is listed first so that */* (the default when no Accept is sent)
    # resolves to application/json — mimeparse picks the last item on equal quality.
    accept_header = request.headers.get("accept", "*/*")
    best_match = mimeparse.best_match(["text/html", "application/json"], accept_header)
    if best_match == "text/html":
        return templates.TemplateResponse(request, "photo.html", {"photo": data, "current_page": "photos"}, headers={"Vary": "Accept"})

    return JSONResponse(content=data, headers={"Vary": "Accept"})


@app.delete("/photos/{photo_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_photo(
    photo_id: str,
    _: Annotated[None, Depends(verify_session)],
    db: Session = Depends(get_db),
):
    """Delete a photo, all its associated data, and its physical files on disk."""
    photo = _get_photo_or_404(photo_id, db)

    # Capture file info before deleting the DB row
    sha = photo.sha256_hash
    ext = photo.file_extension
    media_type = photo.media_type

    # Delete dependent rows manually (no cascade defined on the models)
    db.query(Face).filter(Face.photo_id == photo.id).delete()
    db.query(PhotoPerson).filter(PhotoPerson.photo_id == photo.id).delete()
    db.query(ProcessingStatus).filter(ProcessingStatus.photo_id == photo.id).delete()
    db.delete(photo)
    db.commit()

    # Remove physical files after the DB commit so a failed file deletion doesn't
    # leave an orphaned DB row. Missing files are silently ignored.
    files_to_delete = [
        PHOTOS_DIR / "originals" / f"{sha}.{ext}",
        PHOTOS_DIR / "derivatives" / f"{sha}.{ext}",
    ]
    if media_type == "video":
        files_to_delete.append(PHOTOS_DIR / "derivatives" / f"{sha}_thumb.jpg")
    for f in files_to_delete:
        f.unlink(missing_ok=True)

    await emit_loganne_event("photoDeleted", f"Photo {photo_id} deleted from lucos_photos")


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


@app.get("/photo_files/original/{photo_id_with_ext}")
def get_photo_original(
    photo_id_with_ext: str,
    _: Annotated[None, Depends(verify_session)],
    db: Session = Depends(get_db),
    range: Annotated[str | None, Header()] = None,
):
    """Serve the full-resolution original file, with HTTP range request support for video seeking.

    Expects ``{photo_id}.{ext}`` as the path segment — the extension is ignored (the authoritative
    extension comes from the database) but is required so that browsers and download managers
    save the file with a meaningful filename.
    """
    photo_id = photo_id_with_ext.rsplit(".", 1)[0]
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


@app.get("/photo_files/thumbnail/{photo_id_with_ext}")
def get_photo_thumbnail(
    photo_id_with_ext: str,
    _: Annotated[None, Depends(verify_session)],
    db: Session = Depends(get_db),
):
    """Serve the thumbnail/derivative of a media item.

    Expects ``{photo_id}.{ext}`` as the path segment — the extension is ignored (the authoritative
    extension comes from the database) but is required so that browsers and download managers
    save the file with a meaningful filename.

    For photos: tries a resized derivative, falls back to the original.
    For videos: serves the ffmpeg-generated JPEG thumbnail (``{hash}_thumb.jpg``).
    """
    photo_id = photo_id_with_ext.rsplit(".", 1)[0]
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

    # Photos: try resized derivative (JPEG thumbnail) first, fall back to original
    thumb_path = PHOTOS_DIR / "derivatives" / f"{photo.sha256_hash}_thumb.jpg"
    if thumb_path.exists():
        return FileResponse(
            path=thumb_path,
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )
    original_path = PHOTOS_DIR / "originals" / f"{photo.sha256_hash}.{ext}"
    if not original_path.exists():
        raise HTTPException(status_code=404, detail="Photo file not found")
    media_type = EXTENSION_MIME_TYPES.get(ext, "application/octet-stream")
    return FileResponse(
        path=original_path,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@app.get("/photos/{photo_id}/original")
def redirect_photo_original(
    photo_id: str,
    _: Annotated[None, Depends(verify_session)],
    db: Session = Depends(get_db),
):
    """Redirect legacy URL to the canonical file-serving route."""
    photo = _get_photo_or_404(photo_id, db)
    ext = photo.file_extension or "bin"
    return RedirectResponse(
        url=f"/photo_files/original/{photo.id}.{ext}",
        status_code=status.HTTP_301_MOVED_PERMANENTLY,
    )


@app.get("/photos/{photo_id}/thumbnail")
def redirect_photo_thumbnail(
    photo_id: str,
    _: Annotated[None, Depends(verify_session)],
    db: Session = Depends(get_db),
):
    """Redirect legacy URL to the canonical file-serving route."""
    photo = _get_photo_or_404(photo_id, db)
    ext = photo.file_extension or "bin"
    return RedirectResponse(
        url=f"/photo_files/thumbnail/{photo.id}.{ext}",
        status_code=status.HTTP_301_MOVED_PERMANENTLY,
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
        "profilePictureUrl": person_profile_picture_url(str(person.id)),
    }
    if photo_count is not None:
        data["photoCount"] = photo_count
    return data


PEOPLE_SORT_ORDER = [
    Person.profile_photo_id.is_(None).asc(),
    Person.display_name.asc().nullslast(),
    Person.id.asc(),
]


@app.get("/people")
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


@app.get("/people/{person_id}/profile-picture")
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


@app.put("/people/{person_id}/contact")
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


@app.delete("/people/{person_id}/contact", status_code=status.HTTP_204_NO_CONTENT)
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

    await emit_loganne_event("personTagged", f"Person {person_uuid} tagged on face {face_uuid} in photo {face.photo_id}", url=photo_url(face.photo_id))

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


@app.post("/api/telemetry", status_code=status.HTTP_201_CREATED)
def create_telemetry_event(
    body: dict,
    _: Annotated[None, Depends(verify_key)],
    db: Session = Depends(get_db),
):
    """Record a telemetry event from a client (e.g. the Android app).

    The ``timestamp`` field is the client-supplied event time (ISO-8601, UTC).
    The server also records ``received_at`` independently so that telemetry
    remains useful even when the client clock is wrong.
    """
    event_type = body.get("event_type")
    if not event_type:
        raise HTTPException(status_code=422, detail="event_type is required")

    # Parse optional ISO-8601 timestamp supplied by the client
    client_timestamp = None
    raw_ts = body.get("timestamp")
    if raw_ts is not None:
        try:
            # Replace trailing 'Z' with '+00:00' so fromisoformat works on all Python versions
            normalized = re.sub(r'Z$', '+00:00', str(raw_ts))
            client_timestamp = datetime.fromisoformat(normalized)
            if client_timestamp.tzinfo is None:
                client_timestamp = client_timestamp.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            raise HTTPException(status_code=422, detail="timestamp must be a valid ISO-8601 datetime")

    event = TelemetryEvent(
        event_type=event_type,
        app_version=body.get("app_version"),
        timestamp=client_timestamp,
        data=body.get("data"),
    )
    db.add(event)
    db.commit()
    db.refresh(event)

    return {
        "id": str(event.id),
        "event_type": event.event_type,
        "app_version": event.app_version,
        "timestamp": event.timestamp.isoformat() if event.timestamp else None,
        "received_at": event.received_at.isoformat(),
        "data": event.data,
    }


@app.get("/api/telemetry")
def list_telemetry_events(
    _: Annotated[None, Depends(verify_key)],
    event_type: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    """List recorded telemetry events, optionally filtered by type and date.

    ``since`` is an ISO-8601 date or datetime; filtering is applied against
    ``received_at`` (the server-side receipt timestamp).
    """
    query = db.query(TelemetryEvent)

    if event_type:
        query = query.filter(TelemetryEvent.event_type == event_type)

    if since:
        try:
            normalized = re.sub(r'Z$', '+00:00', str(since))
            # Accept date-only strings by appending midnight UTC
            if 'T' not in normalized and '+' not in normalized:
                normalized = f"{normalized}T00:00:00+00:00"
            since_dt = datetime.fromisoformat(normalized)
            if since_dt.tzinfo is None:
                since_dt = since_dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            raise HTTPException(status_code=422, detail="since must be a valid ISO-8601 date or datetime")
        query = query.filter(TelemetryEvent.received_at >= since_dt)

    events = query.order_by(TelemetryEvent.received_at.desc()).limit(limit).all()

    return {
        "events": [
            {
                "id": str(e.id),
                "event_type": e.event_type,
                "app_version": e.app_version,
                "timestamp": e.timestamp.isoformat() if e.timestamp else None,
                "received_at": e.received_at.isoformat(),
                "data": e.data,
            }
            for e in events
        ],
        "count": len(events),
    }


GITHUB_RELEASES_API_URL = "https://api.github.com/repos/lucas42/lucos_photos_android/releases/latest"
GITHUB_RELEASES_LIST_URL = "https://api.github.com/repos/lucas42/lucos_photos_android/releases?per_page=10"
_APP_LATEST_CACHE: dict = {"data": None, "fetched_at": 0.0}
_APP_LATEST_CACHE_TTL = 300  # 5 minutes
_APP_LATEST_ERROR_CACHE: dict = {"error": None, "fetched_at": 0.0}
_APP_LATEST_ERROR_CACHE_TTL = 60  # 1 minute

_GITHUB_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}


def _extract_apk_result(release: dict, updating: bool = False) -> dict | None:
    """Extract version/download_url/released_at from a GitHub release dict.

    Returns None if the release has no APK asset.
    Includes 'updating: true' when a newer release is being published.
    """
    assets = release.get("assets", [])
    apk_asset = next((a for a in assets if a.get("name", "").endswith(".apk")), None)
    if not apk_asset:
        return None

    tag_name: str = release.get("tag_name", "")
    version = tag_name.lstrip("v") if tag_name else tag_name
    released_at: str = release.get("published_at") or release.get("created_at", "")
    download_url: str = apk_asset.get("browser_download_url", "")

    result: dict = {
        "version": version,
        "download_url": download_url,
        "released_at": released_at,
    }
    if updating:
        result["updating"] = True
    return result


async def _fetch_latest_app_release() -> dict:
    """Fetch the latest release from GitHub Releases API, with a 5-minute in-memory cache.

    Returns a dict with version, download_url, and released_at.

    If the latest GitHub release has no APK yet (i.e. a release is currently being
    published), falls back to the most recent release that does have an APK, and
    includes 'updating: true' in the response so the UI can signal this to the user.

    Raises HTTPException 404 if no release with an APK is found anywhere.
    Raises HTTPException 502 if the GitHub API is unreachable.

    Both successful and error results are cached to avoid hammering GitHub during
    transient failures. Successful results are cached for 5 minutes; errors for 60 seconds.
    """
    now = time.monotonic()
    if _APP_LATEST_CACHE["data"] is not None and now - _APP_LATEST_CACHE["fetched_at"] < _APP_LATEST_CACHE_TTL:
        return _APP_LATEST_CACHE["data"]

    if _APP_LATEST_ERROR_CACHE["error"] is not None and now - _APP_LATEST_ERROR_CACHE["fetched_at"] < _APP_LATEST_ERROR_CACHE_TTL:
        cached_error = _APP_LATEST_ERROR_CACHE["error"]
        raise HTTPException(status_code=cached_error["status_code"], detail=cached_error["detail"])

    def _cache_error_and_raise(status_code: int, detail: str) -> None:
        _APP_LATEST_ERROR_CACHE["error"] = {"status_code": status_code, "detail": detail}
        _APP_LATEST_ERROR_CACHE["fetched_at"] = now
        raise HTTPException(status_code=status_code, detail=detail)

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                GITHUB_RELEASES_API_URL,
                headers=_GITHUB_HEADERS,
                timeout=5.0,
            )
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        _cache_error_and_raise(502, f"Failed to reach GitHub Releases API: {exc}")

    if resp.status_code == 404:
        _cache_error_and_raise(404, "No app releases found")

    if not resp.is_success:
        _cache_error_and_raise(502, f"GitHub Releases API returned {resp.status_code}")

    release = resp.json()
    result = _extract_apk_result(release)

    if result is None:
        # The latest release exists but has no APK yet — a release is in progress.
        # Fall back to the most recent release that does have an APK so users can
        # still download a working version while the new one is being published.
        try:
            async with httpx.AsyncClient() as client:
                list_resp = await client.get(
                    GITHUB_RELEASES_LIST_URL,
                    headers=_GITHUB_HEADERS,
                    timeout=5.0,
                )
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            _cache_error_and_raise(502, f"Failed to reach GitHub Releases API: {exc}")

        if not list_resp.is_success:
            _cache_error_and_raise(502, f"GitHub Releases API returned {list_resp.status_code}")

        releases = list_resp.json()
        for candidate in releases:
            result = _extract_apk_result(candidate, updating=True)
            if result is not None:
                break

        if result is None:
            _cache_error_and_raise(404, "No APK asset found in any recent release")

    _APP_LATEST_CACHE["data"] = result
    _APP_LATEST_CACHE["fetched_at"] = now
    return result


@app.get("/api/app/latest")
async def get_app_latest(_: Annotated[None, Depends(verify_session_or_key)]):
    """Return the version number and download URL of the latest Android app release.

    Fetches from GitHub Releases API and caches the result for 5 minutes.
    Returns 404 if no release exists yet, or 502 if the GitHub API is unreachable.

    Accepts either session cookie auth (browser) or key auth (Android app).
    """
    return await _fetch_latest_app_release()


@app.get("/app", include_in_schema=False)
async def app_page(_: Annotated[None, Depends(verify_session)]):
    return FileResponse(STATIC_DIR / "app.html")


app.mount("/", StaticFiles(directory=STATIC_DIR), name="static")