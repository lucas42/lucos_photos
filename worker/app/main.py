"""Worker entry point.

Starts an RQ worker listening on the 'photos' queue, and a background thread
that periodically sweeps for photos stuck in 'pending' state.
"""

import logging
import os
import threading
import time
from datetime import datetime, timezone, timedelta

from redis import Redis
from rq import Queue, Worker
from rq.job import Retry

from lucos_photos_common.database import SessionLocal
from lucos_photos_common.models import ProcessingState, ProcessingStatus

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Photos stuck in pending for longer than this will be picked up by the sweep
PENDING_SWEEP_THRESHOLD_MINUTES = int(os.environ.get("PENDING_SWEEP_THRESHOLD_MINUTES", 5))
# How often the sweep runs (seconds)
PENDING_SWEEP_INTERVAL_SECONDS = int(os.environ.get("PENDING_SWEEP_INTERVAL_SECONDS", 60))


def sweep_pending_photos(redis_conn: Redis) -> None:
    """Enqueue process_photo jobs for any photos stuck in 'pending' state.

    This is a catch-all for cases where the API crashed between DB commit and
    Redis enqueue, leaving photos in 'pending' with no corresponding RQ job.
    """
    from lucos_photos_common.jobs import process_photo

    threshold = datetime.now(timezone.utc) - timedelta(minutes=PENDING_SWEEP_THRESHOLD_MINUTES)
    db = SessionLocal()
    try:
        stuck = (
            db.query(ProcessingStatus)
            .filter(
                ProcessingStatus.state == ProcessingState.pending,
                ProcessingStatus.updated_at < threshold,
            )
            .all()
        )
        if stuck:
            queue = Queue("photos", connection=redis_conn)
            for status in stuck:
                logger.info("sweep: enqueuing process_photo for stuck photo %s", status.photo_id)
                queue.enqueue(
                    process_photo,
                    str(status.photo_id),
                    retry=Retry(max=3, interval=[10, 30, 60]),
                )
    except Exception:
        logger.exception("sweep: error during pending photo sweep")
    finally:
        db.close()


def run_sweep_loop(redis_conn: Redis) -> None:
    """Background thread: periodically sweep for pending photos."""
    logger.info("Sweep loop starting (interval=%ds, threshold=%dm)", PENDING_SWEEP_INTERVAL_SECONDS, PENDING_SWEEP_THRESHOLD_MINUTES)
    while True:
        time.sleep(PENDING_SWEEP_INTERVAL_SECONDS)
        try:
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
