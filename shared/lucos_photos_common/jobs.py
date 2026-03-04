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

from redis import Redis
from rq import Queue
from rq.job import Retry

from lucos_photos_common.database import SessionLocal
from lucos_photos_common.models import Photo, ProcessingState, ProcessingStatus

logger = logging.getLogger(__name__)

UPLOADS_DIR = Path("/data/uploads")
ORIGINALS_DIR = Path("/data/photos/originals")


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

            # Mark as complete
            status.state = ProcessingState.complete
            db.commit()
            logger.info("process_photo: photo %s processed successfully (%dx%d)", photo_id, photo.width, photo.height)

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
