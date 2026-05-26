"""Routes for photo upload, listing, retrieval, deletion, and file serving."""

import hashlib
import os
import re
import shutil
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode

import mimeparse
from fastapi import APIRouter, Depends, Header, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse, Response
from fastapi.templating import Jinja2Templates
from PIL import Image
from sqlalchemy import case, or_, and_
from sqlalchemy.orm import Session

from app.auth import verify_key, verify_session_or_key
from app.database import get_db
from app.redis_client import enqueue_process_media
from app.serializers import face_to_dict_simple, person_to_dict, photo_to_dict, photo_url
from app.services import emit_loganne_event
from lucos_photos_common.models import Face, MediaItem, Person, PhotoPerson, ProcessingState, ProcessingStatus, photo_date_label

router = APIRouter()

UPLOADS_DIR = Path("/data/uploads")
PHOTOS_DIR = Path("/data/photos")

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
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

# Patterns in uploaded filenames that indicate the file originated from TikTok.
# The Android app is supposed to filter these client-side, but some slip through;
# this acts as a server-side safety net.
_TIKTOK_FILENAME_PATTERNS = re.compile(
    r"tiktok|musically|snaptik|ssstik|tikmate|tik_tok",
    re.IGNORECASE,
)

_CONTENT_TYPE_TO_EXT = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/heic": "heic",
    "image/heif": "heif",
    "video/mp4": "mp4",
    "video/quicktime": "mov",
}

_RANGE_CHUNK_SIZE = 256 * 1024  # 256 KB streaming chunks for range responses


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


def _get_adjacent_photo_ids(photo: MediaItem, db: Session) -> tuple[str | None, str | None]:
    """Return (prev_id, next_id) for the given photo in the gallery ordering.

    The gallery is ordered by taken_at DESC NULLS LAST, uploaded_at DESC.
    "Previous" means the photo that appears earlier in this order (newer).
    "Next" means the photo that appears later in this order (older).
    Only fully processed photos are considered.
    """
    # Base query: only processed photos, excluding the current one
    base = (
        db.query(MediaItem.id)
        .join(ProcessingStatus, MediaItem.id == ProcessingStatus.photo_id)
        .filter(ProcessingStatus.state == ProcessingState.complete)
        .filter(MediaItem.id != photo.id)
    )

    # Use a case expression to handle NULLS LAST portably (works in both Postgres and SQLite).
    # has_taken_at = 0 when taken_at is NOT NULL (sorts first), 1 when NULL (sorts last).
    has_taken_at = case((MediaItem.taken_at.isnot(None), 0), else_=1)

    # "Previous" photo (newer — sorts before current in DESC order).
    # Photos that sort BEFORE current in the DESC ordering (i.e., are "newer"):
    #   (has_taken_at < current) OR
    #   (has_taken_at == current AND taken_at > current.taken_at) OR
    #   (has_taken_at == current AND taken_at == current.taken_at AND uploaded_at > current.uploaded_at)
    prev_filters = []
    if photo.taken_at is not None:
        # Current photo has taken_at: previous is anything with a later taken_at,
        # or same taken_at but later uploaded_at
        prev_filters.append(
            and_(has_taken_at == 0, MediaItem.taken_at > photo.taken_at)
        )
        prev_filters.append(
            and_(has_taken_at == 0, MediaItem.taken_at == photo.taken_at, MediaItem.uploaded_at > photo.uploaded_at)
        )
    else:
        # Current photo has NULL taken_at: previous could be any photo WITH taken_at,
        # or a NULL taken_at photo with later uploaded_at
        prev_filters.append(has_taken_at == 0)
        prev_filters.append(
            and_(has_taken_at == 1, MediaItem.uploaded_at > photo.uploaded_at)
        )

    prev_photo = (
        base.filter(or_(*prev_filters))
        .order_by(has_taken_at.desc(), MediaItem.taken_at.asc().nullslast(), MediaItem.uploaded_at.asc())
        .limit(1)
        .first()
    )

    # "Next" photo (older — sorts after current in DESC order).
    next_filters = []
    if photo.taken_at is not None:
        next_filters.append(
            and_(has_taken_at == 0, MediaItem.taken_at < photo.taken_at)
        )
        next_filters.append(
            and_(has_taken_at == 0, MediaItem.taken_at == photo.taken_at, MediaItem.uploaded_at < photo.uploaded_at)
        )
        # Any photo with NULL taken_at sorts after
        next_filters.append(has_taken_at == 1)
    else:
        # Current photo has NULL taken_at: next is a NULL taken_at photo with earlier uploaded_at
        next_filters.append(
            and_(has_taken_at == 1, MediaItem.uploaded_at < photo.uploaded_at)
        )

    next_photo = (
        base.filter(or_(*next_filters))
        .order_by(has_taken_at.asc(), MediaItem.taken_at.desc().nullslast(), MediaItem.uploaded_at.desc())
        .limit(1)
        .first()
    )

    prev_id = str(prev_photo[0]) if prev_photo else None
    next_id = str(next_photo[0]) if next_photo else None
    return prev_id, next_id


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


@router.post("/photos", status_code=status.HTTP_201_CREATED)
async def upload_photo(
    file: UploadFile,
    _: Annotated[None, Depends(verify_key)],
    db: Session = Depends(get_db),
    x_taken_at: Annotated[str | None, Header()] = None,
):
    content_type = file.content_type or ""
    filename = file.filename or ""

    # Server-side safety net: reject files with TikTok-related filenames.
    # The Android app filters these client-side, but some patterns slip through.
    if _TIKTOK_FILENAME_PATTERNS.search(filename):
        print(f"upload_photo: rejected TikTok filename {filename!r}", flush=True)
        raise HTTPException(status_code=422, detail="TikTok videos are not accepted")

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
        await emit_loganne_event("videoAdded", f"Video {photo_date_label(photo)} added to lucos_photos", url=photo_url(photo.id))
    else:
        await emit_loganne_event("photoAdded", f"Photo {photo_date_label(photo)} added to lucos_photos", url=photo_url(photo.id))

    # Enqueue a job for the worker to process this media item.
    # If Redis is unavailable, we log a warning and continue — the worker's
    # periodic pending sweep will catch it within a few minutes.
    enqueue_process_media(str(photo.id), media_type=media_type)

    return photo_to_dict(photo)


@router.get("/photos")
def list_photos(
    request: Request,
    _: Annotated[None, Depends(verify_session_or_key)],
    limit: int = 100,
    offset: int = 0,
    person_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    media_type: str | None = None,
    db: Session = Depends(get_db),
):
    # Order by taken_at (most recent first), falling back to uploaded_at for photos
    # without a known taken_at date.
    order_cols = [MediaItem.taken_at.desc().nullslast(), MediaItem.uploaded_at.desc()]

    # Only include media items that have been fully processed (thumbnail generated,
    # face detection complete). Items with pending/processing/failed status — or no
    # status row at all — are hidden until the worker finishes with them.
    query = (
        db.query(MediaItem)
        .join(ProcessingStatus, MediaItem.id == ProcessingStatus.photo_id)
        .filter(ProcessingStatus.state == ProcessingState.complete)
    )

    # Filter by person: find photos tagged with this person via PhotoPerson
    if person_id:
        try:
            person_uuid = uuid.UUID(person_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid person_id")
        query = query.join(PhotoPerson, MediaItem.id == PhotoPerson.photo_id).filter(
            PhotoPerson.person_id == person_uuid
        )

    # Filter by date range (on taken_at, ISO date strings YYYY-MM-DD)
    if date_from:
        try:
            parsed_from = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid date_from format, expected YYYY-MM-DD")
        query = query.filter(MediaItem.taken_at >= parsed_from)

    if date_to:
        try:
            parsed_to = datetime.strptime(date_to, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            parsed_to_end = parsed_to + timedelta(days=1)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid date_to format, expected YYYY-MM-DD")
        query = query.filter(MediaItem.taken_at < parsed_to_end)

    # Filter by media type (photo or video)
    if media_type:
        if media_type not in ("photo", "video"):
            raise HTTPException(status_code=422, detail="Invalid media_type, expected 'photo' or 'video'")
        query = query.filter(MediaItem.media_type == media_type)

    photos = query.order_by(*order_cols).offset(offset).limit(limit).all()
    total = query.count()

    # Build URL-encoded filter params string for pagination URLs
    active_filters = {}
    if person_id:
        active_filters["person_id"] = person_id
    if date_from:
        active_filters["date_from"] = date_from
    if date_to:
        active_filters["date_to"] = date_to
    if media_type:
        active_filters["media_type"] = media_type
    filter_params = ("&" + urlencode(active_filters)) if active_filters else ""

    accept_header = request.headers.get("accept", "*/*")
    best_match = mimeparse.best_match(["text/html", "application/json"], accept_header)
    if best_match == "text/html":
        prev_offset = max(0, offset - limit) if offset > 0 else None
        next_offset = offset + limit if offset + limit < total else None
        current_page_num = (offset // limit) + 1

        # Fetch people list for the filter dropdown
        base_person_filter = Person.is_background == False  # noqa: E712
        people = (
            db.query(Person)
            .filter(base_person_filter)
            .order_by(Person.display_name.asc().nullslast())
            .all()
        )

        return templates.TemplateResponse(request, "photos.html", {
            "photos": [photo_to_dict(p) for p in photos],
            "total": total,
            "limit": limit,
            "offset": offset,
            "prev_offset": prev_offset,
            "next_offset": next_offset,
            "current_page_num": current_page_num,
            "current_page": "photos",
            "filter_params": filter_params,
            "person_id": person_id or "",
            "date_from": date_from or "",
            "date_to": date_to or "",
            "media_type": media_type or "",
            "people": [{"id": str(p.id), "name": p.display_name} for p in people],
        }, headers={"Vary": "Accept"})

    return JSONResponse(content={
        "photos": [photo_to_dict(p) for p in photos],
        "total": total,
        "limit": limit,
        "offset": offset,
    }, headers={"Vary": "Accept"})


@router.get("/photos/{photo_id}")
def get_photo(
    photo_id: str,
    request: Request,
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

    processing_status = db.query(ProcessingStatus).filter(ProcessingStatus.photo_id == photo_uuid).first()
    faces = db.query(Face).filter(Face.photo_id == photo_uuid).all()

    data = photo_to_dict(photo)
    data["processingStatus"] = processing_status.state.value if processing_status else None
    data["faces"] = [face_to_dict_simple(f) for f in faces]
    people = (
        db.query(Person)
        .join(PhotoPerson, Person.id == PhotoPerson.person_id)
        .filter(PhotoPerson.photo_id == photo_uuid)
        .order_by(Person.display_name.asc().nullslast())
        .all()
    )
    data["people"] = [person_to_dict(p) for p in people]

    # Query adjacent photos for prev/next navigation.
    # Uses the same ordering as list_photos: taken_at DESC NULLS LAST, uploaded_at DESC.
    # "Previous" = newer (earlier in DESC order), "Next" = older (later in DESC order).
    prev_photo_id, next_photo_id = _get_adjacent_photo_ids(photo, db)
    data["prevPhotoId"] = prev_photo_id
    data["nextPhotoId"] = next_photo_id

    # Content negotiation: use python-mimeparse to pick between HTML and JSON
    # following the HTTP standard (quality values, specificity rules, etc.).
    # text/html is listed first so that */* (the default when no Accept is sent)
    # resolves to application/json — mimeparse picks the last item on equal quality.
    accept_header = request.headers.get("accept", "*/*")
    best_match = mimeparse.best_match(["text/html", "application/json"], accept_header)
    if best_match == "text/html":
        return templates.TemplateResponse(request, "photo.html", {"photo": data, "current_page": "photos"}, headers={"Vary": "Accept"})

    return JSONResponse(content=data, headers={"Vary": "Accept"})


@router.delete("/photos/{photo_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_photo(
    photo_id: str,
    _: Annotated[None, Depends(verify_session_or_key)],
    db: Session = Depends(get_db),
):
    """Delete a photo, all its associated data, and its physical files on disk."""
    photo = _get_photo_or_404(photo_id, db)

    # Capture info before deleting the DB row (ORM attributes are expired after commit)
    date_label = photo_date_label(photo)
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

    await emit_loganne_event("photoDeleted", f"Photo {date_label} deleted from lucos_photos")


@router.get("/photo_files/original/{photo_id_with_ext}")
def get_photo_original(
    photo_id_with_ext: str,
    _: Annotated[None, Depends(verify_session_or_key)],
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


@router.get("/photo_files/thumbnail/{photo_id_with_ext}")
def get_photo_thumbnail(
    photo_id_with_ext: str,
    _: Annotated[None, Depends(verify_session_or_key)],
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


@router.get("/photos/{photo_id}/original")
def redirect_photo_original(
    photo_id: str,
    _: Annotated[None, Depends(verify_session_or_key)],
    db: Session = Depends(get_db),
):
    """Redirect legacy URL to the canonical file-serving route."""
    photo = _get_photo_or_404(photo_id, db)
    ext = photo.file_extension or "bin"
    return RedirectResponse(
        url=f"/photo_files/original/{photo.id}.{ext}",
        status_code=status.HTTP_301_MOVED_PERMANENTLY,
    )


@router.get("/photos/{photo_id}/thumbnail")
def redirect_photo_thumbnail(
    photo_id: str,
    _: Annotated[None, Depends(verify_session_or_key)],
    db: Session = Depends(get_db),
):
    """Redirect legacy URL to the canonical file-serving route."""
    photo = _get_photo_or_404(photo_id, db)
    ext = photo.file_extension or "bin"
    return RedirectResponse(
        url=f"/photo_files/thumbnail/{photo.id}.{ext}",
        status_code=status.HTTP_301_MOVED_PERMANENTLY,
    )
