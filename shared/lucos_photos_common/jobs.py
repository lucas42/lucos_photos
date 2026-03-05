"""Job handlers for the lucos_photos worker.

Each function here is a job that can be enqueued in Redis via RQ.
All jobs must be idempotent — they may be retried on failure.
"""

import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import httpx
from redis import Redis
from rq import Queue
from rq.job import Retry

from lucos_photos_common.database import SessionLocal
from lucos_photos_common.models import Face, Photo, ProcessingState, ProcessingStatus

logger = logging.getLogger(__name__)

UPLOADS_DIR = Path("/data/uploads")
ORIGINALS_DIR = Path("/data/photos/originals")
DERIVATIVES_DIR = Path("/data/photos/derivatives")

THUMBNAIL_WIDTH = 400

# Cosine distance threshold for auto-assigning a person_id from a similar face.
# InsightFace ArcFace embeddings: cosine distance 0.0 = identical, 2.0 = opposite.
# Empirically, same-person matches are typically < 0.4; strangers are > 0.6.
FACE_SIMILARITY_THRESHOLD = 0.4

# Module-level singleton for the InsightFace model.
# Initialised lazily on first call to _get_face_analysis_app() so that importing
# this module doesn't trigger a heavy model load (important for tests and the API
# process which never runs face detection).
_face_analysis_app = None


def _get_face_analysis_app():
    """Return the shared FaceAnalysis instance, initialising it on first call.

    This avoids reloading ~500 MB of model weights for every photo processed.
    The singleton is process-scoped; each worker process loads the model once.
    """
    global _face_analysis_app
    if _face_analysis_app is None:
        import cv2  # noqa: F401 — imported here to keep the heavy dep out of the API process
        from insightface.app import FaceAnalysis

        insightface_root = os.environ.get("INSIGHTFACE_ROOT", os.path.expanduser("~/.insightface"))
        app = FaceAnalysis(name="buffalo_l", root=insightface_root, allowed_modules=["detection", "recognition"])
        # ctx_id=-1 means CPU inference
        app.prepare(ctx_id=-1, det_thresh=0.5, det_size=(640, 640))
        _face_analysis_app = app
        logger.info("_get_face_analysis_app: InsightFace model loaded (buffalo_l)")
    return _face_analysis_app


def detect_and_save_faces(db, photo: "Photo", image_path: Path) -> None:
    """Run InsightFace face detection on an image and persist results to the database.

    For each detected face:
    - Saves normalised bounding box coordinates (0.0–1.0) to the face table
    - Saves the 512-dimension ArcFace embedding vector
    - Searches for similar faces via pgvector cosine distance to auto-assign person_id

    This function is idempotent: if the photo already has face records, they are
    deleted and recreated. This ensures a clean result on retry.

    Args:
        db: SQLAlchemy session (caller is responsible for commit/close)
        photo: Photo ORM object (must have id, width, height set)
        image_path: Absolute path to the image file to analyse
    """
    import cv2

    if photo.width is None or photo.height is None:
        logger.warning("detect_and_save_faces: photo %s has no dimensions, skipping", photo.id)
        return

    app = _get_face_analysis_app()

    img_bgr = cv2.imread(str(image_path))
    if img_bgr is None:
        raise ValueError(f"cv2.imread returned None for {image_path} — file may be corrupt or unsupported")

    detected_faces = app.get(img_bgr)
    logger.info("detect_and_save_faces: detected %d face(s) in photo %s", len(detected_faces), photo.id)

    # Idempotency: delete any existing face records for this photo before reinserting
    existing_faces = db.query(Face).filter(Face.photo_id == photo.id).all()
    if existing_faces:
        logger.info("detect_and_save_faces: removing %d existing face record(s) for photo %s (idempotent re-run)", len(existing_faces), photo.id)
        for f in existing_faces:
            db.delete(f)
        db.flush()

    img_width = photo.width
    img_height = photo.height

    for detected in detected_faces:
        # bbox is [x1, y1, x2, y2] in absolute pixel coords
        x1, y1, x2, y2 = detected.bbox

        # Normalise to 0.0–1.0, clamp to valid range
        norm_x = max(0.0, min(1.0, float(x1) / img_width))
        norm_y = max(0.0, min(1.0, float(y1) / img_height))
        norm_w = max(0.0, min(1.0, float(x2 - x1) / img_width))
        norm_h = max(0.0, min(1.0, float(y2 - y1) / img_height))

        embedding_vector = None
        person_id = None

        if detected.embedding is not None:
            embedding_list = detected.embedding.tolist()
            embedding_vector = embedding_list

            # Search for the nearest existing face embedding using pgvector cosine distance.
            # Only look at faces with an embedding and a confirmed or ML-assigned person.
            # Short-circuit: skip the pgvector ORDER BY entirely if there are no candidates.
            # This avoids the pgvector operator on SQLite (used in tests) when the table is empty.
            from sqlalchemy import text
            candidate_filter = [
                Face.embedding.isnot(None),
                Face.person_id.isnot(None),
                Face.photo_id != photo.id,
            ]
            has_candidates = db.query(Face).filter(*candidate_filter).limit(1).first() is not None
            nearest = (
                db.query(Face)
                .filter(*candidate_filter)
                .order_by(Face.embedding.cosine_distance(embedding_list))
                .limit(1)
                .first()
            ) if has_candidates else None
            if nearest is not None:
                # Compute the actual distance to check against threshold
                dist_row = db.execute(
                    text("SELECT (:emb)::vector <=> embedding FROM face WHERE id = :fid"),
                    {"emb": str(embedding_list), "fid": str(nearest.id)},
                ).fetchone()
                if dist_row is not None and dist_row[0] is not None:
                    distance = float(dist_row[0])
                    if distance < FACE_SIMILARITY_THRESHOLD:
                        person_id = nearest.person_id
                        logger.info(
                            "detect_and_save_faces: auto-assigned person %s to face (distance=%.3f)",
                            person_id, distance,
                        )
                    else:
                        logger.info(
                            "detect_and_save_faces: nearest face distance %.3f exceeds threshold, no person assigned",
                            distance,
                        )

        face_record = Face(
            photo_id=photo.id,
            person_id=person_id,
            person_confirmed=False,
            bbox_x=norm_x,
            bbox_y=norm_y,
            bbox_width=norm_w,
            bbox_height=norm_h,
            embedding=embedding_vector,
        )
        db.add(face_record)

    db.flush()
    logger.info("detect_and_save_faces: saved %d face record(s) for photo %s", len(detected_faces), photo.id)


def emit_loganne_event(event_type: str, **extra_fields) -> None:
    """Fire a best-effort POST to Loganne.

    Failures are logged and swallowed — a Loganne outage must never fail a job.

    Args:
        event_type: The Loganne event type string (e.g. ``"photoProcessed"``).
        **extra_fields: Additional key/value pairs merged into the event payload.
    """
    loganne_endpoint = os.environ.get("LOGANNE_ENDPOINT", "")
    system = os.environ.get("SYSTEM", "lucos_photos")
    if not loganne_endpoint:
        logger.warning("emit_loganne_event: LOGANNE_ENDPOINT not set, skipping event %s", event_type)
        return
    payload = {"type": event_type, "humanReadable": event_type, "system": system}
    payload.update(extra_fields)
    try:
        response = httpx.post(loganne_endpoint, json=payload, timeout=5.0)
        response.raise_for_status()
        logger.info("emit_loganne_event: emitted %s to Loganne", event_type)
    except Exception as exc:
        logger.warning("emit_loganne_event: failed to emit %s: %s", event_type, exc)


def process_photo(photo_id: str) -> None:
    """Move an uploaded photo from staging to originals, extract metadata, and mark as complete.

    This is the primary job enqueued after a photo is uploaded via the API.
    It is idempotent: if the photo is already complete, it exits early.
    """
    photo_uuid = UUID(photo_id)
    db = SessionLocal()
    try:
        photo = db.query(Photo).filter(Photo.id == photo_uuid).first()
        if not photo:
            logger.warning("process_photo: photo %s not found", photo_id)
            return

        status = photo.processing_status
        if status is None:
            logger.warning("process_photo: no processing_status for photo %s", photo_id)
            return

        if status.state == ProcessingState.complete:
            logger.info("process_photo: photo %s already complete, skipping", photo_id)
            return

        # Mark as processing
        status.state = ProcessingState.processing
        status.error_message = None
        db.commit()

        try:
            # Move file from uploads staging to originals
            src = UPLOADS_DIR / f"{photo.sha256_hash}.{photo.file_extension}"
            ORIGINALS_DIR.mkdir(parents=True, exist_ok=True)
            dest = ORIGINALS_DIR / f"{photo.sha256_hash}.{photo.file_extension}"

            if not dest.exists():
                if src.exists():
                    shutil.move(str(src), str(dest))
                    logger.info("process_photo: moved %s to originals", src.name)
                else:
                    raise FileNotFoundError(
                        f"Upload file not found: {src} (and not already in originals)"
                    )
            else:
                logger.info("process_photo: %s already in originals, skipping move", dest.name)
                # Clean up staging copy if it still exists
                if src.exists():
                    src.unlink()

            # Extract image metadata (dimensions and EXIF taken-at date)
            from PIL import Image
            with Image.open(dest) as img:
                photo.width = img.width
                photo.height = img.height

                # Extract EXIF DateTimeOriginal if present (format: "YYYY:MM:DD HH:MM:SS")
                exif_data = img.getexif()
                EXIF_TAG_DATETIME_ORIGINAL = 36867
                raw_taken_at = exif_data.get(EXIF_TAG_DATETIME_ORIGINAL)
                if raw_taken_at:
                    try:
                        taken_at_naive = datetime.strptime(raw_taken_at, "%Y:%m:%d %H:%M:%S")
                        # EXIF datetimes have no timezone — treat as UTC
                        photo.taken_at = taken_at_naive.replace(tzinfo=timezone.utc)
                        logger.info("process_photo: extracted taken_at %s for photo %s", photo.taken_at, photo_id)
                    except ValueError:
                        logger.warning("process_photo: could not parse DateTimeOriginal %r for photo %s", raw_taken_at, photo_id)
                else:
                    logger.info("process_photo: no DateTimeOriginal EXIF tag for photo %s", photo_id)

            # Generate thumbnail derivative
            thumb_path = DERIVATIVES_DIR / f"{photo.sha256_hash}_thumb.jpg"
            DERIVATIVES_DIR.mkdir(parents=True, exist_ok=True)
            if not thumb_path.exists():
                with Image.open(dest) as img:
                    thumb_height = round(img.height * THUMBNAIL_WIDTH / img.width)
                    thumb = img.resize((THUMBNAIL_WIDTH, thumb_height))
                    thumb.save(thumb_path, format="JPEG", quality=85)
                logger.info("process_photo: generated thumbnail for photo %s at %s", photo_id, thumb_path)
            else:
                logger.info("process_photo: thumbnail already exists for photo %s, skipping", photo_id)

            # Run face detection and recognition
            detect_and_save_faces(db, photo, dest)

            # Mark as complete
            status.state = ProcessingState.complete
            db.commit()
            logger.info("process_photo: photo %s processed successfully (%dx%d)", photo_id, photo.width, photo.height)

            # Emit Loganne event — best-effort, must not fail the job
            try:
                emit_loganne_event("photoProcessed", photoId=photo_id)
            except Exception:
                logger.exception("process_photo: unexpected error emitting Loganne event for photo %s", photo_id)

        except Exception as exc:
            logger.exception("process_photo: error processing photo %s", photo_id)
            status.state = ProcessingState.failed
            status.error_message = str(exc)
            db.commit()
            raise

    finally:
        db.close()


def reprocess_photo(photo_id: str) -> None:
    """Reset a photo's processing state to pending and enqueue a fresh process_photo job.

    Used to trigger reprocessing of an already-processed (or failed) photo.
    """
    photo_uuid = UUID(photo_id)
    db = SessionLocal()
    try:
        photo = db.query(Photo).filter(Photo.id == photo_uuid).first()
        if not photo:
            logger.warning("reprocess_photo: photo %s not found", photo_id)
            return

        status = photo.processing_status
        if status is None:
            # Create a new status record if missing
            status = ProcessingStatus(photo_id=photo_uuid, state=ProcessingState.pending)
            db.add(status)
        else:
            status.state = ProcessingState.pending
            status.error_message = None
        db.commit()
        logger.info("reprocess_photo: reset photo %s to pending", photo_id)

    finally:
        db.close()

    # Re-enqueue process_photo immediately
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    redis_conn = Redis.from_url(redis_url)
    queue = Queue("photos", connection=redis_conn)
    queue.enqueue(process_photo, photo_id, retry=Retry(max=3, interval=[10, 30, 60]))
    logger.info("reprocess_photo: enqueued process_photo for photo %s", photo_id)
