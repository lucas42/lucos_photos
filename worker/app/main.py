"""Worker entry point.

Starts an RQ worker listening on the 'photos' queue, and a background thread
that periodically sweeps for photos stuck in 'pending' or 'processing' state.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone, timedelta

from redis import Redis
from rq import Queue, Worker
from rq.job import Retry

from lucos_photos_common.database import SessionLocal
from lucos_photos_common.models import MediaItem, ProcessingState, ProcessingStatus

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Photos stuck in pending for longer than this will be picked up by the sweep
PENDING_SWEEP_THRESHOLD_MINUTES = int(os.environ.get("PENDING_SWEEP_THRESHOLD_MINUTES", 5))
# Photos stuck in processing for longer than this will be re-enqueued (longer threshold
# to avoid interfering with genuinely in-progress jobs)
PROCESSING_SWEEP_THRESHOLD_MINUTES = int(os.environ.get("PROCESSING_SWEEP_THRESHOLD_MINUTES", 30))
# How often the sweep runs (seconds)
PENDING_SWEEP_INTERVAL_SECONDS = int(os.environ.get("PENDING_SWEEP_INTERVAL_SECONDS", 60))
# Circuit breaker: skip sweep if queue already has this many or more jobs waiting.
# Prevents a positive-feedback flood (sweep re-enqueues -> jobs fail -> queue grows -> OOM).
SWEEP_QUEUE_DEPTH_LIMIT = int(os.environ.get("SWEEP_QUEUE_DEPTH_LIMIT", 0))


WORKER_HEARTBEAT_KEY = "worker:heartbeat"


def get_rss_bytes() -> int | None:
    """Return the current RSS of this process in bytes, or None if unreadable."""
    try:
        with open("/proc/self/status") as f:
            content = f.read()
        for line in content.splitlines():
            if line.startswith("VmRSS:"):
                kb = int(line.split()[1])
                return kb * 1024
    except Exception:
        pass
    return None


def publish_heartbeat(redis_conn: Redis) -> None:
    """Write current RSS and PID to Redis for the API to surface in /_info.

    TTL is set to 3× the sweep interval so the key disappears if the worker stops.
    """
    rss = get_rss_bytes()
    payload = json.dumps({
        "rss_bytes": rss,
        "pid": os.getpid(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    ttl = PENDING_SWEEP_INTERVAL_SECONDS * 3
    redis_conn.set(WORKER_HEARTBEAT_KEY, payload, ex=ttl)


def _enqueue_for_media_item(queue: Queue, status: ProcessingStatus) -> None:
    """Enqueue the correct job for a media item, routing videos to process_video
    and photos to process_photo.

    Args:
        queue: The RQ queue to enqueue into.
        status: The ProcessingStatus row (must have .media_item loaded or accessible).
    """
    from lucos_photos_common.jobs import process_photo, process_video

    media_item = status.media_item
    if media_item is not None and media_item.media_type == "video":
        job_fn = process_video
    else:
        job_fn = process_photo

    logger.info(
        "sweep: enqueuing %s for stuck media item %s (state=%s)",
        job_fn.__name__,
        status.photo_id,
        status.state.value,
    )
    queue.enqueue(
        job_fn,
        str(status.photo_id),
        retry=Retry(max=3, interval=[10, 30, 60]),
    )


def sweep_pending_photos(redis_conn: Redis) -> None:
    """Enqueue processing jobs for any media items stuck in 'pending' or 'processing' state.

    'pending' items: stuck longer than PENDING_SWEEP_THRESHOLD_MINUTES (default 5 min).
    These arise when the API crashes between DB commit and Redis enqueue.

    'processing' items: stuck longer than PROCESSING_SWEEP_THRESHOLD_MINUTES (default 30 min).
    These arise when the worker crashes mid-job, leaving the item in 'processing' permanently.

    Both states are swept to ensure the pending count in /_info reflects reality and items
    are not silently abandoned.

    Jobs are routed correctly: videos go to process_video, photos to process_photo.

    Circuit breaker: if the queue already has more than SWEEP_QUEUE_DEPTH_LIMIT jobs waiting
    (default 0), the sweep is skipped entirely. This prevents a positive-feedback flood where
    jobs fail (e.g. due to OOM), the sweep re-enqueues them every 60 seconds, and the queue
    grows unboundedly until Redis itself causes OOM.
    """
    queue = Queue("photos", connection=redis_conn)
    queue_depth = queue.count
    if queue_depth > SWEEP_QUEUE_DEPTH_LIMIT:
        logger.warning(
            "sweep: skipping — queue already has %d jobs waiting (limit=%d). "
            "Will retry when queue drains.",
            queue_depth,
            SWEEP_QUEUE_DEPTH_LIMIT,
        )
        return

    pending_threshold = datetime.now(timezone.utc) - timedelta(minutes=PENDING_SWEEP_THRESHOLD_MINUTES)
    processing_threshold = datetime.now(timezone.utc) - timedelta(minutes=PROCESSING_SWEEP_THRESHOLD_MINUTES)

    db = SessionLocal()
    try:
        stuck = (
            db.query(ProcessingStatus)
            .join(ProcessingStatus.media_item)
            .filter(
                (
                    (ProcessingStatus.state == ProcessingState.pending) &
                    (ProcessingStatus.updated_at < pending_threshold)
                ) | (
                    (ProcessingStatus.state == ProcessingState.processing) &
                    (ProcessingStatus.updated_at < processing_threshold)
                )
            )
            .all()
        )
        if stuck:
            for status in stuck:
                _enqueue_for_media_item(queue, status)
    except Exception:
        logger.exception("sweep: error during pending photo sweep")
    finally:
        db.close()

    # Run face clustering after the pending sweep so newly processed photos
    # get their faces incorporated into clusters.
    try:
        from lucos_photos_common.jobs import cluster_faces
        cluster_faces()
    except Exception:
        logger.exception("sweep: error during face clustering")

    # Sync person display_names with lucos_contacts to catch any drift.
    try:
        from lucos_photos_common.jobs import sweep_contact_display_names
        sweep_contact_display_names()
    except Exception:
        logger.exception("sweep: error during contact display name sync")


def run_sweep_loop(redis_conn: Redis) -> None:
    """Background thread: periodically sweep for pending photos."""
    logger.info("Sweep loop starting (interval=%ds, pending_threshold=%dm, processing_threshold=%dm)",
                PENDING_SWEEP_INTERVAL_SECONDS, PENDING_SWEEP_THRESHOLD_MINUTES, PROCESSING_SWEEP_THRESHOLD_MINUTES)
    while True:
        time.sleep(PENDING_SWEEP_INTERVAL_SECONDS)
        try:
            publish_heartbeat(redis_conn)
            sweep_pending_photos(redis_conn)
        except Exception:
            logger.exception("sweep: unhandled error in sweep loop")


def main() -> None:
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    logger.info("Worker starting, connecting to Redis at %s", redis_url)

    redis_conn = Redis.from_url(redis_url)

    # Start the periodic sweep in a background daemon thread
    sweep_thread = threading.Thread(target=run_sweep_loop, args=(redis_conn,), daemon=True)
    sweep_thread.start()

    # Start the RQ worker (blocks until shutdown)
    queue = Queue("photos", connection=redis_conn)
    worker = Worker([queue], connection=redis_conn)
    logger.info("Starting RQ worker on queue 'photos'")
    worker.work(with_scheduler=False)


if __name__ == "__main__":
    main()
