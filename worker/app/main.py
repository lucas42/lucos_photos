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
from lucos_photos_common.models import Face, MediaItem, Person, ProcessingState, ProcessingStatus

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Photos stuck in pending for longer than this will be picked up by the sweep
PENDING_SWEEP_THRESHOLD_MINUTES = int(os.environ.get("PENDING_SWEEP_THRESHOLD_MINUTES", 5))
# Photos stuck in processing for longer than this will be re-enqueued (longer threshold
# to avoid interfering with genuinely in-progress jobs)
PROCESSING_SWEEP_THRESHOLD_MINUTES = int(os.environ.get("PROCESSING_SWEEP_THRESHOLD_MINUTES", 30))
# How often the sweep runs (seconds)
PENDING_SWEEP_INTERVAL_SECONDS = int(os.environ.get("PENDING_SWEEP_INTERVAL_SECONDS", 60))

# Per-item re-enqueue backoff (replaces the old global SWEEP_QUEUE_DEPTH_LIMIT breaker —
# see sweep_pending_photos docstring). First re-enqueue of a freshly-stuck item is
# immediate; each repeat re-enqueue must wait BACKOFF_BASE_SECONDS * 2^(count-1), capped
# at BACKOFF_CEILING_SECONDS, since the item's last re-enqueue.
BACKOFF_BASE_SECONDS = int(os.environ.get("BACKOFF_BASE_SECONDS", 120))
BACKOFF_CEILING_SECONDS = int(os.environ.get("BACKOFF_CEILING_SECONDS", 3600))
# Backoff state keys are TTL'd well past the ceiling so a long-quiet item's history
# eventually expires, but comfortably outlive any single backoff wait.
REENQUEUE_KEY_TTL_SECONDS = BACKOFF_CEILING_SECONDS * 2
# An item re-enqueued this many times (or first seen this long ago) is "chronically
# stuck" — logged and surfaced via the /_info gauge below.
SWEEP_CHRONIC_THRESHOLD = int(os.environ.get("SWEEP_CHRONIC_THRESHOLD", 5))


WORKER_HEARTBEAT_KEY = "worker:heartbeat"
# Aggregate gauge of currently chronically-stuck items, recomputed from scratch every
# sweep pass (see sweep_pending_photos) — never a SCAN over sweep:* keys.
SWEEP_CHRONICALLY_STUCK_COUNT_KEY = "sweep:chronically_stuck_count"


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
    Errors are absorbed so a Redis write failure cannot suppress the sweep.
    """
    try:
        rss = get_rss_bytes()
        payload = json.dumps({
            "rss_bytes": rss,
            "pid": os.getpid(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        ttl = PENDING_SWEEP_INTERVAL_SECONDS * 3
        redis_conn.set(WORKER_HEARTBEAT_KEY, payload, ex=ttl)
    except Exception:
        logger.warning("publish_heartbeat: failed to write to Redis", exc_info=True)


def _reenqueue_key(prefix: str, item_id) -> str:
    """Redis key for an item's per-item re-enqueue backoff state, e.g.
    'sweep:reenqueue:<photo_id>' or 'sweep:profilepic:<person_id>'."""
    return f"sweep:{prefix}:{item_id}"


def _read_backoff_state(redis_conn: Redis, key: str) -> dict:
    """Read the per-item backoff hash ({count, first_enqueued_at, last_enqueued_at})
    from Redis. Returns {} if the item has no history (never re-enqueued, or its key
    has expired)."""
    raw = redis_conn.hgetall(key)
    return {
        (k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
        for k, v in raw.items()
    }


def _is_chronic(state: dict, count: int) -> bool:
    """An item is chronically stuck once it's been re-enqueued SWEEP_CHRONIC_THRESHOLD
    times, or once it's been stuck continuously for at least BACKOFF_CEILING_SECONDS."""
    if count >= SWEEP_CHRONIC_THRESHOLD:
        return True
    first_enqueued_at = state.get("first_enqueued_at")
    if first_enqueued_at is not None:
        return (time.time() - float(first_enqueued_at)) >= BACKOFF_CEILING_SECONDS
    return False


def _apply_reenqueue_backoff(redis_conn: Redis, prefix: str, item_id, enqueue_fn) -> bool:
    """Gate a single stuck item's re-enqueue by its own exponential backoff, replacing
    the old global queue-depth breaker.

    A freshly-stuck item (no Redis history) is re-enqueued immediately via `enqueue_fn()`.
    A repeat re-enqueue must wait BACKOFF_BASE_SECONDS * 2^(count-1) (capped at
    BACKOFF_CEILING_SECONDS) since its last re-enqueue — if that hasn't elapsed, the item
    is skipped this pass and left for a later one. State (count, first_enqueued_at,
    last_enqueued_at) lives entirely in Redis, keyed per item, so a worker restart does
    not reset it and re-arm the loop.

    Returns True if the item is currently "chronically stuck" (see _is_chronic) — the
    caller tallies this into the /_info gauge and this function logs a WARNING for it.
    """
    key = _reenqueue_key(prefix, item_id)
    state = _read_backoff_state(redis_conn, key)
    count = int(state.get("count", 0))

    if count > 0:
        last_enqueued_at = float(state.get("last_enqueued_at", 0))
        wait = min(BACKOFF_BASE_SECONDS * (2 ** (count - 1)), BACKOFF_CEILING_SECONDS)
        remaining = (last_enqueued_at + wait) - time.time()
        if remaining > 0:
            logger.info(
                "sweep: skipping re-enqueue of %s — re-enqueued %d time(s) already, "
                "%.0fs remaining in backoff",
                key, count, remaining,
            )
            if _is_chronic(state, count):
                logger.warning(
                    "sweep: %s is chronically stuck (re-enqueued %d times, first seen at %s)",
                    key, count, state.get("first_enqueued_at"),
                )
                return True
            return False

    enqueue_fn()

    now = time.time()
    new_count = redis_conn.hincrby(key, "count", 1)
    updates = {"last_enqueued_at": now}
    if "first_enqueued_at" not in state:
        updates["first_enqueued_at"] = now
    redis_conn.hset(key, mapping=updates)
    redis_conn.expire(key, REENQUEUE_KEY_TTL_SECONDS)
    state.update(updates)

    if _is_chronic(state, new_count):
        logger.warning(
            "sweep: %s is chronically stuck (re-enqueued %d times, first seen at %s)",
            key, new_count, state.get("first_enqueued_at"),
        )
        return True
    return False


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


def _enqueue_missing_profile_pictures(redis_conn: Redis) -> int:
    """Enqueue generate_profile_picture for any non-background person who has a face but
    no profile picture. Returns the number of chronically-stuck persons found.

    This is the backstop for generate_profile_picture equivalent to the pending/processing
    sweep above for process_photo/process_video. Enabling the RQ scheduler makes retries
    actually happen, but a job that exhausts all retries is otherwise lost forever — unlike
    process_photo/process_video, there is no ProcessingStatus row to detect that by, so this
    query (person has a face, is not marked background, and has no profile_photo_id) stands
    in for it.

    Gated by the same per-item re-enqueue backoff as _enqueue_for_media_item, keyed per
    person rather than per photo.
    """
    from lucos_photos_common.jobs import _enqueue_profile_picture_for_persons

    db = SessionLocal()
    try:
        person_ids = [
            str(person_id)
            for (person_id,) in (
                db.query(Person.id)
                .join(Face, Face.person_id == Person.id)
                .filter(Person.is_background == False, Person.profile_photo_id.is_(None))  # noqa: E712
                .distinct()
                .all()
            )
        ]
    finally:
        db.close()

    def _enqueue_one(person_id: str) -> None:
        logger.info("sweep: enqueuing profile picture generation for person %s", person_id)
        _enqueue_profile_picture_for_persons([person_id])

    chronic_count = 0
    for person_id in person_ids:
        is_chronic = _apply_reenqueue_backoff(
            redis_conn, "profilepic", person_id, lambda pid=person_id: _enqueue_one(pid)
        )
        if is_chronic:
            chronic_count += 1
    return chronic_count


def sweep_pending_photos(redis_conn: Redis) -> None:
    """Enqueue processing jobs for any media items stuck in 'pending' or 'processing' state.

    'pending' items: stuck longer than PENDING_SWEEP_THRESHOLD_MINUTES (default 5 min).
    These arise when the API crashes between DB commit and Redis enqueue.

    'processing' items: stuck longer than PROCESSING_SWEEP_THRESHOLD_MINUTES (default 30 min).
    These arise when the worker crashes mid-job, leaving the item in 'processing' permanently.

    Both states are swept to ensure the pending count in /_info reflects reality and items
    are not silently abandoned.

    Jobs are routed correctly: videos go to process_video, photos to process_photo.

    Also enqueues generate_profile_picture for any person who has a face but no profile
    picture — the equivalent backstop for that job type (see _enqueue_missing_profile_pictures).

    There is no global circuit breaker here (there used to be one, gated on absolute queue
    depth — see git history for lucas42/lucos_photos#479). A global aggregate can't tell a
    genuine re-enqueue flood apart from two healthy states this service routinely produces:
    a bulk-import burst, and a queue backed up behind a single hung job on this service's
    one sequential worker. Instead, each stuck item is gated by its own exponential
    backoff (_apply_reenqueue_backoff) — a flood is bounded at its source (the same item
    looping) rather than inferred from a proxy that also trips on healthy load.
    """
    queue = Queue("photos", connection=redis_conn)
    chronic_count = 0

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
        for status in stuck:
            is_chronic = _apply_reenqueue_backoff(
                redis_conn, "reenqueue", status.photo_id,
                lambda status=status: _enqueue_for_media_item(queue, status),
            )
            if is_chronic:
                chronic_count += 1
    except Exception:
        logger.exception("sweep: error during pending photo sweep")
    finally:
        db.close()

    try:
        chronic_count += _enqueue_missing_profile_pictures(redis_conn)
    except Exception:
        logger.exception("sweep: error during missing profile picture sweep")

    try:
        redis_conn.set(
            SWEEP_CHRONICALLY_STUCK_COUNT_KEY,
            chronic_count,
            ex=PENDING_SWEEP_INTERVAL_SECONDS * 3,
        )
    except Exception:
        logger.warning("sweep: failed to publish chronically_stuck_count to Redis", exc_info=True)

    # Face clustering and contact display-name sync don't participate in the re-enqueue
    # backoff above — they aren't gated by it.
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
    worker.work(with_scheduler=True)


if __name__ == "__main__":
    main()
