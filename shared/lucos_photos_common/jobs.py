"""Job handlers for the lucos_photos worker.

Each function here is a job that can be enqueued in Redis via RQ.
All jobs must be idempotent — they may be retried on failure.
"""

import json
import logging
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from loganne import updateLoganne
from redis import Redis
from rq import Queue
from rq.job import Retry

from lucos_photos_common.database import SessionLocal
from lucos_photos_common.models import Face, MediaItem, Person, PhotoPerson, ProcessingState, ProcessingStatus

PHOTO_PROCESSED_CHANNEL = "photos:processed"

logger = logging.getLogger(__name__)

# Module-level Redis connection, shared across all job invocations in this process.
_redis: Redis | None = None


def _get_redis() -> Redis:
    """Return the module-level Redis connection, creating it on first call."""
    global _redis
    if _redis is None:
        redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
        _redis = Redis.from_url(redis_url)
    return _redis


def _publish_photo_processed(photo_id: str) -> None:
    """Publish a photo-processed notification to the Redis pub/sub channel.

    Non-fatal: Redis unavailability is logged but not raised.
    """
    try:
        message = json.dumps({"type": "photoProcessed", "photoId": photo_id})
        _get_redis().publish(PHOTO_PROCESSED_CHANNEL, message)
        logger.info("_publish_photo_processed: published event for photo %s", photo_id)
    except Exception:
        logger.exception("_publish_photo_processed: failed to publish event for photo %s", photo_id)

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


def detect_and_save_faces(db, photo: "MediaItem", image_path: Path) -> None:
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
    import numpy as np
    from PIL import Image, ImageOps

    if photo.width is None or photo.height is None:
        logger.warning("detect_and_save_faces: photo %s has no dimensions, skipping", photo.id)
        return

    app = _get_face_analysis_app()

    with Image.open(image_path) as _raw_img:
        img_rgb = ImageOps.exif_transpose(_raw_img).convert("RGB")
    img_bgr = cv2.cvtColor(np.array(img_rgb), cv2.COLOR_RGB2BGR)

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

        det_score = float(detected.det_score) if detected.det_score is not None else None
        kps = detected.kps.tolist() if detected.kps is not None else None

        face_record = Face(
            photo_id=photo.id,
            person_id=person_id,
            person_confirmed=False,
            bbox_x=norm_x,
            bbox_y=norm_y,
            bbox_width=norm_w,
            bbox_height=norm_h,
            embedding=embedding_vector,
            det_score=det_score,
            kps=kps,
        )
        db.add(face_record)

    db.flush()
    logger.info("detect_and_save_faces: saved %d face record(s) for photo %s", len(detected_faces), photo.id)




def _enqueue_profile_picture_for_photo(photo_uuid) -> None:
    """Enqueue generate_profile_picture for each person detected in a photo.

    Called after process_photo completes. Non-fatal: Redis unavailability is logged but not raised.
    """
    try:
        redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
        redis_conn = Redis.from_url(redis_url)
        queue = Queue("photos", connection=redis_conn)

        db = SessionLocal()
        try:
            person_ids = [
                str(f.person_id)
                for f in db.query(Face).filter(
                    Face.photo_id == photo_uuid,
                    Face.person_id.isnot(None),
                ).all()
            ]
        finally:
            db.close()

        for pid in set(person_ids):
            queue.enqueue(generate_profile_picture, pid, retry=Retry(max=3, interval=[10, 30, 60]))
            logger.info("_enqueue_profile_picture_for_photo: enqueued profile picture generation for person %s", pid)

    except Exception:
        logger.exception("_enqueue_profile_picture_for_photo: failed to enqueue profile picture jobs for photo %s", photo_uuid)


def _enqueue_profile_picture_for_persons(person_ids) -> None:
    """Enqueue generate_profile_picture for each person in the given set of IDs.

    Non-fatal: Redis unavailability is logged but not raised.
    """
    if not person_ids:
        return
    try:
        redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
        redis_conn = Redis.from_url(redis_url)
        queue = Queue("photos", connection=redis_conn)
        for pid in person_ids:
            queue.enqueue(generate_profile_picture, pid, retry=Retry(max=3, interval=[10, 30, 60]))
            logger.info("_enqueue_profile_picture_for_persons: enqueued profile picture generation for person %s", pid)
    except Exception:
        logger.exception("_enqueue_profile_picture_for_persons: failed to enqueue profile picture jobs")


def process_photo(photo_id: str) -> None:
    """Move an uploaded photo from staging to originals, extract metadata, and mark as complete.

    This is the primary job enqueued after a photo is uploaded via the API.
    It is idempotent: if the photo is already complete, it exits early.
    """
    photo_uuid = UUID(photo_id)
    db = SessionLocal()
    try:
        photo = db.query(MediaItem).filter(MediaItem.id == photo_uuid).first()
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

        # Check if the actual work products already exist (file in originals + thumbnail).
        # This can happen if a previous run crashed after processing but before updating status.
        # In that case, reconcile the status to complete so the metric stays accurate.
        dest = ORIGINALS_DIR / f"{photo.sha256_hash}.{photo.file_extension}"
        thumb_path = DERIVATIVES_DIR / f"{photo.sha256_hash}_thumb.jpg"
        if dest.exists() and thumb_path.exists():
            logger.info(
                "process_photo: photo %s work already done (originals + thumbnail present) "
                "but status is %s — reconciling to complete",
                photo_id, status.state.value,
            )
            status.state = ProcessingState.complete
            status.error_message = None
            db.commit()
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
            from PIL import Image, ImageOps
            with Image.open(dest) as _raw_img:
                img = ImageOps.exif_transpose(_raw_img)
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
                    if photo.taken_at is not None:
                        logger.info(
                            "process_photo: no DateTimeOriginal EXIF tag for photo %s — keeping client-supplied taken_at %s",
                            photo_id, photo.taken_at,
                        )
                    else:
                        logger.info("process_photo: no DateTimeOriginal EXIF tag for photo %s, taken_at will be null", photo_id)

            # Generate thumbnail derivative
            thumb_path = DERIVATIVES_DIR / f"{photo.sha256_hash}_thumb.jpg"
            DERIVATIVES_DIR.mkdir(parents=True, exist_ok=True)
            if not thumb_path.exists():
                with Image.open(dest) as _raw_img:
                    img = ImageOps.exif_transpose(_raw_img)
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

            # Emit Loganne event — updateLoganne swallows HTTP errors internally
            app_origin = os.environ.get("APP_ORIGIN", "")
            updateLoganne("photoProcessed", f"Photo {photo_id} processed by lucos_photos", url=f"{app_origin}/photos/{photo_id}")

            # Notify the API's WebSocket clients that this photo is ready
            _publish_photo_processed(photo_id)

            # Enqueue profile picture generation for each person detected in this photo
            _enqueue_profile_picture_for_photo(photo_uuid)

        except Exception as exc:
            logger.exception("process_photo: error processing photo %s", photo_id)
            status.state = ProcessingState.failed
            status.error_message = str(exc)
            db.commit()
            raise

    finally:
        db.close()


def _extract_video_metadata(video_path: Path) -> dict:
    """Run ffprobe on a video file and return a dict with duration, codec, width, height, fps.

    Args:
        video_path: Absolute path to the video file.

    Returns:
        dict with keys: duration (float, seconds), codec (str), video_width (int),
        video_height (int), fps (float).

    Raises:
        subprocess.CalledProcessError: if ffprobe exits with a non-zero status.
        ValueError: if the ffprobe output cannot be parsed or required fields are missing.
    """
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        str(video_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    data = json.loads(result.stdout)

    # Find the first video stream
    video_stream = None
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video":
            video_stream = stream
            break

    if video_stream is None:
        raise ValueError(f"No video stream found in {video_path}")

    codec = video_stream.get("codec_name")
    if not codec:
        raise ValueError(f"Could not determine codec for {video_path}")

    width = video_stream.get("width")
    height = video_stream.get("height")
    if width is None or height is None:
        raise ValueError(f"Could not determine video dimensions for {video_path}")

    # fps is expressed as a fraction string like "30000/1001" or "30/1"
    fps = None
    fps_str = video_stream.get("r_frame_rate") or video_stream.get("avg_frame_rate")
    if fps_str and "/" in fps_str:
        numerator, denominator = fps_str.split("/", 1)
        if int(denominator) != 0:
            fps = float(int(numerator) / int(denominator))

    # Duration: prefer stream-level, fall back to format-level
    duration = None
    if "duration" in video_stream:
        duration = float(video_stream["duration"])
    elif "format" in data and "duration" in data["format"]:
        duration = float(data["format"]["duration"])

    return {
        "duration": duration,
        "codec": codec,
        "video_width": int(width),
        "video_height": int(height),
        "fps": fps,
    }


def process_video(photo_id: str) -> None:
    """Move an uploaded video from staging to originals, extract metadata, generate a thumbnail,
    and mark as complete.

    This is the job enqueued after a video is uploaded via the API.
    It is idempotent: if the video is already complete, it exits early.

    Face detection and transcoding are explicitly out of scope (deferred).
    """
    photo_uuid = UUID(photo_id)
    db = SessionLocal()
    try:
        photo = db.query(MediaItem).filter(MediaItem.id == photo_uuid).first()
        if not photo:
            logger.warning("process_video: media item %s not found", photo_id)
            return

        status = photo.processing_status
        if status is None:
            logger.warning("process_video: no processing_status for media item %s", photo_id)
            return

        if status.state == ProcessingState.complete:
            logger.info("process_video: media item %s already complete, skipping", photo_id)
            return

        # Check if the actual work products already exist (file in originals + thumbnail).
        # This can happen if a previous run crashed after processing but before updating status.
        # In that case, reconcile the status to complete so the metric stays accurate.
        dest = ORIGINALS_DIR / f"{photo.sha256_hash}.{photo.file_extension}"
        thumb_path = DERIVATIVES_DIR / f"{photo.sha256_hash}_thumb.jpg"
        if dest.exists() and thumb_path.exists():
            logger.info(
                "process_video: media item %s work already done (originals + thumbnail present) "
                "but status is %s — reconciling to complete",
                photo_id, status.state.value,
            )
            status.state = ProcessingState.complete
            status.error_message = None
            db.commit()
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
                    logger.info("process_video: moved %s to originals", src.name)
                else:
                    raise FileNotFoundError(
                        f"Upload file not found: {src} (and not already in originals)"
                    )
            else:
                logger.info("process_video: %s already in originals, skipping move", dest.name)
                # Clean up staging copy if it still exists
                if src.exists():
                    src.unlink()

            # Extract video metadata using ffprobe
            metadata = _extract_video_metadata(dest)
            photo.duration = metadata["duration"]
            photo.codec = metadata["codec"]
            photo.video_width = metadata["video_width"]
            photo.video_height = metadata["video_height"]
            photo.fps = metadata["fps"]
            logger.info(
                "process_video: extracted metadata for %s — codec=%s, %dx%d, %.2fs, %.2ffps",
                photo_id,
                photo.codec,
                photo.video_width,
                photo.video_height,
                photo.duration or 0.0,
                photo.fps or 0.0,
            )

            # Generate thumbnail: extract a still frame at ~10% into the video
            thumb_path = DERIVATIVES_DIR / f"{photo.sha256_hash}_thumb.jpg"
            DERIVATIVES_DIR.mkdir(parents=True, exist_ok=True)
            if not thumb_path.exists():
                seek_time = (photo.duration * 0.1) if photo.duration else 0.0
                cmd = [
                    "ffmpeg",
                    "-ss", str(seek_time),
                    "-i", str(dest),
                    "-vframes", "1",
                    "-q:v", "2",
                    "-vf", f"scale={THUMBNAIL_WIDTH}:-1",
                    str(thumb_path),
                ]
                subprocess.run(cmd, capture_output=True, check=True)
                logger.info("process_video: generated thumbnail for %s at %s", photo_id, thumb_path)
            else:
                logger.info("process_video: thumbnail already exists for %s, skipping", photo_id)

            # Mark as complete
            status.state = ProcessingState.complete
            db.commit()
            logger.info("process_video: media item %s processed successfully", photo_id)

            # Emit Loganne event — updateLoganne swallows HTTP errors internally
            app_origin = os.environ.get("APP_ORIGIN", "")
            updateLoganne("videoProcessed", f"Video {photo_id} processed by lucos_photos", url=f"{app_origin}/photos/{photo_id}")

            # Notify the API's WebSocket clients that this media item is ready
            _publish_photo_processed(photo_id)

        except Exception as exc:
            logger.exception("process_video: error processing media item %s", photo_id)
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
        photo = db.query(MediaItem).filter(MediaItem.id == photo_uuid).first()
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

    # Re-enqueue the appropriate job based on media type
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    redis_conn = Redis.from_url(redis_url)
    queue = Queue("photos", connection=redis_conn)

    db = SessionLocal()
    try:
        photo = db.query(MediaItem).filter(MediaItem.id == photo_uuid).first()
        job_fn = process_video if (photo and photo.media_type == "video") else process_photo
    finally:
        db.close()

    queue.enqueue(job_fn, photo_id, retry=Retry(max=3, interval=[10, 30, 60]))
    logger.info("reprocess_photo: enqueued %s for photo %s", job_fn.__name__, photo_id)


def resweep_thumbnails() -> None:
    """Delete existing thumbnails for all complete photos and reset them to pending.

    This triggers the worker to regenerate thumbnails for every processed photo.
    Use after a fix that affects thumbnail generation (e.g. EXIF orientation correction)
    so that stale thumbnails on disk are replaced with correctly-oriented ones.

    The worker skips thumbnail generation when the file already exists, so simply
    re-enqueueing would not help — the thumbnail file must be deleted first.
    """
    db = SessionLocal()
    try:
        complete_photos = (
            db.query(MediaItem)
            .join(MediaItem.processing_status)
            .filter(ProcessingStatus.state == ProcessingState.complete)
            .all()
        )
        logger.info("resweep_thumbnails: found %d complete photo(s)", len(complete_photos))

        deleted = 0
        reset = 0
        for photo in complete_photos:
            thumb_path = DERIVATIVES_DIR / f"{photo.sha256_hash}_thumb.jpg"
            if thumb_path.exists():
                thumb_path.unlink()
                deleted += 1
            photo.processing_status.state = ProcessingState.pending
            photo.processing_status.error_message = None
            reset += 1

        db.commit()
        logger.info("resweep_thumbnails: deleted %d thumbnail(s), reset %d photo(s) to pending", deleted, reset)

    except Exception:
        logger.exception("resweep_thumbnails: error during resweep")
        db.rollback()
        raise
    finally:
        db.close()


def _frontality_score(kps) -> float:
    """Estimate how directly a face is facing the camera using 5-point keypoints.

    Uses the horizontal symmetry of the two eye keypoints relative to the nose.
    Returns a value in [0.0, 1.0] where 1.0 = perfectly frontal.

    kps is a list of 5 [x, y] pairs: left eye, right eye, nose, left mouth, right mouth.
    Returns 0.0 if kps is None or malformed.
    """
    try:
        if kps is None or len(kps) < 3:
            return 0.0
        left_eye_x = float(kps[0][0])
        right_eye_x = float(kps[1][0])
        nose_x = float(kps[2][0])
        eye_span = right_eye_x - left_eye_x
        if eye_span <= 0:
            return 0.0
        # How centred is the nose between the two eyes?
        # mid_offset ranges from 0 (perfectly centred) to 0.5 (nose at one eye)
        mid_x = (left_eye_x + right_eye_x) / 2.0
        mid_offset = abs(nose_x - mid_x) / eye_span
        # Convert to a 0–1 score: 0 offset → 1.0, 0.5 offset → 0.0
        return max(0.0, 1.0 - 2.0 * mid_offset)
    except (TypeError, IndexError, ZeroDivisionError):
        return 0.0


def _score_face(face: "Face", photo: "MediaItem") -> int:
    """Return an integer score (0–4) for how suitable a face is as a profile picture.

    Criteria (each worth 1 point):
      1. High detection confidence (det_score > 0.8) — proxy for sharpness/focus
      2. Facing the camera (frontality derived from kps > 0.7)
      3. Face width > 300px
      4. Face height > 300px

    Ties should be broken by taken_at DESC (most recent wins) — handled by the caller.
    """
    score = 0

    if face.det_score is not None and face.det_score > 0.8:
        score += 1

    if _frontality_score(face.kps) > 0.7:
        score += 1

    if photo.width is not None and face.bbox_width * photo.width > 300:
        score += 1

    if photo.height is not None and face.bbox_height * photo.height > 300:
        score += 1

    return score


def generate_profile_picture(person_id: str) -> None:
    """Choose the best profile picture for a person and crop it to a square derivative.

    Scores all photos in which this person appears, picks the highest-scoring face,
    crops the image to a square centred on that face (~80% face area), and writes the
    result to /data/photos/derivatives/{person_id}_profile.jpg.

    Also updates person.profile_photo_id and person.profile_auto_generated in the DB.
    Idempotent: re-running overwrites the existing derivative file.
    """
    from PIL import Image as PILImage
    import math

    person_uuid = UUID(person_id)
    db = SessionLocal()
    try:
        person = db.query(Person).filter(Person.id == person_uuid).first()
        if not person:
            logger.warning("generate_profile_picture: person %s not found", person_id)
            return

        # Skip if manually overridden
        if person.profile_auto_generated is False:
            logger.info("generate_profile_picture: person %s has a manually chosen profile picture, skipping", person_id)
            return

        # Fetch all faces for this person that have both bbox and photo dimensions
        faces = (
            db.query(Face)
            .filter(Face.person_id == person_uuid)
            .join(Face.media_item)
            .filter(MediaItem.width.isnot(None), MediaItem.height.isnot(None))
            .all()
        )

        if not faces:
            logger.info("generate_profile_picture: no suitable faces found for person %s", person_id)
            return

        # Score each face and pick the best
        best_face = None
        best_score = -1
        best_photo = None
        best_taken_at = None

        for face in faces:
            photo = face.media_item
            score = _score_face(face, photo)
            taken_at = photo.taken_at

            if (score > best_score or
                    (score == best_score and taken_at is not None and
                     (best_taken_at is None or taken_at > best_taken_at))):
                best_face = face
                best_score = score
                best_photo = photo
                best_taken_at = taken_at

        if best_face is None or best_photo is None:
            logger.info("generate_profile_picture: no face selected for person %s", person_id)
            return

        logger.info(
            "generate_profile_picture: best face for person %s is in photo %s (score=%d)",
            person_id, best_photo.id, best_score,
        )

        # Locate the original image file
        original_path = ORIGINALS_DIR / f"{best_photo.sha256_hash}.{best_photo.file_extension}"
        if not original_path.exists():
            logger.warning(
                "generate_profile_picture: original file not found at %s for photo %s",
                original_path, best_photo.id,
            )
            return

        # Compute pixel coordinates of face bounding box
        img_w = best_photo.width
        img_h = best_photo.height
        face_x_px = best_face.bbox_x * img_w
        face_y_px = best_face.bbox_y * img_h
        face_w_px = best_face.bbox_width * img_w
        face_h_px = best_face.bbox_height * img_h

        # Crop: square, face occupies ~60% of area → side = face_side / sqrt(0.6)
        face_side = max(face_w_px, face_h_px)
        crop_side = face_side / math.sqrt(0.6)

        # Centre on the bounding box centre
        cx = face_x_px + face_w_px / 2.0
        cy = face_y_px + face_h_px / 2.0

        left = cx - crop_side / 2.0
        top = cy - crop_side / 2.0
        right = left + crop_side
        bottom = top + crop_side

        # Shift the crop window to keep it square when near an edge, then hard-clamp
        # as a final safety net (handles the degenerate case where the face itself is
        # larger than the image).
        if left < 0:
            right -= left  # shift right by the overhang
            left = 0.0
        if top < 0:
            bottom -= top
            top = 0.0
        if right > img_w:
            left -= (right - img_w)  # shift left by the overhang
            right = float(img_w)
        if bottom > img_h:
            top -= (bottom - img_h)
            bottom = float(img_h)
        # Final clamp: face genuinely bigger than image
        left = max(0.0, left)
        top = max(0.0, top)

        # Write derivative
        DERIVATIVES_DIR.mkdir(parents=True, exist_ok=True)
        profile_path = DERIVATIVES_DIR / f"{person_id}_profile.jpg"

        # Convert to integer pixel coords; derive right/bottom from left/top + side
        # to guarantee a perfectly square crop regardless of floating-point rounding.
        crop_side_px = round(right - left)
        left_px = round(left)
        top_px = round(top)

        MAX_PROFILE_SIZE = 600
        with PILImage.open(original_path) as img:
            cropped = img.crop((left_px, top_px, left_px + crop_side_px, top_px + crop_side_px))
            if cropped.width > MAX_PROFILE_SIZE or cropped.height > MAX_PROFILE_SIZE:
                cropped = cropped.resize((MAX_PROFILE_SIZE, MAX_PROFILE_SIZE), PILImage.LANCZOS)
            cropped.save(profile_path, format="JPEG", quality=90)

        logger.info("generate_profile_picture: saved profile picture for person %s at %s", person_id, profile_path)

        # Update DB
        person.profile_photo_id = best_photo.id
        person.profile_auto_generated = True
        db.commit()

        # Emit Loganne event — updateLoganne swallows HTTP errors internally
        app_origin = os.environ.get("APP_ORIGIN", "")
        updateLoganne("profilePhotoUpdated", f"Profile photo updated for person {person_id} in lucos_photos", url=f"{app_origin}/people/{person_id}")

    except Exception:
        logger.exception("generate_profile_picture: error for person %s", person_id)
        db.rollback()
        raise
    finally:
        db.close()


def sync_photo_person(db, photo_id) -> None:
    """Ensure photo_person table reflects all person assignments for a photo.

    Called by both the API (after manual face assignment) and the worker
    (after face clustering assigns person_ids).
    """
    face_person_ids = {
        f.person_id
        for f in db.query(Face).filter(Face.photo_id == photo_id, Face.person_id.isnot(None)).all()
    }

    existing_rows = db.query(PhotoPerson).filter(PhotoPerson.photo_id == photo_id).all()
    for row in existing_rows:
        if row.person_id not in face_person_ids:
            db.delete(row)

    existing_person_ids = {row.person_id for row in existing_rows}
    for pid in face_person_ids:
        if pid not in existing_person_ids:
            db.add(PhotoPerson(photo_id=photo_id, person_id=pid))


def cluster_faces() -> None:
    """Cluster all unassigned face embeddings and create Person records.

    Uses DBSCAN (cosine metric) to group faces that appear to show the same person.
    For each resulting cluster, creates a Person record and assigns all faces in the
    cluster to it. Also updates the photo_person join table.

    Only processes faces where:
      - person_id IS NULL (not yet assigned to anyone)
      - person_confirmed IS False (not manually confirmed — those are authoritative and must not be changed)
      - embedding IS NOT NULL (need an embedding to cluster)

    Idempotent: existing confirmed assignments are never modified.
    """
    db = SessionLocal()
    try:
        # Fetch all unassigned faces with embeddings
        unassigned = (
            db.query(Face)
            .filter(
                Face.person_id.is_(None),
                Face.person_confirmed.is_(False),
                Face.embedding.isnot(None),
            )
            .all()
        )

        if not unassigned:
            logger.info("cluster_faces: no unassigned faces with embeddings, nothing to do")
            return

        logger.info("cluster_faces: clustering %d unassigned face(s)", len(unassigned))

        import numpy as np
        from sklearn.cluster import DBSCAN

        embeddings = np.array([f.embedding for f in unassigned], dtype=np.float32)

        # Normalise embeddings to unit vectors so cosine distance = 1 - dot product.
        # sklearn's cosine metric in DBSCAN computes 1 - cosine_similarity.
        # epsilon=0.4 matches FACE_SIMILARITY_THRESHOLD used elsewhere.
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        # Avoid division by zero for zero vectors
        norms = np.where(norms == 0, 1.0, norms)
        normalised = embeddings / norms

        clustering = DBSCAN(eps=FACE_SIMILARITY_THRESHOLD, min_samples=1, metric="cosine").fit(normalised)
        labels = clustering.labels_

        # Group face indices by cluster label (-1 = noise, skip)
        clusters: dict[int, list[int]] = {}
        for idx, label in enumerate(labels):
            if label == -1:
                continue
            clusters.setdefault(label, []).append(idx)

        logger.info("cluster_faces: found %d cluster(s) (noise faces: %d)",
                    len(clusters), int((labels == -1).sum()))

        # For each cluster, create a Person and assign faces
        affected_photo_ids: set = set()
        for label, indices in clusters.items():
            person = Person()
            db.add(person)
            db.flush()  # get person.id

            for idx in indices:
                face = unassigned[idx]
                face.person_id = person.id
                affected_photo_ids.add(face.photo_id)

        # Update photo_person for all affected photos
        for photo_id in affected_photo_ids:
            sync_photo_person(db, photo_id)

        # Collect affected person IDs before commit so we can enqueue profile picture jobs
        affected_person_ids: set = {
            str(unassigned[idx].person_id)
            for indices in clusters.values()
            for idx in indices
            if unassigned[idx].person_id is not None
        }

        db.commit()
        logger.info("cluster_faces: assigned %d face(s) across %d photo(s)",
                    sum(len(v) for v in clusters.values()), len(affected_photo_ids))

        # Enqueue profile picture generation for each newly-assigned person
        _enqueue_profile_picture_for_persons(affected_person_ids)

    except Exception:
        logger.exception("cluster_faces: error during face clustering")
        db.rollback()
        raise
    finally:
        db.close()


def sync_single_contact_name(contact_id: str, name: str) -> None:
    """Update display_name for any person linked to the given contact_id.

    Called by the Loganne webhook when a contactUpdated event is received.
    Idempotent: no-op if no person is linked to this contact, or if the
    stored name already matches.
    """
    db = SessionLocal()
    try:
        persons = db.query(Person).filter(Person.contact_id == contact_id).all()
        updated = 0
        for person in persons:
            if person.display_name != name:
                logger.info(
                    "sync_single_contact_name: updating person %s display_name from %r to %r",
                    person.id, person.display_name, name,
                )
                person.display_name = name
                updated += 1
        if updated:
            db.commit()
        logger.info(
            "sync_single_contact_name: contact_id=%s updated %d person(s)", contact_id, updated
        )
    except Exception:
        logger.exception("sync_single_contact_name: error for contact_id=%s", contact_id)
        db.rollback()
        raise
    finally:
        db.close()


def sweep_contact_display_names() -> None:
    """Check all persons linked to contacts and sync display_name with lucos_contacts.

    Fetches the canonical name from lucos_contacts for every person with a non-null
    contact_id. Updates display_name where there is a mismatch.

    Acts as a safety net to correct any drift — the Loganne webhook provides
    near-real-time sync; this sweep catches anything the webhook missed.
    """
    import httpx

    contacts_url = os.environ.get("LUCOS_CONTACTS_URL", "")
    contacts_key = os.environ.get("KEY_LUCOS_CONTACTS", "")
    if not contacts_url or not contacts_key:
        logger.warning("sweep_contact_display_names: LUCOS_CONTACTS_URL or KEY_LUCOS_CONTACTS not set, skipping")
        return

    db = SessionLocal()
    try:
        persons = db.query(Person).filter(Person.contact_id.isnot(None)).all()
        logger.info("sweep_contact_display_names: checking %d person(s) with a contact_id", len(persons))

        updated = 0
        for person in persons:
            try:
                response = httpx.get(
                    f"{contacts_url}/people/{person.contact_id}",
                    headers={"Accept": "application/json", "Authorization": f"key {contacts_key}"},
                    timeout=5.0,
                )
                response.raise_for_status()
                canonical_name = response.json().get("name") or None
            except Exception as e:
                logger.warning(
                    "sweep_contact_display_names: failed to fetch name for contact %s: %s",
                    person.contact_id, e,
                )
                continue

            if canonical_name and person.display_name != canonical_name:
                logger.info(
                    "sweep_contact_display_names: updating person %s display_name from %r to %r",
                    person.id, person.display_name, canonical_name,
                )
                person.display_name = canonical_name
                updated += 1

        if updated:
            db.commit()

        logger.info("sweep_contact_display_names: updated %d person(s)", updated)
    except Exception:
        logger.exception("sweep_contact_display_names: error during sweep")
        db.rollback()
        raise
    finally:
        db.close()
